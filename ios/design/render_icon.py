"""Render app-icon.svg to the app's 1024 px icon and to previews.

Run with a throwaway venv, never the app's dependencies:

    python3 -m venv /tmp/icon-venv && /tmp/icon-venv/bin/pip install cairosvg pillow
    /tmp/icon-venv/bin/python ios/design/render_icon.py

Writes PendantDirect/Assets.xcassets/AppIcon.appiconset/icon-1024.png (opaque RGB, as the
App Store requires) and previews/icon-1024.png and previews/icon-60.png.
"""
import io
from pathlib import Path

import cairosvg
from PIL import Image

HERE = Path(__file__).resolve().parent
SVG = HERE / "app-icon.svg"
ICON = HERE.parent / "PendantDirect/Assets.xcassets/AppIcon.appiconset/icon-1024.png"
PREVIEWS = HERE / "previews"


def render(size: int) -> Image.Image:
    png = cairosvg.svg2png(url=str(SVG), output_width=size, output_height=size)
    rgba = Image.open(io.BytesIO(png)).convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba, mask=rgba.split()[3])
    return flat


def main() -> None:
    big = render(1024)
    big.save(ICON, optimize=True)
    PREVIEWS.mkdir(exist_ok=True)
    big.save(PREVIEWS / "icon-1024.png", optimize=True)
    # 60 pt is the home-screen size; downsample the 1024 render like iOS does.
    big.resize((60, 60), Image.LANCZOS).save(PREVIEWS / "icon-60.png", optimize=True)
    print(f"wrote {ICON}, {PREVIEWS / 'icon-1024.png'}, {PREVIEWS / 'icon-60.png'}")


if __name__ == "__main__":
    main()
