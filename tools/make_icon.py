#!/usr/bin/env python3
"""Render the app icon: a round brushed-metal pendant with a glowing LED and
faint sound-wave arcs on a night-blue gradient. Writes a 1024x1024 PNG.

  /usr/bin/python3 tools/make_icon.py ios/PendantDirect/Assets.xcassets/AppIcon.appiconset/icon-1024.png
"""
import math
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 2048  # supersample, downscale at the end
C = S // 2


def radial(size, inner, outer, center=None, radius=None):
    cx, cy = center or (size // 2, size // 2)
    r = radius or size // 2
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = min(1.0, math.hypot(x - cx, y - cy) / r)
            px[x, y] = tuple(int(inner[i] + (outer[i] - inner[i]) * t) for i in range(3))
    return img


def main(out):
    small = 256  # gradients computed small, then upscaled (smooth anyway)
    bg = radial(small, (40, 52, 92), (8, 11, 24), center=(small * 0.5, small * 0.38), radius=small * 0.85)
    img = bg.resize((S, S), Image.BICUBIC).convert("RGBA")

    # Sound-wave arcs either side of the pendant.
    waves = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(waves)
    for i, r in enumerate((560, 700, 840)):
        alpha = 150 - i * 40
        box = (C - r, C - r, C + r, C + r)
        for start in (-38, 142):
            d.arc(box, start, start + 76, fill=(110, 220, 230, alpha), width=26)
    img = Image.alpha_composite(img, waves.filter(ImageFilter.GaussianBlur(3)))

    # Drop shadow.
    R = 400
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((C - R, C - R + 60, C + R, C + R + 60), fill=(0, 0, 0, 170))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(60)))

    # Metal body: radial highlight from upper-left, plus subtle circular brushing.
    body = radial(small, (236, 238, 242), (120, 126, 138), center=(small * 0.36, small * 0.3),
                  radius=small * 0.75).resize((S, S), Image.BICUBIC).convert("RGBA")
    brush = Image.new("L", (S, S), 0)
    bd = ImageDraw.Draw(brush)
    for k in range(0, R, 6):
        bd.ellipse((C - k, C - k, C + k, C + k), outline=18 if (k // 6) % 2 else 0, width=2)
    body = Image.composite(ImageChops.subtract(body, Image.new("RGBA", (S, S), (14, 14, 14, 0))), body, brush)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((C - R, C - R, C + R, C + R), fill=255)
    img.paste(body, (0, 0), mask)

    # Bevel: bright rim top-left, dark rim bottom-right, then a recessed face.
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rim)
    rd.arc((C - R, C - R, C + R, C + R), 150, 330, fill=(255, 255, 255, 170), width=14)
    rd.arc((C - R, C - R, C + R, C + R), -30, 150, fill=(40, 44, 54, 180), width=14)
    r2 = R - 70
    rd.ellipse((C - r2, C - r2, C + r2, C + r2), outline=(70, 76, 88, 150), width=10)
    rd.arc((C - r2, C - r2, C + r2, C + r2), -30, 150, fill=(255, 255, 255, 110), width=8)
    img = Image.alpha_composite(img, rim.filter(ImageFilter.GaussianBlur(2)))

    # LED: soft teal-white glow near the bottom of the face.
    ly = C + 220
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((C - 90, ly - 90, C + 90, ly + 90), fill=(120, 240, 245, 160))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(40)))
    dot = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(dot).ellipse((C - 26, ly - 26, C + 26, ly + 26), fill=(240, 255, 255, 255))
    img = Image.alpha_composite(img, dot.filter(ImageFilter.GaussianBlur(3)))

    img.convert("RGB").resize((1024, 1024), Image.LANCZOS).save(out)


if __name__ == "__main__":
    main(sys.argv[1])
