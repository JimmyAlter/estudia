"""La práctica elige una variante por tarjeta y nunca repite los mismos valores dos veces seguidas."""
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from estudia.db import connect
from estudia.study.practice import (
    TYPED,
    build_quiz,
    build_session_quiz,
    last_answered_member,
    next_variant_item,
    pick_member,
    record_answer,
)

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY = 86400


def local(day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp())


NOW = local(17, 15)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    c.execute("INSERT INTO material(id, section_id, cmid, kind, title, ref) VALUES (1, 1, 1, 'youtube', 'V', 'yt123')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (1, 10, 'a', 'Tema A')")
    c.execute("INSERT INTO topic(id, course_id, slug, name) VALUES (2, 10, 'b', 'Tema B')")
    c.commit()
    return c


def add_q(conn, qid, topic=1, group=None, check_expr=None, typed_safe=None, retired=0):
    conn.execute(
        "INSERT INTO question(id, material_id, topic_id, stem, options_json, correct_index, explanation, "
        "verified, retired, dedupe_key, variant_group, check_expr, typed_safe) "
        "VALUES (?, 1, ?, ?, '[\"1\",\"2\"]', 0, 'e', 1, ?, ?, ?, ?, ?)",
        (qid, topic, f"Enunciado {qid}", retired, f"k{qid}", group, check_expr, typed_safe))
    conn.commit()


def add_group(conn, head, members, topic=1, **kw):
    for qid in (head, *members):
        add_q(conn, qid, topic, group=head, **kw)


def test_two_presentations_in_a_row_use_different_values(conn):
    add_group(conn, 1, [2, 3])
    shown = []
    for i in range(6):
        (item,) = build_quiz(conn, 10, topic_id=1, now=NOW + i * 60)
        shown.append(item)
        record_answer(conn, item.question_id, 0, NOW + i * 60 + 1)
    for before, after in zip(shown, shown[1:]):
        assert after.question_id != before.question_id
        assert after.stem != before.stem
        assert after.card_key == before.card_key == "v:1"
    # Las tres variantes salen antes de que alguna se repita.
    assert {i.question_id for i in shown[:3]} == {1, 2, 3}


def test_quiz_never_shows_two_members_of_a_group(conn):
    add_group(conn, 1, [2, 3, 4])
    add_group(conn, 10, [11], topic=2)
    add_q(conn, 20)
    add_q(conn, 21, topic=2)
    for now in (NOW, NOW + 1, NOW + 2):
        quiz = build_quiz(conn, 10, size=10, now=now)
        assert sorted(i.card_key for i in quiz) == ["q:20", "q:21", "v:1", "v:10"]
    record_answer(conn, 3, 1, NOW)   # error: la tarjeta pasa a vencida, igual sale una vez
    quiz = build_quiz(conn, 10, size=10, now=NOW + 10)
    assert len(quiz) == len({i.card_key for i in quiz}) == 4
    assert quiz[0].card_key == "v:1" and quiz[0].question_id != 3


def test_session_quiz_never_shows_two_members_of_a_group(conn):
    """El repaso mezclado y el foco eligen la variante por separado: el foco no repite la tarjeta."""
    add_group(conn, 1, [2], topic=2)
    add_group(conn, 5, [6, 7])
    add_q(conn, 8)
    record_answer(conn, 1, 1, NOW - DAY)   # vencidas: van al repaso mezclado
    record_answer(conn, 6, 1, NOW - DAY)
    conn.execute("INSERT INTO plan_session(id, course_id, topic_id, starts_at, minutes, kind, topics_json) "
                 "VALUES (1, 10, 1, ?, 50, 'mixed_focus', '[1, 2]')", (NOW,))
    conn.commit()
    for i in range(12):
        quiz = build_session_quiz(conn, 1, size=10, now=NOW + i)
        assert sorted(item.card_key for item in quiz) == ["q:8", "v:1", "v:5"]


def test_requeue_item_is_another_member(conn):
    add_group(conn, 1, [2, 3])
    add_q(conn, 9)
    fb = record_answer(conn, 2, 1, NOW)
    assert fb.requeue is True
    item = fb.requeue_item
    assert item is not None
    assert item.question_id in (1, 3)
    assert item.card_key == "v:1"
    assert item.stem != "Enunciado 2"
    assert item.relearning == {"day": 1}
    single = record_answer(conn, 9, 1, NOW)
    assert single.requeue is True and single.requeue_item is None
    # Un acierto no vuelve a la sesión: sin otra variante.
    assert record_answer(conn, 3, 0, NOW + 1).requeue_item is None
    # En el simulacro tampoco.
    assert record_answer(conn, 3, 1, NOW + 2, mode="mock").requeue_item is None


def test_next_variant_item_needs_another_active_member(conn):
    add_group(conn, 1, [2])
    add_q(conn, 9)
    assert next_variant_item(conn, 9, NOW) is None
    assert next_variant_item(conn, 1, NOW).question_id == 2
    conn.execute("UPDATE question SET retired = 1 WHERE id = 2")
    conn.commit()
    assert next_variant_item(conn, 1, NOW) is None


def test_last_answered_member_ignores_pretest(conn):
    add_group(conn, 1, [2, 3])
    add_q(conn, 9)
    assert last_answered_member(conn, 1) is None
    record_answer(conn, 2, 0, NOW)
    record_answer(conn, 3, 0, NOW + 5, mode="pretest")
    assert last_answered_member(conn, 1) == 2
    record_answer(conn, 3, 0, NOW + 10, mode="mock")
    assert last_answered_member(conn, 1) == 3
    record_answer(conn, 9, 0, NOW)
    assert last_answered_member(conn, 9) == 9


def test_pick_member_prefers_unanswered_and_never_repeats_last():
    members = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    for seed in range(40):
        rng = random.Random(seed)
        assert pick_member(members, 1, {1, 2, 3}, rng)["id"] == 4      # la única sin contestar
        assert pick_member(members, 4, {1, 2, 3, 4}, rng)["id"] != 4   # todas vistas: nunca la última
        assert pick_member(members, 4, {4}, rng)["id"] in (1, 2, 3)
        assert pick_member(members[:1], 1, {1}, rng)["id"] == 1         # no hay otra
    picks = {pick_member(members, None, set(), random.Random(seed))["id"] for seed in range(40)}
    assert picks == {1, 2, 3, 4}   # entre iguales, al azar
    assert pick_member(members, 2, set(), random.Random(7)) == pick_member(members, 2, set(), random.Random(7))


def test_ever_correct_is_per_group(conn):
    add_group(conn, 1, [2], check_expr="1", typed_safe=1)
    record_answer(conn, 1, 0, NOW)   # acierto en la original
    for i in range(10):
        (item,) = build_quiz(conn, 10, topic_id=1, now=NOW + DAY * 30 + i)
        assert item.question_id == 2
        assert item.format == TYPED   # B ya cuenta como acertada: siempre escrita


def test_retired_head_hides_its_active_members(conn):
    """Si la original está retirada, ningún miembro se muestra aunque siga activo."""
    add_group(conn, 1, [2, 3])
    conn.execute("UPDATE question SET retired = 1 WHERE id = 1")
    conn.commit()
    assert build_quiz(conn, 10, topic_id=1, now=NOW) == []
    assert next_variant_item(conn, 2, NOW) is None
