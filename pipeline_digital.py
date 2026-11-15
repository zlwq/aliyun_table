#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from alibabacloud_docmind_api20220711.client import Client as DocMindClient
from alibabacloud_docmind_api20220711 import models as docmind_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models


ENDPOINT = "docmind-api.cn-hangzhou.aliyuncs.com"

SUPPORTED = {
    ".html", ".pdf", ".ppt", ".pptx", ".xlsx", ".xls",
    ".doc", ".docx", ".mobi", ".epub", ".md", ".markdown", ".txt",
}

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif",
}


# ============================================================
# 1. 阿里云 Document Mind：电子文档解析
# ============================================================

def to_plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, list):
        return [
            to_plain(x)
            for x in value
        ]

    if isinstance(value, dict):
        return {
            k: to_plain(v)
            for k, v in value.items()
        }

    if hasattr(value, "to_map"):
        return to_plain(
            value.to_map()
        )

    return str(value)


def make_client():
    load_dotenv()

    ak = os.getenv(
        "ALIBABA_CLOUD_ACCESS_KEY_ID"
    )
    sk = os.getenv(
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
    )

    if not ak or not sk:
        raise RuntimeError(
            ".env 中缺少 "
            "ALIBABA_CLOUD_ACCESS_KEY_ID / "
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )

    config = open_api_models.Config(
        access_key_id=ak,
        access_key_secret=sk,
    )
    config.endpoint = ENDPOINT

    return DocMindClient(config)


def parse_digital_document(
    client,
    path: Path,
):
    suffix = path.suffix.lower()

    if suffix in IMAGE_SUFFIXES:
        raise ValueError(
            "电子文档解析不支持图片。"
            "请改用文档智能解析 / 大模型版 / 表格智能解析。"
        )

    if suffix not in SUPPORTED:
        print(
            f"[警告] 扩展名 {suffix!r} "
            "不在当前已知支持列表中；"
            "仍尝试提交，由服务端最终判断。"
        )

    if suffix == ".pdf":
        print(
            "[提示] 电子文档解析只适用于"
            "有文本层的电子 PDF，不支持扫描版 PDF。"
        )

    with path.open("rb") as f:
        req = (
            docmind_models
            .SubmitDigitalDocStructureJobAdvanceRequest(
                file_url_object=f,
                file_name=path.name,
                reveal_markdown=True,
            )
        )

        runtime = util_models.RuntimeOptions()
        runtime.connect_timeout = 10000
        runtime.read_timeout = 300000

        resp = (
            client
            .submit_digital_doc_structure_job_advance(
                req,
                runtime,
            )
        )

    return to_plain(
        resp.body
    )


# ============================================================
# 2. 精简 JSON
# ============================================================

def simplify_layout(layout):
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

    if (
        "cells" in layout
        and isinstance(
            layout["cells"],
            list,
        )
    ):
        result["cells"] = [
            simplify_cell(cell)
            for cell in layout["cells"]
        ]

    if (
        "layouts" in layout
        and isinstance(
            layout["layouts"],
            list,
        )
    ):
        result["layouts"] = [
            simplify_layout(x)
            for x in layout["layouts"]
        ]

    return result


def simplify_cell(cell):
    if not isinstance(cell, dict):
        return cell

    result = {}

    for key in (
        "ysc",
        "yec",
        "xsc",
        "xec",
    ):
        if key in cell:
            result[key] = cell[key]

    if (
        "layouts" in cell
        and isinstance(
            cell["layouts"],
            list,
        )
    ):
        result["layouts"] = [
            simplify_layout(layout)
            for layout in cell["layouts"]
        ]

    return result


def find_layouts(data):
    if not isinstance(data, dict):
        raise ValueError(
            "输入 JSON 顶层不是对象"
        )

    if isinstance(
        data.get("Data"),
        dict,
    ):
        layouts = (
            data["Data"]
            .get("layouts")
        )

        if isinstance(
            layouts,
            list,
        ):
            return layouts

    layouts = data.get(
        "layouts"
    )

    if isinstance(
        layouts,
        list,
    ):
        return layouts

    raise ValueError(
        "JSON 中没有找到 layouts"
    )


def simplify_json(data):
    layouts = find_layouts(
        data
    )

    return {
        "layouts": [
            simplify_layout(layout)
            for layout in layouts
        ]
    }


# ============================================================
# 3. 从 simplified JSON 转成最终统一格式
#
# 最终固定格式：
#
# {
#   "row_count": ...,
#   "col_count": ...,
#   "cells": [
#     {
#       "text": ...,
#       "row": ...,
#       "col": ...,
#       "rowspan": ...,
#       "colspan": ...
#     }
#   ]
# }
#
# 无 color
# ============================================================

