
#!/usr/bin/env python3

import argparse
import json
import re
from pathlib import Path

from openpyxl import load_workbook


NUMBER_PATTERN = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


def normalize_id(value):
    """把 Excel 中的 1 / 1.0 / '1' 统一成字符串 '1'。"""
    if value is None:
        return None

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def check_number(value):
    if value is None:
        return False, "答案为空"

    # Excel 本身存储成数值，直接合法。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True, ""

    text = str(value).strip()

    if not text:
        return False, "答案为空"

    if not NUMBER_PATTERN.fullmatch(text):
        return False, f"不是合法 number: {text}"

    return True, ""


def check_string(value):
    if value is None:
        return False, "答案为空"

    if not isinstance(value, str):
        return False, f"不是 string，而是 {type(value).__name__}"

    return True, ""


def check_structure_json(obj):
    """
    检查 structure JSON 的基本格式：
    {
        "row_count": ...,
        "col_count": ...,
        "cells": [...]
    }
    """

    required_top = {"row_count", "col_count", "cells"}

    missing = required_top - obj.keys()
    if missing:
        return False, f"JSON 缺少字段: {sorted(missing)}"

    row_count = obj["row_count"]
    col_count = obj["col_count"]
    cells = obj["cells"]

    if not isinstance(row_count, int) or isinstance(row_count, bool):
        return False, "row_count 必须是整数"

    if not isinstance(col_count, int) or isinstance(col_count, bool):
        return False, "col_count 必须是整数"

    if row_count < 0:
        return False, "row_count 不能小于 0"

    if col_count < 0:
        return False, "col_count 不能小于 0"

    if not isinstance(cells, list):
        return False, "cells 必须是数组"

    required_cell = {
        "text",
        "row",
        "col",
        "rowspan",
        "colspan",
    }

    for i, cell in enumerate(cells):
        if not isinstance(cell, dict):
            return False, f"cells[{i}] 不是 JSON 对象"

        missing = required_cell - cell.keys()
        if missing:
            return False, (
                f"cells[{i}] 缺少字段: {sorted(missing)}"
            )

        if not isinstance(cell["text"], str):
            return False, f"cells[{i}].text 必须是字符串"

        for field in ("row", "col", "rowspan", "colspan"):
            value = cell[field]

            if not isinstance(value, int) or isinstance(value, bool):
                return False, (
                    f"cells[{i}].{field} 必须是整数"
                )

        if cell["row"] < 0:
            return False, f"cells[{i}].row 不能小于 0"

        if cell["col"] < 0:
            return False, f"cells[{i}].col 不能小于 0"

        if cell["rowspan"] < 1:
            return False, (
                f"cells[{i}].rowspan 必须 >= 1"
            )

        if cell["colspan"] < 1:
            return False, (
                f"cells[{i}].colspan 必须 >= 1"
            )

    return True, ""


def check_json(value):
    if value is None:
        return False, "答案为空"

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return False, "答案为空"

        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            return False, (
                f"JSON 语法错误: {e.msg} "
                f"(位置 {e.pos})"
            )
    else:
        return False, "JSON 答案必须以文本形式保存"

    if not isinstance(obj, dict):
        return False, (
            "要求 json，但解析结果不是 JSON 对象"
        )

    # 比赛中的 json 就是 structure JSON，
    # 所以继续检查结构字段。
    return check_structure_json(obj)


def check_json_array(value):
    if value is None:
        return False, "答案为空"

    if not isinstance(value, str):
        return False, "json_array 必须以文本形式保存"

    text = value.strip()

    if not text:
        return False, "答案为空"

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return False, (
            f"JSON 数组语法错误: {e.msg} "
            f"(位置 {e.pos})"
        )

    if not isinstance(obj, list):
        return False, (
            f"要求 json_array，但实际解析为 "
            f"{type(obj).__name__}"
        )

    return True, ""


def check_answer(value, answer_format):
    fmt = str(answer_format).strip().lower()

    if fmt == "number":
        return check_number(value)

    if fmt == "string":
        return check_string(value)

    if fmt == "json":
        return check_json(value)

    if fmt == "json_array":
        return check_json_array(value)

    return False, f"未知 answer_format: {answer_format}"


def read_tests(tests_path):
    wb = load_workbook(
        tests_path,
        read_only=True,
        data_only=True,
    )
    ws = wb.active

    headers = {
        str(cell.value).strip(): i
        for i, cell in enumerate(ws[1])
        if cell.value is not None
    }

    if "id" not in headers:
        raise ValueError("tests 文件找不到 id 列")

    if "answer_format" not in headers:
        raise ValueError(
            "tests 文件找不到 answer_format 列"
        )

    id_col = headers["id"]
    format_col = headers["answer_format"]

    result = {}

    for row in ws.iter_rows(min_row=2, values_only=True):
        id_value = normalize_id(row[id_col])

        if id_value is None:
            continue

        result[id_value] = row[format_col]

    wb.close()
    return result


def read_answers(answer_path):
    wb = load_workbook(
        answer_path,
        read_only=True,
        data_only=True,
    )
    ws = wb.active

    answers = {}
    duplicate_ids = set()

    # 第一行是表头，从第二行开始。
    for row in ws.iter_rows(min_row=2, values_only=True):
        if len(row) < 2:
            continue

        id_value = normalize_id(row[0])
        answer = row[1]

        if id_value is None:
            continue

        if id_value in answers:
            duplicate_ids.add(id_value)

        answers[id_value] = answer

    wb.close()

    return answers, duplicate_ids


def id_sort_key(value):
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "检查答案 XLSX 中 answer 的类型是否符合 "
            "tests.xlsx 的 answer_format"
        )
    )

    parser.add_argument(
        "answer_file",
        help="需要检查的答案 xlsx 文件",
    )

    parser.add_argument(
        "--tests",
        default="tests.xlsx",
        help=(
            "题目文件路径，默认当前目录 tests.xlsx"
        ),
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="显示每个错误 ID 的具体原因",
    )

    args = parser.parse_args()

    tests_path = Path(args.tests)
    answer_path = Path(args.answer_file)

    if not tests_path.is_file():
        raise SystemExit(
            f"找不到 tests 文件: {tests_path}"
        )

    if not answer_path.is_file():
        raise SystemExit(
            f"找不到答案文件: {answer_path}"
        )

    tests = read_tests(tests_path)
    answers, duplicate_ids = read_answers(answer_path)

    errors = {}

    # 重复 ID
    for id_value in duplicate_ids:
        errors[id_value] = "答案文件中存在重复 id"

    # 按 tests 检查每一道题
    for id_value, answer_format in tests.items():

        if id_value not in answers:
            errors[id_value] = "答案文件中缺少该 id"
            continue

        value = answers[id_value]

        ok, reason = check_answer(
            value,
            answer_format,
        )

        if not ok:
            errors[id_value] = reason

    # 答案文件中出现 tests 没有的 ID
    for id_value in answers:
        if id_value not in tests:
            errors[id_value] = (
                "答案文件存在 tests 中没有的 id"
            )

    error_ids = sorted(
        errors.keys(),
        key=id_sort_key,
    )

    if not error_ids:
        print("类型检查通过：没有发现错误。")
        return

    print(f"发现 {len(error_ids)} 个可疑 ID：")
    print(" ".join(error_ids))

    if args.verbose:
        print("\n详细信息：")

        for id_value in error_ids:
            fmt = tests.get(id_value, "未知")

            print(
                f"id={id_value}  "
                f"answer_format={fmt}  "
                f"原因={errors[id_value]}"
            )


if __name__ == "__main__":
    main() 