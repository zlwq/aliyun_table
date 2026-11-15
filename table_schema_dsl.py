#!/usr/bin/env python3
"""
Table Schema + Table DSL v1
===========================

目标：
1. 输入统一标准表格 JSON：
   {
     "row_count": ...,
     "col_count": ...,
     "cells": [
       {
         "text": "...",
         "row": 0,
         "col": 0,
         "rowspan": 1,
         "colspan": 1
       }
     ]
   }

2. 完全不调用 LLM，从表格拓扑 + 文本形态推导一个轻量 Table Schema。
   Schema 不保存正文数据，只保存：
   - 表格尺寸
   - 推断出的顶部表头行数
   - 表头单元格
   - 每一列的多级表头 path
   - 可能适合作为筛选键的列（仅列号/表头，不保存正文值）
   - 推断信息/置信度

3. 提供一个小型 JSON DSL interpreter。
   DSL 可以：
   - 定位具体坐标 cell
   - 查找任意文本 cell
   - 定位表头 cell
   - 行标签 × 列表头求交
   - 相对某个 cell 找右/左/上/下一个 cell
   - 根据一个或多个列条件筛选行
   - 从筛选结果抽取目标列

设计原则：
- LLM 将来只负责把自然语言 question 编译成 DSL JSON。
- Interpreter 自己执行，不让 LLM 直接碰结果。
- Schema 是 Planner 的“地图”，完整表格 JSON 是 Interpreter 的“数据库”。
"""

import argparse
import json
import re
from pathlib import Path


SCHEMA_VERSION = "table-schema-v1"
DSL_VERSION = "table-dsl-v1"


# ============================================================
# 基础工具
# ============================================================

def normalize_text(value):
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(text.split())
    return text.strip()


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_number_like(text):
    """
    这里只做“像不像数据值”的粗判定，
    用来辅助推断 header_rows。

    不参与最终检索语义。
    """
    text = normalize_text(text)

    if not text:
        return False

    # 去掉常见货币/百分号/括号/比较符号等。
    candidate = text
    candidate = candidate.replace(",", "")
    candidate = candidate.replace("，", "")
    candidate = candidate.replace("%", "")
    candidate = candidate.replace("％", "")
    candidate = candidate.replace("¥", "")
    candidate = candidate.replace("￥", "")
    candidate = candidate.replace("$", "")
    candidate = candidate.replace("€", "")
    candidate = candidate.replace("£", "")
    candidate = candidate.replace("≤", "")
    candidate = candidate.replace("≥", "")
    candidate = candidate.replace("<", "")
    candidate = candidate.replace(">", "")
    candidate = candidate.replace("≈", "")
    candidate = candidate.replace("~", "-")
    candidate = candidate.replace("～", "-")

    # 纯数 / 小数 / 正负 / 区间
    if re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?"
        r"(?:\s*-\s*[+-]?\d+(?:\.\d+)?)?",
        candidate,
    ):
        return True

    # 常见日期
    if re.fullmatch(
        r"\d{2,4}[-/.年]\d{1,2}"
        r"(?:[-/.月]\d{1,2}日?)?",
        candidate,
    ):
        return True

    # 纯年份
    if re.fullmatch(r"\d{4}年?", candidate):
        return True

    return False


