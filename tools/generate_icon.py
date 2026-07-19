"""Rebuild app/assets/app.ico from app/assets/app_preview.png.

Dev helper only. End users do not need this.
  python tools/generate_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "assets"
MASTER = OUT / "app_preview.png"
ICO = OUT / "app.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    if not MASTER.is_file():
        raise SystemExit(f"Missing master image: {MASTER}")
    master = Image.open(MASTER).convert("RGBA")
    if master.size != (256, 256):
        master = master.resize((256, 256), Image.Resampling.LANCZOS)
        master.save(MASTER)
    master.save(ICO, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"Wrote {ICO} ({ICO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
