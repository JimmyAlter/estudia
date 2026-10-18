"""Simulacro de examen: armado, reloj y corrección con la nota en la escala del examen."""
import json
import math
import random
import sqlite3
from dataclasses import dataclass

from estudia.study.exams import exam_topics
from estudia.study.practice import (
    MCQ, TYPED, AnswerFeedback, NotTypeable, QuizItem, _insert_answer, grade_typed_answer, typed_safe_ids,
)

MIN_QUESTIONS = 5
SECONDS_PER_QUESTION = 90
GRACE_SECONDS = 30
RECENT_PRACTICE_DAYS = 7
TYPED_SHARE_MIN, TYPED_SHARE_MAX = 0.3, 0.5   # de las preguntas con `check_expr`, cuántas se escriben
DEFAULT_MAX_GRADE = 10.0


class NotEnoughQuestions(ValueError):
    """Hay menos de 5 preguntas verificadas disponibles: no se arma el simulacro."""


@dataclass
class MockStart:
    attempt_id: int
    items: list[QuizItem]  # sin la respuesta correcta
    seconds: int
    max_grade: float
    predicted_grade: float | None = None   # la nota que el estudiante esperaba, si la dijo


@dataclass
class MockResult:
    grade: float
    max_grade: float
    correct: int
    total: int
    failed_topics: list[str]
    review: list[AnswerFeedback]  # uno por pregunta, en el orden del intento
    late: bool = False
    predicted_grade: float | None = None


def _exam_row(conn: sqlite3.Connection, exam_id: int):
    row = conn.execute(
        "SELECT id, max_grade, time_limit_s FROM exam WHERE id = ?", (exam_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"examen {exam_id} no existe")
    return row


def _seen_in_mocks(conn: sqlite3.Connection) -> set[int]:
    """Preguntas contestadas en simulacros anteriores."""
    seen: set[int] = set()
    for r in conn.execute("SELECT answers_json FROM mock_attempt WHERE finished_at IS NOT NULL"):
        seen.update(int(k) for k in json.loads(r["answers_json"] or "{}"))
    return seen


def _recently_practiced(conn: sqlite3.Connection, now: int) -> set[int]:
    """Preguntas contestadas en práctica en los últimos 7 días (el simulacro y el pretest no cuentan)."""
    rows = conn.execute(
        "SELECT DISTINCT question_id FROM answer WHERE at >= ? AND COALESCE(mode, '') NOT IN ('mock', 'pretest')",
        (now - RECENT_PRACTICE_DAYS * 86400,),
    )
    return {r[0] for r in rows}


def _pick(conn: sqlite3.Connection, topic_ids: list[int], count: int, seed: int, now: int):
    """Reparte entre los temas en rueda, en orden al azar.

    En cada tema van primero las que no se practicaron en la última semana y, entre esas, las
    que no salieron en un simulacro anterior.
    """
    if not topic_ids:
        return []
    marks = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        "SELECT q.id, q.stem, q.options_json, q.topic_id, q.check_expr, q.correct_index, q.typed_safe, "
        "t.name AS topic FROM question q "
        "JOIN topic t ON t.id = q.topic_id "
        f"WHERE q.topic_id IN ({marks}) AND q.verified = 1 AND q.retired = 0 ORDER BY q.id",
        topic_ids,
    ).fetchall()
    seen = _seen_in_mocks(conn)
    recent = _recently_practiced(conn, now)
    rng = random.Random(seed)
    queues: list[list] = []
    for tid in sorted(set(topic_ids)):
        mine = [r for r in rows if r["topic_id"] == tid]
        rng.shuffle(mine)
        mine.sort(key=lambda r: (r["id"] in recent, r["id"] in seen))   # estable: conserva el azar
        if mine:
            queues.append(mine)
    rng.shuffle(queues)
    picked = []
    while queues and len(picked) < count:
        for q in list(queues):
            picked.append(q.pop(0))
            if not q:
                queues.remove(q)
            if len(picked) == count:
                break
    return picked


def _pick_typed(conn: sqlite3.Connection, picked: list, seed: int) -> list[int]:
    """Ids de las preguntas que se responden escribiendo: entre 30 y 50 % de las que se pueden
    (`practice.typed_safe_ids`: nunca una cuyo distractor aceptaría la regla escrita).

    Con una sola pregunta escribible no existe un entero en ese rango: ninguna se escribe.
    """
    safe = typed_safe_ids(conn, picked)
    able = [q["id"] for q in picked if q["id"] in safe]
    low, high = math.ceil(len(able) * TYPED_SHARE_MIN), math.floor(len(able) * TYPED_SHARE_MAX)
    if low > high:
        return []
    chosen = set(random.Random(seed + 1).sample(able, min(high, max(low, round(len(able) * 0.4)))))
    return [qid for qid in able if qid in chosen]


