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

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES

# pos 坐标通常会有 1~2 像素抖动。
# 程序会在 0~10 像素内自动寻找能还原出正确网格数量的聚类容差。
MAX_BOUNDARY_TOLERANCE = 10


# ============================================================
# 1. 阿里云 Table Understanding
#    自动识别图片 / PDF
# ============================================================

def make_client() -> DocMindClient:
    load_dotenv()

    required = [
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    ]
    missing = [name for name in required if not os.getenv(name)]

    if missing:
        raise RuntimeError(
            "缺少环境变量: "
            + ", ".join(missing)
            + "\n请确认它们已经写入当前目录的 .env"
        )

    config = open_api_models.Config(
        access_key_id=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID"),
        access_key_secret=os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
    )
    config.endpoint = ENDPOINT

    return DocMindClient(config)


def to_plain(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
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


def detect_file_type(file_path: Path) -> str:
    suffix = file_path.suffix.lower()

    if suffix in IMAGE_SUFFIXES:
        return "image"

    if suffix in PDF_SUFFIXES:
        return "pdf"

    raise ValueError(
        f"不支持的文件类型: {file_path}\n"
        f"当前只支持: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
    )


def submit_file(client: DocMindClient, file_path: Path) -> str:
    with file_path.open("rb") as f:
        request = docmind_models.SubmitTableUnderstandingJobAdvanceRequest(
            file_url_object=f,
            file_name=file_path.name,
        )

        runtime = util_models.RuntimeOptions()

        response = client.submit_table_understanding_job_advance(
            request,
            runtime,
        )

    body = to_plain(response.body)
    data = get_any(body, "Data", "data", default={})
    task_id = get_any(data, "Id", "id")

    if not task_id:
        raise RuntimeError(
            "任务提交成功返回中没有找到任务 ID。\n"
            + json.dumps(
                body,
                ensure_ascii=False,
                indent=2,
            )
        )

    print(f"[提交成功] {file_path.name}")
    print(f"[文件类型] {detect_file_type(file_path)}")
    print(f"[任务 ID] {task_id}")

    return task_id


def poll_result(client: DocMindClient, task_id: str):
    request = docmind_models.GetTableUnderstandingResultRequest(
        id=task_id
    )

    for i in range(1, MAX_POLLS + 1):
        response = client.get_table_understanding_result(request)
        body = to_plain(response.body)

        completed = bool(
            get_any(
                body,
                "Completed",
                "completed",
                default=False,
            )
        )
        code = get_any(body, "Code", "code")
        status = get_any(body, "Status", "status")
        message = get_any(body, "Message", "message")

        print(
            f"[轮询 {i}] completed={completed} "
            f"status={status} code={code}"
        )

        if completed:
            if status and str(status).lower() != "success":
                raise RuntimeError(
                    f"解析失败: "
                    f"status={status}, "
                    f"code={code}, "
                    f"message={message}"
                )

            return body

        time.sleep(POLL_SECONDS)

    raise TimeoutError(
        "超过最大轮询次数，仍未得到解析结果。"
    )


def decode_data(body):
    data = get_any(body, "Data", "data")

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data

    if isinstance(data, list) and len(data) == 1:
        item = data[0]

        if isinstance(item, dict):
            nested = get_any(
                item,
                "data",
                "Data",
            )

            if nested is not None:
                return nested

    return data


def run_table_understanding(
    client: DocMindClient,
    file_path: Path,
):
    task_id = submit_file(
        client,
        file_path,
    )

    body = poll_result(
        client,
        task_id,
    )

    data = decode_data(body)

    return {
        "response": body,
        "parsed_data": data,
    }


# ============================================================
# 2. 只保留生成最终标准 JSON 真正需要的信息
#
# 必要字段：
# - tables
# - tableName
# - sheetNumber
# - cells
# - rowNumber
# - colNumber
# - content
# - pos
#
# mergedCell / firstMergedCell 不用于结构推理。
# 合并范围由 pos 的真实几何边界恢复。
# ============================================================

def find_tables(obj):
    found = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if (
                str(key).lower() == "tables"
                and isinstance(value, list)
            ):
                found.extend(value)
            else:
                found.extend(
                    find_tables(value)
                )

    elif isinstance(obj, list):
        for item in obj:
            found.extend(
                find_tables(item)
            )

    return found


def simplify_table_data(raw):
    parsed_data = get_any(
        raw,
        "parsed_data",
    )

    if parsed_data is None:
        response = get_any(
            raw,
            "response",
            default={},
        )

        parsed_data = decode_data(
            response
        )

    tables = find_tables(
        parsed_data
    )

    simplified_tables = []

    for table in tables:
        raw_cells = get_any(
            table,
            "cells",
            default=[],
        ) or []

        cells = []

        for cell in raw_cells:
            if not isinstance(cell, dict):
                continue

            row_number = get_any(
                cell,
                "rowNumber",
                "row_number",
            )
            col_number = get_any(
                cell,
                "colNumber",
                "col_number",
            )
            content = get_any(
                cell,
                "content",
                default="",
            )
            pos = get_any(
                cell,
                "pos",
            )

            if (
                row_number is None
                or col_number is None
                or pos is None
            ):
                continue

            cells.append({
                "rowNumber": row_number,
                "colNumber": col_number,
                "content": content,
                "pos": pos,
            })

        simplified_tables.append({
            "sheetNumber": get_any(
                table,
                "sheetNumber",
                "sheet_number",
            ),
            "tableName": get_any(
                table,
                "tableName",
                "table_name",
                default="",
            ),
            "cells": cells,
        })

    return {
        "tables": simplified_tables
    }


# ============================================================
# 3. 从 pos + rowNumber / colNumber
#    推导 row / col / rowspan / colspan
#
# 重要：
# Table Understanding 中 rowNumber / colNumber 对合并单元格
# 表示的是结束行 / 结束列，而不是左上角。
#
# 因此：
# - rowNumber / colNumber 用于确定总行列数和校验结束索引
# - pos 用于恢复左上角边界和跨度
# ============================================================

def extract_points(pos):
    """
    兼容常见 pos 格式：

    1.
    [
      {"x": 1, "y": 2},
      ...
    ]

    2.
    [
      [x1, y1, x2, y2, x3, y3, x4, y4]
    ]

    3.
    [[x, y], ...]
    """
    points = []

    if not isinstance(pos, list):
        return points

    for item in pos:
        if isinstance(item, dict):
            if "x" in item and "y" in item:
                points.append(
                    (
                        float(item["x"]),
                        float(item["y"]),
                    )
                )

        elif isinstance(item, list):
            if (
                len(item) == 2
                and all(
                    isinstance(v, (int, float))
                    for v in item
                )
            ):
                points.append(
                    (
                        float(item[0]),
                        float(item[1]),
                    )
                )

            elif (
                len(item) >= 8
                and len(item) % 2 == 0
                and all(
                    isinstance(v, (int, float))
                    for v in item
                )
            ):
                for i in range(
                    0,
                    len(item),
                    2,
                ):
                    points.append(
                        (
                            float(item[i]),
                            float(item[i + 1]),
                        )
                    )

    return points


def get_bbox(pos):
    points = extract_points(pos)

    if not points:
        raise ValueError(
            "cell.pos 中没有有效坐标"
        )

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    return (
        min(xs),
        min(ys),
        max(xs),
        max(ys),
    )


def cluster_values(values, tolerance):
    """
    把相差不超过 tolerance 像素的坐标
    视为同一条表格边界线。
    """
    if not values:
        return []

    values = sorted(
        float(v)
        for v in values
    )

    groups = [[values[0]]]

    for value in values[1:]:
        if (
            value - groups[-1][-1]
            <= tolerance
        ):
            groups[-1].append(value)
        else:
            groups.append([value])

    return [
        sum(group) / len(group)
        for group in groups
    ]


def choose_boundaries(
    values,
    expected_count,
    axis_name,
):
    """
    自动尝试不同像素容差。

    目标是让聚类后的边界数量
    恰好等于 row_count+1 或 col_count+1。
    """
    for tolerance in range(
        0,
        MAX_BOUNDARY_TOLERANCE + 1,
    ):
        boundaries = cluster_values(
            values,
            tolerance,
        )

        if len(boundaries) == expected_count:
            print(
                f"[{axis_name}边界] "
                f"容差={tolerance}px, "
                f"边界数={len(boundaries)}"
            )

            return boundaries

    raise ValueError(
        f"无法从 pos 稳定恢复 {axis_name} 网格："
        f"期望 {expected_count} 条边界线。"
    )


def nearest_boundary_index(
    boundaries,
    value,
):
    return min(
        range(len(boundaries)),
        key=lambda i: abs(
            boundaries[i] - value
        ),
    )


def infer_standard_table(table):
    raw_cells = table.get(
        "cells",
        [],
    )

    if not raw_cells:
        raise ValueError(
            "表格没有有效 cells"
        )

    row_numbers = []
    col_numbers = []
    bboxes = []

    for i, cell in enumerate(raw_cells):
        try:
            row_number = int(
                cell["rowNumber"]
            )
            col_number = int(
                cell["colNumber"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as e:
            raise ValueError(
                f"cell#{i} 的 "
                "rowNumber / colNumber 无效"
            ) from e

        if (
            row_number < 0
            or col_number < 0
        ):
            raise ValueError(
                f"cell#{i} 的行列索引为负数"
            )

        bbox = get_bbox(
            cell.get("pos")
        )

        row_numbers.append(
            row_number
        )
        col_numbers.append(
            col_number
        )
        bboxes.append(
            bbox
        )

    # rowNumber / colNumber 是结束索引，
    # 所以最大值 + 1 就是基础网格总行列数。
    row_count = max(
        row_numbers
    ) + 1
    col_count = max(
        col_numbers
    ) + 1

    x_values = []
    y_values = []

    for (
        x_min,
        y_min,
        x_max,
        y_max,
    ) in bboxes:
        x_values.extend(
            [x_min, x_max]
        )
        y_values.extend(
            [y_min, y_max]
        )

    x_boundaries = choose_boundaries(
        x_values,
        col_count + 1,
        "列",
    )

    y_boundaries = choose_boundaries(
        y_values,
        row_count + 1,
        "行",
    )

    cells = []

    for i, (
        cell,
        bbox,
    ) in enumerate(
        zip(
            raw_cells,
            bboxes,
        )
    ):
        (
            x_min,
            y_min,
            x_max,
            y_max,
        ) = bbox

        left = nearest_boundary_index(
            x_boundaries,
            x_min,
        )
        right = nearest_boundary_index(
            x_boundaries,
            x_max,
        )
        top = nearest_boundary_index(
            y_boundaries,
            y_min,
        )
        bottom = nearest_boundary_index(
            y_boundaries,
            y_max,
        )

        if (
            right <= left
            or bottom <= top
        ):
            raise ValueError(
                f"cell#{i} 的 pos 无法形成有效单元格"
            )

        row = top
        col = left
        rowspan = bottom - top
        colspan = right - left

        # 用接口给出的结束索引做强校验。
        expected_end_row = int(
            cell["rowNumber"]
        )
        expected_end_col = int(
            cell["colNumber"]
        )

        actual_end_row = (
            row + rowspan - 1
        )
        actual_end_col = (
            col + colspan - 1
        )

        if (
            actual_end_row
            != expected_end_row
            or actual_end_col
            != expected_end_col
        ):
            raise ValueError(
                f"cell#{i} 结构校验失败："
                f"pos 推出结束位置="
                f"({actual_end_row}, {actual_end_col})，"
                f"接口 rowNumber/colNumber="
                f"({expected_end_row}, {expected_end_col})"
            )

        text = str(
            cell.get(
                "content",
                "",
            )
        ).strip()

        # 字段顺序严格遵守最终协议。
        cells.append({
            "text": text,
            "row": row,
            "col": col,
            "rowspan": rowspan,
            "colspan": colspan,
        })

    cells.sort(
        key=lambda c: (
            c["row"],
            c["col"],
        )
    )

    standard = {
        "row_count": row_count,
        "col_count": col_count,
        "cells": cells,
    }

    validate_standard_table(
        standard
    )

    return standard


def validate_standard_table(data):
    """
    检查：
    - 越界：直接报错
    - 重叠：直接报错
    - 缺失基础格：警告，但不凭空生成单元格

    缺失时仍保存结果，
    因为这表示上游模型可能漏识别了某个单元格。
    """
    rows = data["row_count"]
    cols = data["col_count"]

    occupied = [
        [None for _ in range(cols)]
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
        print(
            f"[警告] 上游模型可能漏识别："
            f"{len(missing)} 个基础格未被任何 cell 覆盖。"
        )
        print(
            f"[警告] 示例缺失位置: "
            f"{missing[:10]}"
        )
    else:
        print(
            "[结构检查] "
            "无重叠、无遗漏"
        )


# ============================================================
# 4. 保存
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
#
# 输出：
# 1. xxx.table.json
# 2. xxx.table.simplified.json
# 3. 单表：
#       xxx.table_standard.json
#    多表：
#       xxx.table_1_standard.json
#       xxx.table_2_standard.json
#       ...
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Table Understanding 流水线："
            "图片/PDF -> 原始 JSON -> "
            "必要字段精简 -> 最终标准表格 JSON"
        )
    )

    parser.add_argument(
        "file",
        type=Path,
        help="PNG/JPG/JPEG/PDF",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="输出目录，默认 outputs",
    )

    args = parser.parse_args()

    file_path = args.file

    if not file_path.is_file():
        raise FileNotFoundError(
            file_path
        )

    detect_file_type(
        file_path
    )

    output_dir = args.output_dir

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------- 1. 调用模型 ----------
    client = make_client()

    raw = run_table_understanding(
        client,
        file_path,
    )

    raw_path = (
        output_dir
        / f"{file_path.stem}.table.json"
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
    simplified = simplify_table_data(
        raw
    )

    simplified_path = (
        output_dir
        / f"{file_path.stem}.table.simplified.json"
    )

    save_json(
        simplified_path,
        simplified,
    )

    print(
        f"[2/3 精简 JSON] "
        f"{simplified_path}"
    )

    # ---------- 3. 转换为最终标准 ----------
    tables = simplified.get(
        "tables",
        [],
    )

    if not tables:
        raise ValueError(
            "没有检测到任何表格"
        )

    standard_tables = []

    for i, table in enumerate(
        tables,
        1,
    ):
        print()
        print(
            f"[转换表格 {i}/{len(tables)}] "
            f"{table.get('tableName', '')}"
        )

        standard = infer_standard_table(
            table
        )

        standard_tables.append(
            standard
        )

    if len(standard_tables) == 1:
        standard_path = (
            output_dir
            / f"{file_path.stem}.table_standard.json"
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
            "分别保存为标准 JSON："
        )

        for i, standard in enumerate(
            standard_tables,
            1,
        ):
            standard_path = (
                output_dir
                / (
                    f"{file_path.stem}."
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