def load_standard_table(path: Path):
    data = load_json(path)

    rows = int(data["row_count"])
    cols = int(data["col_count"])
    raw_cells = data["cells"]

    if rows <= 0 or cols <= 0:
        raise ValueError("row_count / col_count 必须为正整数")

    if not isinstance(raw_cells, list):
        raise ValueError("cells 必须是数组")

    cells = []

    for i, raw in enumerate(raw_cells):
        cell = {
            "id": f"c{i}",
            "text": normalize_text(raw.get("text", "")),
            "row": int(raw["row"]),
            "col": int(raw["col"]),
            "rowspan": int(raw.get("rowspan", 1)),
            "colspan": int(raw.get("colspan", 1)),
        }

        if cell["rowspan"] <= 0 or cell["colspan"] <= 0:
            raise ValueError(f"cell#{i} rowspan/colspan 非法")

        if (
            cell["row"] < 0
            or cell["col"] < 0
            or cell["row"] + cell["rowspan"] > rows
            or cell["col"] + cell["colspan"] > cols
        ):
            raise ValueError(f"cell#{i} 越界")

        cells.append(cell)

    # 稳定行优先排序，再重新编号。
    cells.sort(
        key=lambda c: (
            c["row"],
            c["col"],
            c["rowspan"],
            c["colspan"],
        )
    )

    for i, cell in enumerate(cells):
        cell["id"] = f"c{i}"

    grid = [
        [None for _ in range(cols)]
        for _ in range(rows)
    ]

    cell_by_id = {
        cell["id"]: cell
        for cell in cells
    }

    for cell in cells:
        for r in range(
            cell["row"],
            cell["row"] + cell["rowspan"],
        ):
            for c in range(
                cell["col"],
                cell["col"] + cell["colspan"],
            ):
                if grid[r][c] is not None:
                    raise ValueError(
                        f"基础格 ({r}, {c}) 被多个 cell 覆盖"
                    )
                grid[r][c] = cell["id"]

    missing = [
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if grid[r][c] is None
    ]

    if missing:
        raise ValueError(
            f"表格存在 {len(missing)} 个未覆盖基础格，"
            f"例如 {missing[:10]}"
        )

    return {
        "row_count": rows,
        "col_count": cols,
        "cells": cells,
        "cell_by_id": cell_by_id,
        "grid": grid,
    }


# ============================================================
# Table Schema 推断
# ============================================================

def row_start_signature(table, row):
    """
    只看“从这一行开始的 cell”在列方向上的拓扑。
    正文数据区通常会连续多行拥有相似/相同 signature；
    多级表头通常不会。
    """
    signature = []

    for cell in table["cells"]:
        if cell["row"] != row:
            continue

        signature.append(
            (
                cell["col"],
                cell["colspan"],
                cell["rowspan"],
            )
        )

    return tuple(signature)


def signature_similarity(a, b):
    if not a and not b:
        return 1.0

    if not a or not b:
        return 0.0

    sa = set(a)
    sb = set(b)

    return len(sa & sb) / len(sa | sb)


def row_profile(table, row):
    ids = []
    seen = set()

    for col in range(table["col_count"]):
        cell_id = table["grid"][row][col]

        if cell_id not in seen:
            seen.add(cell_id)
            ids.append(cell_id)

    nonempty = []
    merged = 0
    numeric = 0

    for cell_id in ids:
        cell = table["cell_by_id"][cell_id]

        if (
            cell["rowspan"] > 1
            or cell["colspan"] > 1
        ):
            merged += 1

        if cell["text"]:
            nonempty.append(cell)

            if is_number_like(cell["text"]):
                numeric += 1

    total = max(1, len(ids))
    nonempty_count = max(1, len(nonempty))

    return {
        "cell_count": len(ids),
        "nonempty_count": len(nonempty),
        "merged_ratio": merged / total,
        "numeric_ratio": numeric / nonempty_count,
        "text_ratio": (
            len(nonempty) - numeric
        ) / nonempty_count,
    }


def infer_header_rows(table):
    """
    v1 纯规则策略：

    1. 优先找“正文开始稳定”的最早位置：
       连续 3 行拥有高度相似的起始-cell拓扑。
    2. 至少保留顶部 1 行作为候选表头。
    3. 如果找不到稳定正文区，退化为：
       顶部连续的“低数值比例/高合并比例”行。
    4. 最终上限为前 8 行，避免误吞大量正文。

    对任意表格不可能仅凭 geometry+text 100% 判断语义表头，
    所以 Schema 会同时输出 inference_confidence，
    并允许 CLI 用 --header-rows 手工覆盖。
    """
    rows = table["row_count"]

    if rows <= 1:
        return 0, 1.0, "single-row table"

    max_scan = min(rows - 1, 8)

    # 先找稳定正文区域。
    for start in range(1, max_scan + 1):
        if start + 2 >= rows:
            break

        s0 = row_start_signature(table, start)
        s1 = row_start_signature(table, start + 1)
        s2 = row_start_signature(table, start + 2)

        sim01 = signature_similarity(s0, s1)
        sim12 = signature_similarity(s1, s2)

        if sim01 >= 0.90 and sim12 >= 0.90:
            # 再要求正文起始行不是极端“表头型”。
            p = row_profile(table, start)

            confidence = min(
                0.98,
                0.72
                + 0.12 * sim01
                + 0.12 * sim12
                + 0.04 * (1.0 - p["merged_ratio"]),
            )

            return (
                start,
                round(confidence, 3),
                "stable repeated body-row topology",
            )

    # fallback：逐行看表头特征。
    header_rows = 1

    for r in range(1, max_scan):
        p = row_profile(table, r)

        header_like = (
            p["merged_ratio"] >= 0.18
            or p["numeric_ratio"] <= 0.25
        )

        if not header_like:
            break

        # 不让纯文本正文一路吞下去。
        next_p = (
            row_profile(table, r + 1)
            if r + 1 < rows
            else None
        )

        if (
            next_p
            and p["merged_ratio"] < 0.10
            and next_p["merged_ratio"] < 0.10
            and abs(
                p["numeric_ratio"]
                - next_p["numeric_ratio"]
            ) < 0.10
        ):
            break

        header_rows = r + 1

    return (
        header_rows,
        0.55,
        "fallback text/number/merge heuristic",
    )


