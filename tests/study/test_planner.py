import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import commit_answers

from estudia.db import connect
from estudia.study.exams import create_manual_exam
from estudia.study.mastery import topic_probability
from estudia.study.planner import (
    get_availability,
    mark_done_from_answers,
    plan_health,
    preferred_start_min,
    replan,
    sessions,
    set_availability,
)
from estudia.study.schedule import add_slot, occurrences, update_slot

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400


def local(day: int, hour: int = 0, minute: int = 0, month: int = 10) -> int:
    """Epoch de una hora local de Buenos Aires en 2026 (el 17/10/2026 es sábado)."""
    return int(datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp())


NOW = local(17, 15, 30)  # sábado 15:30


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (10, 's', 'Análisis', 'AM')")
    c.commit()
    return c


def add_topic(conn, id, verified=1, retired=0):
    conn.execute(
        "INSERT OR IGNORE INTO section(id, course_id, num, name, visible) VALUES (?, 10, ?, 's', 1)",
        (1000 + id, id),
    )
    conn.execute(
        "INSERT INTO material(id, section_id, cmid, kind, title, ref, status) "
        "VALUES (?, ?, ?, 'youtube', 't', 'r', 'done')", (id, 1000 + id, id))
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (?, 10, ?, ?)", (id, f"t{id}", f"T{id}"))
    conn.execute("INSERT INTO material_topic(material_id, topic_id) VALUES (?, ?)", (id, id))
    conn.execute(
        "INSERT INTO question(id, topic_id, stem, options_json, correct_index, verified, retired) "
        "VALUES (?, ?, 'p', '[\"x\",\"y\"]', 0, ?, ?)", (id, id, verified, retired))
    conn.commit()


def answer(conn, qid, correct, at):
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at) VALUES (?, 0, ?, ?)", (qid, correct, at))
    commit_answers(conn)


def all_sessions(conn):
    return sessions(conn, 0, 2**40)


def local_day(epoch):
    return datetime.fromtimestamp(epoch, TZ).date()


def hm(epoch):
    return datetime.fromtimestamp(epoch, TZ).strftime("%H:%M")


def everyday(minutes):
    return {d: minutes for d in range(7)}


def test_settings_defaults_and_roundtrip(conn):
    assert preferred_start_min(conn) == 1140
    assert get_availability(conn) == everyday(0)
    set_availability(conn, {0: 120, 5: 50})
    set_availability(conn, {0: 90})
    assert get_availability(conn) == {**everyday(0), 0: 90, 5: 50}
    conn.execute("INSERT INTO kv(key, value) VALUES ('study_start_min', '600')")
    assert preferred_start_min(conn) == 600
    for bad in ({7: 10}, {0: -1}, {0: 1441}, {0: "60"}):
        with pytest.raises(ValueError):
            set_availability(conn, bad)


def test_no_sessions_in_past(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(100))
    now = local(17, 20, 12)  # ya pasó la hora preferida (19:00)
    assert replan(conn, now) > 0
    out = all_sessions(conn)
    assert all(s.starts_at >= now for s in out)
    today = [s for s in out if local_day(s.starts_at) == local_day(now)]
    # arranca en la próxima marca de 5 minutos, con 10 de descanso entre bloques
    assert [hm(s.starts_at) for s in today] == ["20:15", "21:15"]


def test_today_skipped_when_less_than_25_minutes_left(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(100))
    now = local(17, 23, 40)
    replan(conn, now)
    assert all(local_day(s.starts_at) > local_day(now) for s in all_sessions(conn))


