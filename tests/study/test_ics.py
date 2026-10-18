import pytest

from estudia.db import connect
from estudia.study.ics import build_ics
from estudia.study.schedule import add_slot

NOW = 1_800_000_000  # 2027-01-15 08:00 UTC (viernes, 05:00 en Buenos Aires)
DAY = 86400


def _unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")[:-1]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Álgebra, Cálculo; I', 'AC')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'm', 'Matrices')")
    c.commit()
    return c


def test_has_calendar_envelope(conn):
    out = build_ics(conn, NOW)
    assert out.startswith("BEGIN:VCALENDAR\r\n") and out.endswith("END:VCALENDAR\r\n")
    lines = _unfold(out)
    assert "VERSION:2.0" in lines and "PRODID:-//Estudia//ES" in lines
    assert "TZOFFSETFROM:-0300" in lines and "TZOFFSETTO:-0300" in lines and "TZNAME:-03" in lines
    assert "\n" not in out.replace("\r\n", "")


def test_class_is_weekly_rrule(conn):
    add_slot(conn, 10, 0, 19 * 60, 21 * 60, "Aula 3, Medrano")  # lunes
    lines = _unfold(build_ics(conn, NOW))
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in lines
    # el viernes 15/01/2027: el primer lunes es el 18
    assert "DTSTART;TZID=America/Argentina/Buenos_Aires:20270118T190000" in lines
    assert "DTEND;TZID=America/Argentina/Buenos_Aires:20270118T210000" in lines
    assert "SUMMARY:Álgebra\\, Cálculo\\; I (clase)" in lines
    assert "LOCATION:Aula 3\\, Medrano" in lines


def test_class_today_starts_today(conn):
    add_slot(conn, 10, 4, 600, 660, None)  # viernes
    assert "DTSTART;TZID=America/Argentina/Buenos_Aires:20270115T100000" in _unfold(build_ics(conn, NOW))


def test_escaping_and_folding(conn):
    name = "Parcial, \\ñandú; " + "ü" * 60 + "\nlínea"
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source) VALUES (10, ?, ?, 'manual')", (name, NOW + DAY))
    conn.commit()
    out = build_ics(conn, NOW)
    for raw in out.split("\r\n"):
        assert len(raw.encode("utf-8")) <= 75
    assert out.encode("utf-8").decode("utf-8")  # sin caracteres partidos
    summary = next(x for x in _unfold(out) if x.startswith("SUMMARY:Examen"))
    assert "Parcial\\, \\\\ñandú\\; " in summary and "\\nlínea" in summary
    assert "ü" * 60 in summary


def test_stable_uids(conn):
    add_slot(conn, 10, 0, 600, 660, None)
    conn.execute("INSERT INTO exam(id, course_id, name, opens_at, source) VALUES (7, 10, 'P1', ?, 'manual')", (NOW + DAY,))
    conn.execute("INSERT INTO deadline(id, course_id, kind, ext_id, name, due_at) VALUES (3, 10, 'assign', 1, 'TP', ?)", (NOW + 2 * DAY,))
    conn.execute("INSERT INTO deadline(id, course_id, kind, ext_id, name, due_at) VALUES (4, 10, 'event', 2, 'Charla', ?)", (NOW + 3 * DAY,))
    conn.execute("INSERT INTO deadline(id, course_id, kind, ext_id, name, due_at) VALUES (5, 10, 'assign', 9, 'Lejos', ?)", (NOW + 90 * DAY,))
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes) VALUES (9, 10, 1, 7, ?, 50)", (NOW + DAY,))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'd', 'Determinantes')")
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, exam_id, starts_at, minutes, kind, topics_json) "
                 "VALUES (11, 10, 2, 7, ?, 25, 'mixed_only', '[2, 1]')", (NOW + DAY + 3600,))
    conn.commit()
    out = build_ics(conn, NOW)
    uids = [x for x in _unfold(out) if x.startswith("UID:")]
    assert sorted(uids) == sorted(f"UID:estudia-{k}@estudia.local" for k in
                                  ("class-1", "exam-7", "assign-3", "event-4", "session-9", "session-11"))
    assert build_ics(conn, NOW + 5) and [x for x in _unfold(build_ics(conn, NOW + 5)) if x.startswith("UID:")] == uids
    lines = _unfold(out)
    assert any(x.startswith("SUMMARY:Entrega: TP") for x in lines)
    assert any(x.startswith("SUMMARY:Evento: Charla") for x in lines)
    # la sesión dice qué se hace: foco en un tema o repaso mezclado
    assert "SUMMARY:Estudiar · Foco: Matrices · Álgebra\\, Cálculo\\; I" in lines
    assert "SUMMARY:Estudiar · Repaso mezclado: Determinantes y Matrices · Álgebra\\, Cálculo\\; I" in lines
    assert f"DTSTART:{'20270116T080000Z'}" in lines and "DTEND:20270116T100000Z" in lines  # examen de 2 h


def test_parses_with_icalendar_lib(conn):
    icalendar = pytest.importorskip("icalendar")
    add_slot(conn, 10, 2, 600, 720, "Aula; 1")
    conn.execute("INSERT INTO exam(id, course_id, name, opens_at, source) VALUES (7, 10, 'Ñ, P1', ?, 'manual')", (NOW + DAY,))
    conn.commit()
    cal = icalendar.Calendar.from_ical(build_ics(conn, NOW).encode("utf-8"))
    events = list(cal.walk("VEVENT"))
    assert len(events) == 2
    cls = next(e for e in events if str(e["UID"]).startswith("estudia-class"))
    assert str(cls["SUMMARY"]) == "Álgebra, Cálculo; I (clase)" and str(cls["LOCATION"]) == "Aula; 1"
    assert cls["RRULE"]["FREQ"] == ["WEEKLY"]
    assert cls["DTSTART"].dt.utcoffset().total_seconds() == -3 * 3600
    exam = next(e for e in events if str(e["UID"]).startswith("estudia-exam"))
    assert str(exam["SUMMARY"]) == "Examen: Ñ, P1 · Álgebra, Cálculo; I"


def test_control_characters_are_stripped(conn):
    name = "Parcial\x00 uno\x07\x1b\x7f\tdos\nlínea"
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source) VALUES (10, ?, ?, 'manual')", (name, NOW + DAY))
    conn.commit()
    out = build_ics(conn, NOW)
    assert not any(ord(ch) < 0x20 and ch not in "\r\n" or ord(ch) == 0x7f for ch in out)
    summary = next(x for x in _unfold(out) if x.startswith("SUMMARY:Examen"))
    assert "Parcial uno dos\\nlínea" in summary


def test_tab_becomes_a_space(conn):
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source) VALUES (10, ?, ?, 'manual')",
                 ("Parcial\tuno", NOW + DAY))
    conn.commit()
    summary = next(x for x in _unfold(build_ics(conn, NOW)) if x.startswith("SUMMARY:Examen"))
    assert "Parcial uno" in summary
