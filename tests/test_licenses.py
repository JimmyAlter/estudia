import importlib.metadata as md
import importlib.util
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "estudia"


def _closure() -> dict[str, md.Distribution]:
    seen: dict[str, md.Distribution] = {}
    pending = [Requirement(r) for r in (md.requires("estudia") or [])]
    while pending:
        req = pending.pop()
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        name = re.sub(r"[-_.]+", "-", req.name).lower()
        if name in seen:
            continue
        try:
            dist = md.distribution(req.name)
        except md.PackageNotFoundError:
            continue  # dependencia de otra plataforma, no instalada
        seen[name] = dist
        pending.extend(Requirement(r) for r in (dist.requires or []))
    return seen


def _license_texts(dist: md.Distribution) -> list[str]:
    meta = dist.metadata
    texts = [meta.get("License-Expression") or "", meta.get("License") or ""]
    texts += [c for c in (meta.get_all("Classifier") or []) if c.startswith("License")]
    return [t for t in texts if t]


_LONG_NAMES = [
    (r"GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL"),
    (r"GNU (?:LESSER|LIBRARY) GENERAL PUBLIC LICENSE", "LGPL"),
    (r"GNU GENERAL PUBLIC LICENSE", "GPL"),
]
# GPL o AGPL, pero no LGPL (la L delante lo descarta).
_GPL_TOKEN = re.compile(r"(?<![A-Z])A?GPL")
_PERMISSIVE = re.compile(
    r"\b(?:MIT|BSD|APACHE|ISC|PSF|MPL|HPND|UNLICENSE|CC0|ZLIB)\b"
)


def _normalize(text: str) -> str:
    up = text.upper()
    for pattern, short in _LONG_NAMES:
        up = re.sub(pattern, short, up)
    return up


def _is_copyleft_only(text: str) -> bool:
    """True si la licencia obliga a GPL/AGPL sin una alternativa permisiva.

    Política: LGPL sola se permite (enlace dinámico), GPL/AGPL no. Se parte la expresión
    en alternativas con " OR " (con espacios, así `-or-later` no cuenta); una alternativa
    salva si no tiene GPL/AGPL y nombra una licencia permisiva o LGPL. Las " AND " no
    salvan nada: cualquier término GPL/AGPL en la alternativa la condena.
    """
    up = _normalize(text)
    if not _GPL_TOKEN.search(up):
        return False
    for alt in re.split(r"\s+OR\s+", up):
        if not _GPL_TOKEN.search(alt) and (_PERMISSIVE.search(alt) or "LGPL" in alt):
            return False
    return True


def _dist_is_copyleft_only(texts: list[str], expression: str) -> bool:
    if expression:  # la expresión SPDX manda
        return _is_copyleft_only(expression)
    gpl = [t for t in texts if _is_copyleft_only(t)]
    return bool(gpl) and not any(_PERMISSIVE.search(_normalize(t)) for t in texts)


def test_no_gpl_or_agpl_runtime_dependency():
    closure = _closure()
    assert "pypdfium2" in closure and "python-pptx" in closure
    culpables = {
        n: _license_texts(d)
        for n, d in closure.items()
        if _dist_is_copyleft_only(_license_texts(d), d.metadata.get("License-Expression") or "")
    }
    assert not culpables


@pytest.mark.parametrize(
    "text, culprit",
    [
        ("GNU Affero General Public License v3", True),
        ("GNU General Public License v3", True),
        ("GPL-3.0-or-later AND MIT", True),
        ("GPL-3.0 OR MIT", False),
        ("AGPL-3.0-only AND LGPL-2.1", True),
        ("LGPL-2.1", False),
        ("GNU Lesser General Public License v3 (LGPLv3)", False),
        ("BSD-3-Clause OR Apache-2.0", False),
        ("permit stuff or other GPL-3.0", True),
        ("MIT", False),
    ],
)
def test_copyleft_detection_table(text, culprit):
    assert _is_copyleft_only(text) is culprit


_IMPORT_RE = re.compile(r"\b(import|from)\s+(fitz|pymupdf)\b")
_DYNAMIC_RE = re.compile(r"__import__\(\s*[\"'](fitz|pymupdf)")


def test_pymupdf_never_used():
    assert importlib.util.find_spec("fitz") is None
    assert importlib.util.find_spec("pymupdf") is None
    for root in (SRC, ROOT / "scripts"):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert not _IMPORT_RE.search(text), path
            assert not _DYNAMIC_RE.search(text), path
            assert "pymupdf" not in text.lower(), path
    for name in ("uv.lock", "pyproject.toml"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert not re.search(r"\bpymupdf\b", text), name
        assert not re.search(r'name = "fitz"|["\']fitz["\'>=<]', text), name
