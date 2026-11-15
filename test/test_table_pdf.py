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
MAX_POLLS = 720  # 10秒一次，最多约120分钟


def make_client() -> DocMindClient:
    # .env -> 当前进程环境变量
    load_dotenv()

    required = [
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "缺少环境变量: " + ", ".join(missing) +
            "\n请确认它们已经写入当前目录的 .env"
        )

    # 直接读取 .env，避免默认凭证链继续尝试 CLI / ECS metadata。
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")

    config = open_api_models.Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
    )
    config.endpoint = ENDPOINT
    return DocMindClient(config)


def to_plain(value):
    """把 Tea SDK 对象尽量转换成普通 Python 对象。"""
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


def submit_file(client: DocMindClient, file_path: Path) -> str:
    f = open(file_path, "rb")
    try:
        request = docmind_models.SubmitTableUnderstandingJobAdvanceRequest(
            file_url_object=f,
            file_name=file_path.name,
        )
        runtime = util_models.RuntimeOptions()
        response = client.submit_table_understanding_job_advance(request, runtime)
    finally:
        f.close()

    body = to_plain(response.body)
    data = get_any(body, "Data", "data", default={})
    task_id = get_any(data, "Id", "id")

    if not task_id:
        raise RuntimeError(
            "任务提交成功返回中没有找到任务 ID。\n"
            + json.dumps(body, ensure_ascii=False, indent=2)
        )

    print(f"[提交成功] {file_path.name}")
    print(f"[任务 ID] {task_id}")
    return task_id


def poll_result(client: DocMindClient, task_id: str):
    request = docmind_models.GetTableUnderstandingResultRequest(id=task_id)

    for i in range(1, MAX_POLLS + 1):
        response = client.get_table_understanding_result(request)
        body = to_plain(response.body)

        completed = bool(get_any(body, "Completed", "completed", default=False))
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
                    f"解析失败: status={status}, code={code}, message={message}"
                )
            return body

        time.sleep(POLL_SECONDS)

    raise TimeoutError("超过最大轮询次数，仍未得到解析结果。")


def decode_data(body):
    data = get_any(body, "Data", "data")

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data

    # 某些接口/版本可能外面再包一层列表或 data 字段
    if isinstance(data, list) and len(data) == 1:
        item = data[0]
        if isinstance(item, dict):
            nested = get_any(item, "data", "Data")
            if nested is not None:
                return nested

    return data


def find_tables(obj):
    """递归寻找返回 JSON 中所有名为 tables 的数组。"""
    found = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() == "tables" and isinstance(value, list):
                found.extend(value)
            else:
                found.extend(find_tables(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_tables(item))

    return found


def print_table_summary(data):
    tables = find_tables(data)

    print()
    print("=" * 80)
    print(f"共找到 {len(tables)} 个表格")
    print("=" * 80)

    for ti, table in enumerate(tables, 1):
        name = get_any(table, "tableName", "table_name", default=f"table_{ti}")
        sheet = get_any(table, "sheetNumber", "sheet_number")
        cells = get_any(table, "cells", default=[]) or []

        print(f"\n[表格 {ti}] name={name!r}, sheetNumber={sheet}, cells={len(cells)}")

        for ci, cell in enumerate(cells, 1):
            row = get_any(cell, "rowNumber", "row_number")
            col = get_any(cell, "colNumber", "col_number")
            text = get_any(cell, "content", default="")
            merged = get_any(
                cell, "mergedCell", "isMergedCell",
                "merged_cell", "is_merged_cell"
            )
            first_merged = get_any(
                cell, "firstMergedCell", "isFirstMergedCell",
                "first_merged_cell", "is_first_merged_cell"
            )
            pos = get_any(cell, "pos")

            print(
                f"  cell#{ci:03d} "
                f"row={row!s:<3} col={col!s:<3} "
                f"merged={merged!s:<5} firstMerged={first_merged!s:<5} "
                f"text={text!r}"
            )
            print(f"           pos={pos}")


def process_one(client: DocMindClient, file_path: Path, output_dir: Path):
    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    task_id = submit_file(client, file_path)
    body = poll_result(client, task_id)
    data = decode_data(body)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{file_path.stem}.table.json"

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            {"response": body, "parsed_data": data},
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"[完整结果已保存] {output_path}")
    print_table_summary(data)



def main():
    parser = argparse.ArgumentParser(
        description="用阿里云 Document Mind 表格智能解析处理一个 PDF 文件"
    )
    parser.add_argument("pdf", type=Path, help="本地 PDF 文件")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
        help="原始 JSON 输出目录，默认 outputs",
    )
    args = parser.parse_args()

    if args.pdf.suffix.lower() != ".pdf":
        raise ValueError(f"只支持 PDF 文件: {args.pdf}")

    client = make_client()
    process_one(client, args.pdf, args.output_dir)


if __name__ == "__main__":
    main()