def test_respects_availability(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    create_manual_exam(conn, 10, "Parcial", local(31, 9), [], None)
    set_availability(conn, {0: 120, 1: 50, 2: 30, 3: 0, 4: 75, 5: 125, 6: 100})
    replan(conn, NOW)
    by_day: dict = {}
    for s in all_sessions(conn):
        by_day.setdefault(local_day(s.starts_at), []).append(s)
    for day, items in by_day.items():
        assert sum(s.minutes for s in items) <= get_availability(conn)[day.weekday()]
    def minutes(d):
        return [s.minutes for s in by_day.get(local_day(local(d)), [])]
    monday = by_day[local_day(local(19))]
    assert [(hm(s.starts_at), s.minutes) for s in monday] == [("19:00", 50), ("20:00", 50)]  # 120: sobran 20
    assert minutes(20) == [25, 25]       # martes 50
    assert minutes(21) == [25]           # miércoles 30
    assert minutes(22) == []             # jueves 0
    assert minutes(23) == [50, 25]       # viernes 75
    assert minutes(18) == [50, 50]       # domingo 100
    saturday = by_day[local_day(local(17))]  # sábado 125
    assert [(hm(s.starts_at), s.minutes) for s in saturday] == [("19:00", 50), ("20:00", 50), ("21:00", 25)]


def test_skips_class_overlap(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, {0: 150})
    # dos franjas que se pisan el lunes: 18:30-20:00 y 19:30-21:00 se fusionan en 18:30-21:00
    add_slot(conn, 10, 0, 1110, 1200, None)
    add_slot(conn, 10, 0, 1170, 1260, None)
    replan(conn, NOW)
    monday = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(local(19))]
    assert [hm(s.starts_at) for s in monday] == ["21:00", "22:00", "23:00"]
    # un día que termina a las 24:00 no se pasa al siguiente
    add_slot(conn, 10, 0, 1260, 1290, None)  # 21:00-21:30
    replan(conn, NOW)
    monday = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(local(19))]
    assert [hm(s.starts_at) for s in monday] == ["21:30", "22:30"]
    assert all(s.starts_at + s.minutes * 60 <= local(20) for s in monday)


def test_weak_topic_first(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    for _ in range(5):
        answer(conn, 1, 1, NOW - 3600)
    answer(conn, 2, 0, NOW - 3600)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(150))
    replan(conn, NOW)
    first_day = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(NOW)]
    # El débil primero. Cinco aciertos el mismo día no suben la estabilidad FSRS: el día del examen
    # T1 vale R ≈ 0,81 (prioridad 0,36) y T2, en reaprendizaje, 0,5 (0,94). Tras una vez T2 baja a
    # 0,94 × 0,35 ≈ 0,33 y pasa T1, que ya no tiene prioridad 0 porque se olvida.
    assert [s.topic_id for s in first_day] == [2, 1, 2]
    assert first_day[0].topic == "T2" and first_day[0].exam == "Parcial" and first_day[0].course == "Análisis"


def test_repeated_topic_loses_priority(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    answer(conn, 2, 1, NOW - 3600)
    answer(conn, 2, 0, NOW - 3600)  # T2 al 50 %, T1 al 0 %
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(150))
    replan(conn, NOW)
    first_day = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(NOW)]
    # T1 1,875 · T2 0,9375; tras una vez T1 vale 1,875 × 0,35 ≈ 0,66 y pasa T2; T2 repetido ≈ 0,33
    assert [s.topic_id for s in first_day] == [1, 2, 1]


def test_no_sessions_after_exam(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    near = create_manual_exam(conn, 10, "Primero", local(20, 9), [1], None)
    far = create_manual_exam(conn, 10, "Segundo", local(24, 9), [1, 2], None)
    create_manual_exam(conn, 10, "Viejo", local(10, 9), [1, 2], None)          # pasado
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source) VALUES (10, 'Sin fecha', NULL, 'manual')")
    conn.commit()
    set_availability(conn, everyday(50))
    replan(conn, NOW)
    out = all_sessions(conn)
    assert out
    exam_day = {near: local_day(local(20)), far: local_day(local(24))}
    assert {s.exam_id for s in out} <= {near, far}
    for s in out:
        assert local_day(s.starts_at) < exam_day[s.exam_id]
    # después del primer examen, el tema 1 se prepara para el segundo
    assert {s.exam_id for s in out if s.topic_id == 1 and local_day(s.starts_at) < local_day(local(20))} == {near}
    assert {s.exam_id for s in out if s.topic_id == 1 and local_day(s.starts_at) >= local_day(local(20))} <= {far}
    assert max(local_day(s.starts_at) for s in out) == local_day(local(23))


