"""Turn images/jarvis.png into the app icon.

Produces a multi-resolution .ico (Windows picks the size it needs for the
titlebar, taskbar and alt-tab) plus a square PNG the HUD embeds. Re-run after
replacing the source image.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from jarvis.config import ROOT  # noqa: E402

SRC = ROOT / "images" / "jarvis.png"
ICO = ROOT / "assets" / "jarvis.ico"
PNG = ROOT / "assets" / "jarvis.png"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _square(img: Image.Image) -> Image.Image:
    """Pad to a transparent square so nothing is cropped or stretched."""
    img = img.convert("RGBA")
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def main() -> int:
    if not SRC.is_file():
        print(f"source image not found: {SRC}")
        return 1

    ICO.parent.mkdir(parents=True, exist_ok=True)
    square = _square(Image.open(SRC))

    # A 256px master PNG for the HUD; the .ico carries every size Windows asks
    # for so the small titlebar rendering stays crisp instead of downscaling
    # 256 -> 16 on the fly.
    square.resize((256, 256), Image.LANCZOS).save(PNG)
    square.save(ICO, sizes=[(s, s) for s in ICO_SIZES])

    print(f"  wrote {ICO} ({', '.join(str(s) for s in ICO_SIZES)} px)")
    print(f"  wrote {PNG} (256 px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
