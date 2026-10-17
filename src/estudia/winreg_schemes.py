"""Registro de `estudia://` y `moodlemobile://` en Windows (HKCU, sin permisos de administrador).

Toda la lógica trabaja contra un objeto `reg` inyectable (`get`, `set`, `delete_tree`,
`exists`), así los tests usan un dict. `RealReg` es la implementación sobre `winreg`,
que se importa recién al usarla para que el módulo se pueda importar en Linux.
"""

import base64
import json

ESTUDIA_KEY = r"Software\Classes\estudia"
MOODLE_KEY = r"Software\Classes\moodlemobile"
BACKUP_KEY = r"Software\Estudia\PrevMoodleMobile"
_LABELS = {ESTUDIA_KEY: "URL:Estudia", MOODLE_KEY: "URL:moodlemobile"}


PREFS_KEY = r"Software\Estudia"
def _cmd_path(key: str) -> str:
    return key + r"\shell\open\command"


def _icon_path(key: str) -> str:
    return key + r"\DefaultIcon"


def _exe_of(command: str) -> str:
    """El ejecutable de un comando como `"C:\\x\\a.exe" -m algo "%1"`."""
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    return command.split(" ", 1)[0]


def _dump(tree: dict) -> str:
    """El arbol exportado como JSON (los REG_BINARY van en base64)."""
    def enc(node):
        values = [[n, {"b64": base64.b64encode(d).decode()} if isinstance(d, bytes) else d, t]
                  for n, d, t in node["values"]]
        return {"values": values, "keys": {k: enc(v) for k, v in node["keys"].items()}}
    return json.dumps(enc(tree))


def _load(text: str) -> dict:
    def dec(node):
        values = [(n, base64.b64decode(d["b64"]) if isinstance(d, dict) else d, t)
                  for n, d, t in node["values"]]
        return {"values": values, "keys": {k: dec(v) for k, v in node["keys"].items()}}
    return dec(json.loads(text))


def is_ours(command: str | None) -> bool:
    """Un comando de Estudia, de esta instalacion o de otra (otra carpeta, modo dev o congelado)."""
    if not command:
        return False
    exe = _exe_of(command).replace("/", "\\").rsplit("\\", 1)[-1].lower()
    return exe == "estudiahandler.exe" or "estudia.server.handler" in command.lower()


def _foreign(reg) -> bool:
    current = reg.get(_cmd_path(MOODLE_KEY), "")
    return bool(current) and not is_ours(current)


def register(reg, command: str, takeover_moodle: bool = True) -> None:
    """Registra los esquemas apuntando a `command` (completo, con el `"%1"`).

    `estudia://` se registra siempre. `moodlemobile://` ajeno solo se pisa con
    `takeover_moodle` (la instalacion explicita), respaldando la clave entera una vez.
    """
    keys = [ESTUDIA_KEY]
    foreign = _foreign(reg)
    if takeover_moodle or not foreign:
        keys.append(MOODLE_KEY)
        if foreign and not reg.exists(BACKUP_KEY):
            reg.set(BACKUP_KEY, "tree", _dump(reg.export_tree(MOODLE_KEY)))
    for key in keys:
        reg.set(key, "", _LABELS[key])
        reg.set(key, "URL Protocol", "")
        reg.set(_icon_path(key), "", f'"{_exe_of(command)}",0')
        reg.set(_cmd_path(key), "", command)


def unregister(reg, command: str) -> None:
    """Borra `estudia`; `moodlemobile` vuelve a como estaba si todavia es de Estudia.

    Si ahora es de otra app, no se toca y solo se descarta el respaldo.
    """
    reg.delete_tree(ESTUDIA_KEY)
    current = reg.get(_cmd_path(MOODLE_KEY), "")
    if current is not None and is_ours(current):
        backup = reg.get(BACKUP_KEY, "tree")
        reg.delete_tree(MOODLE_KEY)
        if backup is not None:
            reg.import_tree(MOODLE_KEY, _load(backup))
    # Software\Estudia solo guarda el respaldo: se va entero al consumirlo.
    reg.delete_tree(PREFS_KEY)


def is_registered(reg, command: str, takeover_moodle: bool = True) -> bool:
    """Sin `takeover_moodle`, un `moodlemobile` ajeno cuenta como en orden."""
    if reg.get(_cmd_path(ESTUDIA_KEY), "") != command:
        return False
    current = reg.get(_cmd_path(MOODLE_KEY), "")
    return current == command or (not takeover_moodle and _foreign(reg))


class RealReg:
    """`reg` sobre HKCU con el módulo `winreg` (solo Windows)."""

    def __init__(self):
        import winreg
        self._w = winreg

    def get(self, path: str, name: str) -> str | None:
        try:
            with self._w.OpenKey(self._w.HKEY_CURRENT_USER, path) as key:
                return self._w.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, path: str, name: str, value: str) -> None:
        with self._w.CreateKey(self._w.HKEY_CURRENT_USER, path) as key:
            self._w.SetValueEx(key, name, 0, self._w.REG_SZ, value)

    def exists(self, path: str) -> bool:
        try:
            self._w.CloseKey(self._w.OpenKey(self._w.HKEY_CURRENT_USER, path))
        except FileNotFoundError:
            return False
        return True

    def export_tree(self, path: str) -> dict:
        """Todos los valores (nombre, dato, tipo) y subclaves, sin perder tipos."""
        values, keys = [], []
        with self._w.OpenKey(self._w.HKEY_CURRENT_USER, path) as key:
            while True:
                try:
                    values.append(self._w.EnumValue(key, len(values)))
                except OSError:
                    break
            while True:
                try:
                    keys.append(self._w.EnumKey(key, len(keys)))
                except OSError:
                    break
        return {"values": values,
                "keys": {name: self.export_tree(path + "\\" + name) for name in keys}}

    def import_tree(self, path: str, tree: dict) -> None:
        with self._w.CreateKey(self._w.HKEY_CURRENT_USER, path) as key:
            for name, data, kind in tree["values"]:
                self._w.SetValueEx(key, name, 0, kind, data)
        for name, sub in tree["keys"].items():
            self.import_tree(path + "\\" + name, sub)

    def delete_tree(self, path: str) -> None:
        try:
            with self._w.OpenKey(self._w.HKEY_CURRENT_USER, path) as key:
                children = []
                while True:
                    try:
                        children.append(self._w.EnumKey(key, len(children)))
                    except OSError:
                        break
        except FileNotFoundError:
            return
        for child in children:
            self.delete_tree(path + "\\" + child)
        self._w.DeleteKey(self._w.HKEY_CURRENT_USER, path)
