"""Registro de `estudia://` y `moodlemobile://` en Windows (HKCU, sin permisos de administrador).

Toda la lógica trabaja contra un objeto `reg` inyectable (`get`, `set`, `delete_tree`,
`exists`), así los tests usan un dict. `RealReg` es la implementación sobre `winreg`,
que se importa recién al usarla para que el módulo se pueda importar en Linux.
"""

ESTUDIA_KEY = r"Software\Classes\estudia"
MOODLE_KEY = r"Software\Classes\moodlemobile"
BACKUP_KEY = r"Software\Estudia\PrevMoodleMobile"
_LABELS = {ESTUDIA_KEY: "URL:Estudia", MOODLE_KEY: "URL:moodlemobile"}


def _cmd_path(key: str) -> str:
    return key + r"\shell\open\command"


def _icon_path(key: str) -> str:
    return key + r"\DefaultIcon"


def _exe_of(command: str) -> str:
    """El ejecutable de un comando como `"C:\\x\\a.exe" -m algo "%1"`."""
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    return command.split(" ", 1)[0]


def register(reg, command: str) -> None:
    """Registra los dos esquemas apuntando a `command` (ya completo, con el `"%1"`).

    Si `moodlemobile` ya era de otra app, guarda su comando una sola vez para
    devolverlo al desinstalar.
    """
    previous = reg.get(_cmd_path(MOODLE_KEY), "")
    if previous and previous != command and reg.get(BACKUP_KEY, "command") is None:
        reg.set(BACKUP_KEY, "command", previous)
    for key, label in _LABELS.items():
        reg.set(key, "", label)
        reg.set(key, "URL Protocol", "")
        reg.set(_icon_path(key), "", f'"{_exe_of(command)}",0')
        reg.set(_cmd_path(key), "", command)


def unregister(reg, command: str) -> None:
    """Borra `estudia`; devuelve `moodlemobile` a como estaba, o lo borra si era solo nuestro."""
    reg.delete_tree(ESTUDIA_KEY)
    backup = reg.get(BACKUP_KEY, "command")
    if backup is not None:
        reg.set(_cmd_path(MOODLE_KEY), "", backup)
        reg.delete_tree(BACKUP_KEY)
    elif reg.get(_cmd_path(MOODLE_KEY), "") == command:
        reg.delete_tree(MOODLE_KEY)


def is_registered(reg, command: str) -> bool:
    return all(reg.get(_cmd_path(key), "") == command for key in _LABELS)


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
