"""Versión de Estudia. La fuente de verdad es el repo: `src/estudia/version.py` y `pyproject.toml`.

Uso: `python scripts/version_from_tag.py [--check vX.Y.Z] [--write-env] [--bump vX.Y.Z] [--root CARPETA]`

- `--check vX.Y.Z`: sale con error si el tag no es `v` + la versión del repo (el workflow de
  release lo corre en los builds de tags y no reescribe nada).
- `--write-env`: escribe `web/.env.production` (`VITE_APP_VERSION`) con la versión del repo.
- `--bump vX.Y.Z`: ayuda para subir la versión: reescribe `version.py`, la de `[project]` en
  `pyproject.toml` y el `version_info` de PyInstaller (`packaging/windows/version_info.txt`).

Siempre verifica que esos tres archivos digan la misma versión e imprime la versión en stdout
(los errores van a stderr). Solo usa la biblioteca estándar.
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
    return f"""# Generado por scripts/version_from_tag.py --bump: no editar a mano.
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


def _project_table(pyproject: str) -> tuple[int, int]:
    start = pyproject.index("[project]\n")
    end = pyproject.find("\n[", start + 1)
    return start, len(pyproject) if end == -1 else end


def _project_version(pyproject: str, version: str) -> str:
    """Cambia solo el `version` de la tabla `[project]`."""
    start, end = _project_table(pyproject)
    table = _replace_once(pyproject[start:end], r'^version = "[^"]*"$',
                          f'version = "{version}"', "pyproject.toml")
    return pyproject[:start] + table + pyproject[end:]


def _find(text: str, pattern: str, where: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"no se encontró la versión en {where}")
    return match.group(1)


def repo_version(root: Path) -> str:
    """La versión del repo. Error si version.py, pyproject.toml y version_info no coinciden."""
    pyproject = (root / "pyproject.toml").read_text("utf-8")
    start, end = _project_table(pyproject)
    from_pyproject = _find(pyproject[start:end], r'^version = "([^"]*)"$', "pyproject.toml")
    from_module = _find((root / "src" / "estudia" / "version.py").read_text("utf-8"),
                        r'^__version__ = "([^"]*)"$', "version.py")
    if from_pyproject != from_module:
        raise ValueError(f"pyproject.toml dice {from_pyproject} y version.py dice {from_module}")
    parse_tag(f"v{from_module}")
    if (root / VERSION_INFO).read_text("utf-8") != version_info(from_module):
        raise ValueError(f"{VERSION_INFO.as_posix()} no corresponde a {from_module}: "
                         "corré este script con --bump")
    return from_module


def check(tag: str, root: Path) -> str:
    version = parse_tag(tag)
    current = repo_version(root)
    if version != current:
        raise ValueError(f"el tag {tag} no coincide con la versión del repo ({current}): "
                         "subí la versión con --bump antes de poner el tag")
    return current


def write_env(root: Path) -> str:
    version = repo_version(root)
    (root / "web" / ".env.production").write_text(f"VITE_APP_VERSION={version}\n", "utf-8")
    return version


def bump(version: str, root: Path) -> None:
    pyproject = root / "pyproject.toml"
    version_py = root / "src" / "estudia" / "version.py"
    # Todo se calcula antes de escribir: si algo falla, no queda nada a medias.
    new_pyproject = _project_version(pyproject.read_text("utf-8"), version)
    new_version_py = _replace_once(version_py.read_text("utf-8"), r'^__version__ = "[^"]*"$',
                                   f'__version__ = "{version}"', "version.py")
    pyproject.write_text(new_pyproject, "utf-8")
    version_py.write_text(new_version_py, "utf-8")
    (root / VERSION_INFO).write_text(version_info(version), "utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", metavar="vX.Y.Z")
    parser.add_argument("--write-env", action="store_true")
    parser.add_argument("--bump", metavar="vX.Y.Z")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        if args.bump is not None:
            bump(parse_tag(args.bump), args.root)
        if args.check is not None:
            check(args.check, args.root)
        version = write_env(args.root) if args.write_env else repo_version(args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
