#!/usr/bin/env python3
import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from alibabacloud_docmind_api20220711.client import Client as DocMindClient
from alibabacloud_docmind_api20220711 import models as docmind_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models


ENDPOINT = "docmind-api.cn-hangzhou.aliyuncs.com"
POLL_SECONDS = 10
MAX_POLLS = 720

SUPPORTED = {
    ".pdf", ".doc", ".docx",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif",
}


# ============================================================
# 1. 调用阿里云 Document Mind：文档智能解析
# ============================================================

def to_plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [to_plain(x) for x in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if hasattr(value, "to_map"):
        return to_plain(value.to_map())
    return str(value)


def get_any(d, *names, default=None):
    if not isinstance(d, dict):
        return default

    for name in names:
        if name in d:
            return d[name]

    lower = {str(k).lower(): v for k, v in d.items()}

    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]

    return default


def make_client():
    load_dotenv()

    ak = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    sk = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")

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


def parse_document(client, path: Path):
    with path.open("rb") as f:
        req = docmind_models.SubmitDocStructureJobAdvanceRequest(
            file_url_object=f,
            file_name=path.name,
        )

        runtime = util_models.RuntimeOptions()
        runtime.connect_timeout = 10000
        runtime.read_timeout = 300000

        submit_body = to_plain(
            client.submit_doc_structure_job_advance(
                req,
                runtime,
            ).body
        )

    task_id = get_any(
        get_any(submit_body, "Data", "data", default={}),
        "Id",
        "id",
    )

    if not task_id:
        raise RuntimeError(
            "提交成功但未找到任务 ID:\n"
            + json.dumps(
                submit_body,
                ensure_ascii=False,
                indent=2,
            )
        )

    print(f"[提交成功] {path.name}")
    print(f"[任务 ID] {task_id}")

    result_body = None

    for i in range(1, MAX_POLLS + 1):
        req = docmind_models.GetDocStructureResultRequest(
            id=task_id,
            reveal_markdown=True,
        )

        body = to_plain(
            client.get_doc_structure_result(req).body
        )

        completed = bool(
            get_any(
                body,
                "Completed",
                "completed",
                default=False,
            )
        )

        status = get_any(
            body,
            "Status",
            "status",
        )

        code = get_any(
            body,
            "Code",
            "code",
        )

        print(
            f"[轮询 {i}] "
            f"completed={completed} "
            f"status={status} "
            f"code={code}"
        )

        if completed:
            if (
                status
                and str(status).lower() != "success"
            ):
                raise RuntimeError(
                    "解析失败:\n"
                    + json.dumps(
                        body,
                        ensure_ascii=False,
                        indent=2,
                    )
                )

            result_body = body
            break

        time.sleep(POLL_SECONDS)

    if result_body is None:
        raise TimeoutError(
            "超过最大轮询次数，任务仍未完成"
        )

    return {
        "submit_response": submit_body,
        "result_response": result_body,
    }


# ============================================================
# 2. 精简文档智能解析 JSON
# ============================================================

KEEP_KEYS = {
    # 版面结构
    "layouts",
    "type",
    "pageNum",

    # 文本内容
    "text",
    "markdownContent",

    # 区域坐标
    "pos",
    "x",
    "y",

    # 表格结构
    "numCol",
    "numRow",
    "cells",
    "xsc",
    "ysc",
    "xec",
    "yec",
    "cellId",
    "tableConf",
}

DROP = object()


