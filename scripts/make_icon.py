"""Genera el ícono de Estudia (libro abierto, línea simple) en src/estudia/assets/.

Uso: uv run python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

INK = (42, 44, 46, 255)  # --ink de web/src/index.css
LIGHT = (236, 234, 230, 255)  # trazo claro de la bandeja
HALO = (42, 44, 46, 153)  # contorno de 1 px, ~60% de opacidad
OUT = Path(__file__).resolve().parent.parent / "src" / "estudia" / "assets"
SCALE = 4  # se dibuja grande y se reduce para suavizar los bordes


def draw(size: int, tray: bool = False) -> Image.Image:
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(round(s * 0.06), SCALE)

    def p(x: float, y: float) -> tuple[float, float]:
        return (x * s, y * s)

    # Tapas: dos páginas que se abren desde el lomo central.
    left = [p(0.50, 0.30), p(0.30, 0.22), p(0.10, 0.28), p(0.10, 0.76), p(0.30, 0.70), p(0.50, 0.78)]
    right = [p(0.50, 0.30), p(0.70, 0.22), p(0.90, 0.28), p(0.90, 0.76), p(0.70, 0.70), p(0.50, 0.78)]
    strokes = [left + [left[0]], right + [right[0]], [p(0.50, 0.30), p(0.50, 0.78)]]
    if tray:
        # Contorno oscuro de 1 px debajo del trazo claro: se lee en barras claras y oscuras.
        for pts in strokes:
            d.line(pts, fill=HALO, width=w + 2 * SCALE * max(size // 64, 1), joint="curve")
    for pts in strokes:
        d.line(pts, fill=LIGHT if tray else INK, width=w, joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw(64).save(OUT / "icono.png")
    draw(64, tray=True).save(OUT / "icono-bandeja.png")
    draw(256).save(OUT / "icono.ico", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])


if __name__ == "__main__":
    main()
