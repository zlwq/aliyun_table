#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def simplify_layout(layout):
    if not isinstance(layout, dict):
        return layout

    result = {}

    for key in ("text", "markdownContent", "numCol", "numRow"):
        if key in layout:
            result[key] = layout[key]

    if "cells" in layout and isinstance(layout["cells"], list):
        result["cells"] = [simplify_cell(cell) for cell in layout["cells"]]

    if "layouts" in layout and isinstance(layout["layouts"], list):
        result["layouts"] = [simplify_layout(x) for x in layout["layouts"]]

    return result


def simplify_cell(cell):
    if not isinstance(cell, dict):
        return cell

    result = {}

    for key in ("ysc", "yec", "xsc", "xec"):
        if key in cell:
            result[key] = cell[key]

    if "layouts" in cell and isinstance(cell["layouts"], list):
        result["layouts"] = [
            simplify_layout(layout)
            for layout in cell["layouts"]
        ]

    return result


def find_layouts(data):
    if not isinstance(data, dict):
        raise ValueError("输入 JSON 顶层不是对象")

    if isinstance(data.get("Data"), dict):
        layouts = data["Data"].get("layouts")
        if isinstance(layouts, list):
            return layouts

    layouts = data.get("layouts")
    if isinstance(layouts, list):
        return layouts

    raise ValueError("JSON 中没有找到 layouts")


def simplify_json(data):
    layouts = find_layouts(data)

    return {
        "layouts": [
            simplify_layout(layout)
            for layout in layouts
        ]
    }


def main():
    parser = argparse.ArgumentParser(
        description="精简阿里云电子文档解析 JSON，只保留表格问答核心结构"
    )
    parser.add_argument(
        "input_json",
        type=Path,
        help="原始 digital_doc_structure.json 文件路径",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出 JSON 路径；默认生成 *.simplified.json",
    )

    args = parser.parse_args()
    input_path = args.input_json

    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    if args.output is None:
        if input_path.suffix.lower() == ".json":
            output_path = input_path.with_name(
                input_path.stem + ".simplified.json"
            )
        else:
            output_path = input_path.with_name(
                input_path.name + ".simplified.json"
            )
    else:
        output_path = args.output

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    simplified = simplify_json(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            simplified,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"[完成] 已保存精简 JSON: {output_path}")


if __name__ == "__main__":
    main()