def test_past_exam_only_no_sessions(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Viejo", local(10, 9), [], None)
    create_manual_exam(conn, 10, "Hoy", local(17, 18), [], None)  # es hoy: no queda ningún día antes
    set_availability(conn, everyday(100))
    assert replan(conn, NOW) == 0
    assert all_sessions(conn) == []


def test_no_topics_no_sessions(conn):
    add_topic(conn, 1, verified=0)
    add_topic(conn, 2, retired=1)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(100))
    assert replan(conn, NOW) == 0
    assert all_sessions(conn) == []
    conn.execute("INSERT INTO course(id, site_id, fullname, shortname) VALUES (11, 's', 'Vacío', 'V')")
    create_manual_exam(conn, 11, "Sin temas", local(25, 9), [], None)
    assert replan(conn, NOW) == 0


def test_replan_after_slot_change_no_overlap(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    create_manual_exam(conn, 10, "Parcial", local(31, 9), [], None)
    set_availability(conn, everyday(100))
    slot = add_slot(conn, 10, 0, 600, 660, None)
    replan(conn, NOW)
    first = [(s.topic_id, s.exam_id, s.starts_at, s.minutes) for s in all_sessions(conn)]
    # idempotente
    replan(conn, NOW)
    assert [(s.topic_id, s.exam_id, s.starts_at, s.minutes) for s in all_sessions(conn)] == first
    # la clase pasa a pisar la hora de estudio
    update_slot(conn, slot, 10, 0, 1140, 1260, None)
    add_slot(conn, 10, 3, 1170, 1230, None)
    replan(conn, NOW)
    out = all_sessions(conn)
    classes = occurrences(conn, NOW, NOW + 15 * DAY)
    assert classes
    for s in out:
        end = s.starts_at + s.minutes * 60
        assert all(end <= c.starts_at or s.starts_at >= c.ends_at for c in classes)
    # las sesiones tampoco se pisan entre sí
    spans = sorted((s.starts_at, s.starts_at + s.minutes * 60) for s in out)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_replan_keeps_past_and_done_sessions(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(100))
    replan(conn, local(16, 10))  # el viernes
    before = all_sessions(conn)
    friday = [s for s in before if local_day(s.starts_at) == local_day(local(16))]
    conn.execute("UPDATE plan_session SET done = 1 WHERE id = ?", (friday[0].id,))
    conn.commit()
    replan(conn, NOW)
    after = all_sessions(conn)
    kept = [(s.starts_at, s.done) for s in after if local_day(s.starts_at) == local_day(local(16))]
    assert kept == [(friday[0].starts_at, True), (friday[1].starts_at, False)]
    assert [(s.starts_at, s.topic_id) for s in after if s.starts_at >= NOW] == [
        (s.starts_at, s.topic_id) for s in before if s.starts_at >= NOW]


def test_done_session_today_counts_against_budget(conn):
    add_topic(conn, 1)
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(150))
    # ya estudió 50 minutos hoy a la mañana
    conn.execute(
        "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, done) VALUES (10, 1, ?, ?, 50, 1)",
        (exam, local(17, 9)))
    conn.commit()
    replan(conn, NOW)
    today = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(NOW)]
    assert [(hm(s.starts_at), s.minutes, s.done) for s in today] == [
        ("09:00", 50, True), ("19:00", 50, False), ("20:00", 50, False)]


def test_replan_during_session_in_progress(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    set_availability(conn, everyday(100))
    replan(conn, local(17, 15))
    now = local(17, 19, 20)  # la de las 19:00 está en curso y no está hecha
    replan(conn, now)
    today = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(now)]
    assert sum(s.minutes for s in today) <= 100
    assert hm(today[0].starts_at) == "19:00" and not today[0].done
    assert today[1].starts_at >= today[0].starts_at + (50 + 10) * 60
    # Los dos de 25 que entran después son del mismo tema: van juntos en uno de 50.
    assert [(hm(s.starts_at), s.minutes) for s in today] == [("19:00", 50), ("20:00", 50)]


