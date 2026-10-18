"""Ningún texto del backend habla de fracaso ni de racha rota."""
import re
from pathlib import Path

import estudia

PUNISHING = re.compile(r"fracas|racha rota", re.IGNORECASE)


def test_no_punishing_words_in_backend_texts():
    root = Path(estudia.__file__).parent
    files = sorted(root.rglob("*.py"))
    assert len(files) > 10
    offending = [str(f.relative_to(root)) for f in files if PUNISHING.search(f.read_text(encoding="utf-8"))]
    assert offending == []
