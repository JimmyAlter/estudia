"""Práctica: armado del cuestionario, registro de respuestas y repaso de errores."""
import json
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from fsrs import Rating

from estudia.generation.verify import UncheckableExpr, grade_typed, typed_accepts_option, typed_checkable
from estudia.study.mastery import topic_probability_at_exam
from estudia.study.sessions import MIXED_ONLY, session_topics, split_minutes
from estudia.study.srs import (
    RELEARN_DAYS,
    STATE_RELEARNING,
    STATE_REVIEW,
    SURE,
    UNSURE,
    card_key,
    day_start,
    local_day,
    rating_for,
    rebuild_card,
    sync_review_state,
    validate_answer_extras,
)

# Formato en que se muestra cada ítem ([CA] §2.1).
MCQ, THINK_FIRST, TYPED = "mcq", "think_first", "typed"
TYPED_SHARE = 0.5          # con check_expr, vistas y nunca acertadas: escritas en la mitad de los casos
# Fase dentro de una sesión del plan: repaso mezclado de vencidas primero, después el tema foco.
REVIEW, FOCUS = "review", "focus"
# Modos sin sesión de práctica detrás: no vuelven "al final de la sesión" ni muestran próxima fecha.
_NO_SESSION_MODES = ("mock", "pretest")
# "Explicá por qué": solo en práctica, después de un acierto con "Me parece", y nunca en 2 respuestas
# calificadas seguidas: sale la 1.ª, la 4.ª, la 7.ª...
EXPLAIN_EVERY = 3
_EXPLAIN_MODES = ("mcq", "mcq_think_first", "typed", "typed_then_mcq")


@dataclass
class QuizItem:
    """Pregunta para mostrar; a propósito no incluye la respuesta correcta."""

    question_id: int
    stem: str
    options: list[str]
    topic: str
    format: str = MCQ                # mcq | think_first | typed
    phase: str = FOCUS               # review | focus
    relearning: dict | None = None   # {"day": 1..3} si está en reaprendizaje


@dataclass
class AnswerFeedback:
    correct: bool
    correct_index: int
    explanation: str
    common_error: str | None
    source_seconds: int | None
    youtube_id: str | None
    chosen_index: int | None = None  # índice original; None: escrita o sin contestar (simulacro)
    typed_ok: bool | None = None     # solo en respuestas escritas
    requeue: bool = False            # vuelve al final de la sesión
    next_label: str | None = None    # "Vuelve mañana", "Vuelve en 2 días", "La sabés: vuelve en 9 días"
    relearn_progress: str | None = None   # "1 de 3 días" mientras está en reaprendizaje
    feedback_style: str = "full"     # alarm | full | short | doubt (ver `feedback_style`)
    question_id: int | None = None   # de qué pregunta es (la revisión del simulacro se arma por id)
    answer_id: int | None = None     # fila de `answer` recién guardada
    explain_prompt: bool = False     # pedir "Explicá por qué" (se responde con `record_explain_match`)


class UnreadableAnswer(ValueError):
    """La respuesta escrita no se pudo leer: se pide reescribirla y no se registra nada."""


NOT_TYPEABLE_TEXT = "Esta pregunta no admite respuesta escrita"
# Pregunta marcada (la regla escrita aceptaría un distractor): se contesta con las opciones.
OPTIONS_ONLY_TEXT = "Esta pregunta va con opciones. Tocá «Mostrame opciones»."


class NotTypeable(ValueError):
    """La pregunta no tiene `check_expr`: no se puede responder escribiendo."""


def _weakest_topic(conn: sqlite3.Connection, course_id: int, now: int) -> int | None:
    """El tema con menor probabilidad de saberlo el día de su examen (o hoy, si no tiene)."""
    rows = conn.execute(
        "SELECT DISTINCT t.id FROM topic t JOIN question q ON q.topic_id = t.id "
        "WHERE t.course_id = ? AND q.verified = 1 AND q.retired = 0 ORDER BY t.id",
        (course_id,),
    ).fetchall()
    if not rows:
        return None
    return min((r["id"] for r in rows), key=lambda tid: topic_probability_at_exam(conn, tid, now))


