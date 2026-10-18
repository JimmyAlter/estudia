from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia import reminders, tray
from estudia.db import connect
from estudia.study.schedule import add_slot

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def ts(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis Matemático', 'AM')")
    c.execute("INSERT INTO topic(id, course_id, name) VALUES (1, 10, 'Funciones cuadráticas')")
    c.commit()
    return c


def add_exam(conn, at, name="Primer parcial de Química"):
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source) VALUES (10, ?, ?, 'manual')", (name, at))
    conn.commit()


def test_exam_tomorrow_once(conn):
    add_exam(conn, ts(2026, 10, 20, 9))  # martes
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 17, 59), set()) == []
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 19, 18, 0), set())
    assert r.text == "Mañana: Primer parcial de Química, 9:00"
    assert "exam:" in r.key and r.key.endswith(":2026-10-20")
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 20), {r.key}) == []
    assert reminders.due_reminders(conn, ts(2026, 10, 20, 8, 59), set()) == []  # ya es el mismo día


def test_class_15_min_before(conn):
    add_slot(conn, 10, 0, 9 * 60, 11 * 60, "aula 302")  # lunes
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 8, 45), set()) == []  # (inicio - 15 min, inicio]
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 19, 8, 45) + 1, set())
    assert r.text == "En 15 minutos: Análisis Matemático, aula 302"
    assert r.key.startswith("class:") and r.key.endswith(":2026-10-19")
    (r2,) = reminders.due_reminders(conn, ts(2026, 10, 19, 8, 59), set())
    assert r2.text.startswith("En 1 minuto: ")
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 9, 1), set()) == []


def test_class_without_place_has_no_trailing_comma(conn):
    add_slot(conn, 10, 0, 9 * 60, 11 * 60, None)
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 19, 8, 50), set())
    assert r.text == "En 10 minutos: Análisis Matemático"


def test_session_start(conn):
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, starts_at, minutes) "
                 "VALUES (5, 10, 1, ?, 25)", (ts(2026, 10, 19, 19),))
    conn.commit()
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 18, 59), set()) == []
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 19, 19, 3), set())
    assert (r.key, r.text) == ("session:5", "Hora de estudiar: Funciones cuadráticas, 25 minutos")
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 19, 10), set()) == []  # más de 10 min tarde


def test_disabled_returns_nothing(conn):
    add_exam(conn, ts(2026, 10, 20, 9))
    conn.execute("INSERT INTO kv(key, value) VALUES ('reminders_enabled', '0')")
    conn.commit()
    assert not reminders.reminders_enabled(conn)
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 19), set()) == []


def test_no_repeat_after_restart(conn):
    add_exam(conn, ts(2026, 10, 20, 9))
    now = ts(2026, 10, 19, 19)
    calls = []
    assert reminders.check_once(conn, now, lambda t, x: calls.append(x) or True) == 1
    assert reminders.check_once(conn, now + 60, lambda t, x: calls.append(x) or True) == 0
    assert len(calls) == 1


def test_failed_send_is_retried(conn):
    add_exam(conn, ts(2026, 10, 20, 9))
    now = ts(2026, 10, 19, 19)
    assert reminders.check_once(conn, now, lambda t, x: False) == 0
    assert reminders.check_once(conn, now + 60, lambda t, x: True) == 1


def test_sent_is_pruned_to_five_days(conn):
    now = ts(2026, 10, 19, 19)
    reminders.mark_sent(conn, ["old"], now - 6 * 86400)
    reminders.mark_sent(conn, ["new"], now)
    assert set(reminders.load_sent(conn)) == {"new"}


def test_send_falls_back_to_notify_send(monkeypatch):
    monkeypatch.setattr(tray, "notify", lambda t, x: False)
    monkeypatch.setattr(reminders.sys, "platform", "linux")
    monkeypatch.setattr(reminders.shutil, "which", lambda n: "/usr/bin/notify-send")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(reminders.subprocess, "run", fake_run)
    assert reminders.send("Estudia", "hola")
    assert seen["cmd"] == ["/usr/bin/notify-send", "-a", "Estudia", "Estudia", "hola"]
    assert seen["timeout"] == 5
    monkeypatch.setattr(reminders.shutil, "which", lambda n: None)
    assert reminders.send("Estudia", "hola") is False


def test_tray_notify_order_and_safety(monkeypatch):
    assert tray.notify("T", "x") is False
    calls = []

    class Icon:
        def notify(self, message, title=None):
            calls.append((message, title))

    monkeypatch.setattr(tray, "_icon", Icon())
    assert tray.notify("Título", "texto") is True
    assert calls == [("texto", "Título")]

    class Broken:
        def notify(self, *a):
            raise RuntimeError

    monkeypatch.setattr(tray, "_icon", Broken())
    assert tray.notify("T", "x") is False