def find_table_layouts(obj):
    """
    递归寻找真正的表格 layout。

    判定：
    - numRow
    - numCol
    - cells
    """
    found = []

    if isinstance(
        obj,
        dict,
    ):
        if (
            "numRow" in obj
            and "numCol" in obj
            and isinstance(
                obj.get("cells"),
                list,
            )
        ):
            found.append(
                obj
            )

        for value in obj.values():
            found.extend(
                find_table_layouts(
                    value
                )
            )

    elif isinstance(
        obj,
        list,
    ):
        for item in obj:
            found.extend(
                find_table_layouts(
                    item
                )
            )

    return found


def extract_layout_text(layout):
    """
    递归提取 cell.layouts 中的文字。
    """
    if not isinstance(
        layout,
        dict,
    ):
        return ""

    text = layout.get(
        "text"
    )

    if text is not None:
        return str(
            text
        )

    nested = layout.get(
        "layouts"
    )

    if not isinstance(
        nested,
        list,
    ):
        return ""

    return "".join(
        extract_layout_text(item)
        for item in nested
    )


def extract_cell_text(cell):
    layouts = cell.get(
        "layouts",
        [],
    )

    if not isinstance(
        layouts,
        list,
    ):
        return ""

    text = "".join(
        extract_layout_text(layout)
        for layout in layouts
    )

    return text.strip()


def is_empty_placeholder_cell(cell):
    """
    电子文档解析偶尔会返回这种空占位：

        ysc = 某行
        yec = 同一行
        xsc = 0
        xec = -1
        layouts = []

    它不是真实单元格。

    只过滤这种“无文字 + 明显非法跨度”的空占位；
    如果非法 cell 里有文字，则不吞掉，后续直接报错。
    """
    if not isinstance(
        cell,
        dict,
    ):
        return False

    text = extract_cell_text(
        cell
    )

    if text:
        return False

    try:
        ysc = int(
            cell["ysc"]
        )
        yec = int(
            cell["yec"]
        )
        xsc = int(
            cell["xsc"]
        )
        xec = int(
            cell["xec"]
        )
    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return False

    return (
        ysc >= 0
        and yec == ysc
        and xsc == 0
        and xec == -1
    )


