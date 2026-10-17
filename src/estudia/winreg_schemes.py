"""Registro de `estudia://` y `moodlemobile://` en Windows (HKCU, sin permisos de administrador).

Toda la lógica trabaja contra un objeto `reg` inyectable (`get`, `set`, `delete_tree`,
`exists`), así los tests usan un dict. `RealReg` es la implementación sobre `winreg`,
que se importa recién al usarla para que el módulo se pueda importar en Linux.
"""

ESTUDIA_KEY = r"Software\Classes\estudia"
MOODLE_KEY = r"Software\Classes\moodlemobile"
BACKUP_KEY = r"Software\Estudia\PrevMoodleMobile"
_LABELS = {ESTUDIA_KEY: "URL:Estudia", MOODLE_KEY: "URL:moodlemobile"}


PREFS_KEY = r"Software\Estudia"
# Valores de la clave moodlemobile que se respaldan y restauran enteros: (subclave, nombre).
_MOODLE_VALUES = (("", ""), ("", "URL Protocol"), (r"\DefaultIcon", ""),
                  (r"\shell\open\command", ""))


def _cmd_path(key: str) -> str:
    return key + r"\shell\open\command"


def _icon_path(key: str) -> str:
    return key + r"\DefaultIcon"


def _exe_of(command: str) -> str:
    """El ejecutable de un comando como `"C:\\x\\a.exe" -m algo "%1"`."""
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    return command.split(" ", 1)[0]


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
    if takeover_moodle or not _foreign(reg):
        keys.append(MOODLE_KEY)
        if _foreign(reg) and not reg.exists(BACKUP_KEY):
            for sub, name in _MOODLE_VALUES:
                value = reg.get(MOODLE_KEY + sub, name)
                if value is not None:
                    reg.set(BACKUP_KEY + sub, name, value)
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
        had_backup = reg.exists(BACKUP_KEY)
        reg.delete_tree(MOODLE_KEY)
        if had_backup:
            for sub, name in _MOODLE_VALUES:
                value = reg.get(BACKUP_KEY + sub, name)
                if value is not None:
                    reg.set(MOODLE_KEY + sub, name, value)
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