def test_exam_tomorrow_checked_at_2359(conn):
    add_exam(conn, ts(2026, 10, 20, 9))
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 19, 23, 59), set())
    assert r.text == "Mañana: Primer parcial de Química, 9:00"


def test_thread_retries_connect_until_it_works(conn, monkeypatch):
    import sqlite3
    import threading

    monkeypatch.setattr(reminders, "CHECK_EVERY_S", 0)
    stop = threading.Event()
    attempts = []

    def flaky_connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return conn

    checks = []
    monkeypatch.setattr(reminders, "check_once", lambda c: checks.append(c) or stop.set())
    reminders.run_reminders(stop, flaky_connect)
    assert len(attempts) == 3
    assert checks == [conn]


def test_thread_gives_up_connecting_when_stopped(monkeypatch):
    import threading

    stop = threading.Event()

    def never_connects():
        stop.set()
        raise OSError("sin disco")

    reminders.run_reminders(stop, never_connects)  # no levanta y termina


def test_run_reminders_needs_a_connect_from_the_caller():
    """Sin `connect` no hay base por defecto que abrir: la elige quien lanza el hilo."""
    import inspect

    param = inspect.signature(reminders.run_reminders).parameters["connect"]
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize("kind, topics, text", [
    ("mixed_only", [1, 2], "Hora de estudiar, 50 minutos. Repaso mezclado: Funciones cuadráticas y Límites"),
    ("mixed_focus", [1, 2], "Hora de estudiar, 50 minutos. Foco: Funciones cuadráticas, con repaso de Límites"),
])
def test_mixed_session_uses_the_planner_label(conn, kind, topics, text):
    import json

    conn.execute("INSERT INTO topic(id, course_id, name) VALUES (2, 10, 'Límites')")
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, starts_at, minutes, kind, topics_json) "
                 "VALUES (6, 10, 1, ?, 50, ?, ?)", (ts(2026, 10, 19, 19), kind, json.dumps(topics)))
    conn.commit()
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 19, 19, 3), set())
    assert (r.key, r.text) == ("session:6", text)


def add_enrollment(conn, opens, closes, name="Inscripción a finales"):
    cur = conn.execute(
        "INSERT INTO exam(course_id, name, opens_at, closes_at, source, kind) "
        "VALUES (10, ?, ?, ?, 'manual', 'inscripcion')", (name, opens, closes))
    conn.commit()
    return cur.lastrowid


def test_enrollment_reminder_three_days_before_close(conn):
    eid = add_enrollment(conn, ts(2026, 10, 1), ts(2026, 10, 23, 18, 30))
    assert reminders.due_reminders(conn, ts(2026, 10, 20, 8, 59), set()) == []
    (r,) = reminders.due_reminders(conn, ts(2026, 10, 20, 9, 0), set())
    assert r.key == f"enroll:{eid}:{ts(2026, 10, 23, 18, 30)}"
    assert r.text == "Cierra la inscripción: Inscripción a finales, el 23/10 a las 18:30"
    sent = []
    assert reminders.check_once(conn, ts(2026, 10, 20, 9, 0), lambda t, x: sent.append(x) or True) == 1
    assert reminders.check_once(conn, ts(2026, 10, 20, 9, 1), lambda t, x: sent.append(x) or True) == 0
    assert f"enroll:{eid}:{ts(2026, 10, 23, 18, 30)}" in reminders.load_sent(conn)
    # Todavía dentro de la ventana, cuatro días después del primer aviso no se repite.
    assert reminders.due_reminders(conn, ts(2026, 10, 23, 18, 0), set(reminders.load_sent(conn))) == []
    assert reminders.due_reminders(conn, ts(2026, 10, 23, 18, 30), set()) == []  # ya cerró


def test_enrollment_has_no_exam_eve_reminder(conn):
    add_enrollment(conn, ts(2026, 10, 20, 9), ts(2026, 10, 30))
    assert reminders.due_reminders(conn, ts(2026, 10, 19, 18, 0), set()) == []


def test_enrollment_reminder_rearms_when_close_date_moves(conn):
    eid = add_enrollment(conn, ts(2026, 10, 1), ts(2026, 10, 23, 18, 30))
    now = ts(2026, 10, 22, 10)
    (first,) = reminders.due_reminders(conn, now, set())
    conn.execute("UPDATE exam SET closes_at = ? WHERE id = ?", (ts(2026, 10, 25, 18, 30), eid))
    conn.commit()
    (second,) = reminders.due_reminders(conn, now, {first.key})
    assert second.key != first.key and "el 25/10" in second.text
