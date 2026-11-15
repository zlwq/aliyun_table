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
    ".pdf", ".doc", ".docx", ".rtf", ".docm",
    ".ppt", ".pptx", ".xls", ".xlsx", ".xlsm",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif",
    ".md", ".markdown", ".html", ".epub", ".mobi", ".txt",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".mp3", ".wav", ".aac",
}


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
    low = {str(k).lower(): v for k, v in d.items()}
    for name in names:
        if name.lower() in low:
            return low[name.lower()]
    return default


def make_client():
    load_dotenv()
    ak = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    sk = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not ak or not sk:
        raise RuntimeError(".env 中缺少 ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    config = open_api_models.Config(access_key_id=ak, access_key_secret=sk)
    config.endpoint = ENDPOINT
    return DocMindClient(config)


def submit(client, path: Path, vlm_enhancement: bool):
    with path.open("rb") as f:
        kwargs = {
            "file_url_object": f,
            "file_name": path.name,
            "output_format": ["markdown", "visualLayoutInfo"],
        }
        if vlm_enhancement:
            kwargs["llm_enhancement"] = True
            kwargs["enhancement_mode"] = "VLM"
            kwargs["output_html_table"] = True

        req = docmind_models.SubmitDocParserJobAdvanceRequest(**kwargs)
        runtime = util_models.RuntimeOptions()
        resp = client.submit_doc_parser_job_advance(req, runtime)

    body = to_plain(resp.body)
    task_id = get_any(get_any(body, "Data", "data", default={}), "Id", "id")
    if not task_id:
        raise RuntimeError("提交成功但未找到任务 ID:\n" + json.dumps(body, ensure_ascii=False, indent=2))
    print(f"[提交成功] {path.name}\n[任务 ID] {task_id}")
    return task_id, body


def wait_until_done(client, task_id: str):
    for i in range(1, MAX_POLLS + 1):
        req = docmind_models.QueryDocParserStatusRequest(id=task_id)
        body = to_plain(client.query_doc_parser_status(req).body)
        data = get_any(body, "Data", "data", default={}) or {}
        status = str(get_any(data, "Status", "status", default="")).lower()
        progress = get_any(data, "Processing", "processing")
        parsed = get_any(data, "NumberOfSuccessfulParsing", "numberOfSuccessfulParsing", "number_of_successful_parsing")
        print(f"[轮询 {i}] status={status or 'unknown'} progress={progress} parsed={parsed}")

        if status == "success":
            return body
        if status == "fail":
            raise RuntimeError("解析失败:\n" + json.dumps(body, ensure_ascii=False, indent=2))
        time.sleep(POLL_SECONDS)
    raise TimeoutError("超过最大轮询次数，任务仍未完成")


def fetch_all_results(client, task_id: str, total_layouts: int, step: int = 500):
    chunks = []
    if not total_layouts:
        total_layouts = step

    start = 0
    while start < total_layouts:
        req = docmind_models.GetDocParserResultRequest(
            id=task_id,
            layout_step_size=min(step, 3000),
            layout_num=start,
        )
        body = to_plain(client.get_doc_parser_result(req).body)
        chunks.append(body)
        start += step
    return chunks


def main():
    ap = argparse.ArgumentParser(description="阿里云 Document Mind：文档解析（大模型版）")
    ap.add_argument("file", type=Path, help="任意一个受支持的本地文件")
    ap.add_argument("--output-dir", type=Path, default=Path("outputs"))
    ap.add_argument("--vlm-enhancement", action="store_true", help="额外开启 VLM 增强（更慢、可能额外计费）")
    ap.add_argument("--chunk-size", type=int, default=500, help="GetDocParserResult 每次获取的 layout 数，1~3000")
    args = ap.parse_args()

    path = args.file
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() not in SUPPORTED:
        print(f"[警告] 扩展名 {path.suffix!r} 不在当前脚本已知支持列表中；仍尝试提交，由服务端最终判断。")
    if not 1 <= args.chunk_size <= 3000:
        raise ValueError("--chunk-size 必须在 1~3000")

    client = make_client()
    task_id, submit_body = submit(client, path, args.vlm_enhancement)
    status_body = wait_until_done(client, task_id)
    status_data = get_any(status_body, "Data", "data", default={}) or {}
    total = int(get_any(status_data, "NumberOfSuccessfulParsing", "numberOfSuccessfulParsing", "number_of_successful_parsing", default=0) or 0)
    chunks = fetch_all_results(client, task_id, total, args.chunk_size)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"{path.stem}.doc_parser_large.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump({
            "task_id": task_id,
            "submit_response": submit_body,
            "status_response": status_body,
            "result_chunks": chunks,
        }, f, ensure_ascii=False, indent=2)
    print(f"[结果已保存] {out}")


if __name__ == "__main__":
    main()
