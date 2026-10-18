from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study.schedule import (
    add_slot, delete_slot, list_slots, occurrences, suggest_from_course_name, suggestions, update_slot,
)

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (11, 's', 'AM1 - 8.30 HS', 'ED')")
    c.commit()
    return c


def test_suggest_from_frba_name():
    s = suggest_from_course_name("AM1 - 8.30 HS")
    assert (s.start_min, s.end_min) == (510, 630)


@pytest.mark.parametrize("name,span", [
    ("Comisión 2 - 14:00 a 16:00 hs", (840, 960)), ("Turno Noche (19 a 23 hs)", (1140, 1380)),
    ("08:30-11:00", (510, 660)), ("08:30-10:30", (510, 630)), ("20 a 24 hs", (1200, 1440)),
    ("AM1 - 8.30 HS", (510, 630)), ("Aula 7 - 8 hs", (480, 600)),
    ("Comisión 10 - 19 hs", (1140, 1260)), ("Grupo 12 - 18.30 hs", (1110, 1230)),
    ("14.00 - 16.00 hs", (840, 960)), ("8 a 12 hs", (480, 720)), ("19 a 18 hs", (1140, 1260)), ("2027 - 2028", None),
])
def test_suggest_ranges(name, span):
    s = suggest_from_course_name(name)
    assert ((s.start_min, s.end_min) if s else None) == span


@pytest.mark.parametrize("name,start", [
    ("8:30 hs", 510), ("18 HS", 1080), ("a las 19", 1140), ("8.30HS", 510),
    ("08:30", 510), ("8 hs", 480), ("19hs", 1140), ("23 hs", 1380),
    ("AM1 - 2027", None), ("AM1", None), ("Sin hora", None), ("5 hs", None),
    ("Física 2027 - AM1 19hs", 1140), ("Versión 1.20", None),
])
def test_suggest_variants(name, start):
    s = suggest_from_course_name(name)
    assert (s.start_min if s else None) == start


def test_slot_validation(conn):
    for args in [(10, 7, 60, 120), (10, -1, 60, 120), (10, 0, 120, 60), (10, 0, 60, 60),
                 (10, 0, -1, 60), (10, 0, 60, 1441), (99, 0, 60, 120)]:
        with pytest.raises(ValueError):
            add_slot(conn, *args, None)
    sid = add_slot(conn, 10, 6, 0, 1440, "Aula 1")
    with pytest.raises(ValueError):
        update_slot(conn, sid, 10, 0, 100, 50, None)
    with pytest.raises(ValueError):
        update_slot(conn, sid, 99, 0, 50, 100, None)
    update_slot(conn, sid, 10, 2, 600, 700, None)
    s = list_slots(conn)[0]
    assert (s.course, s.weekday, s.start_min, s.end_min, s.place) == ("Análisis", 2, 600, 700, None)
    delete_slot(conn, sid)
    assert list_slots(conn) == []


def test_occurrences_week(conn):
    add_slot(conn, 10, 0, 510, 630, "Aula 3")  # lunes 8:30
    start = int(datetime(2026, 10, 17, 0, 0, tzinfo=TZ).timestamp())  # sábado
    occ = occurrences(conn, start, start + 7 * 86400)
    assert len(occ) == 1
    o = occ[0]
    assert o.starts_at == int(datetime(2026, 10, 19, 8, 30, tzinfo=TZ).timestamp())
    assert o.ends_at - o.starts_at == 7200
    assert (o.course, o.place) == ("Análisis", "Aula 3")
    # una clase de las 23:00 locales cae en otro día en UTC, pero sigue siendo del día local
    add_slot(conn, 10, 0, 1380, 1440, None)
    occ = occurrences(conn, start, start + 7 * 86400)
    assert [datetime.fromtimestamp(o.starts_at, TZ).weekday() for o in occ] == [0, 0]


def test_suggestions_skip_courses_with_slots(conn):
    assert [s["course_id"] for s in suggestions(conn)] == [11]
    add_slot(conn, 11, 1, 600, 700, None)
    assert suggestions(conn) == []


def test_duplicate_slot_rejected_but_overlap_allowed(conn):
    sid = add_slot(conn, 10, 0, 510, 630, None)
    with pytest.raises(ValueError):
        add_slot(conn, 10, 0, 510, 630, "otra aula")
    other = add_slot(conn, 10, 0, 600, 700, None)  # superposición parcial: permitida
    with pytest.raises(ValueError):
        update_slot(conn, other, 10, 0, 510, 630, None)
    update_slot(conn, sid, 10, 0, 510, 630, "Aula 2")  # sobre sí mismo no es duplicado


def test_occurrences_window_edges(conn):
    add_slot(conn, 10, 0, 510, 630, None)
    mon = datetime(2026, 10, 19, 0, 0, tzinfo=TZ)
    t = lambda h, m=0: int(mon.replace(hour=h, minute=m).timestamp())  # noqa: E731
    assert occurrences(conn, t(9), t(12)) == []  # la ventana arranca con la clase ya empezada
    assert len(occurrences(conn, t(8, 30), t(9))) == 1
    assert occurrences(conn, t(12), t(8)) == []  # inicio > fin
    assert len(occurrences(conn, t(0), t(0) + 10 * 7 * 86400)) == 10


def test_dismissed_suggestion_does_not_come_back(conn):
    from estudia.study.schedule import dismiss_suggestion

    assert [s["course_id"] for s in suggestions(conn)] == [11]
    dismiss_suggestion(conn, 11)
    dismiss_suggestion(conn, 11)   # repetirlo no rompe nada
    assert suggestions(conn) == []
