"""Guardia: la suite nunca abre un navegador de verdad (ver conftest)."""
import os
import webbrowser


def test_webbrowser_open_is_inert():
    assert webbrowser.open("http://127.0.0.1:1/") is True
    assert webbrowser.open_new_tab("http://127.0.0.1:1/") is True
    assert webbrowser.open.__module__ == "conftest"


def test_subprocesses_get_an_inert_browser():
    assert os.environ["BROWSER"] == "true"
