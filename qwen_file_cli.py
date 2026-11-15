#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openpyxl import load_workbook


DEFAULT_MODEL = "qwen3.8-max"

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json",
    ".py", ".log", ".yaml", ".yml", ".xml",
}

EXCEL_EXTS = {".xlsx"}


def make_client() -> OpenAI:
    load_dotenv()

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 DASHSCOPE_API_KEY，请检查 .env"
        )

    base_url = os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=3600.0,
    )


def read_text_file(path: Path) -> str:
    """
    读取普通文本文件。
    优先 UTF-8，失败时尝试 GB18030。
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(
            encoding="gb18030",
            errors="replace",
        )


def xlsx_to_text(path: Path) -> str:
    """
    把 xlsx 转成紧凑文本。

    每个 sheet 都会保留名称；
    每一行使用 JSON 数组表示，
    能稳定保留列顺序和空单元格。
    """
    wb = load_workbook(
        path,
        read_only=True,
        data_only=True,
    )

    parts = []

    try:
        for ws in wb.worksheets:
            parts.append(
                f"--- SHEET: {ws.title} ---"
            )

            for row in ws.iter_rows(values_only=True):
                values = list(row)

                # 只去掉行尾完全无意义的空单元格，
                # 中间空值保留。
                while values and values[-1] is None:
                    values.pop()

                if not values:
                    continue

                parts.append(
                    json.dumps(
                        values,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                )

    finally:
        wb.close()

    return "\n".join(parts)


def file_to_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix in TEXT_EXTS:
        return read_text_file(path)

    if suffix in EXCEL_EXTS:
        return xlsx_to_text(path)

    raise ValueError(
        f"多文件 qwen3.8-max 文本链路暂不支持: {path.name}\n"
        "当前支持: "
        + ", ".join(
            sorted(TEXT_EXTS | EXCEL_EXTS)
        )
    )


def build_combined_input(paths: list[Path]) -> str:
    """
    将多个文件内容直接拼成一个大文本，
    之后作为普通 messages 内容发给 qwen3.8-max。

    不上传 File API。
    不使用 fileid://。
    不使用 qwen-doc-turbo。
    """
    parts = []

    for index, path in enumerate(
        paths,
        start=1,
    ):
        content = file_to_text(path)

        parts.append(
            "\n".join(
                [
                    (
                        f"===== BEGIN FILE {index}: "
                        f"{path.name} ====="
                    ),
                    content,
                    (
                        f"===== END FILE {index}: "
                        f"{path.name} ====="
                    ),
                ]
            )
        )

    return "\n\n".join(parts)


def ask_qwen_max(
    client: OpenAI,
    paths: list[Path],
    prompt: str,
    model: str,
    enable_thinking: bool,
    thinking_budget: int,
    max_tokens: int,
) -> str:
    combined_files = build_combined_input(paths)

    user_text = (
        "下面是用户提供的多个文件内容。"
        "请严格区分各文件边界，并结合这些文件回答后面的要求。\n\n"
        f"{combined_files}\n\n"
        "===== USER REQUEST =====\n"
        f"{prompt}"
    )

    extra_body = {}

    if enable_thinking:
        extra_body["enable_thinking"] = True
        extra_body["thinking_budget"] = thinking_budget
    else:
        extra_body["enable_thinking"] = False

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant.",
            },
            {
                "role": "user",
                "content": user_text,
            },
        ],
        max_tokens=max_tokens,
        extra_body=extra_body,
    )

    if not completion.choices:
        raise RuntimeError("Qwen 返回 choices 为空")

    choice = completion.choices[0]

    print(
        f"[finish_reason] "
        f"{choice.finish_reason}"
    )

    usage = getattr(completion, "usage", None)
    if usage is not None:
        prompt_tokens = getattr(
            usage,
            "prompt_tokens",
            None,
        )
        completion_tokens = getattr(
            usage,
            "completion_tokens",
            None,
        )
        total_tokens = getattr(
            usage,
            "total_tokens",
            None,
        )

        print(
            "[tokens] "
            f"input={prompt_tokens} "
            f"output={completion_tokens} "
            f"total={total_tokens}"
        )

    return choice.message.content or ""


def main():
    parser = argparse.ArgumentParser(
        description=(
            "使用 qwen3.8-max 直接读取多个本地文本/XLSX文件，"
            "把内容拼进 messages 后一次性回答。"
            "不走 File API，不使用 qwen-doc-turbo。"
        )
    )

    parser.add_argument(
        "--file",
        nargs="+",
        required=True,
        help=(
            "一个或多个文件，例如："
            "--file answer_prompt.txt tests.xlsx documents.txt"
        ),
    )

    parser.add_argument(
        "--prompt",
        required=True,
        help="发送给 qwen3.8-max 的具体要求",
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            f"模型名称，默认 {DEFAULT_MODEL}"
        ),
    )

    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="关闭 thinking；默认开启",
    )

    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=16384,
        help="thinking budget，默认 16384",
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=131072,
        help="最大输出 tokens，默认 131072",
    )

    args = parser.parse_args()

    paths = [
        Path(value)
        for value in args.file
    ]

    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(
                f"找不到文件: {path}"
            )

    print("[处理方式] qwen3.8-max 直接文本上下文")
    print(f"[模型] {args.model}")
    print(
        f"[thinking] "
        f"{'关闭' if args.no_thinking else '开启'}"
    )

    print("[文件]")
    for path in paths:
        print(f"  - {path}")

    combined = build_combined_input(paths)
    print(
        f"[合并文本字符数] {len(combined):,}"
    )

    client = make_client()

    answer = ask_qwen_max(
        client=client,
        paths=paths,
        prompt=args.prompt,
        model=args.model,
        enable_thinking=not args.no_thinking,
        thinking_budget=args.thinking_budget,
        max_tokens=args.max_tokens,
    )

    print()
    print(answer)


if __name__ == "__main__":
    main()