def _ranked(
    conn: sqlite3.Connection, course_id: int, topic_ids: list[int] | None, now: int
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Preguntas disponibles en orden de práctica: (vencidas, resto). Ver `build_quiz`."""
    # `review_state` ya está al día: lo sincronizan `connect` y cada respuesta registrada.
    sql = (
        "SELECT q.id, q.stem, q.options_json, q.check_expr, q.correct_index, q.typed_safe, "
        "t.name AS topic, rs.state, rs.due, "
        "rs.last_review, rs.relearn_days, rs.relearn_last_day, hit.question_id IS NOT NULL AS ever_correct "
        "FROM question q JOIN topic t ON t.id = q.topic_id "
        "LEFT JOIN review_state rs ON rs.card_key = 'q:' || q.id "
        "LEFT JOIN (SELECT DISTINCT question_id FROM answer "
        "WHERE correct = 1 AND COALESCE(mode, '') != 'pretest') hit ON hit.question_id = q.id "
        "WHERE t.course_id = ? AND q.verified = 1 AND q.retired = 0"
    )
    params: list = [course_id]
    if topic_ids is not None:
        sql += f" AND t.id IN ({','.join('?' * len(topic_ids))})"
        params.extend(topic_ids)
    questions = conn.execute(sql + " ORDER BY q.id", params).fetchall()

    today = local_day(now)
    today_end = day_start(today + timedelta(days=1))
    session_errors, due, relearning_today, fresh, rest = [], [], [], [], []
    for q in questions:
        relearning = q["state"] == STATE_RELEARNING
        if q["state"] is None:
            fresh.append(q)
        elif relearning and q["relearn_days"] == 0 and local_day(q["last_review"]) == today:
            session_errors.append(q)
        elif q["due"] <= now:
            due.append(q)
        elif relearning and q["due"] < today_end:
            relearning_today.append(q)
        else:
            rest.append(q)

    rng = random.Random(now)
    rng.shuffle(fresh)
    rng.shuffle(rest)
    overdue = (
        sorted(session_errors, key=lambda q: (q["last_review"], q["id"]))
        + sorted(due, key=lambda q: (q["due"], q["id"]))
    )
    return overdue, sorted(relearning_today, key=lambda q: (q["due"], q["id"])) + fresh + rest


def typed_safe_ids(conn: sqlite3.Connection, rows) -> set[int]:
    """Ids de las preguntas de `rows` que se pueden ofrecer escritas.

    Hace falta un `check_expr` que sirva y que la regla escrita no acepte ningún distractor (con
    `check_expr` "sqrt(2)", "1.41" de distractor: la opción múltiple lo da mal y la escrita bien).
    Se mira la columna `typed_safe`; si todavía no se sabe se calcula en el worker y se anota.
    A la primera duda (worker caído o lento) se corta: el resto queda sin saber y esta vez no se
    ofrece escrito. Los veredictos se escriben juntos al final, en una transacción corta (nunca con
    el candado tomado mientras trabaja el worker). Cada fila necesita id, options_json,
    correct_index, check_expr y typed_safe.
    """
    safe: set[int] = set()
    verdicts: dict[int, bool] = {}
    for q in rows:
        if not typed_checkable(q["check_expr"]):
            continue
        if q["typed_safe"] is not None:
            if q["typed_safe"] == 1:
                safe.add(q["id"])
            continue
        verdict: bool | None = True
        for i, option in enumerate(json.loads(q["options_json"])):
            if i == q["correct_index"]:
                continue
            accepted = typed_accepts_option(q["check_expr"], option)
            if accepted is not False:
                verdict = None if accepted is None else False
                break
        if verdict is None:
            break   # el worker no contesta: no se insiste con las demás
        verdicts[q["id"]] = verdict
        if verdict:
            safe.add(q["id"])
    if verdicts:
        opened_here = not conn.in_transaction
        conn.executemany("UPDATE question SET typed_safe = ? WHERE id = ?",
                         [(1 if ok else 0, qid) for qid, ok in verdicts.items()])
        if opened_here:
            conn.commit()
    return safe


def _format(q: sqlite3.Row, now: int, typed_ok: set[int]) -> str:
    """mcq para las nuevas; typed si se puede escribir (`typed_safe_ids`) y ya se acertó (si no, en
    la mitad de los casos, con semilla `now` y la pregunta); think_first para el resto de las ya vistas."""
    if q["state"] is None:
        return MCQ
    if q["id"] in typed_ok and (q["ever_correct"] or random.Random(f"{now}:{q['id']}").random() < TYPED_SHARE):
        return TYPED
    return THINK_FIRST


def _relearning(q: sqlite3.Row, today: date) -> dict | None:
    """Qué día de reaprendizaje sería acertarla ahora: el siguiente al último cumplido, o ese mismo."""
    if q["state"] != STATE_RELEARNING:
        return None
    day = q["relearn_days"] + (0 if q["relearn_last_day"] == today.isoformat() else 1)
    return {"day": min(max(day, 1), RELEARN_DAYS)}


def _items(conn: sqlite3.Connection, rows: list[sqlite3.Row], phase: str, now: int) -> list[QuizItem]:
    today = local_day(now)
    typed_ok = typed_safe_ids(conn, [q for q in rows if q["state"] is not None])
    return [
        QuizItem(q["id"], q["stem"], json.loads(q["options_json"]), q["topic"],
                 format=_format(q, now, typed_ok), phase=phase, relearning=_relearning(q, today))
        for q in rows
    ]


def build_quiz(
    conn: sqlite3.Connection,
    course_id: int,
    *,
    topic_id: int | None = None,
    topic_ids: list[int] | None = None,
    weakest: bool = False,
    size: int = 10,
    now: int,
) -> list[QuizItem]:
    """Cuestionario de hasta `size` preguntas verificadas y no retiradas.

    Orden:
    1. errores de hoy todavía sin acertar (vuelven al final de la sesión), del más viejo al más nuevo;
    2. vencidas (`due <= now`), la más atrasada primero;
    3. en reaprendizaje que vencen más tarde hoy;
    4. nunca vistas;
    5. el resto.
    Los grupos 4 y 5 van al azar con semilla `now`. Gana `topic_id`, después `topic_ids` (varios
    temas mezclados) y por último `weakest`. Sin sesión del plan, todos los ítems son de fase foco.
    """
    if topic_id is not None:
        topic_ids = [topic_id]
    elif not topic_ids:
        topic_ids = None
        if weakest:
            weakest_id = _weakest_topic(conn, course_id, now)
            if weakest_id is None:
                return []
            topic_ids = [weakest_id]
    overdue, others = _ranked(conn, course_id, topic_ids, now)
    return _items(conn, (overdue + others)[:size], FOCUS, now)


def build_session_quiz(conn: sqlite3.Connection, session_id: int, *, size: int = 10, now: int) -> list[QuizItem]:
    """Cuestionario de una sesión del plan: repaso mezclado de vencidas primero y después el foco.

    - `focus`: todo del tema foco, como `build_quiz`.
    - `mixed_focus`: las vencidas más atrasadas de todos los temas de la sesión, en la proporción
      de minutos de repaso (10 de 50 → 2 de 10 ítems, redondeando para arriba), y el resto del foco.
    - `mixed_only`: todo es repaso mezclado de sus temas: vencidas primero y después el resto,
      nuevas incluidas.
    `LookupError` si la sesión no existe.
    """
    s = conn.execute(
        "SELECT course_id, topic_id, minutes, COALESCE(kind, 'focus') AS kind, topics_json "
        "FROM plan_session WHERE id = ?",
        (session_id,),
    ).fetchone()
    if s is None:
        raise LookupError(f"sesión {session_id} inexistente")
    topics = session_topics(s)
    if s["kind"] == MIXED_ONLY:
        overdue, others = _ranked(conn, s["course_id"], topics, now)
        return _items(conn, (overdue + others)[:size], REVIEW, now)

    review_min, _ = split_minutes(s["kind"], s["minutes"])
    review = []
    if review_min:
        overdue, _ = _ranked(conn, s["course_id"], topics, now)
        review = overdue[:math.ceil(size * review_min / s["minutes"])]
    taken = {q["id"] for q in review}
    overdue, others = _ranked(conn, s["course_id"], [s["topic_id"]], now)
    focus = [q for q in overdue + others if q["id"] not in taken][:size - len(review)]
    return _items(conn, review, REVIEW, now) + _items(conn, focus, FOCUS, now)


def feedback_style(correct: bool, confidence: int | None) -> str:
    """alarm: error con "Seguro"; full: error dudando; short: acierto con "Seguro"; doubt: acierto
    con "Me parece" o "Adiviné". Sin confianza cuenta como "Seguro", como en `srs.rating_for`."""
    sure = confidence is None or confidence == SURE
    if correct:
        return "short" if sure else "doubt"
    return "alarm" if sure else "full"


def _next_label(review: sqlite3.Row, rating: Rating, now: int) -> str:
    """Cuándo vuelve, en palabras. "La sabés" solo fuera del reaprendizaje y sin dudar."""
    if rating == Rating.Again:
        return "Vuelve al final de la sesión"
    days = (local_day(review["due"]) - local_day(now)).days
    when = "hoy" if days <= 0 else "mañana" if days == 1 else f"en {days} días"
    if review["state"] == STATE_REVIEW and rating in (Rating.Good, Rating.Easy):
        return f"La sabés: vuelve {when}"
    return f"Vuelve {when}"


def should_prompt_explain(conn: sqlite3.Connection, correct: bool, confidence: int | None, mode: str) -> bool:
    """¿Corresponde pedir "Explicá por qué" después de esta respuesta (todavía sin guardar)?

    Solo un acierto con "Me parece" en un modo de práctica, y solo si ninguna de las
    `EXPLAIN_EVERY - 1` respuestas calificadas anteriores (acierto con "Me parece" en práctica) lo pidió.
    """
    if not correct or confidence != UNSURE or mode not in _EXPLAIN_MODES:
        return False
    recent = conn.execute(
        "SELECT explain_prompted FROM answer WHERE correct = 1 AND confidence = ? "
        f"AND mode IN ({','.join('?' * len(_EXPLAIN_MODES))}) ORDER BY at DESC, id DESC LIMIT ?",
        (UNSURE, *_EXPLAIN_MODES, EXPLAIN_EVERY - 1),
    ).fetchall()
    return not any(r["explain_prompted"] == 1 for r in recent)


def record_explain_match(conn: sqlite3.Connection, answer_id: int, same: bool) -> None:
    """Guarda si el estudiante dijo lo mismo que la explicación, rehace la tarjeta y hace commit.

    `LookupError` si la respuesta no existe; `ValueError` si no se pidió explicación o ya se contestó.
    Lo que escribió el estudiante no llega acá: no se manda ni se guarda.
    """
    row = conn.execute(
        "SELECT question_id, explain_prompted, explain_match FROM answer WHERE id = ?", (answer_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"respuesta {answer_id} inexistente")
    if row["explain_prompted"] != 1:
        raise ValueError("A esta respuesta no se le pidió explicar")
    if row["explain_match"] is not None:
        raise ValueError("Esta explicación ya se contestó")
    conn.execute("UPDATE answer SET explain_match = ? WHERE id = ?", (1 if same else 0, answer_id))
    rebuild_card(conn, row["question_id"])
    conn.commit()


def record_answer(
    conn: sqlite3.Connection, question_id: int, chosen_index: int | None, now: int, **extras
) -> AnswerFeedback:
    """Registra la respuesta (con commit), actualiza el repaso y devuelve la corrección.

    `extras` (opcionales): `confidence` (0 adiviné, 1 me parece, 2 seguro; None cuenta como
    seguro), `mode` (ver `srs.MODES`, por defecto "mcq"), `latency_ms`, `option_order` (orden en
    que se mostraron las opciones: `chosen_index` es entonces la posición tocada), `typed_text`
    (en modo "typed" es la respuesta y `chosen_index` va en None), `options_revealed_ms`.
    """
    feedback = _insert_answer(conn, question_id, chosen_index, now, **extras)
    conn.commit()
    return feedback


def grade_typed_answer(check_expr: str | None, typed_text: str, unreadable_as_wrong: bool = False) -> bool:
    """Corrige una respuesta escrita (puede tardar: no llamarla con una transacción abierta).

    `NotTypeable` si la pregunta no se puede corregir escribiendo; `UnreadableAnswer` si no se
    pudo leer (salvo `unreadable_as_wrong`, que la cuenta como error); `GradingUnavailable` pasa de largo.
    """
    if not typed_checkable(check_expr):
        raise NotTypeable(NOT_TYPEABLE_TEXT)
    try:
        ok = grade_typed(check_expr, typed_text)
    except UncheckableExpr:
        raise NotTypeable(NOT_TYPEABLE_TEXT) from None
    if ok is None:
        if not unreadable_as_wrong:   # simulacro: lo ilegible cuenta como error, no frena la entrega
            raise UnreadableAnswer("No pude leer la expresión")
        return False
    return ok


def _insert_answer(
    conn: sqlite3.Connection, question_id: int, chosen_index: int | None, now: int, *,
    confidence: int | None = None, mode: str = "mcq", latency_ms: int | None = None,
    option_order: list[int] | None = None, typed_text: str | None = None,
    options_revealed_ms: int | None = None, unreadable_as_wrong: bool = False,
    typed_ok: bool | None = None, unanswered: bool = False,
) -> AnswerFeedback:
    """Como `record_answer` pero sin commit: el que llama maneja la transacción.

    `LookupError` si la pregunta no existe, no está verificada o está retirada;
    `NotTypeable` si es escrita y la pregunta no tiene un `check_expr` que sirva para corregir; `UnreadableAnswer`
    si la respuesta escrita no se puede leer; `verify.GradingUnavailable` si no se pudo corregir
    ahora (worker caído o lento); `ValueError` si `chosen_index` está fuera de rango o
    falta, si `option_order` no es una permutación de las opciones (en una escrita tiene que
    venir vacío o no venir) o si algún dato extra no es válido. No escribe nada en esos casos: la respuesta escrita se corrige antes de insertar.
    """
    option_order_json = validate_answer_extras(
        confidence, mode, latency_ms, option_order, typed_text, options_revealed_ms)
    q = conn.execute(
        "SELECT q.*, m.ref AS youtube_id FROM question q "
        "LEFT JOIN material m ON m.id = q.material_id "
        "WHERE q.id = ? AND q.verified = 1 AND q.retired = 0",
        (question_id,),
    ).fetchone()
    if q is None:
        raise LookupError(f"pregunta {question_id} no disponible")
    options = json.loads(q["options_json"])

    # `typed_ok`: corrección escrita ya hecha por el que llama (fuera de la transacción).
    # `unanswered` (solo simulacro): sin respuesta, cuenta como error y no lleva opción ni texto.
    if unanswered:
        if mode != "mock" or chosen_index is not None or typed_text is not None:
            raise ValueError("Solo el simulacro registra preguntas sin contestar")
        option_order_json = None
        correct = False
    # En el simulacro se escribe con mode="mock" y typed_text; las demás respuestas con mode="typed".
    elif mode == "typed" or (mode == "mock" and typed_text is not None):
        if chosen_index is not None:
            raise ValueError("Una respuesta escrita no lleva opción elegida")
        if option_order:
            raise ValueError("Una respuesta escrita no muestra opciones")
        option_order_json = None
        if typed_text is None:
            raise ValueError("Falta la respuesta escrita")
        if typed_ok is None:
            if mode == "typed" and q["typed_safe"] == 0:
                # La regla escrita aceptaría un distractor: esta pregunta no se responde escribiendo.
                raise NotTypeable(OPTIONS_ONLY_TEXT)
            typed_ok = grade_typed_answer(q["check_expr"], typed_text, unreadable_as_wrong)
        correct = typed_ok
    else:
        if chosen_index is None:
            raise ValueError("Falta la opción elegida")
        if not 0 <= chosen_index < len(options):
            raise ValueError(f"opción {chosen_index} fuera de rango")
        if option_order is not None:
            if sorted(option_order) != list(range(len(options))):
                raise ValueError("El orden mostrado no corresponde a las opciones de la pregunta")
            chosen_index = option_order[chosen_index]   # de la posición mostrada a la original
        correct = chosen_index == q["correct_index"]

    explain_prompt = should_prompt_explain(conn, correct, confidence, mode)
    cur = conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at, confidence, latency_ms, mode, "
        "option_order_json, typed_text, options_revealed_ms, explain_prompted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (question_id, chosen_index, 1 if correct else 0, now, confidence, latency_ms, mode,
         option_order_json, typed_text, options_revealed_ms, 1 if explain_prompt else 0),
    )
    answer_id = cur.lastrowid
    sync_review_state(conn)

    requeue, next_label, relearn_progress = False, None, None
    review = conn.execute(
        "SELECT state, due, relearn_days FROM review_state WHERE card_key = ?", (card_key(question_id),)
    ).fetchone()
    if mode not in _NO_SESSION_MODES and review is not None:
        rating = rating_for(correct, confidence, mode)
        requeue = rating == Rating.Again
        next_label = _next_label(review, rating, now)
        if review["state"] == STATE_RELEARNING:
            relearn_progress = f"{review['relearn_days']} de {RELEARN_DAYS} días"
    return AnswerFeedback(
        correct=correct,
        correct_index=q["correct_index"],
        explanation=q["explanation"],
        common_error=q["common_error"],
        source_seconds=q["source_seconds"],
        youtube_id=q["youtube_id"],
        chosen_index=chosen_index,
        typed_ok=typed_ok,
        requeue=requeue,
        next_label=next_label,
        relearn_progress=relearn_progress,
        feedback_style=feedback_style(correct, confidence),
        question_id=question_id,
        answer_id=answer_id,
        explain_prompt=explain_prompt,
    )


def retire_question(conn: sqlite3.Connection, question_id: int) -> None:
    """Botón "Esta está mal": la pregunta deja de mostrarse."""
    conn.execute("UPDATE question SET retired = 1 WHERE id = ?", (question_id,))
    conn.commit()


def unretire_question(conn: sqlite3.Connection, question_id: int) -> None:
    """Deshacer el reporte de error: la pregunta vuelve a mostrarse."""
    conn.execute("UPDATE question SET retired = 0 WHERE id = ?", (question_id,))
    conn.commit()
