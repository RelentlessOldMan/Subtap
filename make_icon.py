"""Generate subtap.ico from the app's favicon design (see FAVICON in subtap.py).

Pillow can't rasterize SVG, so we redraw the same shapes here -- a rounded dark
tile, four cyan waveform bars, and a yellow playhead -- coordinates copied 1:1
from the 32x32 SVG viewBox. We draw big and downscale each size with LANCZOS so
the rounded corners stay smooth. Re-run this whenever the favicon design changes.
"""
from PIL import Image, ImageDraw

# (x, y, w, h) in the 32x32 design space, plus corner radius and fill.
BG = (0, 0, 32, 32, 7, (27, 30, 39, 255))          # #1b1e27 rounded tile
CYAN = (79, 209, 255, 255)                          # #4fd1ff bars
YELLOW = (255, 210, 79, 255)                         # #ffd24f playhead
BARS = [                                             # (x, y, w, h, r)
    (5, 13, 3, 6, 1.5),
    (10, 10, 3, 12, 1.5),
    (19, 7, 3, 18, 1.5),
    (24, 12, 3, 8, 1.5),
]
PLAYHEAD = (14.75, 4, 2.5, 24, 1.25)

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 8  # supersample: render at size*SS, then shrink for antialiasing


def _rr(draw, box, r, s, fill):
    x, y, w, h = box
    draw.rounded_rectangle(
        [x * s, y * s, (x + w) * s, (y + h) * s], radius=r * s, fill=fill
    )


def render(size):
    s = size * SS / 32.0
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _rr(d, BG[:4], BG[4], s, BG[5])
    for x, y, w, h, r in BARS:
        _rr(d, (x, y, w, h), r, s, CYAN)
    x, y, w, h, r = PLAYHEAD
    _rr(d, (x, y, w, h), r, s, YELLOW)
    return img.resize((size, size), Image.LANCZOS)


def main():
    frames = [render(n) for n in SIZES]
    frames[0].save(
        "subtap.ico",
        format="ICO",
        sizes=[(n, n) for n in SIZES],
        append_images=frames[1:],
    )
    print("wrote subtap.ico with sizes:", SIZES)


if __name__ == "__main__":
    main()