def get_unique_cell_ids_in_region(
    table,
    row_start,
    row_end,
    col_start,
    col_end,
):
    ids = []
    seen = set()

    for r in range(row_start, row_end):
        for c in range(col_start, col_end):
            cell_id = table["grid"][r][c]

            if cell_id not in seen:
                seen.add(cell_id)
                ids.append(cell_id)

    return ids


def build_column_paths(table, header_rows):
    columns = []

    for col in range(table["col_count"]):
        path = []
        path_cell_ids = []
        seen = set()

        for row in range(header_rows):
            cell_id = table["grid"][row][col]

            if cell_id in seen:
                continue

            seen.add(cell_id)
            cell = table["cell_by_id"][cell_id]

            if not cell["text"]:
                continue

            path.append(cell["text"])
            path_cell_ids.append(cell_id)

        columns.append({
            "col": col,
            "path": path,
            "leaf": path[-1] if path else "",
            "name": " / ".join(path),
            "header_cell_ids": path_cell_ids,
        })

    return columns


def infer_key_column_candidates(
    table,
    header_rows,
    columns,
):
    """
    只输出“可能适合做 WHERE 条件的列号”，
    不输出这些列的正文值。

    规则很保守：
    - 看正文区
    - 左侧前 3 列中
    - 非空率较高
    - 文本比例较高
    - 去重率较高
    """
    body_start = header_rows

    if body_start >= table["row_count"]:
        return []

    candidates = []

    for col in range(
        min(3, table["col_count"])
    ):
        values = []

        for row in range(
            body_start,
            table["row_count"],
        ):
            cell_id = table["grid"][row][col]
            text = table["cell_by_id"][cell_id]["text"]

            if text:
                values.append(text)

        body_rows = (
            table["row_count"] - body_start
        )

        if not values or body_rows <= 0:
            continue

        nonempty_ratio = len(values) / body_rows
        text_ratio = (
            sum(
                1
                for value in values
                if not is_number_like(value)
            )
            / len(values)
        )
        unique_ratio = (
            len(set(values))
            / len(values)
        )

        score = (
            0.35 * nonempty_ratio
            + 0.35 * text_ratio
            + 0.30 * unique_ratio
        )

        if (
            nonempty_ratio >= 0.60
            and text_ratio >= 0.55
            and score >= 0.65
        ):
            candidates.append({
                "col": col,
                "column": columns[col]["name"],
                "score": round(score, 3),
            })

    return candidates


def build_table_schema(
    table,
    header_rows_override=None,
):
    if header_rows_override is None:
        (
            header_rows,
            confidence,
            reason,
        ) = infer_header_rows(table)
    else:
        header_rows = int(header_rows_override)

        if not (
            0 <= header_rows
            <= table["row_count"]
        ):
            raise ValueError(
                "--header-rows 超出范围"
            )

        confidence = 1.0
        reason = "manual override"

    columns = build_column_paths(
        table,
        header_rows,
    )

    header_cell_ids = (
        get_unique_cell_ids_in_region(
            table,
            0,
            header_rows,
            0,
            table["col_count"],
        )
        if header_rows > 0
        else []
    )

    header_cells = []

    for cell_id in header_cell_ids:
        cell = table["cell_by_id"][cell_id]

        header_cells.append({
            "id": cell["id"],
            "text": cell["text"],
            "row": cell["row"],
            "col": cell["col"],
            "rowspan": cell["rowspan"],
            "colspan": cell["colspan"],
        })

    key_candidates = infer_key_column_candidates(
        table,
        header_rows,
        columns,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "row_count": table["row_count"],
        "col_count": table["col_count"],
        "header_rows": header_rows,
        "header_inference": {
            "confidence": confidence,
            "reason": reason,
        },
        "columns": columns,
        "header_cells": header_cells,
        "key_column_candidates": key_candidates,
    }


