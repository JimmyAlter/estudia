"""Repaso por pregunta: FSRS (librería `fsrs`, MIT) más reaprendizaje sucesivo.

Cada tarjeta vista tiene una fila en `review_state`. Una pregunta sin grupo es su propia tarjeta
(`card_key = 'q:<id>'`); las variantes numéricas de una original comparten una sola
(`'v:<id de la original>'`, ver `card_ref`). La fila se deriva siempre de la historia de `answer`
y de los exámenes del tema (fecha y si es recuperatorio), nada más: se reconstruye reproduciendo
en orden las respuestas de la pregunta (o de todos los miembros del grupo) con los exámenes de hoy.
Toda consulta que une `question` con `review_state` usa `CARD_KEY_SQL`, y la que cuenta tarjetas,
`HEAD`: nadie más arma la clave a mano.
`sync_review_state` rehace las tarjetas con respuestas nuevas (marca en `kv`) y las de los temas
cuyos exámenes cambiaron desde la última vez (otra marca en `kv`), así que reproducir toda la
historia y registrar respuesta por respuesta dan lo mismo, y repetirlo no cambia nada.

Notas FSRS:
- error → Again; acierto con "Adiviné" → Again (un acierto de suerte no es recuperación);
- acierto con "Me parece", o con opciones pedidas después de intentar escribir → Hard;
- acierto con "Seguro" → Good; con "Seguro" escribiendo sin pedir opciones → Easy.
- Confianza NULL (respuestas viejas o sin preguntar) cuenta como "Seguro".
- Las respuestas `pretest` no tocan nada.
- "Explicá por qué": si el estudiante dice que no dijo lo mismo (`explain_match = 0`), la nota FSRS de
  ese acierto es Again, así la pregunta vuelve pronto (~1 día). Para el reaprendizaje sucesivo sigue
  contando como acierto: no reinicia los días ni manda la pregunta a reaprendizaje; solo cambia la
  fecha. "Sí, dije lo mismo" no cambia nada.

FSRS se usa sin pasos de aprendizaje (`learning_steps=()`, `relearning_steps=()`): el corto
plazo lo maneja el reaprendizaje sucesivo. Después de un error (o de un "Adiviné") la pregunta
queda "en reaprendizaje" hasta acertarla con Hard o mejor en 3 días locales distintos
(Buenos Aires). Mientras tanto vence ya (vuelve en la misma sesión), al empezar el día local
siguiente al 1.er acierto y 2 días después del 2.º, proponga lo que proponga FSRS.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler, State

from estudia.study.exams import RETAKE, exam_topics
from estudia.study.schedule import TZ

GUESS, UNSURE, SURE = 0, 1, 2   # confianza: adiviné, me parece, seguro
MODES = ("mcq", "mcq_think_first", "typed", "typed_then_mcq", "mock", "pretest", "probe")

STATE_REVIEW = int(State.Review)          # 2: graduada o nunca fallada
STATE_RELEARNING = int(State.Relearning)  # 3: en reaprendizaje sucesivo
RELEARN_DAYS = 3                          # días locales distintos con acierto para graduarse

RETENTION_FAR = 0.85     # retención deseada si faltan más de 7 días para el examen
RETENTION_NEAR = 0.92    # en la última semana, y siempre antes de un recuperatorio
NEAR_EXAM_DAYS = 7
PULL_FIRST_DAY = 4       # el repaso adelantado cae entre 4 ...
PULL_LAST_DAY = 2        # ... y 2 días antes del examen

# Última respuesta ya reflejada en review_state. Vale porque `answer` es de solo agregar: ninguna
# respuesta se borra nunca. Si se borrara la de id más alto, SQLite podría reusar ese id (la
# tabla no es AUTOINCREMENT) y la respuesta nueva quedaría debajo de la marca, sin reflejar.
WATERMARK_KEY = "review_state_answer_id"
# Exámenes por tema con los que se derivó review_state: pares [fecha, es_recuperatorio]. Una marca
# de antes (solo fechas) no coincide nunca y rehace una vez los temas con examen.
EXAMS_KEY = "review_state_exam_dates"

_SCHEDULERS: dict[float, Scheduler] = {}


# Clave de la tarjeta de cada fila de `question` (alias `q`), para unir con `review_state`.
CARD_KEY_SQL = "(CASE WHEN q.variant_group IS NULL THEN 'q:' || q.id ELSE 'v:' || q.variant_group END)"
# Una fila por tarjeta: las preguntas sin grupo y la original (cabeza) de cada grupo.
HEAD = "(q.variant_group IS NULL OR q.variant_group = q.id)"


def card_key(question_id: int) -> str:
    """Clave de una pregunta sin grupo. Para cualquier pregunta, `card_ref(...).key`."""
    return f"q:{question_id}"


def group_card_key(group_id: int) -> str:
    return f"v:{group_id}"


@dataclass
class CardRef:
    """La tarjeta de una pregunta: su clave, la original (o ella misma), los miembros y el tema."""

    key: str
    head_id: int
    member_ids: list[int]
    topic_id: int

    @property
    def grouped(self) -> bool:
        """True si es la tarjeta de un grupo de variantes (`v:`); False si es una pregunta suelta."""
        return self.key == group_card_key(self.head_id)


def card_ref(conn: sqlite3.Connection, question_id: int) -> CardRef | None:
    """Sin grupo: `(q:<id>, id, [id], tema)`. En un grupo: `(v:<g>, g, miembros en orden, tema de la
    original)`. None si la pregunta no existe."""
    q = conn.execute("SELECT id, topic_id, variant_group FROM question WHERE id = ?", (question_id,)).fetchone()
    if q is None:
        return None
    group = q["variant_group"]
    if group is None:
        return CardRef(card_key(q["id"]), q["id"], [q["id"]], q["topic_id"])
    members = [r[0] for r in conn.execute(
        "SELECT id FROM question WHERE variant_group = ? ORDER BY id", (group,))]
    head = conn.execute("SELECT topic_id FROM question WHERE id = ?", (group,)).fetchone()
    return CardRef(group_card_key(group), group, members, head["topic_id"] if head else q["topic_id"])


def scheduler(desired_retention: float = RETENTION_FAR) -> Scheduler:
    if desired_retention not in _SCHEDULERS:
        _SCHEDULERS[desired_retention] = Scheduler(
            desired_retention=desired_retention,
            learning_steps=(),
            relearning_steps=(),
            enable_fuzzing=False,  # misma historia, mismo resultado
        )
    return _SCHEDULERS[desired_retention]


def rating_for(correct: bool, confidence: int | None, mode: str | None,
               explain_match: int | None = None) -> Rating:
    """Nota FSRS. Con `explain_match == 0` ("Explicá por qué": no dijo lo mismo) es Again, para que la
    pregunta vuelva pronto. Ojo: eso no la vuelve un error; el reaprendizaje sucesivo mira
    `_base_rating` (ver `_replay`)."""
    if explain_match == 0:
        return Rating.Again
    return _base_rating(correct, confidence, mode)


def _base_rating(correct: bool, confidence: int | None, mode: str | None) -> Rating:
    if not correct:
        return Rating.Again
    confidence = SURE if confidence is None else confidence
    if confidence == GUESS:
        return Rating.Again
    if confidence == UNSURE or mode == "typed_then_mcq":
        return Rating.Hard
    if mode == "typed":
        return Rating.Easy
    return Rating.Good


def retrievability(stability: float, last_review: int, at: int) -> float:
    """Probabilidad FSRS de recordar en `at` (curva de olvido de FSRS-6, en días enteros)."""
    card = Card(card_id=0, state=State.Review, stability=stability, difficulty=5.0,
                last_review=_utc(last_review))
    return scheduler().get_card_retrievability(card, _utc(at))


# --- Días locales ------------------------------------------------------------------------------

def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def local_day(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, TZ).date()


def day_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())


# --- Examen como horizonte ---------------------------------------------------------------------

def exam_dates_by_topic(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """Tema → fechas (epoch) de los exámenes con fecha que lo incluyen, en orden."""
    out: dict[int, list[int]] = {}
    for exam in conn.execute("SELECT id, opens_at FROM exam WHERE opens_at IS NOT NULL AND COALESCE(kind, 'parcial') != 'inscripcion'"):
        for topic_id in exam_topics(conn, exam["id"]):
            out.setdefault(topic_id, []).append(exam["opens_at"])
    return {t: sorted(set(dates)) for t, dates in out.items()}


def exam_schedule_by_topic(conn: sqlite3.Connection) -> dict[int, list[tuple[int, bool]]]:
    """Tema → (fecha, es_recuperatorio) de los exámenes con fecha que lo incluyen, en orden."""
    out: dict[int, list[tuple[int, bool]]] = {}
    for exam in conn.execute(
        "SELECT id, opens_at, COALESCE(kind, 'parcial') AS kind FROM exam "
        "WHERE opens_at IS NOT NULL AND COALESCE(kind, 'parcial') != 'inscripcion'"
    ):
        for topic_id in exam_topics(conn, exam["id"]):
            out.setdefault(topic_id, []).append((exam["opens_at"], exam["kind"] == RETAKE))
    return {t: sorted(set(items)) for t, items in out.items()}


def target_retention(at: int, exam_at: int | None, retake: bool = False) -> float:
    """0,92 si el examen cae dentro de los próximos 7 días locales; si no (o sin examen), 0,85.

    Antes de un recuperatorio (`retake`) es 0,92 desde el primer día, falte lo que falte.
    """
    if retake:
        return RETENTION_NEAR
    if exam_at is None or exam_at <= at:
        return RETENTION_FAR
    days_left = (local_day(exam_at) - local_day(at)).days
    return RETENTION_NEAR if days_left <= NEAR_EXAM_DAYS else RETENTION_FAR


def _next_exam(schedule: list[tuple[int, bool]], at: int) -> tuple[int | None, bool]:
    """Fecha del próximo examen después de `at` y si ese día hay un recuperatorio."""
    exam_at = next((d for d, _ in schedule if d > at), None)
    return exam_at, any(retake for d, retake in schedule if d == exam_at)


def _pulled_due(due: int, stability: float, last_review: int, exam_dates: list[int]) -> int:
    """Regla de Cepeda: si la próxima fecha cae después de un examen y la recuperabilidad ese día
    queda debajo de 0,92, el repaso se adelanta a entre 4 y 2 días antes del examen, nunca antes
    del día local siguiente al último repaso.

    El umbral es siempre el de la última semana porque el repaso adelantado cae dentro de ella.
    Así el resultado depende solo de la tarjeta y de los exámenes, no de cuándo se calcula.
    """
    for exam_at in exam_dates:
        if exam_at <= last_review:
            continue
        if due <= exam_at:
            break  # vence antes de este examen, y por lo tanto de los siguientes
        if retrievability(stability, last_review, exam_at) >= RETENTION_NEAR:
            continue
        exam_day = local_day(exam_at)
        start = max(day_start(exam_day - timedelta(days=PULL_FIRST_DAY)),
                    day_start(local_day(last_review) + timedelta(days=1)))
        if start < day_start(exam_day - timedelta(days=PULL_LAST_DAY - 1)):
            due = min(due, start)
    return due


# --- Reconstrucción de una tarjeta -------------------------------------------------------------

def _replay(key: str, card_id: int, answers: list[sqlite3.Row],
            schedule: list[tuple[int, bool]]) -> dict | None:
    """Estado de la tarjeta `key` después de las respuestas dadas (ya ordenadas, sin pretest), con los
    exámenes de su tema como pares (fecha, es_recuperatorio). `card_id` es el id de la original."""
    if not answers:
        return None
    card = Card(card_id=card_id)
    state = STATE_REVIEW
    relearn_days = 0
    relearn_last: date | None = None
    due = 0
    for a in answers:
        at = a["at"]
        rating = rating_for(bool(a["correct"]), a["confidence"], a["mode"], a["explain_match"])
        retention = target_retention(at, *_next_exam(schedule, at))
        card, _ = scheduler(retention).review_card(card, rating, _utc(at))
        due = int(card.due.timestamp())
        today = local_day(at)
        if _base_rating(bool(a["correct"]), a["confidence"], a["mode"]) == Rating.Again:
            # Un error (o "Adiviné"; no un "No" de "Explicá por qué", que solo adelanta la fecha).
            # Vuelve al final de la misma sesión y el contador arranca de nuevo.
            state, relearn_days, relearn_last, due = STATE_RELEARNING, 0, None, at
            if a["mode"] == "mock":
                # Un error en el simulacro no se repasa en el momento: vuelve mañana.
                due = day_start(today + timedelta(days=1))
        elif state == STATE_RELEARNING:
            if relearn_last != today:
                relearn_days += 1
                relearn_last = today
            if relearn_days >= RELEARN_DAYS:
                state = STATE_REVIEW
            else:
                # 1.er día: mañana; 2.º día: dentro de 2 días. Manda esta regla, no FSRS.
                due = day_start(relearn_last + timedelta(days=relearn_days))
    due = _pulled_due(due, card.stability, answers[-1]["at"], sorted({d for d, _ in schedule}))
    return {
        "card_key": key,
        "stability": card.stability,
        "difficulty": card.difficulty,
        "state": state,
        "due": due,
        "last_review": answers[-1]["at"],
        "relearn_days": relearn_days,
        "relearn_last_day": relearn_last.isoformat() if relearn_last else None,
    }


def rebuild_card(conn: sqlite3.Connection, question_id: int,
                 schedule_by_topic: dict[int, list[tuple[int, bool]]] | None = None) -> None:
    """Rehace la tarjeta de la pregunta desde su historia (sin commit).

    En un grupo reproduce las respuestas de todos los miembros, en orden, y borra las filas `q:` que
    hubieran quedado de cuando un miembro no tenía grupo: su historia ya está en la del grupo.
    """
    ref = card_ref(conn, question_id)
    if ref is None:
        return
    if schedule_by_topic is None:
        schedule_by_topic = exam_schedule_by_topic(conn)
    marks = ",".join("?" * len(ref.member_ids))
    answers = conn.execute(
        "SELECT correct, confidence, mode, explain_match, at FROM answer "
        f"WHERE question_id IN ({marks}) AND COALESCE(mode, '') != 'pretest' ORDER BY at, id",
        ref.member_ids,
    ).fetchall()
    if ref.grouped:
        conn.executemany("DELETE FROM review_state WHERE card_key = ?",
                         [(card_key(m),) for m in ref.member_ids])
    row = _replay(ref.key, ref.head_id, answers, schedule_by_topic.get(ref.topic_id, []))
    if row is None:
        conn.execute("DELETE FROM review_state WHERE card_key = ?", (ref.key,))
        return
    conn.execute(
        "INSERT OR REPLACE INTO review_state(card_key, stability, difficulty, state, due, "
        "last_review, relearn_days, relearn_last_day) VALUES (:card_key, :stability, :difficulty, "
        ":state, :due, :last_review, :relearn_days, :relearn_last_day)",
        row,
    )


def _marker(schedule: dict[int, list[tuple[int, bool]]]) -> dict[int, list[list]]:
    """La forma en que `EXAMS_KEY` guarda los exámenes por tema: pares [fecha, es_recuperatorio]."""
    return {t: [[d, retake] for d, retake in items] for t, items in schedule.items()}


def _stored_exam_schedule(conn: sqlite3.Connection) -> dict[int, list]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (EXAMS_KEY,)).fetchone()
    try:
        return {int(t): items for t, items in json.loads(row["value"]).items()} if row else {}
    except (TypeError, ValueError, AttributeError):
        return {}  # una marca rota obliga a rehacer los temas con examen, nada más


def _set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def sync_review_state(conn: sqlite3.Connection) -> int:
    """Deja `review_state` al día (sin commit). Devuelve las tarjetas rehechas.

    Rehace las tarjetas con respuestas nuevas y las de los temas cuyos exámenes se cargaron,
    movieron, cambiaron de tipo (recuperatorio o no) o borraron desde la última vez: un adelanto por un examen que ya no está se deshace.
    También las de los grupos con una fila `q:` vieja de un miembro (una original contestada que
    después recibió variantes): el grupo hereda esa historia. Cada grupo se rehace una sola vez.
    Si no cambió nada, solo lee: no abre una escritura.
    """
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (WATERMARK_KEY,)).fetchone()
    done = int(row["value"]) if row else 0
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM answer").fetchone()[0]
    schedule = exam_schedule_by_topic(conn)
    current = _marker(schedule)
    stored = _stored_exam_schedule(conn)
    changed_topics = sorted(t for t in set(current) | set(stored) if current.get(t) != stored.get(t))
    # Grupos con una fila `q:` de un miembro: la original ya tenía tarjeta cuando recibió variantes.
    heads = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT q.variant_group FROM question q JOIN review_state rs "
            "ON rs.card_key = 'q:' || q.id WHERE q.variant_group IS NOT NULL"
        )
    }
    if top <= done and not changed_topics and not heads:
        return 0

    # Cada id se pasa a su tarjeta (la original del grupo), así un grupo se rehace una vez.
    heads |= {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT COALESCE(q.variant_group, a.question_id) FROM answer a "
            "LEFT JOIN question q ON q.id = a.question_id "
            "WHERE a.id > ? AND COALESCE(a.mode, '') != 'pretest'",
            (done,),
        )
    }
    if changed_topics:
        marks = ",".join("?" * len(changed_topics))
        heads |= {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT COALESCE(q.variant_group, q.id) FROM answer a JOIN question q ON q.id = a.question_id "
                f"WHERE q.topic_id IN ({marks}) AND COALESCE(a.mode, '') != 'pretest'",
                changed_topics,
            )
        }
    for head in sorted(heads):
        rebuild_card(conn, head, schedule)
    if top > done:
        _set_kv(conn, WATERMARK_KEY, str(top))
    if changed_topics:
        _set_kv(conn, EXAMS_KEY, json.dumps({str(t): items for t, items in sorted(current.items())}))
    return len(heads)


def _is_ms(value) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def validate_answer_extras(confidence, mode, latency_ms, option_order, typed_text,
                           options_revealed_ms=None) -> str | None:
    """Valida los datos extra de una respuesta; devuelve `option_order` como JSON (o None)."""
    if confidence is not None and (isinstance(confidence, bool) or confidence not in (GUESS, UNSURE, SURE)):
        raise ValueError("La confianza tiene que ser 0 (adiviné), 1 (me parece) o 2 (seguro)")
    if mode not in MODES:
        raise ValueError(f"Modo de respuesta desconocido: {mode}")
    if not _is_ms(latency_ms):
        raise ValueError("La latencia tiene que ser un entero de milisegundos")
    if not _is_ms(options_revealed_ms):
        raise ValueError("El momento en que se abrieron las opciones tiene que ser un entero de milisegundos")
    if typed_text is not None and not isinstance(typed_text, str):
        raise ValueError("La respuesta escrita tiene que ser texto")
    if option_order is None:
        return None
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in option_order):
        raise ValueError("El orden de opciones tiene que ser una lista de enteros")
    return json.dumps(list(option_order))
