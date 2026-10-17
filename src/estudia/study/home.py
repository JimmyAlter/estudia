"""Vista de inicio: próximo examen, temas con dominio, sugerencia y estado de procesamiento."""
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from estudia.study.mastery import topic_mastery

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
    topics = [
        TopicRow(r["id"], r["name"], topic_mastery(conn, r["id"], now), bool(r["practiced"]))
        for r in rows
    ]
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
        failed=counts.get("failed", 0),
    )
    return HomeView(exam, topics, suggestion, processing)
