#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from alibabacloud_docmind_api20220711.client import Client as DocMindClient
from alibabacloud_docmind_api20220711 import models as docmind_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

ENDPOINT = "docmind-api.cn-hangzhou.aliyuncs.com"
SUPPORTED = {
    ".html", ".pdf", ".ppt", ".pptx", ".xlsx", ".xls",
    ".doc", ".docx", ".mobi", ".epub", ".md", ".markdown", ".txt",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}


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
    ap = argparse.ArgumentParser(description="阿里云 Document Mind：电子文档解析（同步）")
    ap.add_argument("file", type=Path, help="本地电子文档；不支持图片和扫描 PDF")
    ap.add_argument("--output-dir", type=Path, default=Path("outputs"))
    args = ap.parse_args()

    path = args.file
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        raise ValueError("电子文档解析不支持图片。图片请使用文档智能解析/大模型版/文档抽取。")
    if suffix not in SUPPORTED:
        print(f"[警告] 扩展名 {suffix!r} 不在官方当前支持列表中；仍尝试提交，由服务端判断。")
    if suffix == ".pdf":
        print("[提示] 电子文档解析只适用于有文本层的电子 PDF，不支持扫描版 PDF。")

    client = make_client()
    with path.open("rb") as f:
        req = docmind_models.SubmitDigitalDocStructureJobAdvanceRequest(
            file_url_object=f,
            file_name=path.name,
            reveal_markdown=True,
        )
        runtime = util_models.RuntimeOptions()
        runtime.connect_timeout = 10000
        runtime.read_timeout = 300000
        resp = client.submit_digital_doc_structure_job_advance(req, runtime)

    body = to_plain(resp.body)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"{path.stem}.digital_doc_structure.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    print(f"[完成] status={body.get('Status', body.get('status'))}")
    print(f"[结果已保存] {out}")


if __name__ == "__main__":
    main()
