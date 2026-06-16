# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the AutoClip app icon: an orange circle with white 'AC'.
Writes a multi-size autoclip/gui/autoclip.ico used by the tray, window, exe, installer.

Requires Pillow:  py -m pip install pillow
"""
import os
from PIL import Image, ImageDraw, ImageFont

ORANGE = (255, 76, 0, 255)     # theme accent #ff4c00
WHITE = (255, 255, 255, 255)
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
OUT = os.path.join(os.path.dirname(__file__), "..", "autoclip", "gui", "autoclip.ico")


def render(size: int) -> Image.Image:
    # supersample for smooth edges, then downscale
    ss = size * 4
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = ss // 16
    d.ellipse([m, m, ss - 1 - m, ss - 1 - m], fill=ORANGE)
    for fp in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf"):
        try:
            font = ImageFont.truetype(fp, int(ss * 0.44))
            break
        except OSError:
            font = ImageFont.load_default()
    text = "AC"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((ss - tw) / 2 - bbox[0], (ss - th) / 2 - bbox[1]), text, fill=WHITE, font=font)
    return img.resize((size, size), Image.LANCZOS)


def main():
    base = render(256)
    imgs = [render(s) for (s, _) in SIZES]
    base.save(os.path.abspath(OUT), format="ICO", sizes=SIZES, append_images=imgs)
    print("wrote", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
