import copy
import json
from pathlib import Path

import pytest

from estudia.db import connect
from estudia.moodle.client import MoodleError, TokenInvalid
from estudia.moodle.sync import _sync_deadlines, sync_all

FIXTURES = Path(__file__).parent.parent / "fixtures" / "moodle"
ASSIGN = json.loads((FIXTURES / "mod_assign_get_assignments_sample.json").read_text("utf-8"))
EVENTS = json.loads(
    (FIXTURES / "core_calendar_get_action_events_by_timesort_sample.json").read_text("utf-8"))
SITE_URL = "https://aulasvirtuales.frba.utn.edu.ar"
NOW = 1806000000


class DeadlineClient:
    site_url = SITE_URL

    def __init__(self, assign=ASSIGN, events=EVENTS, assign_error=None, events_error=None):
        self.assign, self.events = assign, events
        self.assign_error, self.events_error = assign_error, events_error
        self.calls: list[tuple[str, dict]] = []

    def call(self, fn, **params):
        self.calls.append((fn, params))
        if fn == "mod_assign_get_assignments":
            if self.assign_error:
                raise self.assign_error
            return self.assign
        if fn == "core_calendar_get_action_events_by_timesort":
            if self.events_error:
                raise self.events_error
            return self.events
        raise AssertionError(fn)


@pytest.fixture
def conn():
    return connect(Path(":memory:"))


def rows(conn, kind):
    return {r["ext_id"]: r for r in conn.execute("SELECT * FROM deadline WHERE kind=?", (kind,))}


def test_assignments_stored(conn):
    cliente = DeadlineClient()
    _sync_deadlines(conn, cliente, [101], NOW)
    a = rows(conn, "assign")
    assert set(a) == {501, 502}  # 503 sin fechas
    assert (a[501]["due_at"], a[501]["course_id"], a[501]["name"]) == (
        1807000000, 101, "TP 1: Rectas y planos")
    assert a[501]["url"] == f"{SITE_URL}/mod/assign/view.php?id=91001"
    assert a[502]["due_at"] == 1808000000  # cae a cutoffdate
    fn, params = cliente.calls[0]
    assert (fn, params) == ("mod_assign_get_assignments", {"courseids": [101]})


