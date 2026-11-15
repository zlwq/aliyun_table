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
SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".gif"}


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


def main():
    ap = argparse.ArgumentParser(description="阿里云 Document Mind：文档抽取（通用 KV 抽取）")
    ap.add_argument("file", type=Path, help="PDF 或图片")
    ap.add_argument("--output-dir", type=Path, default=Path("outputs"))
    args = ap.parse_args()

    path = args.file
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError("文档抽取官方仅支持 PDF 和图片：jpg/jpeg/png/bmp/gif")

    client = make_client()
    with path.open("rb") as f:
        req = docmind_models.SubmitDocumentExtractJobAdvanceRequest(
            file_url_object=f,
            file_name=path.name,
        )
        runtime = util_models.RuntimeOptions()
        submit_body = to_plain(client.submit_document_extract_job_advance(req, runtime).body)

    task_id = get_any(get_any(submit_body, "Data", "data", default={}), "Id", "id")
    if not task_id:
        raise RuntimeError("提交成功但未找到任务 ID:\n" + json.dumps(submit_body, ensure_ascii=False, indent=2))
    print(f"[提交成功] {path.name}\n[任务 ID] {task_id}")

    result_body = None
    for i in range(1, MAX_POLLS + 1):
        req = docmind_models.GetDocumentExtractResultRequest(id=task_id)
        body = to_plain(client.get_document_extract_result(req).body)
        completed = bool(get_any(body, "Completed", "completed", default=False))
        status = get_any(body, "Status", "status")
        code = get_any(body, "Code", "code")
        print(f"[轮询 {i}] completed={completed} status={status} code={code}")
        if completed:
            if status and str(status).lower() != "success":
                raise RuntimeError("抽取失败:\n" + json.dumps(body, ensure_ascii=False, indent=2))
            result_body = body
            break
        time.sleep(POLL_SECONDS)
    else:
        raise TimeoutError("超过最大轮询次数，任务仍未完成")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"{path.stem}.document_extract.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump({"submit_response": submit_body, "result_response": result_body}, f, ensure_ascii=False, indent=2)
    print(f"[结果已保存] {out}")


if __name__ == "__main__":
    main()
