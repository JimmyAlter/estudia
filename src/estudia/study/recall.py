"""'Contá lo que te acordás': qué títulos del resumen no mencionó el estudiante.

Solo compara palabras, sin IA. El texto del estudiante vive únicamente en memoria.
"""
import re
import unicodedata
from dataclasses import dataclass
from math import ceil

# Sin acentos, porque `normalize_words` los saca antes de comparar.
STOPWORDS: frozenset[str] = frozenset("""
a al algo algun alguna algunas alguno algunos ante antes aqui as asi aun aunque cada como con contra cual cuales
cuando de del desde donde dos e el ella ellas ello ellos en entre era eran es esa esas ese eso esos esta estas
este esto estos fue fueron ha han hay hasta la las le les lo los mas me mi mis mucho muy ni no nos nosotros o
otra otras otro otros para pero poco por porque que quien quienes se sea sean segun ser si sido sin sobre son su
sus tambien tan te ti tiene tienen todo todos tu tus un una unas uno unos y ya yo
""".split())

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_CLOSING_HASHES = re.compile(r"[ \t]+#+$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")


def normalize_words(text: str) -> set[str]:
    """Minúsculas, sin acentos, solo [a-z0-9]+ y sin palabras vacías.

    Los símbolos de la notación matemática (√, π, ², ...) no son palabras: se descartan,
    salvo los que NFKD vuelve dígitos o letras ('x²' queda 'x2').
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return {w for w in re.findall(r"[a-z0-9]+", plain) if w not in STOPWORDS}


_PAGE_MARK = re.compile(r"\s*\((?:pág|diap)\. \d+\)$")


def summary_headings(summary_md: str) -> list[str]:
    """Los títulos Markdown (# a ######), sin los # ni los ** ni el "(pág. N)" o "(diap. N)" final, en orden."""
    headings: list[str] = []
    in_code = False
    for line in summary_md.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        title = _CLOSING_HASHES.sub("", m.group(1)).replace("**", "").strip()
        # La marca de página de un documento ("Límites (pág. 14)") no es parte del título.
        title = _PAGE_MARK.sub("", title).strip()
        if title:
            headings.append(title)
    return headings


def same_word(a: str, b: str) -> bool:
    """Iguales, o la más corta (5 letras o más) es el comienzo de la larga."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 5 and long_.startswith(short)


@dataclass
class RecallHeading:
    title: str
    mentioned: bool


def recall_check(summary_md: str, text: str) -> list[RecallHeading]:
    """Un título cuenta como mencionado si aparece al menos la mitad (redondeada hacia arriba) de sus palabras."""
    said = normalize_words(text)
    result = []
    for title in summary_headings(summary_md):
        words = normalize_words(title)
        if not words:
            result.append(RecallHeading(title, True))
            continue
        hits = sum(1 for w in words if any(same_word(w, s) for s in said))
        result.append(RecallHeading(title, hits >= ceil(len(words) / 2)))
    return result
