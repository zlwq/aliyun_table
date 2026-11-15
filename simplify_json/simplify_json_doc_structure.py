#!/usr/bin/env python3
import json
import sys
from pathlib import Path


# 文档智能解析：最终保留字段
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


def get_any(d, *names, default=None):
    """大小写兼容地读取 dict 字段。"""
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


def decode_json_string(value):
    """如果字段本身是 JSON 字符串，则尝试解码。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def normalize_data(data):
    """
    兼容阿里云可能出现的几种 Data 形式：
    - dict
    - JSON 字符串
    - 单元素 list，里面再包 data / Data
    """
    data = decode_json_string(data)

    if isinstance(data, list) and len(data) == 1:
        item = decode_json_string(data[0])

        if isinstance(item, dict):
            nested = get_any(item, "data", "Data")
            if nested is not None:
                return decode_json_string(nested)

    return data


def extract_payload(root):
    """
    兼容 test_doc_structure.py 保存格式：

        {
            "submit_response": ...,
            "result_response": ...
        }

    只取 result_response 中真正的解析结果，
    submit_response、任务状态、RequestId 等全部丢弃。
    """
    if not isinstance(root, dict):
        return root

    result_response = get_any(root, "result_response")

    if isinstance(result_response, dict):
        data = get_any(result_response, "Data", "data")
        if data is not None:
            return normalize_data(data)

        # 某些返回可能直接把解析结果放在 result_response 中
        return result_response

    data = get_any(root, "Data", "data")
    if data is not None:
        return normalize_data(data)

    return root


def simplify(value, keep_primitive=False):
    """
    递归保留 KEEP_KEYS 中的字段。

    非白名单字段如果只是“通往目标字段的父容器”，
    会自动保留父路径，避免把 layouts/cells 等内容从根上剪掉。
    """
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
    """
    清理空 dict / 空 list。
    不按值去重，避免误删真实重复文本或表格数值。
    """
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


def main():
    if len(sys.argv) not in (2, 3):
        print(
            f"用法: python {Path(sys.argv[0]).name} 输入.json [输出.json]",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path = Path(sys.argv[1])

    if len(sys.argv) == 3:
        output_path = Path(sys.argv[2])
    else:
        output_path = input_path.with_name(
            f"{input_path.stem}.simplified{input_path.suffix}"
        )

    if not input_path.is_file():
        print(
            f"错误：找不到输入文件：{input_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with input_path.open("r", encoding="utf-8") as f:
            root = json.load(f)
    except json.JSONDecodeError as e:
        print(
            f"错误：JSON 格式无效：{e}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = extract_payload(root)

    simplified = simplify(payload)
    if simplified is DROP:
        simplified = {}

    simplified = cleanup(simplified)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            simplified,
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"已生成：{output_path}")

    if isinstance(simplified, dict):
        print(
            "顶层字段："
            + ", ".join(simplified.keys())
        )


if __name__ == "__main__":
    main()
