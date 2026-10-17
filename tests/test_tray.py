import multiprocessing
import sys
import time
import types
from types import SimpleNamespace

import pytest

from estudia import tray


class FakeIcon:
    instances = []

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.stopped = False
        self.visible = False
        FakeIcon.instances.append(self)

    def run(self, setup=None):
        if setup:
            setup(self)

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_pystray(monkeypatch):
    FakeIcon.instances.clear()
    mod = types.ModuleType("pystray")
    mod.Menu = lambda *items: list(items)
    mod.MenuItem = lambda text, action, default=False: SimpleNamespace(
        text=text, action=action, default=default
    )
    mod.Icon = FakeIcon
    monkeypatch.setitem(sys.modules, "pystray", mod)
    return mod


def test_menu_labels(fake_pystray):
    tray.run_tray("http://127.0.0.1:1/", lambda: None)
    icon = FakeIcon.instances[0]
    assert [i.text for i in icon.menu] == ["Abrir Estudia", "Salir"]
    assert icon.menu[0].default is True
    assert (icon.name, icon.title) == ("Estudia", "Estudia")


def test_open_calls_browser(fake_pystray, monkeypatch):
    opened = []
    monkeypatch.setattr(tray.webbrowser, "open", opened.append)
    tray.run_tray("http://127.0.0.1:1/", lambda: None)
    icon = FakeIcon.instances[0]
    icon.menu[0].action(icon, icon.menu[0])
    assert opened == ["http://127.0.0.1:1/"]


def test_quit_calls_on_quit_and_stops(fake_pystray):
    calls = []
    tray.run_tray("http://127.0.0.1:1/", lambda: calls.append("quit"))
    icon = FakeIcon.instances[0]
    icon.menu[1].action(icon, icon.menu[1])
    assert calls == ["quit"]
    assert icon.stopped


def test_quit_stops_icon_even_if_on_quit_fails(fake_pystray):
    def boom():
        raise RuntimeError("x")

    tray.run_tray("http://127.0.0.1:1/", boom)
    icon = FakeIcon.instances[0]
    with pytest.raises(RuntimeError):
        icon.menu[1].action(icon, icon.menu[1])
    assert icon.stopped


def test_tray_available_false_when_import_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)  # import -> ImportError
    assert tray.tray_available() is False


def test_shutdown_terminates_children():
    server = SimpleNamespace(should_exit=False)
    child = multiprocessing.Process(target=time.sleep, args=(60,))
    child.start()
    try:
        tray.shutdown(server)
        assert multiprocessing.active_children() == []
        assert server.should_exit is True
    finally:
        if child.is_alive():
            child.kill()


def test_tray_uses_light_variant_icon():
    from importlib import resources

    assert (resources.files("estudia") / "assets" / "icono-bandeja.png").is_file()


def test_should_stop_stops_icon(fake_pystray):
    tray.run_tray("http://127.0.0.1:1/", lambda: None, lambda: True)
    icon = FakeIcon.instances[0]
    assert icon.stopped
    assert icon.visible is True
