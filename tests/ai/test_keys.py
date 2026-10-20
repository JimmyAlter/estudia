import pytest

from estudia.ai.keys import GEMINI_KEY_PATTERN, normalize_key

KEY = "AIza" + "a" * 35


def test_pattern_matches_ai_studio_shape():
    assert GEMINI_KEY_PATTERN.match(KEY)
    assert not GEMINI_KEY_PATTERN.match("AIza123")


@pytest.mark.parametrize("raw", [
    KEY,
    f"  {KEY}\n",
    f"“{KEY}”",
    f'"{KEY}"',
    f"'{KEY}'",
    f"  “ {KEY} ” \n",
])
def test_normalize_key_strips_whitespace_and_quotes(raw):
    assert normalize_key(raw) == KEY


@pytest.mark.parametrize("raw", ["", "   \n", '""', "“”"])
def test_normalize_key_returns_none_only_when_empty(raw):
    assert normalize_key(raw) is None


def test_normalize_key_does_not_gate_on_format():
    # El formato es una pista: lo que no coincide pasa tal cual (limpio) a Google.
    assert normalize_key(" AIza123 ") == "AIza123"
    assert normalize_key("sk-" + "a" * 40) == "sk-" + "a" * 40
    assert normalize_key("AIza" + "a" * 10 + " " + "a" * 25) == "AIza" + "a" * 10 + " " + "a" * 25
