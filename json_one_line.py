#!/usr/bin/env python3

import json
import sys
from pathlib import Path


if len(sys.argv) != 2:
    print("用法: python json_one_line.py 输入.json")
    sys.exit(1)

input_path = Path(sys.argv[1])

if not input_path.is_file():
    raise FileNotFoundError(f"找不到文件: {input_path}")

with input_path.open("r", encoding="utf-8") as f:
    data = json.load(f)

output_path = Path(__file__).resolve().parent / "compact.json"

with output_path.open("w", encoding="utf-8") as f:
    json.dump(
        data,
        f,
        ensure_ascii=False,
        separators=(",", ":"),
    )

print(f"已输出: {output_path}")