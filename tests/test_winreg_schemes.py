from estudia import winreg_schemes as ws

OURS = r'"C:\Estudia\EstudiaHandler.exe" "%1"'
OTHER = r'"C:\Otra\app.exe" "%1"'
MOODLE_CMD = ws._cmd_path(ws.MOODLE_KEY)
ESTUDIA_CMD = ws._cmd_path(ws.ESTUDIA_KEY)


class FakeReg:
    def __init__(self):
        self.d = {}

    def get(self, path, name):
        return self.d.get((path, name))

    def set(self, path, name, value):
        self.d[(path, name)] = value

    def exists(self, path):
        return any(p == path or p.startswith(path + "\\") for p, _ in self.d)

    def delete_tree(self, path):
        self.d = {k: v for k, v in self.d.items()
                  if not (k[0] == path or k[0].startswith(path + "\\"))}


def test_register_fresh():
    reg = FakeReg()
    ws.register(reg, OURS)
    assert reg.get(ws.ESTUDIA_KEY, "") == "URL:Estudia"
    assert reg.get(ws.MOODLE_KEY, "") == "URL:moodlemobile"
    assert reg.get(ws.ESTUDIA_KEY, "URL Protocol") == ""
    assert reg.get(ws.MOODLE_KEY, "URL Protocol") == ""
    assert reg.get(ESTUDIA_CMD, "") == OURS
    assert reg.get(MOODLE_CMD, "") == OURS
    assert reg.get(ws.ESTUDIA_KEY + r"\DefaultIcon", "") == r'"C:\Estudia\EstudiaHandler.exe",0'
    assert not reg.exists(ws.BACKUP_KEY)
    assert ws.is_registered(reg, OURS)
    assert not ws.is_registered(reg, OTHER)


def test_register_backs_up_existing_moodlemobile():
    reg = FakeReg()
    reg.set(MOODLE_CMD, "", OTHER)
    ws.register(reg, OURS)
    assert reg.get(ws.BACKUP_KEY + r"\shell\open\command", "") == OTHER
    assert reg.get(MOODLE_CMD, "") == OURS


BACKUP_CMD = ws.BACKUP_KEY + r"\shell\open\command"


def _foreign_moodle(reg, cmd=OTHER):
    reg.set(ws.MOODLE_KEY, "", "URL:Otra")
    reg.set(ws.MOODLE_KEY, "URL Protocol", "")
    reg.set(ws.MOODLE_KEY + r"\DefaultIcon", "", r"C:\Otra\app.exe,0")
    reg.set(MOODLE_CMD, "", cmd)


def test_register_twice_keeps_original_backup():
    reg = FakeReg()
    _foreign_moodle(reg)
    ws.register(reg, OURS)
    ws.register(reg, OURS)
    assert reg.get(BACKUP_CMD, "") == OTHER
    # Otra app lo pisa y volvemos a registrar: el respaldo original no se toca.
    reg.set(MOODLE_CMD, "", r'"C:\Tercera\x.exe" "%1"')
    ws.register(reg, OURS)
    assert reg.get(BACKUP_CMD, "") == OTHER


def test_unregister_restores_whole_previous_key():
    reg = FakeReg()
    _foreign_moodle(reg)
    ws.register(reg, OURS)
    ws.unregister(reg, OURS)
    assert reg.get(MOODLE_CMD, "") == OTHER
    assert reg.get(ws.MOODLE_KEY, "") == "URL:Otra"
    assert reg.get(ws.MOODLE_KEY + r"\DefaultIcon", "") == r"C:\Otra\app.exe,0"
    assert not reg.exists(ws.PREFS_KEY)
    assert not reg.exists(ws.ESTUDIA_KEY)


def test_unregister_without_backup_deletes_ours_only():
    reg = FakeReg()
    ws.register(reg, OURS)
    ws.unregister(reg, OURS)
    assert not reg.exists(ws.ESTUDIA_KEY)
    assert not reg.exists(ws.MOODLE_KEY)


def test_unregister_leaves_foreign_moodlemobile_untouched():
    reg = FakeReg()
    reg.set(MOODLE_CMD, "", OTHER)
    ws.unregister(reg, OURS)
    assert reg.get(MOODLE_CMD, "") == OTHER


def test_unregister_foreign_replaced_after_register_keeps_current_and_drops_backup():
    reg = FakeReg()
    reg.set(MOODLE_CMD, "", OTHER)
    ws.register(reg, OURS)
    reg.set(MOODLE_CMD, "", r'"C:\B\b.exe" "%1"')
    ws.unregister(reg, OURS)
    assert reg.get(MOODLE_CMD, "") == r'"C:\B\b.exe" "%1"'
    assert not reg.exists(ws.PREFS_KEY)


def test_reinstall_in_other_form_never_backs_up_our_own_command():
    reg = FakeReg()
    dev = r'"C:\Py\pythonw.exe" -m estudia.server.handler "%1"'
    frozen = r'"D:\Nueva\EstudiaHandler.exe" "%1"'
    ws.register(reg, dev)
    ws.register(reg, frozen)
    assert not reg.exists(ws.PREFS_KEY)
    # con un ajeno de por medio, el respaldo es el ajeno original
    reg2 = FakeReg()
    reg2.set(MOODLE_CMD, "", OTHER)
    ws.register(reg2, dev)
    ws.register(reg2, frozen)
    assert reg2.get(BACKUP_CMD, "") == OTHER
    ws.unregister(reg2, frozen)
    assert reg2.get(MOODLE_CMD, "") == OTHER


def test_startup_register_never_overwrites_foreign_moodlemobile():
    reg = FakeReg()
    reg.set(MOODLE_CMD, "", OTHER)
    assert ws.is_registered(reg, OURS, takeover_moodle=False) is False  # falta estudia://
    ws.register(reg, OURS, takeover_moodle=False)
    assert reg.get(ESTUDIA_CMD, "") == OURS
    assert reg.get(MOODLE_CMD, "") == OTHER
    assert not reg.exists(ws.PREFS_KEY)
    assert ws.is_registered(reg, OURS, takeover_moodle=False)
    assert not ws.is_registered(reg, OURS)


def test_startup_register_takes_missing_or_own_moodlemobile():
    reg = FakeReg()
    ws.register(reg, OURS, takeover_moodle=False)
    assert reg.get(MOODLE_CMD, "") == OURS
    new = r'"D:\Nueva\EstudiaHandler.exe" "%1"'
    assert not ws.is_registered(reg, new, takeover_moodle=False)
    ws.register(reg, new, takeover_moodle=False)
    assert reg.get(MOODLE_CMD, "") == new


def test_path_with_spaces_and_accents():
    reg = FakeReg()
    exe = r"C:\Users\Juan Pérez\AppData\Local\Programs\Estudia\EstudiaHandler.exe"
    cmd = f'"{exe}" "%1"'
    ws.register(reg, cmd)
    assert reg.get(ESTUDIA_CMD, "") == cmd
    assert reg.get(ws.ESTUDIA_KEY + r"\DefaultIcon", "") == f'"{exe}",0'
    assert ws.is_registered(reg, cmd)
    assert ws.is_ours(cmd)
