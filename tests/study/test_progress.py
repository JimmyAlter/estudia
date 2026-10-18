""""Cómo vengo": calibración y nota prevista contra la real."""
import pytest
from fastapi.testclient import TestClient

from estudia.db import connect
from estudia.server.app import create_app
from estudia.study.exams import SKIPPED_KEY
from estudia.study.progress import MIN_CALIBRATION, calibration, prediction_gap, predictions, progress_view
from test_app import BASE, PORT, FakeMoodle, make_provider


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def answers(conn, n, confidence, correct, mode="mcq", start=0):
    for i in range(n):
        conn.execute("INSERT INTO answer(question_id, chosen_index, correct, at, confidence, mode)"
                     " VALUES (1, 0, ?, ?, ?, ?)", (1 if i < correct else 0, start + i, confidence, mode))
    conn.commit()


def exam(conn, exam_id=1, name="Parcial", max_grade=10.0, opens_at=1000):
    conn.execute("INSERT INTO exam(id, course_id, name, opens_at, max_grade) VALUES (?, 1, ?, ?, ?)",
                 (exam_id, name, opens_at, max_grade))


def mock(conn, exam_id, predicted, grade, finished_at=2000, max_grade=10.0):
    conn.execute("INSERT INTO mock_attempt(exam_id, started_at, finished_at, grade, max_grade, predicted_grade)"
                 " VALUES (?, 1, ?, ?, ?, ?)", (exam_id, finished_at, grade, max_grade, predicted))


def real(conn, exam_id, grade, max_grade=10.0):
    conn.execute("INSERT INTO exam_result(exam_id, grade, max_grade, at) VALUES (?, ?, ?, 3000)",
                 (exam_id, grade, max_grade))
    conn.commit()


def test_calibration_needs_20_answers(conn):
    answers(conn, 19, 2, 10)
    cal = calibration(conn)
    assert (cal.total, cal.missing, cal.levels) == (19, 1, [])
    assert MIN_CALIBRATION == 20


def test_calibration_rates_per_level(conn):
    answers(conn, 14, 2, 10)
    answers(conn, 6, 1, 3)
    cal = calibration(conn)
    assert (cal.total, cal.missing) == (20, 0)
    assert [(l.label, l.confidence, l.answers) for l in cal.levels] == [("Seguro", 2, 14), ("Me parece", 1, 6)]
    assert cal.levels[0].hit_rate == pytest.approx(10 / 14)
    assert cal.levels[1].hit_rate == pytest.approx(0.5)


def test_calibration_ignores_pretest_and_null_confidence(conn):
    answers(conn, 20, 2, 20, mode="pretest")
    answers(conn, 20, 2, 20, mode="probe")
    answers(conn, 20, None, 20, mode="mock")
    assert calibration(conn).total == 0
    answers(conn, 20, 0, 5)
    cal = calibration(conn)
    assert cal.total == 20 and [l.label for l in cal.levels] == ["Adiviné"]


def test_predictions_against_real_grade(conn):
    exam(conn)
    mock(conn, 1, predicted=7, grade=6)
    real(conn, 1, 5)
    rows = predictions(conn)
    assert len(rows) == 1
    r = rows[0]
    assert (r.exam_id, r.exam, r.predicted, r.mock_grade, r.real, r.max_grade) == (1, "Parcial", 7, 6, 5, 10)
    assert prediction_gap(rows) == 2.0


def test_predictions_use_latest_finished_mock_and_scale(conn):
    exam(conn, max_grade=100)
    mock(conn, 1, predicted=90, grade=50, finished_at=2000, max_grade=100)
    mock(conn, 1, predicted=80, grade=60, finished_at=2500, max_grade=100)
    conn.execute("INSERT INTO mock_attempt(exam_id, started_at, predicted_grade) VALUES (1, 3000, 10)")  # sin terminar
    real(conn, 1, 70, 100)
    rows = predictions(conn)
    assert [(r.predicted, r.mock_grade) for r in rows] == [(80, 60)]
    assert prediction_gap(rows) == 1.0  # |80 - 70| / 100 * 10


def test_same_scale_mock_prediction_is_unchanged(conn):
    """Simulacro y examen en la misma escala (de 10): la prevista y la nota pasan tal cual."""
    exam(conn)
    mock(conn, 1, predicted=7.5, grade=6.25, max_grade=10.0)
    real(conn, 1, 5, 10.0)
    r, = predictions(conn)
    assert (r.predicted, r.mock_grade, r.real, r.max_grade) == (7.5, 6.25, 5.0, 10.0)
    assert prediction_gap([r]) == 2.5


def test_prediction_converted_when_mock_scale_differs(conn):
    """Con el formato de la cátedra el simulacro puede ser sobre 100 y la nota real sobre 10:
    la prevista y la nota del simulacro se llevan a la escala de la real antes de comparar."""
    exam(conn)
    mock(conn, 1, predicted=80, grade=60, max_grade=100.0)
    real(conn, 1, 7, 10.0)
    r, = predictions(conn)
    assert (r.predicted, r.mock_grade, r.real, r.max_grade) == (8.0, 6.0, 7.0, 10.0)
    assert prediction_gap([r]) == 1.0


def test_gap_ignores_rows_without_prediction(conn):
    exam(conn, 1)
    exam(conn, 2, name="Final")
    mock(conn, 1, predicted=None, grade=6)
    mock(conn, 2, predicted=8, grade=6)
    real(conn, 1, 4)
    real(conn, 2, 6)
    rows = predictions(conn)
    assert len(rows) == 2 and prediction_gap(rows) == 2.0
    assert prediction_gap([r for r in rows if r.predicted is None]) is None


def test_no_predictions_without_results(conn):
    exam(conn)
    mock(conn, 1, predicted=7, grade=6)
    conn.commit()
    view = progress_view(conn)
    assert view.predictions == [] and view.gap is None
    assert view.calibration.total == 0


def test_skipped_result_is_not_a_grade(conn):
    exam(conn)
    mock(conn, 1, predicted=7, grade=6)
    real(conn, 1, 5)
    conn.execute("INSERT INTO kv(key, value) VALUES (?, '[1]')", (SKIPPED_KEY,))
    conn.commit()
    assert predictions(conn) == []


def test_progress_route(tmp_path):
    db_path = tmp_path / "estudia.db"
    app = create_app(db_path=db_path, moodle_client_factory=lambda url, token: FakeMoodle(url, token),
                     ai_provider_factory=make_provider, open_browser=lambda url: None,
                     web_dist=tmp_path / "no-existe", port=PORT)
    c = TestClient(app, base_url=BASE)
    body = c.get("/api/progress").json()
    assert body == {"calibration": {"total": 0, "missing": 20, "levels": []}, "predictions": [], "gap": None}
