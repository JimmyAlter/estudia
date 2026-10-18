""""Cómo vengo": calibración (cuando decís Seguro, cuánto acertás) y nota prevista contra la real."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from estudia.study.exams import _skipped

MIN_CALIBRATION = 20
LABELS = ((2, "Seguro"), (1, "Me parece"), (0, "Adiviné"))


@dataclass
class CalibrationLevel:
    confidence: int
    label: str
    answers: int
    hit_rate: float


@dataclass
class Calibration:
    total: int
    missing: int
    levels: list[CalibrationLevel]


@dataclass
class PredictionRow:
    exam_id: int
    exam: str
    predicted: float | None
    mock_grade: float
    real: float
    max_grade: float


@dataclass
class ProgressView:
    calibration: Calibration
    predictions: list[PredictionRow]
    gap: float | None


def calibration(conn: sqlite3.Connection) -> Calibration:
    # El pretest y el sondeo no cuentan; los simulacros no tienen confianza (NULL) y quedan fuera solos.
    rows = conn.execute(
        "SELECT confidence, COUNT(*) AS n, SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS ok"
        " FROM answer WHERE confidence IS NOT NULL AND confidence IN (0, 1, 2)"
        " AND (mode IS NULL OR mode NOT IN ('pretest', 'probe')) GROUP BY confidence"
    ).fetchall()
    stats = {r["confidence"]: (r["n"], r["ok"] or 0) for r in rows}
    total = sum(n for n, _ in stats.values())
    if total < MIN_CALIBRATION:
        return Calibration(total, MIN_CALIBRATION - total, [])
    levels = [
        CalibrationLevel(conf, label, stats[conf][0], stats[conf][1] / stats[conf][0])
        for conf, label in LABELS if conf in stats
    ]
    return Calibration(total, 0, levels)


def predictions(conn: sqlite3.Connection) -> list[PredictionRow]:
    """El último simulacro terminado de cada examen que tiene nota real."""
    skipped = _skipped(conn)
    rows = conn.execute(
        "SELECT e.id AS exam_id, e.name, r.grade AS real, r.max_grade AS real_max, e.max_grade AS exam_max,"
        " m.predicted_grade, m.grade AS mock_grade, m.max_grade AS mock_max"
        " FROM exam_result r JOIN exam e ON e.id = r.exam_id"
        " JOIN mock_attempt m ON m.id = (SELECT id FROM mock_attempt WHERE exam_id = r.exam_id"
        "   AND finished_at IS NOT NULL AND grade IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1)"
        " WHERE r.grade IS NOT NULL ORDER BY e.opens_at, e.id"
    ).fetchall()
    out = []
    for r in rows:
        if r["exam_id"] in skipped:
            continue
        scale = r["real_max"] or r["exam_max"] or r["mock_max"] or 10.0
        out.append(PredictionRow(r["exam_id"], r["name"] or "", r["predicted_grade"],
                                 float(r["mock_grade"]), float(r["real"]), float(scale)))
    return out


def prediction_gap(rows: list[PredictionRow]) -> float | None:
    """Promedio de |prevista - real| en escala de 10, de los exámenes donde se dijo una nota prevista."""
    gaps = [abs(r.predicted - r.real) / r.max_grade * 10 for r in rows if r.predicted is not None and r.max_grade]
    return round(sum(gaps) / len(gaps), 1) if gaps else None


def progress_view(conn: sqlite3.Connection) -> ProgressView:
    rows = predictions(conn)
    return ProgressView(calibration(conn), rows, prediction_gap(rows))
