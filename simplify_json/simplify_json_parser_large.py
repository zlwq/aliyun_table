#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def simplify_layout(layout):
    """
    精简 layout。

    删除：
    - type
    - pageNum

    保留：
    - text
    - markdownContent
    - numCol
    - numRow
    - cells
    - layouts
    """
    if not isinstance(layout, dict):
        return layout

    result = {}

    for key in (
        "text",
        "markdownContent",
        "numCol",
        "numRow",
    ):
        if key in layout:
            result[key] = layout[key]

    if "cells" in layout and isinstance(layout["cells"], list):
        result["cells"] = [
            simplify_cell(cell)
            for cell in layout["cells"]
        ]

    if "layouts" in layout and isinstance(layout["layouts"], list):
        result["layouts"] = [
            simplify_layout(x)
            for x in layout["layouts"]
        ]

    return result


def simplify_cell(cell):
    """
    单元格只保留：
    - ysc
    - yec
    - xsc
    - xec
    - layouts
    """
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


def extract_layouts_from_chunk(chunk):
    """
    原始结构通常为：
    result_chunks -> Data -> layouts

    Data 只作为外壳，
    输出时直接去掉。
    """
    if not isinstance(chunk, dict):
        return []

    data = chunk.get("Data")

    if isinstance(data, dict):
        layouts = data.get("layouts", [])
    else:
        layouts = chunk.get("layouts", [])

    if not isinstance(layouts, list):
        return []

    return [
        simplify_layout(layout)
        for layout in layouts
    ]


def simplify_json(data):
    """
    最终只保留：

    result_chunks
    └── layouts
        ├── text
        ├── markdownContent
        ├── numCol
        ├── numRow
        ├── cells
        │   ├── ysc
        │   ├── yec
        │   ├── xsc
        │   ├── xec
        │   └── layouts
        │       ├── text
        │       ├── markdownContent
        │       ├── numCol
        │       ├── numRow
        │       ├── cells
        │       └── layouts
        └── layouts

    已删除：
    - type
    - pageNum
    - Data
    - style
    - pos
    - uniqueId
    - cellId
    - alignment
    - index
    - subType
    - 以及任务状态和其他元数据
    """
    result_chunks = data.get("result_chunks")

    if not isinstance(result_chunks, list):
        raise ValueError("JSON 中没有找到有效的 result_chunks")

    simplified_chunks = []

    for chunk in result_chunks:
        simplified_chunks.append({
            "layouts": extract_layouts_from_chunk(chunk)
        })

    return {
        "result_chunks": simplified_chunks
    }


def main():
    parser = argparse.ArgumentParser(
        description="精简阿里云文档解析大模型版 JSON，只保留比赛需要的核心内容"
    )

    parser.add_argument(
        "input_json",
        type=Path,
        help="原始 doc_parser_large.json 文件路径",
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

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            simplified,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"[完成] 已保存精简 JSON: {output_path}"
    )


if __name__ == "__main__":
    main()