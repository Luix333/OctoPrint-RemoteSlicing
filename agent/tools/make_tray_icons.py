"""Draws the agent's tray icons into remote_slicing_agent/icons/ (needs Pillow; the agent itself doesn't).

A rounded square in the state's colour with three white layers, the bottom one widest: a sliced part. Every size is
drawn on its own at 8x and scaled down, so the 16 px one stays sharp rather than being a shrunk 64 px one.

    python agent/tools/make_tray_icons.py
"""
import os

from PIL import Image, ImageDraw

STATES = {
    "online": ((38, 120, 220), (255, 255, 255)),
    "slicing": ((232, 140, 20), (255, 255, 255)),
    "offline": ((120, 126, 134), (226, 229, 232)),
}
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
SCALE = 8
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "remote_slicing_agent", "icons")


def draw(size, background, layers):
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inset = round(size * 0.04) * SCALE
    d.rounded_rectangle((inset, inset, s - 1 - inset, s - 1 - inset), radius=round(s * 0.22), fill=background)
    # whole target pixels, so the layers stay crisp at 16 px
    bar, gap = max(2, round(size * 0.14)), max(1, round(size * 0.075))
    top = (size - (3 * bar + 2 * gap)) // 2
    for i, fraction in enumerate((0.38, 0.55, 0.72)):
        width = max(2, round(size * fraction / 2) * 2 - size % 2)  # same parity as the size: centred exactly
        x, y = (size - width) // 2, top + i * (bar + gap)
        d.rounded_rectangle((x * SCALE, y * SCALE, (x + width) * SCALE - 1, (y + bar) * SCALE - 1),
                            radius=bar * SCALE / 2.6, fill=layers)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for state, (background, layers) in STATES.items():
        images = [draw(size, background, layers) for size in SIZES]
        path = os.path.join(OUT, state + ".ico")
        images[-1].save(path, format="ICO", sizes=[(n, n) for n in SIZES], append_images=images[:-1])
        print(path, sorted(Image.open(path).info["sizes"]))


if __name__ == "__main__":
    main()