# ============================================================
# DSL selector / resolver
# ============================================================

def resolve_ref(value, env):
    if (
        isinstance(value, str)
        and value.startswith("$")
    ):
        name = value[1:]

        if name not in env:
            raise ValueError(
                f"未知变量: {value}"
            )

        return env[name]

    return value


def unique_cell_ids(ids):
    result = []
    seen = set()

    for cell_id in ids:
        if cell_id in seen:
            continue
        seen.add(cell_id)
        result.append(cell_id)

    return result


def cells_to_public(table, cell_ids):
    result = []

    for cell_id in unique_cell_ids(cell_ids):
        cell = table["cell_by_id"][cell_id]

        result.append({
            "id": cell["id"],
            "text": cell["text"],
            "row": cell["row"],
            "col": cell["col"],
            "rowspan": cell["rowspan"],
            "colspan": cell["colspan"],
        })

    return result


def normalize_path(path):
    if isinstance(path, str):
        # 允许：
        # "2026 / 价格"
        # "2026 > 价格"
        if ">" in path:
            parts = path.split(">")
        elif "/" in path:
            parts = path.split("/")
        else:
            parts = [path]

        return [
            normalize_text(x)
            for x in parts
            if normalize_text(x)
        ]

    if isinstance(path, list):
        return [
            normalize_text(x)
            for x in path
            if normalize_text(x)
        ]

    raise ValueError(
        f"非法 column path: {path!r}"
    )


def resolve_column(schema, selector):
    """
    selector 可以是：
    - int 列号
    - "价格"             -> 优先匹配 leaf
    - "2026 / 价格"      -> 完整 path
    - ["2026", "价格"]   -> 完整 path
    """
    if isinstance(selector, int):
        if not 0 <= selector < schema["col_count"]:
            raise ValueError(
                f"列号越界: {selector}"
            )

        return selector

    path = normalize_path(selector)
    columns = schema["columns"]

    # 先完整 path 精确匹配。
    exact = [
        column["col"]
        for column in columns
        if column["path"] == path
    ]

    if len(exact) == 1:
        return exact[0]

    if len(exact) > 1:
        raise ValueError(
            f"column path 不唯一: {path}"
        )

    # 单个词时再尝试 leaf。
    if len(path) == 1:
        leaf = path[0]

        leaf_matches = [
            column["col"]
            for column in columns
            if column["leaf"] == leaf
        ]

        if len(leaf_matches) == 1:
            return leaf_matches[0]

        if len(leaf_matches) > 1:
            choices = [
                columns[col]["path"]
                for col in leaf_matches
            ]

            raise ValueError(
                f"列名 {leaf!r} 不唯一，"
                f"请使用完整 path。候选: {choices}"
            )

    raise ValueError(
        f"Schema 中找不到列: {selector!r}"
    )


def cell_scope_matches(
    cell,
    header_rows,
    scope,
):
    if scope == "all":
        return True

    intersects_header = (
        cell["row"] < header_rows
    )

    if scope == "header":
        return intersects_header

    if scope == "body":
        return not intersects_header

    raise ValueError(
        f"未知 scope: {scope}"
    )


def match_text(
    actual,
    expected,
    mode,
):
    actual = normalize_text(actual)
    expected = normalize_text(expected)

    if mode == "exact":
        return actual == expected

    if mode == "contains":
        return expected in actual

    if mode == "startswith":
        return actual.startswith(expected)

    if mode == "endswith":
        return actual.endswith(expected)

    if mode == "regex":
        return bool(
            re.search(
                expected,
                actual,
            )
        )

    raise ValueError(
        f"未知 match mode: {mode}"
    )


def op_find_text(
    table,
    schema,
    step,
):
    text = step["text"]
    mode = step.get(
        "match",
        "exact",
    )
    scope = step.get(
        "scope",
        "all",
    )

    result = []

    for cell in table["cells"]:
        if not cell_scope_matches(
            cell,
            schema["header_rows"],
            scope,
        ):
            continue

        if match_text(
            cell["text"],
            text,
            mode,
        ):
            result.append(
                cell["id"]
            )

    return result


