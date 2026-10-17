from estudia import winreg_schemes as ws

OURS = r'"C:\Estudia\EstudiaHandler.exe" "%1"'
OTHER = r'"C:\Otra\app.exe" "%1"'
MOODLE_CMD = ws._cmd_path(ws.MOODLE_KEY)
ESTUDIA_CMD = ws._cmd_path(ws.ESTUDIA_KEY)


REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD = 1, 2, 3, 4


class FakeReg:
    def __init__(self):
        self.d = {}  # (path, name) -> (dato, tipo)

    def get(self, path, name):
        item = self.d.get((path, name))
        return None if item is None else item[0]

    def set(self, path, name, value):
        self.d[(path, name)] = (value, REG_SZ)

    def set_typed(self, path, name, value, kind):
        self.d[(path, name)] = (value, kind)

    def type_of(self, path, name):
        return self.d[(path, name)][1]

    def export_tree(self, path):
        node = {"values": [], "keys": {}}
        for (p, name), (data, kind) in self.d.items():
            if p == path:
                node["values"].append((name, data, kind))
        subs = {p[len(path) + 1:].split("\\")[0] for p, _ in self.d if p.startswith(path + "\\")}
        node["keys"] = {sub: self.export_tree(path + "\\" + sub) for sub in sorted(subs)}
        return node

    def import_tree(self, path, tree):
        for name, data, kind in tree["values"]:
            self.d[(path, name)] = (data, kind)
        for sub, node in tree["keys"].items():
            self.import_tree(path + "\\" + sub, node)

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
    assert _backup_cmd(reg) == OTHER
    assert reg.get(MOODLE_CMD, "") == OURS


def _backup_cmd(reg):
    node = ws._load(reg.get(ws.BACKUP_KEY, "tree"))
    for part in ("shell", "open", "command"):
        node = node["keys"][part]
    return dict((n, d) for n, d, _ in node["values"])[""]


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
    assert _backup_cmd(reg) == OTHER
    # Otra app lo pisa y volvemos a registrar: el respaldo original no se toca.
    reg.set(MOODLE_CMD, "", r'"C:\Tercera\x.exe" "%1"')
    ws.register(reg, OURS)
    assert _backup_cmd(reg) == OTHER


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
    assert _backup_cmd(reg2) == OTHER
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


def test_restore_keeps_types_extra_values_and_nested_keys():
    reg = FakeReg()
    reg.set(ws.MOODLE_KEY, "", "URL:Otra")
    reg.set(ws.MOODLE_KEY, "URL Protocol", "")
    reg.set_typed(ws.MOODLE_KEY, "EditFlags", 2, REG_DWORD)
    reg.set_typed(ws.MOODLE_KEY, "Blob", b"\x00\xffab", REG_BINARY)
    reg.set_typed(ws.MOODLE_KEY + r"\DefaultIcon", "", r"%ProgramFiles%\Otra\a.exe,0", REG_EXPAND_SZ)
    reg.set_typed(MOODLE_CMD, "", r'"%ProgramFiles%\Otra\a.exe" "%1"', REG_EXPAND_SZ)
    reg.set(ws.MOODLE_KEY + r"\shell\open\ddeexec", "", "[open(\"%1\")]")
    reg.set(ws.MOODLE_KEY + r"\shell\edit\command", "", r'"C:\Otra\ed.exe" "%1"')
    before = reg.export_tree(ws.MOODLE_KEY)
    ws.register(reg, OURS)
    assert reg.get(MOODLE_CMD, "") == OURS
    ws.unregister(reg, OURS)
    assert reg.export_tree(ws.MOODLE_KEY) == before
    assert reg.type_of(MOODLE_CMD, "") == REG_EXPAND_SZ
    assert reg.type_of(ws.MOODLE_KEY + r"\DefaultIcon", "") == REG_EXPAND_SZ
    assert reg.type_of(ws.MOODLE_KEY, "EditFlags") == REG_DWORD
    assert reg.get(ws.MOODLE_KEY + r"\shell\open\ddeexec", "") == '[open("%1")]'
    assert reg.get(ws.MOODLE_KEY + r"\shell\edit\command", "") == r'"C:\Otra\ed.exe" "%1"'
    assert reg.get(ws.MOODLE_KEY, "Blob") == b"\x00\xffab"