def start_mock(
    conn: sqlite3.Connection, exam_id: int, now: int, predicted_grade: float | None = None
) -> MockStart:
    """Arma el intento. `predicted_grade` es la nota que el estudiante espera, en la escala del examen.

    `LookupError` si el examen no existe; `NotEnoughQuestions` si no alcanzan las preguntas;
    `ValueError` si la nota esperada no es un número entre 0 y la escala.
    """
    exam = _exam_row(conn, exam_id)
    topics = exam_topics(conn, exam_id)
    count = min(20, max(10, len(topics) * 3))
    picked = _pick(conn, topics, count, now, now)
    if len(picked) < MIN_QUESTIONS:
        raise NotEnoughQuestions(
            f"Hay {len(picked)} preguntas verificadas para este examen; hacen falta al menos {MIN_QUESTIONS}"
        )
    max_grade = float(exam["max_grade"] or DEFAULT_MAX_GRADE)
    if predicted_grade is not None:
        if (isinstance(predicted_grade, bool) or not isinstance(predicted_grade, (int, float))
                or not math.isfinite(predicted_grade)):
            raise ValueError("La nota esperada tiene que ser un número")
        if not 0 <= predicted_grade <= max_grade:
            raise ValueError(f"La nota esperada tiene que estar entre 0 y {max_grade:g}")
        predicted_grade = round(float(predicted_grade), 2)
    seconds = int(exam["time_limit_s"] or SECONDS_PER_QUESTION * len(picked))
    typed_ids = _pick_typed(conn, picked, now)
    cur = conn.execute(
        "INSERT INTO mock_attempt(exam_id, started_at, question_ids_json, max_grade, seconds, "
        "predicted_grade, typed_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (exam_id, now, json.dumps([q["id"] for q in picked]), max_grade, seconds,
         predicted_grade, json.dumps(typed_ids)),
    )
    conn.commit()
    items = [
        QuizItem(q["id"], q["stem"], json.loads(q["options_json"]), q["topic"],
                 format=TYPED if q["id"] in typed_ids else MCQ)
        for q in picked
    ]
    return MockStart(cur.lastrowid, items, seconds, max_grade, predicted_grade)


def _parse_answer(raw) -> tuple[int | None, str | None]:
    """Una respuesta es el índice de la opción, `{"index": n}` o `{"typed": "texto"}`."""
    if isinstance(raw, dict):
        if ("index" in raw) == ("typed" in raw):
            raise ValueError("Cada respuesta lleva una opción (index) o un texto (typed)")
        raw = raw["index"] if "index" in raw else raw["typed"]
    if isinstance(raw, str):
        return None, raw
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("La opción elegida tiene que ser un número")
    return raw, None


def finish_mock(
    conn: sqlite3.Connection, attempt_id: int, answers: dict[int, int | dict], now: int
) -> MockResult:
    """Corrige el intento, todo en una transacción.

    Las respuestas se guardan con `mode='mock'` y sin confianza (FSRS: acierto Good, error Again);
    cada error vuelve al día local siguiente. Las escritas solo valen en las preguntas que el
    intento marcó como escritas; lo ilegible cuenta como error.

    `ValueError` si ya terminó o si alguna opción está fuera de rango; `NotEnoughQuestions`
    si ninguna pregunta sigue disponible. En cualquier error no queda nada escrito y el
    intento sigue sin entregar.
    """
    att = conn.execute("SELECT * FROM mock_attempt WHERE id = ?", (attempt_id,)).fetchone()
    if att is None:
        raise LookupError(f"intento {attempt_id} no existe")
    ordered = json.loads(att["question_ids_json"])
    seconds = int(att["seconds"] or SECONDS_PER_QUESTION * len(ordered))
    late = now > att["started_at"] + seconds + GRACE_SECONDS
    max_grade = float(att["max_grade"] or DEFAULT_MAX_GRADE)

    if att["finished_at"] is not None:
        raise ValueError("El simulacro ya se entregó")

    # Lecturas y corrección escrita ANTES de la transacción: el worker puede tardar segundos y
    # no se retiene el candado de escritura de SQLite mientras tanto. Si no está disponible,
    # `GradingUnavailable` sale sin haber reclamado nada y el intento sigue sin entregar.
    marks = ",".join("?" * len(ordered))
    info = {
        r["id"]: r
        for r in conn.execute(
            "SELECT q.id, q.options_json, q.explanation, q.common_error, q.source_seconds, "
            "q.correct_index, q.check_expr, t.name AS topic, m.ref AS youtube_id FROM question q "
            "JOIN topic t ON t.id = q.topic_id LEFT JOIN material m ON m.id = q.material_id "
            f"WHERE q.verified = 1 AND q.retired = 0 AND q.id IN ({marks})",
            ordered,
        )
    }
    # Si una pregunta se retiró o dejó de estar verificada durante el intento, no se corrige.
    live = [qid for qid in ordered if qid in info]
    if not live:
        raise NotEnoughQuestions("Ninguna pregunta del simulacro sigue disponible")
    typed_ids = set(json.loads(att["typed_json"] or "[]"))
    given: dict[int, tuple[int | None, str | None]] = {}
    for qid in live:
        if qid not in answers:
            continue
        idx, text = _parse_answer(answers[qid])
        if text is not None:
            if qid not in typed_ids:
                raise ValueError(f"La pregunta {qid} no se responde escribiendo")
            if not text.strip():
                continue   # en blanco: sin contestar
        elif not 0 <= idx < len(json.loads(info[qid]["options_json"])):
            raise ValueError(f"opción {idx} fuera de rango para la pregunta {qid}")
        given[qid] = (idx, text)
    typed_ok: dict[int, bool] = {}
    for qid, (_, text) in given.items():
        if text is None:
            continue
        try:
            typed_ok[qid] = grade_typed_answer(info[qid]["check_expr"], text, unreadable_as_wrong=True)
        except NotTypeable:
            # La pregunta dejó de poder corregirse escrita durante el intento: cuenta como error,
            # como lo ilegible; nunca frena la entrega.
            typed_ok[qid] = False

    with conn:  # commit si sale bien, rollback ante cualquier excepción
        # Se reclama primero: de dos entregas simultáneas, solo una gana.
        claimed = conn.execute(
            "UPDATE mock_attempt SET finished_at = ? WHERE id = ? AND finished_at IS NULL",
            (now, attempt_id),
        )
        if claimed.rowcount != 1:
            raise ValueError("El simulacro ya se entregó")

        review: list[AnswerFeedback] = []
        failed: list[str] = []
        correct = 0
        for qid in live:
            q = info[qid]
            if qid in given:
                idx, text = given[qid]
                fb = _insert_answer(conn, qid, idx, now, mode="mock", typed_text=text,
                                    typed_ok=typed_ok.get(qid))
            else:   # sin contestar: error del día, vuelve mañana como cualquier error del simulacro
                fb = _insert_answer(conn, qid, None, now, mode="mock", unanswered=True)
            review.append(fb)
            if fb.correct:
                correct += 1
            elif q["topic"] not in failed:
                failed.append(q["topic"])

        total = len(live)
        grade = round(max_grade * correct / total, 2)
        conn.execute(
            "UPDATE mock_attempt SET answers_json = ?, grade = ?, max_grade = ? WHERE id = ?",
            (json.dumps({str(k): (v[0] if v[1] is None else {"typed": v[1]}) for k, v in given.items()}),
             grade, max_grade, attempt_id),
        )
    return MockResult(grade, max_grade, correct, total, failed, review, late, att["predicted_grade"])