def op_header(
    table,
    schema,
    step,
):
    # 方式一：按 text 找表头 cell。
    if "text" in step:
        text = step["text"]
        mode = step.get(
            "match",
            "exact",
        )

        return [
            item["id"]
            for item in schema["header_cells"]
            if match_text(
                item["text"],
                text,
                mode,
            )
        ]

    # 方式二：按 column path 找这个 path 上某一级表头。
    if "path" in step:
        path = normalize_path(
            step["path"]
        )

        col = resolve_column(
            schema,
            path,
        )

        column = schema["columns"][col]
        ids = column["header_cell_ids"]

        if not ids:
            return []

        level = int(
            step.get(
                "level",
                -1,
            )
        )

        try:
            return [
                ids[level]
            ]
        except IndexError as e:
            raise ValueError(
                f"header level 越界: "
                f"path={path}, level={level}"
            ) from e

    raise ValueError(
        "header op 需要 text 或 path"
    )


def next_distinct_cell(
    table,
    start_cell_id,
    direction,
):
    cell = table["cell_by_id"][
        start_cell_id
    ]

    if direction == "right":
        row = cell["row"]
        col = (
            cell["col"]
            + cell["colspan"]
        )
        dr, dc = 0, 1

    elif direction == "left":
        row = cell["row"]
        col = cell["col"] - 1
        dr, dc = 0, -1

    elif direction == "down":
        row = (
            cell["row"]
            + cell["rowspan"]
        )
        col = cell["col"]
        dr, dc = 1, 0

    elif direction == "up":
        row = cell["row"] - 1
        col = cell["col"]
        dr, dc = -1, 0

    else:
        raise ValueError(
            f"未知 direction: {direction}"
        )

    while (
        0 <= row < table["row_count"]
        and 0 <= col < table["col_count"]
    ):
        cell_id = table["grid"][row][col]

        if cell_id != start_cell_id:
            return cell_id

        row += dr
        col += dc

    return None


def op_relative(
    table,
    step,
    env,
):
    source = resolve_ref(
        step["input"],
        env,
    )

    if not isinstance(source, list):
        raise ValueError(
            "relative input 必须是 cell id 列表"
        )

    direction = step["direction"]
    steps = int(
        step.get(
            "steps",
            1,
        )
    )

    if steps <= 0:
        raise ValueError(
            "relative.steps 必须 > 0"
        )

    result = []

    for cell_id in source:
        current = cell_id

        for _ in range(steps):
            current = next_distinct_cell(
                table,
                current,
                direction,
            )

            if current is None:
                break

        if current is not None:
            result.append(current)

    return unique_cell_ids(result)


def op_cell(
    table,
    step,
):
    row = int(step["row"])
    col = int(step["col"])

    if not (
        0 <= row < table["row_count"]
        and 0 <= col < table["col_count"]
    ):
        raise ValueError(
            f"cell 坐标越界: ({row}, {col})"
        )

    return [
        table["grid"][row][col]
    ]


def op_intersection(
    table,
    step,
    env,
):
    row_cells = resolve_ref(
        step["row"],
        env,
    )
    col_cells = resolve_ref(
        step["col"],
        env,
    )

    if (
        not isinstance(row_cells, list)
        or not isinstance(col_cells, list)
    ):
        raise ValueError(
            "intersection row/col "
            "都必须是 cell id 列表"
        )

    result = []

    for row_cell_id in row_cells:
        row_cell = table["cell_by_id"][
            row_cell_id
        ]

        row_indices = range(
            row_cell["row"],
            row_cell["row"]
            + row_cell["rowspan"],
        )

        for col_cell_id in col_cells:
            col_cell = table["cell_by_id"][
                col_cell_id
            ]

            col_indices = range(
                col_cell["col"],
                col_cell["col"]
                + col_cell["colspan"],
            )

            for r in row_indices:
                for c in col_indices:
                    result.append(
                        table["grid"][r][c]
                    )

    return unique_cell_ids(result)


