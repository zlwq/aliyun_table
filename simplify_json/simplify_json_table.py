#!/usr/bin/env python3
import json
import sys
from pathlib import Path


# v4：
# 1. 删除 docInfo
# 2. 删除 layouts[].blocks
# 3. 删除 layouts 中 type == "table" 的整块重复表格表达
# 4. 删除 RequestId
# 5. 删除 pageNum
# 6. 删除 type
# 7. 删除整个 tableKVs 及其所有内容
# 8. 自动删除空 dict / 空 list
# 9. tableName 与 sheetName 完全相同时，只保留 tableName
KEEP_KEYS = {
    # 表格主体
    "tables",
    "sheetNumber",
    "cells",
    "colNumber",
    "rowNumber",
    "content",
    "sheetName",

    # 合并单元格
    "mergedCell",
    "firstMergedCell",
    "isMergedCell",
    "isFirstMergedCell",

    # 表格标识 / 关系
    "tableId",
    "tableName",
    "cellIdRelations",

    # 页面信息
    "pages",
    "pageIdCurDoc",
    "pageIdAllDocs",
    "fileName",
    "fileIndex",
    "sourceFileType",
    "pageType",
    "docType",

    # 版面与文本
    "layouts",
    "text",

    # 坐标
    "pos",
    "x",
    "y",
    "numCol",
    "numRow",
    "xsc",
    "ysc",
    "xec",
    "yec",
    "cellId",

    # 置信度
    "docConf",
    "tableConf",

    # 统计信息
    "tableCount",
    "pageCountEstimate",
}


DROP = object()


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


def decode_json_string(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def normalize_table_data(data):
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
    兼容 table_understanding_image.py 保存的格式：

        {
            "response": body,
            "parsed_data": data
        }

    优先读取 parsed_data，避免 response.Data 与 parsed_data
    重复保存同一份解析结果。
    """
    if not isinstance(root, dict):
        return root

    parsed_data = get_any(root, "parsed_data")
    if parsed_data is not None:
        return normalize_table_data(parsed_data)

    response = get_any(root, "response", default={})
    if isinstance(response, dict):
        response_data = get_any(response, "Data", "data")
        if response_data is not None:
            return normalize_table_data(response_data)

    root_data = get_any(root, "Data", "data")
    if root_data is not None:
        return normalize_table_data(root_data)

    return root


def simplify(value, keep_primitive=False):
    """
    递归保留目标字段，同时自动保留通往目标字段的必要父容器。
    """
    if isinstance(value, dict):
        # 在删除 type 字段之前先利用原始 type 判断：
        # layouts 中 type == "table" 的对象与 tables[].cells 重复，整块删除。
        if str(value.get("type", "")).lower() == "table":
            return DROP

        result = {}

        for key, child_value in value.items():
            # 明确整块删除的字段
            if key in {
                "docInfo",
                "blocks",
                "tableKVs",
                "RequestId",
                "requestId",
                "pageNum",
                "type",
            }:
                continue

            selected = key in KEEP_KEYS
            child = simplify(child_value, keep_primitive=selected)

            if child is DROP:
                continue

            result[key] = child

        return result if result else DROP

    if isinstance(value, list):
        result = []

        for item in value:
            child = simplify(item, keep_primitive=keep_primitive)
            if child is not DROP:
                result.append(child)

        return result if result else DROP

    return value if keep_primitive else DROP


def cleanup(obj):
    """
    清除空容器和明显重复元数据。
    不对真实表格值做“按值去重”。
    """
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            obj[key] = cleanup(obj[key])

            if obj[key] in ({}, []):
                del obj[key]

        # tableName 与 sheetName 一样时只保留一个
        if (
            obj.get("tableName") is not None
            and obj.get("tableName") == obj.get("sheetName")
        ):
            obj.pop("sheetName", None)

        return obj

    if isinstance(obj, list):
        cleaned = []

        for item in obj:
            item = cleanup(item)
            if item not in ({}, []):
                cleaned.append(item)

        return cleaned

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
        print(f"错误：找不到输入文件：{input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with input_path.open("r", encoding="utf-8") as f:
            root = json.load(f)
    except json.JSONDecodeError as e:
        print(f"错误：JSON 格式无效：{e}", file=sys.stderr)
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
        print("顶层字段：" + ", ".join(simplified.keys()))


if __name__ == "__main__":
    main()