def test_assignment_without_dates_ignored(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    assert 503 not in rows(conn, "assign")


def test_events_skip_quiz(conn):
    cliente = DeadlineClient()
    n = _sync_deadlines(conn, cliente, [101], NOW)
    e = rows(conn, "event")
    assert set(e) == {7003, 7004}  # sin quiz (7001) ni assign (7002)
    assert e[7003]["due_at"] == 1806500000
    assert e[7003]["url"] == f"{SITE_URL}/mod/forum/view.php?id=91050"
    assert e[7004]["url"] is None  # URL de otro sitio
    assert n == 4
    fn, params = cliente.calls[1]
    assert fn == "core_calendar_get_action_events_by_timesort"
    assert params == {"timesortfrom": NOW - 7 * 86400, "timesortto": NOW + 180 * 86400,
                      "limitnum": 50}


def test_upsert_updates_due_date(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    assign = copy.deepcopy(ASSIGN)
    assign["courses"][0]["assignments"][0]["duedate"] = 1807999999
    _sync_deadlines(conn, DeadlineClient(assign=assign), [101], NOW)
    assert conn.execute("SELECT COUNT(*) FROM deadline WHERE kind='assign'").fetchone()[0] == 2
    assert rows(conn, "assign")[501]["due_at"] == 1807999999


def test_removed_assignment_and_event_are_deleted(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    # una fila fuera de la ventana de eventos no se toca
    conn.execute("INSERT INTO deadline(course_id, kind, ext_id, name, due_at) "
                 "VALUES(101, 'event', 9999, 'viejo', ?)", (NOW - 30 * 86400,))
    assign = copy.deepcopy(ASSIGN)
    assign["courses"][0]["assignments"].pop(0)
    events = {"events": EVENTS["events"][3:]}
    _sync_deadlines(conn, DeadlineClient(assign=assign, events=events), [101], NOW)
    assert set(rows(conn, "assign")) == {502}
    assert set(rows(conn, "event")) == {7004, 9999}


def test_unavailable_source_is_skipped_and_keeps_stored(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    for codigo in ("invalidparameter", "accessexception"):
        error = TokenInvalid(codigo) if codigo == "accessexception" else MoodleError(codigo)
        cliente = DeadlineClient(assign_error=error, events_error=MoodleError(codigo))
        assert _sync_deadlines(conn, cliente, [101], NOW) == 0
    assert set(rows(conn, "assign")) == {501, 502}
    assert set(rows(conn, "event")) == {7003, 7004}


def test_events_are_paged(conn):
    class Paged(DeadlineClient):
        def call(self, fn, **params):
            if fn != "core_calendar_get_action_events_by_timesort":
                return super().call(fn, **params)
            self.calls.append((fn, params))
            ini = params.get("aftereventid", 0)
            return {"events": [
                {"id": i, "name": f"e{i}", "modulename": "forum", "timesort": NOW + i,
                 "course": {"id": 101}} for i in range(ini + 1, min(ini + 50, 120) + 1)]}

    cliente = Paged()
    _sync_deadlines(conn, cliente, [101], NOW)
    assert len(rows(conn, "event")) == 120
    paginas = [p for f, p in cliente.calls if f.startswith("core_calendar")]
    assert [p.get("aftereventid") for p in paginas] == [None, 50, 100]


def test_failure_in_deadlines_rolls_back_whole_sync(conn):
    from test_sync import FakeClient

    class Roto(FakeClient):
        def call(self, fn, **params):
            if fn == "mod_assign_get_assignments":
                return ASSIGN
            if fn == "core_calendar_get_action_events_by_timesort":
                raise MoodleError("HTTP 500")
            return super().call(fn, **params)

    with pytest.raises(MoodleError):
        sync_all(conn, Roto(), "utn-frba")
    for tabla in ("deadline", "course", "section", "material", "exam", "site"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM kv WHERE key='last_sync_at'").fetchone() is None


def test_invalid_token_is_not_skipped(conn):
    with pytest.raises(TokenInvalid):
        _sync_deadlines(conn, DeadlineClient(assign_error=TokenInvalid("invalidtoken")),
                        [101], NOW)


def test_assignments_not_deleted_on_empty_response_with_warning(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    vacio = {"courses": [], "warnings": [{"item": "course", "warningcode": "2"}]}
    _sync_deadlines(conn, DeadlineClient(assign=vacio), [101], NOW)
    assert set(rows(conn, "assign")) == {501, 502}


def test_assignments_not_deleted_with_warnings_even_if_course_present(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    parcial = copy.deepcopy(ASSIGN)
    parcial["courses"][0]["assignments"] = []
    parcial["warnings"] = [{"warningcode": "1"}]
    _sync_deadlines(conn, DeadlineClient(assign=parcial), [101], NOW)
    assert set(rows(conn, "assign")) == {501, 502}


def test_assignments_only_deleted_for_courses_in_response(conn):
    conn.execute("INSERT INTO deadline(course_id, kind, ext_id, name, due_at) "
                 "VALUES(999, 'assign', 888, 'otro curso', 1807000000)")
    _sync_deadlines(conn, DeadlineClient(), [101, 999], NOW)
    assert 888 in rows(conn, "assign")


def test_event_of_unsynced_course_dropped_and_courseless_kept(conn):
    eventos = {"events": [
        {"id": 1, "name": "ajeno", "modulename": "forum", "timesort": NOW + 10, "courseid": 77},
        {"id": 2, "name": "personal", "modulename": "", "timesort": NOW + 20, "courseid": 0},
    ]}
    _sync_deadlines(conn, DeadlineClient(events=eventos), [101], NOW)
    e = rows(conn, "event")
    assert set(e) == {2}
    assert e[2]["course_id"] is None


def test_empty_first_page_with_stored_rows_deletes_nothing(conn):
    _sync_deadlines(conn, DeadlineClient(), [101], NOW)
    _sync_deadlines(conn, DeadlineClient(events={"events": []}), [101], NOW)
    assert set(rows(conn, "event")) == {7003, 7004}


def test_page_cap_deletes_nothing(conn):
    conn.execute("INSERT INTO deadline(course_id, kind, ext_id, name, due_at) "
                 "VALUES(101, 'event', 99999, 'previo', ?)", (NOW + 5,))

    class Infinito(DeadlineClient):
        def call(self, fn, **params):
            if fn != "core_calendar_get_action_events_by_timesort":
                return super().call(fn, **params)
            self.calls.append((fn, params))
            ini = params.get("aftereventid", 0)
            return {"events": [
                {"id": i, "name": f"e{i}", "modulename": "forum", "timesort": NOW + i,
                 "course": {"id": 101}} for i in range(ini + 1, ini + 51)]}

    cliente = Infinito()
    _sync_deadlines(conn, cliente, [101], NOW)
    assert len([c for c in cliente.calls if c[0].startswith("core_calendar")]) == 10
    assert 99999 in rows(conn, "event")
    assert len(rows(conn, "event")) == 501


@pytest.mark.parametrize("url, kept", [
    (SITE_URL + "/calendar/view.php?id=1", True),
    (SITE_URL + "/", True),
    (SITE_URL + ".evil.tld/phish", False),          # mismo prefijo, otro sitio
    (SITE_URL + "@evil.tld/x", False),
    ("https://otro-sitio.example.com/evento", False),
    (None, False),
])
def test_event_url_must_be_on_the_same_site(conn, url, kept):
    eventos = {"events": [
        {"id": 1, "name": "evento", "modulename": "", "timesort": NOW + 10, "courseid": 101, "url": url},
    ]}
    _sync_deadlines(conn, DeadlineClient(events=eventos), [101], NOW)
    assert rows(conn, "event")[1]["url"] == (url if kept else None)