def compare_value(
    actual,
    cmp_name,
    expected,
):
    actual_text = normalize_text(actual)
    expected_text = normalize_text(expected)

    if cmp_name == "eq":
        return actual_text == expected_text

    if cmp_name == "ne":
        return actual_text != expected_text

    if cmp_name == "contains":
        return expected_text in actual_text

    if cmp_name == "startswith":
        return actual_text.startswith(
            expected_text
        )

    if cmp_name == "endswith":
        return actual_text.endswith(
            expected_text
        )

    if cmp_name in {
        "gt",
        "ge",
        "lt",
        "le",
    }:
        try:
            a = float(
                actual_text
                .replace(",", "")
                .replace("%", "")
            )
            b = float(
                expected_text
                .replace(",", "")
                .replace("%", "")
            )
        except ValueError:
            return False

        if cmp_name == "gt":
            return a > b
        if cmp_name == "ge":
            return a >= b
        if cmp_name == "lt":
            return a < b
        if cmp_name == "le":
            return a <= b

    raise ValueError(
        f"未知比较操作: {cmp_name}"
    )


def op_filter_rows(
    table,
    schema,
    step,
):
    where = step.get(
        "where",
        [],
    )

    if not isinstance(where, list):
        raise ValueError(
            "filter_rows.where 必须是数组"
        )

    result = []

    for row in range(
        schema["header_rows"],
        table["row_count"],
    ):
        matched = True

        for cond in where:
            col = resolve_column(
                schema,
                cond["column"],
            )

            cell_id = table["grid"][row][col]
            text = table["cell_by_id"][
                cell_id
            ]["text"]

            cmp_name = cond.get(
                "cmp",
                "eq",
            )

            if not compare_value(
                text,
                cmp_name,
                cond["value"],
            ):
                matched = False
                break

        if matched:
            result.append(row)

    return result


def op_extract(
    table,
    schema,
    step,
    env,
):
    target_col = resolve_column(
        schema,
        step["column"],
    )

    if "rows" in step:
        rows = resolve_ref(
            step["rows"],
            env,
        )
    else:
        rows = op_filter_rows(
            table,
            schema,
            {
                "where": step.get(
                    "where",
                    [],
                )
            },
        )

    if not isinstance(rows, list):
        raise ValueError(
            "extract rows 必须是 row index 列表"
        )

    result = []

    for row in rows:
        if not isinstance(row, int):
            raise ValueError(
                "extract rows 中必须是整数"
            )

        if not (
            0 <= row < table["row_count"]
        ):
            continue

        result.append(
            table["grid"][row][target_col]
        )

    return unique_cell_ids(result)


# ============================================================
# DSL Interpreter
# ============================================================

def execute_step(
    table,
    schema,
    step,
    env,
):
    op = step["op"]

    if op == "find_text":
        return op_find_text(
            table,
            schema,
            step,
        )

    if op == "header":
        return op_header(
            table,
            schema,
            step,
        )

    if op == "cell":
        return op_cell(
            table,
            step,
        )

    if op == "relative":
        return op_relative(
            table,
            step,
            env,
        )

    if op == "intersection":
        return op_intersection(
            table,
            step,
            env,
        )

    if op == "filter_rows":
        return op_filter_rows(
            table,
            schema,
            step,
        )

    if op == "extract":
        return op_extract(
            table,
            schema,
            step,
            env,
        )

    raise ValueError(
        f"未知 DSL op: {op}"
    )


def format_result(
    table,
    result,
    mode,
):
    if mode == "raw":
        return result

    if mode == "rows":
        return result

    if not isinstance(result, list):
        return result

    # cell id list
    if all(
        isinstance(x, str)
        and x in table["cell_by_id"]
        for x in result
    ):
        if mode == "cells":
            return cells_to_public(
                table,
                result,
            )

        if mode == "coords":
            return [
                {
                    "row": table["cell_by_id"][cell_id]["row"],
                    "col": table["cell_by_id"][cell_id]["col"],
                }
                for cell_id in unique_cell_ids(result)
            ]

        if mode == "values":
            values = [
                table["cell_by_id"][cell_id]["text"]
                for cell_id in unique_cell_ids(result)
            ]

            if len(values) == 1:
                return values[0]

            return values

    return result


