"""Pone la versión de un tag `vX.Y.Z` en todos los lugares que la usan.

Uso: `python scripts/version_from_tag.py vX.Y.Z [--root CARPETA]`

Reescribe la versión de `[project]` en `pyproject.toml`, `src/estudia/version.py`,
`web/.env.production` (`VITE_APP_VERSION`) y el `version_info` de PyInstaller
(`packaging/windows/version_info.txt`, que da la versión de los .exe y del instalador).
Solo usa la biblioteca estándar: el workflow lo corre antes de instalar dependencias.
"""
import argparse
import re
import sys
from pathlib import Path

TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
VERSION_INFO = Path("packaging/windows/version_info.txt")


def parse_tag(tag: str) -> str:
    """`v1.2.3` -> `1.2.3`. Cualquier otra forma (sin `v`, incompleta, prerelease) es error."""
    if not TAG_RE.fullmatch(tag):
        raise ValueError(f"el tag tiene que ser vX.Y.Z (por ejemplo v0.2.0), no {tag!r}")
    return tag[1:]


def version_info(version: str) -> str:
    """Archivo `version_info` de PyInstaller para los dos ejecutables."""
    numbers = ", ".join(version.split(".") + ["0"])
    return f"""# Generado por scripts/version_from_tag.py: no editar a mano.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({numbers}),
    prodvers=({numbers}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040A04B0', [
        StringStruct('CompanyName', 'JimmyAlter'),
        StringStruct('FileDescription', 'Estudia'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'Estudia'),
        StringStruct('LegalCopyright', 'JimmyAlter'),
        StringStruct('OriginalFilename', 'Estudia.exe'),
        StringStruct('ProductName', 'Estudia'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x040A, 1200])])
  ]
)
"""


def _replace_once(text: str, pattern: str, repl: str, where: str) -> str:
    new, count = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"no se encontró la versión en {where}")
    return new


def _project_version(pyproject: str, version: str) -> str:
    """Cambia solo el `version` de la tabla `[project]`."""
    start = pyproject.index("[project]\n")
    end = pyproject.find("\n[", start + 1)
    end = len(pyproject) if end == -1 else end
    table = _replace_once(pyproject[start:end], r'^version = "[^"]*"$',
                          f'version = "{version}"', "pyproject.toml")
    return pyproject[:start] + table + pyproject[end:]


def apply(version: str, root: Path) -> None:
    pyproject = root / "pyproject.toml"
    version_py = root / "src" / "estudia" / "version.py"
    # Todo se calcula antes de escribir: si algo falla, no queda nada a medias.
    new_pyproject = _project_version(pyproject.read_text("utf-8"), version)
    new_version_py = _replace_once(version_py.read_text("utf-8"), r'^__version__ = "[^"]*"$',
                                   f'__version__ = "{version}"', "version.py")
    pyproject.write_text(new_pyproject, "utf-8")
    version_py.write_text(new_version_py, "utf-8")
    (root / "web" / ".env.production").write_text(f"VITE_APP_VERSION={version}\n", "utf-8")
    (root / VERSION_INFO).write_text(version_info(version), "utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        version = parse_tag(args.tag)
        apply(version, args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Versión {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