@pytest.mark.parametrize("right", [(0, 3, 6, 9), (1, 3, 5, 8)])  # aciertos de 10: dominio 0,0·0,3·0,6·0,9 y 0,1·0,3·0,5·0,8
def test_topics_rotate_across_days(conn, right):
    for t in (1, 2, 3, 4):
        add_topic(conn, t)
    for topic, ok in zip((1, 2, 3, 4), right):
        for i in range(10):
            answer(conn, topic, int(i < ok), NOW - 3600)
    create_manual_exam(conn, 10, "Parcial", local(17, 9, month=11), [], None)
    set_availability(conn, everyday(25))  # un bloque por día
    replan(conn, NOW, horizon_days=7)
    out = all_sessions(conn)
    assert len(out) == 7
    counts = {t: sum(s.topic_id == t for s in out) for t in (1, 2, 3, 4)}
    weak = [t for t, ok in zip((1, 2, 3, 4), right) if ok < 7]
    assert all(counts[t] >= 1 for t in weak)                 # todos los de dominio < 0,7
    assert counts[1] == max(counts.values())                 # el más flojo va más (o empata)
    # las sesiones que quedan cuentan para la rotación: con el sábado ya hecho en T1, el domingo no repite
    conn.execute("UPDATE plan_session SET done = 1 WHERE starts_at < ?", (local(18),))
    conn.commit()
    replan(conn, NOW, horizon_days=7)
    assert [s.topic_id for s in all_sessions(conn)][:2] == [1, 2]


