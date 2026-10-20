"""Completa la plantilla de las notas del release con la versión y el SHA-256 del instalador.

Uso: `python scripts/render_release_notes.py --template PLANTILLA --version X.Y.Z --sha256 HASH --out SALIDA`

Es determinista: mismos datos, mismo texto. Falla (ValueError, código 1 en la CLI) si la versión
no es X.Y.Z, si el hash no tiene 64 caracteres hexa en minúscula, si a la plantilla le falta
`{{VERSION}}` o `{{SHA256}}`, o si después de reemplazar queda algún `{{`. Solo biblioteca estándar.
"""
import argparse
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def render(template: str, version: str, sha256: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"la versión tiene que ser X.Y.Z, no {version!r}")
    if not SHA256_RE.fullmatch(sha256):
        raise ValueError("el SHA-256 tiene que tener 64 caracteres hexa en minúscula")
    for marker in ("{{VERSION}}", "{{SHA256}}"):
        if marker not in template:
            raise ValueError(f"a la plantilla le falta {marker}")
    text = template.replace("{{VERSION}}", version).replace("{{SHA256}}", sha256)
    if "{{" in text:
        raise ValueError("quedó un marcador {{ sin reemplazar")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        text = render(args.template.read_text(encoding="utf-8"), args.version, args.sha256)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
