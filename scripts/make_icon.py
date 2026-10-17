"""Genera el ícono de Estudia (libro abierto, línea simple) en src/estudia/assets/.

Uso: uv run python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

INK = (42, 44, 46, 255)  # --ink de web/src/index.css
OUT = Path(__file__).resolve().parent.parent / "src" / "estudia" / "assets"
SCALE = 4  # se dibuja grande y se reduce para suavizar los bordes


def draw(size: int) -> Image.Image:
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(round(s * 0.06), SCALE)

    def p(x: float, y: float) -> tuple[float, float]:
        return (x * s, y * s)

    # Tapas: dos páginas que se abren desde el lomo central.
    left = [p(0.50, 0.30), p(0.30, 0.22), p(0.10, 0.28), p(0.10, 0.76), p(0.30, 0.70), p(0.50, 0.78)]
    right = [p(0.50, 0.30), p(0.70, 0.22), p(0.90, 0.28), p(0.90, 0.76), p(0.70, 0.70), p(0.50, 0.78)]
    for pts in (left, right):
        d.line(pts + [pts[0]], fill=INK, width=w, joint="curve")
    d.line([p(0.50, 0.30), p(0.50, 0.78)], fill=INK, width=w)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw(64).save(OUT / "icono.png")
    draw(256).save(OUT / "icono.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])


if __name__ == "__main__":
    main()
