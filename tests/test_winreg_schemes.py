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
    assert reg.get(ws.BACKUP_KEY, "command") == OTHER
    assert reg.get(MOODLE_CMD, "") == OURS


def test_register_twice_keeps_original_backup():
    reg = FakeReg()
    reg.set(MOODLE_CMD, "", OTHER)
    ws.register(reg, OURS)
    ws.register(reg, OURS)
    assert reg.get(ws.BACKUP_KEY, "command") == OTHER
    # Otra app lo pisa y volvemos a registrar: el respaldo original no se toca.
    reg.set(MOODLE_CMD, "", r'"C:\Tercera\x.exe" "%1"')
    ws.register(reg, OURS)
    assert reg.get(ws.BACKUP_KEY, "command") == OTHER


def test_unregister_restores_previous():
    reg = FakeReg()
    reg.set(MOODLE_CMD, "", OTHER)
    ws.register(reg, OURS)
    ws.unregister(reg, OURS)
    assert reg.get(MOODLE_CMD, "") == OTHER
    assert not reg.exists(ws.BACKUP_KEY)
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
