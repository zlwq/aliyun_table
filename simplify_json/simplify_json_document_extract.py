#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def simplify_kv_item(item):
    """
    单个 KV 项只保留：
    - key
    - value
    """
    if not isinstance(item, dict):
        return item

    result = {}

    if "value" in item:
        result["value"] = item["value"]

    if "key" in item:
        result["key"] = item["key"]

    return result


def simplify_kv_info(kv_info):
    """
    保留 kvInfo 原始顺序，
    每项只保留 key / value。
    """
    if not isinstance(kv_info, list):
        return kv_info

    return [simplify_kv_item(item) for item in kv_info]


def simplify_document_extract(data):
    """
    极简 Document Extract JSON。

    最终只保留：

    result
      ├─ documentFileName
      └─ kvInfo

    删除：
    kvListInfo
    submit_response
    result_response 外层状态信息
    extInfo
    table_id
    pageInfo
    fileType
    errorMessage
    errorCode
    status
    Status
    Completed
    RequestId
    Id
    以及其他无关字段
    """
    result_response = data.get("result_response")
    if not isinstance(result_response, dict):
        raise ValueError("JSON 中没有找到有效的 result_response")

    response_data = result_response.get("Data")
    if not isinstance(response_data, dict):
        raise ValueError("JSON 中没有找到 result_response.Data")

    result = response_data.get("result")
    if not isinstance(result, dict):
        raise ValueError("JSON 中没有找到 result_response.Data.result")

    simplified = {}

    if "documentFileName" in result:
        simplified["documentFileName"] = result["documentFileName"]

    if "kvInfo" in result:
        simplified["kvInfo"] = simplify_kv_info(
            result["kvInfo"]
        )

    return {
        "result": simplified
    }


def main():
    parser = argparse.ArgumentParser(
        description="极简阿里云 Document Extract JSON，仅保留 documentFileName 和 kvInfo"
    )
    parser.add_argument(
        "input_json",
        type=Path,
        help="原始 document_extract.json 文件路径",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出 JSON 路径；默认在原文件名后添加 .simplified.json",
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

    simplified = simplify_document_extract(data)

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