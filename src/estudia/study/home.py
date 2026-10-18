"""Vista de inicio: próximo examen, temas con dominio, sugerencia y estado de procesamiento."""
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from estudia.study.mastery import exam_horizon, topic_probability

TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass
class ExamCard:
    name: str
    course: str
    opens_at: int
    days_left: int
    attempts: int
    time_limit_s: int
    max_grade: float


@dataclass
class TopicRow:
    id: int
    name: str
    mastery: float
    practiced: bool
    seen: int = 0               # preguntas (verificadas, no retiradas) ya vistas
    total: int = 0              # preguntas verificadas y no retiradas del tema
    at: int | None = None       # fecha del examen al que se refiere `mastery`; None: hoy


@dataclass
class ProcessingState:
    pending: int = 0
    done: int = 0
    failed: int = 0


@dataclass
class HomeView:
    exam: ExamCard | None
    topics: list[TopicRow] = field(default_factory=list)
    suggestion: TopicRow | None = None
    processing: ProcessingState = field(default_factory=ProcessingState)


def next_exam(conn: sqlite3.Connection, now: int):
    """Examen vigente con menor `opens_at`; uno abierto ahora cuenta como próximo."""
    return conn.execute(
        "SELECT e.course_id, e.name, e.opens_at, e.attempts, e.time_limit_s, e.max_grade, "
        "c.fullname AS course FROM exam e JOIN course c ON c.id = e.course_id "
        "WHERE COALESCE(NULLIF(e.closes_at, 0), e.opens_at) > ? "
        "ORDER BY e.opens_at, e.id LIMIT 1",
        (now,),
    ).fetchone()


def _days_left(opens_at: int, now: int) -> int:
    today = datetime.fromtimestamp(now, TIMEZONE).date()
    opens = datetime.fromtimestamp(opens_at, TIMEZONE).date()
    return max(0, (opens - today).days)


def home_view(conn: sqlite3.Connection, now: int) -> HomeView:
    row = next_exam(conn, now)
    exam = None
    course_id = None
    if row:
        course_id = row["course_id"]
        exam = ExamCard(
            name=row["name"],
            course=row["course"],
            opens_at=row["opens_at"],
            days_left=_days_left(row["opens_at"], now),
            attempts=row["attempts"],
            time_limit_s=row["time_limit_s"],
            max_grade=row["max_grade"],
        )

    sql = (
        "SELECT t.id, t.name, MIN(s.num) AS num, "
        "EXISTS(SELECT 1 FROM answer a JOIN question q ON q.id = a.question_id "
        "WHERE q.topic_id = t.id AND q.retired = 0) AS practiced "
        "FROM topic t JOIN material_topic mt ON mt.topic_id = t.id "
        "JOIN material m ON m.id = mt.material_id "
        "JOIN section s ON s.id = m.section_id "
        "WHERE s.visible = 1 "
    )
    params: tuple = ()
    if course_id is not None:
        sql += "AND t.course_id = ? "
        params = (course_id,)
    sql += "GROUP BY t.id ORDER BY num, t.name"
    rows = conn.execute(sql, params).fetchall()
    question_counts = {
        r["topic_id"]: (r["seen"], r["total"])
        for r in conn.execute(
            "SELECT q.topic_id, COUNT(*) AS total, COUNT(rs.card_key) AS seen FROM question q "
            "LEFT JOIN review_state rs ON rs.card_key = 'q:' || q.id "
            "WHERE q.verified = 1 AND q.retired = 0 GROUP BY q.topic_id"
        )
    }
    # Dominio = probabilidad de saber el tema el día de su examen más próximo (o hoy, si no tiene).
    topics = []
    for r in rows:
        horizon = exam_horizon(conn, r["id"], now)
        seen, total = question_counts.get(r["id"], (0, 0))
        topics.append(TopicRow(r["id"], r["name"], topic_probability(conn, r["id"], horizon),
                               bool(r["practiced"]), seen, total, horizon if horizon != now else None))
    # `topics` ya viene ordenado por sección y nombre: min() es estable ante empates.
    suggestion = min(topics, key=lambda t: t.mastery, default=None)

    counts = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT m.status, COUNT(*) AS n FROM material m "
            "JOIN section s ON s.id = m.section_id WHERE s.visible = 1 GROUP BY m.status"
        )
    }
    processing = ProcessingState(
        pending=counts.get("pending", 0),
        done=counts.get("done", 0),
        # Un video que pide un enlace público tampoco se pudo preparar.
        failed=counts.get("failed", 0) + counts.get("needs_public_link", 0),
    )
    return HomeView(exam, topics, suggestion, processing)
