import httpx
import pytest
import respx

from estudia.server import handler


def test_handler_posts_callback_to_local_port(tmp_path, monkeypatch):
    (tmp_path / "port").write_text("8765", "utf-8")
    monkeypatch.setattr(handler, "data_dir", lambda: tmp_path)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://127.0.0.1:8765/api/state").mock(
            return_value=httpx.Response(200, json={"step": "login"})
        )
        route = mock.post("http://127.0.0.1:8765/api/login/callback").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        assert handler.main(["estudia://token=abc"]) == 0
    assert route.calls.last.request.read() == b'{"url":"estudia://token=abc"}'


def test_handler_without_running_app(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(handler, "data_dir", lambda: tmp_path)
    assert handler.main(["estudia://token=secreto"]) == 1
    assert "secreto" not in capsys.readouterr().err


def test_desktop_entry_registers_scheme():
    entry = handler.desktop_entry("/usr/bin/python3")
    assert "MimeType=x-scheme-handler/estudia;x-scheme-handler/moodlemobile;" in entry
    assert 'Exec="/usr/bin/python3" -m estudia.server.handler %u' in entry


def test_handler_refuses_when_port_is_not_estudia(tmp_path, monkeypatch, capsys):
    (tmp_path / "port").write_text("8765", "utf-8")
    monkeypatch.setattr(handler, "data_dir", lambda: tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8765/api/state").mock(
            return_value=httpx.Response(200, text="<html>otra cosa</html>")
        )
        callback = mock.post("http://127.0.0.1:8765/api/login/callback")
        assert handler.main(["estudia://token=secreto"]) == 1
    assert not callback.called
    assert "secreto" not in capsys.readouterr().err


def test_handler_forwards_moodlemobile_url(tmp_path, monkeypatch):
    (tmp_path / "port").write_text("8765", "utf-8")
    monkeypatch.setattr(handler, "data_dir", lambda: tmp_path)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("http://127.0.0.1:8765/api/state").mock(
            return_value=httpx.Response(200, json={"step": "login"})
        )
        route = mock.post("http://127.0.0.1:8765/api/login/callback").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        assert handler.main(["moodlemobile://token=abc"]) == 0
    assert route.calls.last.request.read() == b'{"url":"moodlemobile://token=abc"}'


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(handler.Path, "home", lambda: tmp_path)
    runs = []
    monkeypatch.setattr(handler.subprocess, "run", lambda cmd, check: runs.append(cmd))
    monkeypatch.setattr(handler.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(handler.sys, "platform", "linux")
    return tmp_path, runs


def test_install_registers_both_schemes(fake_home, capsys):
    home, runs = fake_home
    assert handler.main(["--install"]) == 0
    assert (home / ".local/share/applications/estudia-handler.desktop").is_file()
    assert ["xdg-mime", "default", "estudia-handler.desktop", "x-scheme-handler/estudia"] in runs
    assert ["xdg-mime", "default", "estudia-handler.desktop", "x-scheme-handler/moodlemobile"] in runs


def test_ensure_installed_runs_once(fake_home):
    home, runs = fake_home
    handler.ensure_installed()
    assert (home / ".local/share/applications/estudia-handler.desktop").is_file()
    count = len(runs)
    assert count > 0
    handler.ensure_installed()
    assert len(runs) == count


def test_ensure_installed_skips_without_xdg_mime(fake_home, monkeypatch):
    home, runs = fake_home
    monkeypatch.setattr(handler.shutil, "which", lambda name: None)
    handler.ensure_installed()
    assert runs == []
    assert not (home / ".local/share/applications/estudia-handler.desktop").exists()


def test_ensure_installed_fails_quietly(fake_home, monkeypatch, capsys):
    home, _ = fake_home

    def broken(cmd, check):
        raise handler.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(handler.subprocess, "run", broken)
    handler.ensure_installed()
    # Sin registrar: se vuelve a intentar en el próximo arranque.
    assert not (home / ".local/share/applications/estudia-handler.desktop").exists()
    assert capsys.readouterr().out == ""


def test_ensure_installed_ignores_other_platforms(fake_home, monkeypatch):
    home, runs = fake_home
    monkeypatch.setattr(handler.sys, "platform", "darwin")
    handler.ensure_installed()
    assert runs == []


DESKTOP = ".local/share/applications/estudia-handler.desktop"


@pytest.mark.parametrize("stale", [
    "",  # vacío
    "[Desktop Entry]\nExec=\"{python}\" -m estudia.server.handler %u\n"
    "MimeType=x-scheme-handler/estudia;\n",  # sin moodlemobile
    "[Desktop Entry]\nExec=\"/venv/vieja/bin/python\" -m estudia.server.handler %u\n"
    "MimeType=x-scheme-handler/estudia;x-scheme-handler/moodlemobile;\n",  # otro intérprete
])
def test_ensure_installed_reinstalls_a_stale_file(fake_home, stale):
    home, runs = fake_home
    path = home / DESKTOP
    path.parent.mkdir(parents=True)
    path.write_text(stale.format(python=handler.sys.executable), "utf-8")
    handler.ensure_installed()
    assert len(runs) >= 2
    assert path.read_text("utf-8") == handler.desktop_entry(handler.sys.executable)


def test_ensure_installed_keeps_a_current_file(fake_home):
    home, runs = fake_home
    path = home / DESKTOP
    path.parent.mkdir(parents=True)
    path.write_text(handler.desktop_entry(handler.sys.executable), "utf-8")
    handler.ensure_installed()
    assert runs == []


def test_handler_command_frozen(monkeypatch):
    monkeypatch.setattr(handler.sys, "frozen", True, raising=False)
    monkeypatch.setattr(handler.sys, "executable", "/app/Estudia.exe")
    assert handler.handler_command() == '"/app/EstudiaHandler.exe" "%1"'


def test_handler_command_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delattr(handler.sys, "frozen", raising=False)
    monkeypatch.setattr(handler.sys, "executable", str(tmp_path / "python.exe"))
    (tmp_path / "pythonw.exe").touch()
    assert handler.handler_command() == f'"{tmp_path / "pythonw.exe"}" -m estudia.server.handler "%1"'


def test_handler_command_falls_back_to_python_without_pythonw(tmp_path, monkeypatch):
    monkeypatch.delattr(handler.sys, "frozen", raising=False)
    monkeypatch.setattr(handler.sys, "executable", str(tmp_path / "python.exe"))
    assert handler.handler_command() == f'"{tmp_path / "python.exe"}" -m estudia.server.handler "%1"'


class _Spy:
    def __init__(self):
        self.calls = []

    def register(self, reg, cmd, takeover_moodle=True):
        self.calls.append(("register", cmd, takeover_moodle))

    def unregister(self, reg, cmd):
        self.calls.append(("unregister", cmd))

    def is_registered(self, reg, cmd, takeover_moodle=True):
        self.calls.append(("is_registered", cmd, takeover_moodle))
        return False

    RealReg = staticmethod(lambda: "reg")


@pytest.fixture
def win(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(handler, "winreg_schemes", spy)
    monkeypatch.setattr(handler.sys, "platform", "win32")
    monkeypatch.setattr(handler, "handler_command", lambda: "CMD")
    return spy


def test_install_and_uninstall_on_windows(win):
    assert handler.main(["--install"]) == 0
    assert handler.main(["--uninstall"]) == 0
    assert win.calls == [("register", "CMD", True), ("unregister", "CMD")]


def test_ensure_installed_on_windows_registers_when_stale(win):
    handler.ensure_installed()
    assert win.calls == [("is_registered", "CMD", False), ("register", "CMD", False)]


def test_data_dir_flag_prints_without_creating(tmp_path, monkeypatch, capsys):
    target = tmp_path / "nueva"
    monkeypatch.setattr(handler, "data_path", lambda: target)
    assert handler.main(["--data-dir"]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert not target.exists()