def decode_json_string(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass

    return value


def normalize_data(data):
    data = decode_json_string(data)

    if isinstance(data, list) and len(data) == 1:
        item = decode_json_string(data[0])

        if isinstance(item, dict):
            nested = get_any(
                item,
                "data",
                "Data",
            )

            if nested is not None:
                return decode_json_string(nested)

    return data


def extract_payload(root):
    if not isinstance(root, dict):
        return root

    result_response = get_any(
        root,
        "result_response",
    )

    if isinstance(result_response, dict):
        data = get_any(
            result_response,
            "Data",
            "data",
        )

        if data is not None:
            return normalize_data(data)

        return result_response

    data = get_any(
        root,
        "Data",
        "data",
    )

    if data is not None:
        return normalize_data(data)

    return root


def simplify(value, keep_primitive=False):
    if isinstance(value, dict):
        result = {}

        for key, child_value in value.items():
            selected = key in KEEP_KEYS

            child = simplify(
                child_value,
                keep_primitive=selected,
            )

            if child is DROP:
                continue

            result[key] = child

        return result if result else DROP

    if isinstance(value, list):
        result = []

        for item in value:
            child = simplify(
                item,
                keep_primitive=keep_primitive,
            )

            if child is not DROP:
                result.append(child)

        return result if result else DROP

    return value if keep_primitive else DROP


def cleanup(obj):
    if isinstance(obj, dict):
        result = {}

        for key, value in obj.items():
            value = cleanup(value)

            if value in ({}, []):
                continue

            result[key] = value

        return result

    if isinstance(obj, list):
        result = []

        for item in obj:
            item = cleanup(item)

            if item in ({}, []):
                continue

            result.append(item)

        return result

    return obj


def simplify_json(raw):
    payload = extract_payload(raw)

    simplified = simplify(payload)

    if simplified is DROP:
        simplified = {}

    return cleanup(simplified)


# ============================================================
# 3. 从 ysc / yec / xsc / xec 推出最终统一表格结构
#
# 推理规则：
#
# row     = ysc
# col     = xsc
# rowspan = yec - ysc + 1
# colspan = xec - xsc + 1
#
# row_count = 所有单元格 max(yec) + 1
# col_count = 所有单元格 max(xec) + 1
#
# 最终不输出 color
# ============================================================

def is_table_cell(cell):
    return (
        isinstance(cell, dict)
        and all(
            key in cell
            for key in (
                "ysc",
                "yec",
                "xsc",
                "xec",
            )
        )
    )


def find_table_layouts(obj):
    """
    递归寻找包含表格 cells 的 layout。

    不依赖 numRow / numCol。
    只要 cells 中存在 ysc/yec/xsc/xec，
    就认为它是一个表格候选。
    """
    found = []

    if isinstance(obj, dict):
        cells = obj.get("cells")

        if (
            isinstance(cells, list)
            and cells
            and all(
                is_table_cell(cell)
                for cell in cells
            )
        ):
            found.append(obj)

        for value in obj.values():
            found.extend(
                find_table_layouts(value)
            )

    elif isinstance(obj, list):
        for item in obj:
            found.extend(
                find_table_layouts(item)
            )

    return found


def extract_text_from_layout(layout):
    """
    从 layout 中递归取文字。

    如果当前层已有 text，就直接使用；
    否则继续向子 layouts 查找。
    """
    if not isinstance(layout, dict):
        return ""

    if "text" in layout:
        value = layout.get("text")

        if value is not None:
            return str(value)

    nested = layout.get("layouts")

    if not isinstance(nested, list):
        return ""

    return "".join(
        extract_text_from_layout(item)
        for item in nested
    )


def extract_cell_text(cell):
    layouts = cell.get("layouts", [])

    if not isinstance(layouts, list):
        return ""

    text = "".join(
        extract_text_from_layout(layout)
        for layout in layouts
    )

    return text.strip()


def infer_standard_table(table):
    raw_cells = table.get("cells")

    if not isinstance(raw_cells, list) or not raw_cells:
        raise ValueError(
            "表格中没有有效 cells"
        )

    parsed_cells = []

    max_yec = -1
    max_xec = -1

    for i, cell in enumerate(raw_cells):
        try:
            ysc = int(cell["ysc"])
            yec = int(cell["yec"])
            xsc = int(cell["xsc"])
            xec = int(cell["xec"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"第 {i} 个 cell 的 "
                "ysc/yec/xsc/xec 无效"
            ) from e

        if (
            ysc < 0
            or xsc < 0
            or yec < ysc
            or xec < xsc
        ):
            raise ValueError(
                f"第 {i} 个 cell 坐标非法: "
                f"ysc={ysc}, yec={yec}, "
                f"xsc={xsc}, xec={xec}"
            )

        max_yec = max(max_yec, yec)
        max_xec = max(max_xec, xec)

        # 字段顺序严格按照最终协议。
        parsed_cells.append({
            "text": extract_cell_text(cell),
            "row": ysc,
            "col": xsc,
            "rowspan": yec - ysc + 1,
            "colspan": xec - xsc + 1,
        })

    # 完全根据四个坐标字段推出行列总数。
    row_count = max_yec + 1
    col_count = max_xec + 1

    # 统一为稳定的行优先顺序。
    parsed_cells.sort(
        key=lambda cell: (
            cell["row"],
            cell["col"],
        )
    )

    result = {
        "row_count": row_count,
        "col_count": col_count,
        "cells": parsed_cells,
    }

    validate_standard_table(result)

    return result


def validate_standard_table(data):
    """
    严格验证最终结构：

    1. 不越界
    2. 不重叠
    3. 所有基础网格都必须被覆盖
    """
    rows = data["row_count"]
    cols = data["col_count"]

    if rows <= 0 or cols <= 0:
        raise ValueError(
            "推理得到的 row_count / col_count 非法"
        )

    occupied = [
        [None for _ in range(cols)]
        for _ in range(rows)
    ]

    for i, cell in enumerate(data["cells"]):
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

        for r in range(r0, r0 + rs):
            for c in range(c0, c0 + cs):
                previous = occupied[r][c]

                if previous is not None:
                    raise ValueError(
                        "检测到表格结构重叠："
                        f"基础格 ({r}, {c}) "
                        f"同时属于 cell#{previous} "
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
            "检测到表格结构不完整："
            f"有 {len(missing)} 个基础格没有被覆盖，"
            f"例如 {missing[:10]}"
        )


def simplified_to_standard_tables(simplified):
    """
    将精简 JSON 中检测到的所有表格
    全部转换为最终统一标准格式。
    """
    tables = find_table_layouts(
        simplified
    )

    if not tables:
        raise ValueError(
            "精简 JSON 中没有找到包含 "
            "ysc/yec/xsc/xec 的表格 cells"
        )

    print(
        f"[表格检测] 共找到 {len(tables)} 个表格"
    )

    standard_tables = []

    for i, table in enumerate(
        tables,
        1,
    ):
        standard = infer_standard_table(
            table
        )

        standard_tables.append(
            standard
        )

        print(
            f"[表格 {i}] "
            f"{standard['row_count']} × "
            f"{standard['col_count']}, "
            f"cells={len(standard['cells'])}"
        )

    return standard_tables


# ============================================================
# 4. 保存 JSON
# ============================================================

def save_json(path: Path, data):
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
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "文档智能解析流水线："
            "原始 JSON -> 精简 JSON -> "
            "通过 ysc/yec/xsc/xec 推出所有表格的统一标准 JSON"
        )
    )

    parser.add_argument(
        "file",
        type=Path,
        help="PDF / Word / 图片",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
    )

    args = parser.parse_args()

    path = args.file

    if not path.is_file():
        raise FileNotFoundError(path)

    if path.suffix.lower() not in SUPPORTED:
        print(
            f"[警告] 扩展名 {path.suffix!r} "
            "不在当前已知支持列表中；"
            "仍尝试提交，由服务端判断。"
        )

    output_dir = args.output_dir

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------- 第 1 步：模型原始结果 ----------
    client = make_client()

    raw = parse_document(
        client,
        path,
    )

    raw_path = (
        output_dir
        / f"{path.stem}.doc_structure.json"
    )

    save_json(
        raw_path,
        raw,
    )

    print(
        f"[1/3 原始 JSON] {raw_path}"
    )

    # ---------- 第 2 步：精简 ----------
    simplified = simplify_json(
        raw
    )

    simplified_path = (
        output_dir
        / f"{path.stem}.doc_structure.simplified.json"
    )

    save_json(
        simplified_path,
        simplified,
    )

    print(
        f"[2/3 精简 JSON] {simplified_path}"
    )

    # ---------- 第 3 步：推理所有表格的最终标准结构 ----------
    standard_tables = simplified_to_standard_tables(
        simplified
    )

    if len(standard_tables) == 1:
        standard_path = (
            output_dir
            / f"{path.stem}.table_standard.json"
        )

        save_json(
            standard_path,
            standard_tables[0],
        )

        print(
            f"[3/3 最终标准 JSON] {standard_path}"
        )

    else:
        print(
            f"[3/3] 共 {len(standard_tables)} 个表格，"
            "分别保存为："
        )

        for i, standard in enumerate(
            standard_tables,
            1,
        ):
            standard_path = (
                output_dir
                / f"{path.stem}.table_{i}_standard.json"
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