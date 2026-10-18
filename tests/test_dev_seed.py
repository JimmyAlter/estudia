"""La app sembrada (`scripts/dev_seed.py`) muestra todo lo de la etapa 3a: es lo que mira el crítico."""
import importlib.util
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.generation.pipeline import BUDGET, STOP_KEY
from estudia.reminders import due_reminders
from estudia.study.home import home_view
from estudia.study.mastery import topic_probability
from estudia.study.planner import RETAKE_THRESHOLD, plan_health, retake_topics
from estudia.study.progress import progress_view

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_seed.py"


@pytest.fixture(scope="module")
def dev_seed():
    spec = importlib.util.spec_from_file_location("dev_seed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded(dev_seed, tmp_path):
    db = tmp_path / "estudia.db"
    dev_seed.seed(db, with_exam=True)
    conn = connect(db)
    yield conn, int(time.time())
    conn.close()


def test_dev_seed_acepta_el_puerto(dev_seed, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(dev_seed.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    monkeypatch.setattr("sys.argv", ["dev_seed.py", "--port", "8790", "--step", "facultad", "--no-exam"])
    monkeypatch.setattr(dev_seed.uvicorn, "run", lambda app, host, port, log_level: seen.update(port=port))
    monkeypatch.setattr(dev_seed.keyring, "set_keyring", lambda backend: None)
    dev_seed.main()
    assert seen == {"port": 8790}


def test_la_app_sembrada_muestra_todo_lo_de_la_etapa_3a(seeded):
    conn, now = seeded
    home = home_view(conn, now)
    # Una clase recién procesada con pretest, y un curso con 10 preguntas mientras falta una clase.
    assert home.pretest is not None
    assert home.processing.pending > 0
    assert home.ready is not None and home.ready.questions == 10
    assert conn.execute("SELECT value FROM kv WHERE key = ?", (STOP_KEY,)).fetchone()[0] == BUDGET

    # 25 o más respuestas con confianza (alcanza para calibrar) y un simulacro con predicción y nota real.
    progress = progress_view(conn)
    assert progress.calibration.total >= 25 and progress.calibration.missing == 0
    assert any(p.predicted is not None for p in progress.predictions)

    # Una inscripción que cierra en 2 días, dentro del aviso de los 3 días.
    closes = conn.execute("SELECT closes_at FROM exam WHERE kind = 'inscripcion'").fetchone()[0]
    assert datetime.fromtimestamp(closes, TZ).date() - datetime.fromtimestamp(now, TZ).date() == timedelta(days=2)
    assert any(r.key.startswith("enroll:") for r in due_reminders(conn, now, set()))

    # Un recuperatorio con un tema firme (P ≥ 0,7) que no se programa.
    retake = conn.execute("SELECT id, opens_at, topics_json FROM exam WHERE kind = 'recuperatorio'").fetchone()
    firm = [t for t in json.loads(retake["topics_json"])
            if topic_probability(conn, t, retake["opens_at"]) >= RETAKE_THRESHOLD]
    assert firm and not set(firm) & set(retake_topics(conn, retake["id"]))

    # Un final a 20 días, con su nota de menos de 4 semanas.
    final = next(h for h in plan_health(conn, now) if h.kind == "final")
    assert (datetime.fromtimestamp(final.at, TZ).date() - datetime.fromtimestamp(now, TZ).date()).days == 20
    assert final.note