def convert_table_to_standard(
    table,
):
    declared_rows = int(
        table["numRow"]
    )
    declared_cols = int(
        table["numCol"]
    )

    if (
        declared_rows <= 0
        or declared_cols <= 0
    ):
        raise ValueError(
            "numRow / numCol 必须为正整数"
        )

    raw_cells = table.get(
        "cells",
        [],
    )

    if not isinstance(
        raw_cells,
        list,
    ):
        raise ValueError(
            "cells 不是数组"
        )

    cells = []

    max_yec = -1
    max_xec = -1

    skipped_placeholders = 0

    for i, cell in enumerate(
        raw_cells
    ):
        if is_empty_placeholder_cell(
            cell
        ):
            skipped_placeholders += 1
            continue

        try:
            ysc = int(
                cell["ysc"]
            )
            yec = int(
                cell["yec"]
            )
            xsc = int(
                cell["xsc"]
            )
            xec = int(
                cell["xec"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as e:
            raise ValueError(
                f"第 {i} 个 cell 的 "
                "ysc/yec/xsc/xec 无效"
            ) from e

        if (
            ysc < 0
            or xsc < 0
            or yec < ysc
            or xec < xsc
            or yec >= declared_rows
            or xec >= declared_cols
        ):
            raise ValueError(
                f"第 {i} 个 cell 坐标非法: "
                f"ysc={ysc}, "
                f"yec={yec}, "
                f"xsc={xsc}, "
                f"xec={xec}, "
                f"text={extract_cell_text(cell)!r}"
            )

        max_yec = max(
            max_yec,
            yec,
        )
        max_xec = max(
            max_xec,
            xec,
        )

        # 字段顺序严格按照最终约定。
        cells.append({
            "text": extract_cell_text(
                cell
            ),
            "row": ysc,
            "col": xsc,
            "rowspan": (
                yec - ysc + 1
            ),
            "colspan": (
                xec - xsc + 1
            ),
        })

    if not cells:
        raise ValueError(
            "过滤空占位后没有有效 cell"
        )

    # 再用四个坐标字段独立推导一次总行列数，
    # 与 numRow / numCol 做校验。
    inferred_rows = (
        max_yec + 1
    )
    inferred_cols = (
        max_xec + 1
    )

    if (
        inferred_rows
        != declared_rows
        or inferred_cols
        != declared_cols
    ):
        raise ValueError(
            "表格尺寸校验失败："
            f"numRow/numCol="
            f"{declared_rows}x{declared_cols}，"
            f"由 yec/xec 推导="
            f"{inferred_rows}x{inferred_cols}"
        )

    cells.sort(
        key=lambda c: (
            c["row"],
            c["col"],
        )
    )

    result = {
        "row_count": declared_rows,
        "col_count": declared_cols,
        "cells": cells,
    }

    validate_standard_table(
        result
    )

    if skipped_placeholders:
        print(
            f"[提示] 已忽略 "
            f"{skipped_placeholders} 个"
            "电子文档解析产生的空占位 cell"
        )

    return result


def validate_standard_table(
    data,
):
    rows = data[
        "row_count"
    ]
    cols = data[
        "col_count"
    ]

    occupied = [
        [
            None
            for _ in range(cols)
        ]
        for _ in range(rows)
    ]

    for i, cell in enumerate(
        data["cells"]
    ):
        r0 = cell["row"]
        c0 = cell["col"]
        rs = cell["rowspan"]
        cs = cell["colspan"]

        if (
            r0 < 0
            or c0 < 0
            or rs <= 0
            or cs <= 0
            or r0 + rs > rows
            or c0 + cs > cols
        ):
            raise ValueError(
                f"最终 JSON 中 cell#{i} 越界"
            )

        for r in range(
            r0,
            r0 + rs,
        ):
            for c in range(
                c0,
                c0 + cs,
            ):
                if (
                    occupied[r][c]
                    is not None
                ):
                    raise ValueError(
                        "最终 JSON 出现单元格重叠："
                        f"基础格 ({r}, {c}) "
                        f"同时属于 "
                        f"cell#{occupied[r][c]} "
                        f"和 cell#{i}"
                    )

                occupied[r][c] = i

    missing = [
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if occupied[r][c] is None
    ]

    if missing:
        raise ValueError(
            "最终 JSON 结构不完整："
            f"有 {len(missing)} 个基础格"
            "没有被任何单元格覆盖，"
            f"例如 {missing[:10]}"
        )

    print(
        "[结构检查] "
        "无重叠、无遗漏"
    )


def simplified_to_standard_tables(
    simplified,
):
    tables = find_table_layouts(
        simplified
    )

    if not tables:
        raise ValueError(
            "精简 JSON 中没有找到表格结构 "
            "(numRow / numCol / cells)"
        )

    return [
        convert_table_to_standard(
            table
        )
        for table in tables
    ]


# ============================================================
# 4. 保存 JSON
# ============================================================

def save_json(
    path: Path,
    data,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )


# ============================================================
# 5. 主程序
#
# 输出：
#
# xxx.digital_doc_structure.json
# xxx.digital_doc_structure.simplified.json
#
# 单表：
# xxx.table_standard.json
#
# 多表：
# xxx.table_1_standard.json
# xxx.table_2_standard.json
# ...
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "电子文档解析流水线："
            "原始 JSON -> 精简 JSON -> "
            "统一标准表格 JSON"
        )
    )

    parser.add_argument(
        "file",
        type=Path,
        help=(
            "本地电子文档；"
            "不支持图片和扫描 PDF"
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "outputs"
        ),
    )

    args = parser.parse_args()

    path = args.file

    if not path.is_file():
        raise FileNotFoundError(
            path
        )

    output_dir = (
        args.output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------- 1. 原始解析 ----------
    client = make_client()

    raw = parse_digital_document(
        client,
        path,
    )

    raw_path = (
        output_dir
        / (
            f"{path.stem}."
            "digital_doc_structure.json"
        )
    )

    save_json(
        raw_path,
        raw,
    )

    print(
        f"[1/3 原始 JSON] "
        f"{raw_path}"
    )

    # ---------- 2. 精简 ----------
    simplified = simplify_json(
        raw
    )

    simplified_path = (
        output_dir
        / (
            f"{path.stem}."
            "digital_doc_structure."
            "simplified.json"
        )
    )

    save_json(
        simplified_path,
        simplified,
    )

    print(
        f"[2/3 精简 JSON] "
        f"{simplified_path}"
    )

    # ---------- 3. 转换标准格式 ----------
    standard_tables = (
        simplified_to_standard_tables(
            simplified
        )
    )

    if len(
        standard_tables
    ) == 1:
        standard_path = (
            output_dir
            / (
                f"{path.stem}."
                "table_standard.json"
            )
        )

        save_json(
            standard_path,
            standard_tables[0],
        )

        print(
            f"[3/3 最终标准 JSON] "
            f"{standard_path}"
        )

    else:
        print(
            f"[3/3] 共检测到 "
            f"{len(standard_tables)} 张表，"
            "分别保存："
        )

        for i, standard in enumerate(
            standard_tables,
            1,
        ):
            standard_path = (
                output_dir
                / (
                    f"{path.stem}."
                    f"table_{i}_standard.json"
                )
            )

            save_json(
                standard_path,
                standard,
            )

            print(
                f"  - {standard_path}"
            )


if __name__ == "__main__":
    main()