def test_mark_done_from_answers(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    for topic, at in ((1, local(17, 19)), (2, local(17, 20)), (1, local(18, 19))):
        conn.execute(
            "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes) VALUES (10, ?, ?, ?, 50)",
            (topic, exam, at))
    conn.commit()
    for _ in range(5):
        answer(conn, 1, 1, local(17, 10))   # hoy, a la mañana
    for _ in range(4):
        answer(conn, 2, 1, local(17, 10))   # solo 4: no alcanza
    answer(conn, 2, 1, local(16, 23, 50))   # de ayer: no cuenta para hoy
    now = local(17, 21)
    assert mark_done_from_answers(conn, now) == 1
    done = {(s.topic_id, local_day(s.starts_at)) for s in all_sessions(conn) if s.done}
    assert done == {(1, local_day(local(17)))}   # la de mañana no se toca
    assert mark_done_from_answers(conn, now) == 0


def test_late_night_utc_uses_local_day(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(19, 9), [], None)  # lunes
    set_availability(conn, {5: 50, 6: 0})  # solo el sábado
    now = local(17, 21, 1)  # sábado 21:01 local = domingo 00:01 UTC
    assert datetime.fromtimestamp(now, ZoneInfo("UTC")).date() > local_day(now)
    replan(conn, now)
    out = all_sessions(conn)
    # Dos de 25 del mismo tema, seguidos: una sola sesión de 50.
    assert [(local_day(s.starts_at), hm(s.starts_at), s.minutes) for s in out] == [
        (local_day(local(17)), "21:05", 50)]
    health = plan_health(conn, now)
    assert health[0].minutes_available == 50  # sábado 50 + domingo 0; el lunes es el examen


def test_plan_health_verdicts(conn):
    for t in (1, 2):
        add_topic(conn, t)
    answer(conn, 1, 1, NOW)  # T1: R ≈ 0,91 el día del examen; T2 sin ver: 0. Dominio medio ≈ 0,455
    exam = create_manual_exam(conn, 10, "Primer parcial", local(20, 9), [], None)  # martes
    create_manual_exam(conn, 10, "Viejo", local(10, 9), [], None)
    conn.execute("INSERT INTO exam(course_id, name, opens_at, source) VALUES (10, 'Sin fecha', NULL, 'manual')")
    conn.commit()
    # necesita round(2 × 50 × (1 − 0,455)) = 55 minutos; quedan sábado, domingo y lunes
    set_availability(conn, {5: 22, 6: 22, 0: 22})
    [h] = plan_health(conn, NOW)
    assert (h.exam_id, h.exam, h.course, h.minutes_needed, h.minutes_available, h.verdict) == (
        exam, "Primer parcial", "Análisis", 55, 66, "llegás")  # 66 = 120 %
    set_availability(conn, {0: 0})
    assert (plan_health(conn, NOW)[0].minutes_available, plan_health(conn, NOW)[0].verdict) == (44, "justo")
    set_availability(conn, {6: 0})
    assert plan_health(conn, NOW)[0].verdict == "no llegás"  # 22 < 44
    set_availability(conn, {5: 100})
    assert plan_health(conn, NOW)[0].verdict == "llegás"
    # lo ya estudiado hoy se descuenta
    conn.execute(
        "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, done) VALUES (10, 1, ?, ?, 50, 1)",
        (exam, local(17, 9)))
    conn.commit()
    assert (plan_health(conn, NOW)[0].minutes_available, plan_health(conn, NOW)[0].verdict) == (50, "justo")
    # tarde a la noche, hoy solo cuenta lo que queda hasta las 24:00 (como en replan)
    late = local(17, 23)
    assert plan_health(conn, late)[0].minutes_available == 50
    conn.execute("DELETE FROM plan_session")
    conn.commit()
    assert plan_health(conn, late)[0].minutes_available == 60


def test_plan_health_without_topics(conn):
    create_manual_exam(conn, 10, "Parcial", local(20, 9), [], None)
    [h] = plan_health(conn, NOW)
    assert (h.minutes_needed, h.minutes_available, h.verdict) == (0, 0, "llegás")


# --- Tarea 2.2: probabilidad al día del examen, sesiones mezcladas y "¿llegás?" que se explica ---

def answer_with(conn, qid, correct, at, confidence):
    conn.execute(
        "INSERT INTO answer(question_id, chosen_index, correct, at, confidence) VALUES (?, 0, ?, ?, ?)",
        (qid, correct, at, confidence))
    commit_answers(conn)


def test_mastered_topic_returns_when_forgotten(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    answer(conn, 1, 1, NOW - 3600)                 # T1: recién acertada, 100 % hoy, ≈ 67 % el día del examen
    for days in (60, 45, 25):
        answer(conn, 2, 1, NOW - days * DAY)       # T2: espaciada, ≈ 96 % hoy y ≈ 93 % el día del examen
    exam = create_manual_exam(conn, 10, "Parcial", local(17, 9, month=11), [], None)
    set_availability(conn, everyday(25))
    replan(conn, NOW, horizon_days=7)
    out = all_sessions(conn)
    # Con el dominio de hoy T1 no tendría nada que estudiar; con P(examen) es el que más lo necesita.
    assert out[0].topic_id == 1 and out[0].exam_id == exam
    assert sum(s.topic_id == 1 for s in out) > sum(s.topic_id == 2 for s in out)


def test_last_week_sessions_all_mixed(conn):
    for t in (1, 2, 3, 4):
        add_topic(conn, t)
    parcial = create_manual_exam(conn, 10, "Parcial", local(25, 9), [1, 2, 3], None)   # sábado 17 + 8 días
    final = create_manual_exam(conn, 10, "Final", local(17, 9, month=11), [4], None)
    set_availability(conn, everyday(100))
    replan(conn, NOW)
    out = all_sessions(conn)
    week = [s for s in out if s.exam_id == parcial and (local_day(local(25)) - local_day(s.starts_at)).days <= 7]
    assert week and all(s.kind == "mixed_only" for s in week)
    for s in week:
        assert sorted(s.topic_ids) == [1, 2, 3] and s.topic_ids[0] == s.topic_id
        assert (s.review_minutes, s.focus_minutes) == (s.minutes, 0)
        assert s.label == "Repaso mezclado: T1, T2 y 1 más"
    # el sábado faltan 8 días: todavía hay foco
    saturday = [s for s in out if local_day(s.starts_at) == local_day(NOW)]
    assert saturday and all(s.kind != "mixed_only" for s in saturday)
    # la última semana de un examen no mezcla los temas de otro más lejano
    other = [s for s in out if s.exam_id == final and local_day(s.starts_at) < local_day(local(25))]
    assert other and all(s.kind != "mixed_only" and s.topic_ids[0] == 4 for s in other)


def test_new_topic_blocked_first_two_sessions(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    exam = create_manual_exam(conn, 10, "Parcial", local(17, 9, month=11), [], None)
    # T2 ya tuvo dos sesiones hechas (y una salteada, que no cuenta)
    for day, done in ((14, 1), (15, 1), (16, 0)):
        conn.execute(
            "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, done) "
            "VALUES (10, 2, ?, ?, 50, ?)", (exam, local(day, 19), done))
    conn.commit()
    set_availability(conn, everyday(75))   # un bloque de 50 y uno de 25 por día
    replan(conn, NOW, horizon_days=7)
    new = [s for s in all_sessions(conn) if s.starts_at >= NOW]
    t1 = [s for s in new if s.topic_id == 1]
    assert len(t1) >= 3
    assert [s.kind for s in t1[:2]] == ["focus", "focus"]
    assert all(s.kind == "mixed_focus" for s in t1[2:])
    t2 = [s for s in new if s.topic_id == 2]
    assert t2 and all(s.kind == "mixed_focus" for s in t2)
    for s in new:
        if s.kind == "focus":
            assert (s.topic_ids, s.review_minutes, s.focus_minutes) == ([s.topic_id], 0, s.minutes)
            assert s.label == f"Foco: T{s.topic_id}"
        else:
            # repaso de vencidas de todos los temas del examen y después el tema foco
            assert s.topic_ids[0] == s.topic_id and sorted(s.topic_ids) == [1, 2]
            assert (s.review_minutes, s.focus_minutes) == {50: (10, 40), 25: (7, 18)}[s.minutes]
            assert s.label == f"Foco: T{s.topic_id}, con repaso de T{3 - s.topic_id}"


def test_health_explains_weak_topics_and_today(conn):
    for t in (1, 2, 3, 4):
        add_topic(conn, t)
    for qid in (1, 2, 3):                      # T1–T3 vistas una vez (vencen antes del examen); T4 nunca
        answer(conn, qid, 1, NOW - 3600)
    answer(conn, 2, 0, NOW - 1800)             # T2 en reaprendizaje: vence ya
    exam = create_manual_exam(conn, 10, "Parcial", local(27, 9), [], None)
    set_availability(conn, everyday(100))
    replan(conn, NOW)
    [h] = plan_health(conn, NOW)
    at = local(27, 9)
    p = {t: topic_probability(conn, t, at) for t in (1, 2, 3, 4)}
    due = {t: conn.execute(
        "SELECT COUNT(*) FROM review_state WHERE card_key = ? AND due < ?", (f"q:{t}", at)).fetchone()[0]
        for t in (1, 2, 3, 4)}
    assert due[2] == 1 and due[4] == 0 and h.due_reviews == sum(due.values())
    # minutos = Σ 50 × (1 − P) + 1 por cada repaso que vence antes del examen
    assert h.minutes_needed == round(sum(50 * (1 - p[t]) + due[t] for t in p))
    # por qué: los 3 temas con menor P, con sus minutos
    order = sorted(p, key=lambda t: p[t])[:3]
    assert [w.topic_id for w in h.weak_topics] == order
    assert order[0] == 4 and h.weak_topics[0].topic == "T4" and h.weak_topics[0].minutes == 50
    for w in h.weak_topics:
        assert w.probability == pytest.approx(p[w.topic_id])
        assert w.minutes == round(50 * (1 - p[w.topic_id]) + due[w.topic_id])
    # qué hacer hoy: la próxima sesión de hoy de este examen
    first = [s for s in all_sessions(conn) if s.exam_id == exam][0]
    assert local_day(first.starts_at) == local_day(NOW)
    assert (h.today.id, h.today.topic, h.today.minutes, h.today.label) == (
        first.id, first.topic, first.minutes, first.label)
    # hecha la de hoy, pasa a la siguiente de hoy; sin ninguna pendiente hoy, no hay
    conn.execute("UPDATE plan_session SET done = 1 WHERE id = ?", (first.id,))
    conn.commit()
    assert plan_health(conn, NOW).pop().today.id != first.id
    assert plan_health(conn, local(17, 23, 59)).pop().today is None


def test_done_requires_non_guess_correct(conn):
    add_topic(conn, 1)
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    conn.execute(
        "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes) VALUES (10, 1, ?, ?, 50)",
        (exam, local(17, 19)))
    conn.commit()
    now = local(17, 21)
    answer_with(conn, 1, 1, local(17, 19, 5), 2)       # seguro
    answer_with(conn, 1, 1, local(17, 19, 6), None)    # sin confianza: cuenta como seguro
    for minute in (7, 8, 9):
        answer_with(conn, 1, 1, local(17, 19, minute), 0)   # aciertos con "Adiviné": no cuentan
    answer_with(conn, 1, 0, local(17, 19, 10), 2)      # error
    assert mark_done_from_answers(conn, now) == 0      # 6 respuestas, solo 2 aciertos de verdad
    answer_with(conn, 1, 1, local(17, 19, 11), 1)      # "Me parece" sí cuenta
    assert mark_done_from_answers(conn, now) == 1


def test_mixed_only_done_counts_answers_of_all_its_topics(conn):
    add_topic(conn, 1)
    add_topic(conn, 2)
    exam = create_manual_exam(conn, 10, "Parcial", local(20, 9), [], None)
    set_availability(conn, everyday(25))   # una sesión por día
    replan(conn, NOW)
    first = all_sessions(conn)[0]
    assert first.kind == "mixed_only" and sorted(first.topic_ids) == [1, 2]
    for qid, minute in ((1, 1), (2, 2), (1, 3), (2, 4), (2, 5)):
        answer(conn, qid, 1, local(17, 19, minute))
    assert mark_done_from_answers(conn, local(17, 21)) == 1
    assert all_sessions(conn)[0].done and all_sessions(conn)[0].exam_id == exam


@pytest.mark.parametrize("spread, done", [((1, 2, 3, 4, 5), 1), ((1, 2, 3, 4, 5, 61, 62, 63, 64, 65), 2)])
def test_each_answer_counts_for_one_session(conn, spread, done):
    """Dos sesiones mezcladas el mismo día: cinco respuestas hacen una sola, diez hacen las dos."""
    add_topic(conn, 1)
    add_topic(conn, 2)
    create_manual_exam(conn, 10, "Parcial", local(22, 9), [], None)   # en 5 días: última semana
    set_availability(conn, everyday(110))
    replan(conn, NOW)
    today = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(NOW)]
    assert [(hm(s.starts_at), s.kind) for s in today] == [("19:00", "mixed_only"), ("20:00", "mixed_only")]
    for i, minute in enumerate(spread):
        answer(conn, 1 + i % 2, 1, local(17, 19) + minute * 60)
    assert mark_done_from_answers(conn, local(17, 22)) == done
    assert [s.done for s in all_sessions(conn) if s.id in {t.id for t in today}] == [True, done == 2]
    # repetirlo no reparte de nuevo las mismas respuestas
    assert mark_done_from_answers(conn, local(17, 22)) == 0


def test_mixed_labels_name_their_topics(conn):
    for t in (1, 2, 3, 4):
        add_topic(conn, t)
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    rows = [("mixed_only", [3, 1, 2, 4]), ("mixed_only", [2, 1]), ("mixed_only", [4]),
            ("mixed_focus", [1, 2, 3]), ("mixed_focus", [1]), ("focus", [2])]
    for i, (kind, topics) in enumerate(rows):
        conn.execute(
            "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, kind, topics_json) "
            "VALUES (10, ?, ?, ?, 50, ?, ?)", (topics[0], exam, local(18, 8 + i), kind, json.dumps(topics)))
    conn.commit()
    assert [s.label for s in all_sessions(conn)] == [
        "Repaso mezclado: T1, T2 y 2 más", "Repaso mezclado: T1 y T2", "Repaso mezclado: T4",
        "Foco: T1, con repaso de T2 y T3", "Foco: T1", "Foco: T2"]


@pytest.mark.parametrize("minutes, now", [
    (range(1, 11), local(17, 22)),        # 10 respuestas en la primera: la de las 20:00 sigue abierta
    ([-60] * 5, local(17, 22)),           # práctica temprana (18:00): cuenta solo para la primera
    (range(6, 11), local(17, 19, 40)),    # a las 19:40 la de las 20:00 todavía no empezó
])
def test_answers_never_count_for_a_later_session(conn, minutes, now):
    add_topic(conn, 1)
    add_topic(conn, 2)
    create_manual_exam(conn, 10, "Parcial", local(22, 9), [], None)
    set_availability(conn, everyday(110))
    replan(conn, NOW)
    today = [s for s in all_sessions(conn) if local_day(s.starts_at) == local_day(NOW)]
    assert [hm(s.starts_at) for s in today] == ["19:00", "20:00"]
    for i, minute in enumerate(minutes):
        answer(conn, 1 + i % 2, 1, local(17, 19) + minute * 60)
    assert mark_done_from_answers(conn, now) == 1
    assert [s.done for s in all_sessions(conn) if s.id in {t.id for t in today}] == [True, False]


# --- Crítico 3a, ronda 1: rótulos estables y sesiones seguidas del mismo conjunto ---

def test_mixed_label_is_stable_whatever_the_focus(conn):
    for t in (1, 2, 3):
        add_topic(conn, t)
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    rows = [("mixed_only", [3, 1, 2]), ("mixed_only", [2, 3, 1]), ("mixed_focus", [2, 3, 1]),
            ("mixed_focus", [2, 1, 3])]
    for i, (kind, topics) in enumerate(rows):
        conn.execute(
            "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, kind, topics_json) "
            "VALUES (10, ?, ?, ?, 50, ?, ?)", (topics[0], exam, local(18, 8 + i), kind, json.dumps(topics)))
    conn.commit()
    assert [s.label for s in all_sessions(conn)] == [
        "Repaso mezclado: T1, T2 y 1 más", "Repaso mezclado: T1, T2 y 1 más",
        "Foco: T2, con repaso de T1 y T3", "Foco: T2, con repaso de T1 y T3"]
    # El orden de los temas para practicar no cambia: el foco sigue primero.
    assert [s.topic_ids[0] for s in all_sessions(conn)] == [3, 2, 2, 2]


def test_back_to_back_sessions_of_the_same_topics_merge(conn):
    add_topic(conn, 1)
    create_manual_exam(conn, 10, "Parcial", local(31, 9), [], None)
    set_availability(conn, {0: 50, 5: 100})   # lunes: dos bloques de 25; sábado: dos de 50
    replan(conn, NOW)
    by_day: dict = {}
    for s in all_sessions(conn):
        by_day.setdefault(local_day(s.starts_at), []).append(s)
    # Dos de 25 seguidos del mismo tema son una sesión de 50, sin pasarse del bloque.
    monday = by_day[local_day(local(19))]
    assert [(hm(s.starts_at), s.minutes, s.label) for s in monday] == [("19:00", 50, "Foco: T1")]
    # Dos de 50 no se unen (el bloque es de 50), pero dicen lo mismo.
    saturday = by_day[local_day(local(17))]
    assert [(hm(s.starts_at), s.minutes) for s in saturday] == [("19:00", 50), ("20:00", 50)]
    assert len({s.label for s in saturday}) == 1


def test_last_week_back_to_back_mixed_sessions_merge(conn):
    for t in (1, 2):
        add_topic(conn, t)
    exam = create_manual_exam(conn, 10, "Parcial", local(22, 9), [], None)   # jueves 22
    set_availability(conn, everyday(50))
    replan(conn, NOW)
    week = [s for s in all_sessions(conn) if s.exam_id == exam]
    by_day: dict = {}
    for s in week:
        by_day.setdefault(local_day(s.starts_at), []).append(s)
    assert by_day and all(s.kind == "mixed_only" for s in week)
    for items in by_day.values():
        assert [(hm(s.starts_at), s.minutes) for s in items] == [("19:00", 50)]
        assert items[0].label == "Repaso mezclado: T1 y T2"


def test_names_with_y_inside_are_joined_with_commas(conn):
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (21, 10, 'a', 'Paralelismo y perpendicularidad')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (22, 10, 'b', 'Funciones lineales y pendiente')")
    conn.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (23, 10, 'c', 'Rectas')")
    exam = create_manual_exam(conn, 10, "Parcial", local(25, 9), [], None)
    rows = [("mixed_only", [21, 22]), ("mixed_focus", [23, 21]), ("mixed_only", [23, 21, 22])]
    for i, (kind, topics) in enumerate(rows):
        conn.execute(
            "INSERT INTO plan_session(course_id, topic_id, exam_id, starts_at, minutes, kind, topics_json) "
            "VALUES (10, ?, ?, ?, 50, ?, ?)", (topics[0], exam, local(18, 8 + i), kind, json.dumps(topics)))
    conn.commit()
    # Con una "y" adentro de un nombre, "A y B" se leería como tres temas: van con coma.
    assert [s.label for s in all_sessions(conn)] == [
        "Repaso mezclado: Funciones lineales y pendiente, Paralelismo y perpendicularidad",
        "Foco: Rectas, con repaso de Paralelismo y perpendicularidad",
        "Repaso mezclado: Funciones lineales y pendiente, Paralelismo y perpendicularidad y 1 más",
    ]
