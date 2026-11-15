#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from alibabacloud_ocr_api20210707.client import Client as OcrClient
from alibabacloud_ocr_api20210707 import models as ocr_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models


ENDPOINT = "ocr-api.cn-hangzhou.aliyuncs.com"


def make_client() -> OcrClient:
    load_dotenv()

    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")

    if not access_key_id or not access_key_secret:
        raise RuntimeError(
            "缺少 ALIBABA_CLOUD_ACCESS_KEY_ID 或 "
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET，请检查 .env"
        )

    config = open_api_models.Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
    )
    config.endpoint = ENDPOINT

    return OcrClient(config)


def main():
    if len(sys.argv) != 2:
        print("用法: python aliyun_ocr.py 文件路径")
        sys.exit(1)

    file_path = Path(sys.argv[1])

    if not file_path.is_file():
        raise FileNotFoundError(file_path)

    client = make_client()
    runtime = util_models.RuntimeOptions()

    with file_path.open("rb") as f:
        request = ocr_models.RecognizeGeneralRequest(body=f)
        response = client.recognize_general_with_options(request, runtime)

    data = response.body.data

    if isinstance(data, str):
        data = json.loads(data)

    print(data.get("content", ""))


if __name__ == "__main__":
    main()