def mock_result(conn: sqlite3.Connection, attempt_id: int) -> MockResult:
    """El resultado de un simulacro ya entregado, leído de lo guardado (para un reintento de entrega).

    `LookupError` si el intento no existe; `ValueError` si todavía no terminó.
    """
    att = conn.execute("SELECT * FROM mock_attempt WHERE id = ?", (attempt_id,)).fetchone()
    if att is None:
        raise LookupError(f"intento {attempt_id} no existe")
    if att["finished_at"] is None:
        raise ValueError("El simulacro todavía no terminó")
    ordered = json.loads(att["question_ids_json"])
    marks = ",".join("?" * len(ordered))
    # Las respuestas del intento: modo simulacro, registradas en el instante de la entrega.
    rows = {
        r["question_id"]: r
        for r in conn.execute(
            "SELECT a.question_id, a.chosen_index, a.correct, a.typed_text, q.correct_index, q.explanation, "
            "q.common_error, q.source_seconds, t.name AS topic, m.ref AS youtube_id FROM answer a "
            "JOIN question q ON q.id = a.question_id JOIN topic t ON t.id = q.topic_id "
            "LEFT JOIN material m ON m.id = q.material_id "
            f"WHERE a.mode = 'mock' AND a.at = ? AND a.question_id IN ({marks}) ORDER BY a.id",
            (att["finished_at"], *ordered),
        )
    }
    review: list[AnswerFeedback] = []
    failed: list[str] = []
    for qid in ordered:
        r = rows.get(qid)
        if r is None:
            continue   # se retiró durante el intento: no se corrigió
        correct = bool(r["correct"])
        review.append(AnswerFeedback(
            correct=correct, correct_index=r["correct_index"], explanation=r["explanation"],
            common_error=r["common_error"], source_seconds=r["source_seconds"], youtube_id=r["youtube_id"],
            chosen_index=r["chosen_index"], typed_ok=correct if r["typed_text"] is not None else None,
            feedback_style="full" if not correct else "short", question_id=qid,
        ))
        if not correct and r["topic"] not in failed:
            failed.append(r["topic"])
    seconds = int(att["seconds"] or SECONDS_PER_QUESTION * len(ordered))
    max_grade = float(att["max_grade"] or DEFAULT_MAX_GRADE)
    return MockResult(
        grade=float(att["grade"] or 0), max_grade=max_grade,
        correct=sum(r.correct for r in review), total=len(review), failed_topics=failed, review=review,
        late=att["finished_at"] > att["started_at"] + seconds + GRACE_SECONDS,
        predicted_grade=att["predicted_grade"],
    )
