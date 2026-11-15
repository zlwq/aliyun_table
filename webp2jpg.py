#!/usr/bin/env python3
import sys
from pathlib import Path
from PIL import Image

if len(sys.argv) != 2:
    print("用法: python webp2jpg.py <file.webp>")
    sys.exit(1)

webp_path = Path(sys.argv[1])

if not webp_path.is_file():
    raise FileNotFoundError(webp_path)

jpg_path = webp_path.with_suffix(".jpg")

with Image.open(webp_path) as img:
    img.convert("RGB").save(jpg_path, "JPEG", quality=95)

print(jpg_path)