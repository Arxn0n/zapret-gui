"""Генерирует assets/icon.ico и assets/icon.png. Запуск: python tools/make_icon.py (нужен Pillow)."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
OUT = Path(__file__).resolve().parent.parent / "assets"


def make() -> Image.Image:
    # фон: скруглённый квадрат с вертикальным градиентом
    grad = Image.new("RGBA", (S, S))
    top, bot = (75, 139, 255), (37, 89, 196)
    px = grad.load()
    for y in range(S):
        t = y / (S - 1)
        c = tuple(round(a + (b - a) * t) for a, b in zip(top, bot)) + (255,)
        for x in range(S):
            px[x, y] = c
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    cx, cy, r, w = 512, 520, 270, 76
    box = (cx - r, cy - r, cx + r, cy + r)
    d.arc(box, 305, 360, fill=white, width=w)        # символ питания: кольцо с разрывом сверху
    d.arc(box, 0, 235, fill=white, width=w)
    import math
    for ang in (305, 235):                            # скруглённые концы дуги
        a = math.radians(ang)
        ex, ey = cx + (r - w / 2) * math.cos(a), cy + (r - w / 2) * math.sin(a)
        d.ellipse((ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2), fill=white)
    d.line((cx, 235, cx, cy - 20), fill=white, width=w)   # вертикальная черта
    for y in (235, cy - 20):
        d.ellipse((cx - w / 2, y - w / 2, cx + w / 2, y + w / 2), fill=white)

    # зелёный индикатор «работает»
    bx, by = 782, 782
    d.ellipse((bx - 170, by - 170, bx + 170, by + 170), fill=(31, 63, 143, 255))
    d.ellipse((bx - 135, by - 135, bx + 135, by + 135), fill=(46, 204, 113, 255))
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    big = make()
    big.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    big.resize((256, 256), Image.LANCZOS).save(
        OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("saved", OUT)