def run_program(
    table,
    schema,
    program,
):
    if (
        program.get(
            "dsl_version",
            DSL_VERSION,
        )
        != DSL_VERSION
    ):
        raise ValueError(
            "不支持的 dsl_version"
        )

    steps = program.get(
        "steps",
        [],
    )

    if not isinstance(
        steps,
        list,
    ):
        raise ValueError(
            "steps 必须是数组"
        )

    env = {}

    for i, step in enumerate(
        steps,
        1,
    ):
        if not isinstance(
            step,
            dict,
        ):
            raise ValueError(
                f"step#{i} 不是对象"
            )

        result = execute_step(
            table,
            schema,
            step,
            env,
        )

        save_as = step.get(
            "save_as"
        )

        if save_as:
            env[save_as] = result

    return_spec = program.get(
        "return"
    )

    if return_spec is None:
        return {
            "variables": env
        }

    if isinstance(
        return_spec,
        str,
    ):
        result = resolve_ref(
            return_spec,
            env,
        )
        mode = "values"

    elif isinstance(
        return_spec,
        dict,
    ):
        result = resolve_ref(
            return_spec["input"],
            env,
        )
        mode = return_spec.get(
            "mode",
            "values",
        )

    else:
        raise ValueError(
            "return 必须是字符串或对象"
        )

    return {
        "result": format_result(
            table,
            result,
            mode,
        ),
        "variables": env,
    }


# ============================================================
# CLI
# ============================================================

def cmd_schema(args):
    table = load_standard_table(
        args.table
    )

    schema = build_table_schema(
        table,
        header_rows_override=(
            args.header_rows
        ),
    )

    output = (
        args.output
        if args.output
        else args.table.with_name(
            args.table.stem
            + ".schema.json"
        )
    )

    save_json(
        output,
        schema,
    )

    print(
        f"[Schema] {output}"
    )
    print(
        f"[Header rows] "
        f"{schema['header_rows']}"
    )
    print(
        f"[Confidence] "
        f"{schema['header_inference']['confidence']}"
    )

    print("[Columns]")

    for column in schema["columns"]:
        print(
            f"  {column['col']:>3}: "
            f"{column['name']!r}"
        )


def cmd_query(args):
    table = load_standard_table(
        args.table
    )
    schema = load_json(
        args.schema
    )
    program = load_json(
        args.program
    )

    result = run_program(
        table,
        schema,
        program,
    )

    if args.output:
        save_json(
            args.output,
            result,
        )
        print(
            f"[Result] {args.output}"
        )
    else:
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


def cmd_both(args):
    table = load_standard_table(
        args.table
    )

    schema = build_table_schema(
        table,
        header_rows_override=(
            args.header_rows
        ),
    )

    schema_path = (
        args.schema_output
        if args.schema_output
        else args.table.with_name(
            args.table.stem
            + ".schema.json"
        )
    )

    save_json(
        schema_path,
        schema,
    )

    program = load_json(
        args.program
    )

    result = run_program(
        table,
        schema,
        program,
    )

    print(
        f"[Schema] {schema_path}"
    )

    if args.output:
        save_json(
            args.output,
            result,
        )
        print(
            f"[Result] {args.output}"
        )
    else:
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "从统一标准表格 JSON 自动生成 "
            "Table Schema，并执行 Table DSL"
        )
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    # schema
    p_schema = sub.add_parser(
        "schema",
        help="生成 Table Schema",
    )
    p_schema.add_argument(
        "table",
        type=Path,
        help="标准表格 JSON",
    )
    p_schema.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
    )
    p_schema.add_argument(
        "--header-rows",
        type=int,
        default=None,
        help=(
            "手工指定顶部表头行数；"
            "默认使用纯规则自动推断"
        ),
    )
    p_schema.set_defaults(
        func=cmd_schema
    )

    # query
    p_query = sub.add_parser(
        "query",
        help="使用已有 Schema 执行 DSL",
    )
    p_query.add_argument(
        "table",
        type=Path,
    )
    p_query.add_argument(
        "schema",
        type=Path,
    )
    p_query.add_argument(
        "program",
        type=Path,
        help="DSL JSON 文件",
    )
    p_query.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
    )
    p_query.set_defaults(
        func=cmd_query
    )

    # both
    p_both = sub.add_parser(
        "run",
        help="先生成 Schema，再立刻执行 DSL",
    )
    p_both.add_argument(
        "table",
        type=Path,
    )
    p_both.add_argument(
        "program",
        type=Path,
    )
    p_both.add_argument(
        "--header-rows",
        type=int,
        default=None,
    )
    p_both.add_argument(
        "--schema-output",
        type=Path,
        default=None,
    )
    p_both.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
    )
    p_both.set_defaults(
        func=cmd_both
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
