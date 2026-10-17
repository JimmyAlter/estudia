from estudia.secrets import delete_secret, get_secret, set_secret


def test_secret_roundtrip():
    set_secret("x", "y")
    assert get_secret("x") == "y"
    delete_secret("x")
    assert get_secret("x") is None


def test_delete_missing_secret_is_noop():
    delete_secret("no-existe")
