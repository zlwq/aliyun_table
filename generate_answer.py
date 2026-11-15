#!/usr/bin/env python3
import json
import os
import re
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, BadRequestError
from openpyxl import load_workbook


# ============================================================
# 路径 / 模型配置
# ============================================================

ROOT = Path(__file__).resolve().parent

DOCUMENTS_PATH = ROOT / "documents.txt"
TESTS_PATH = ROOT / "tests.xlsx"
PROMPT_PATH = ROOT / "answer_prompt.txt"
SUBMIT_PATH = ROOT / "submit-template.xlsx"

LOG_DIR = ROOT / "answer_runs"
PROGRESS_PATH = ROOT / "answer_progress.json"

MODEL = "qwen3.8-max"

# Qwen3.8-Max 官方最大回答长度为 131072 tokens。
MAX_OUTPUT_TOKENS = 131072

# 表格问答保留一定推理能力，但不让思维链无限吃输出预算。
ENABLE_THINKING = True
THINKING_BUDGET = 16384

# 单次非流式请求可能运行较久。
REQUEST_TIMEOUT_SECONDS = 3600.0


# ============================================================
# 基础工具
# ============================================================

def normalize_id(value):
    """把 Excel 中的 id 稳定转成用于匹配的字符串。"""
    if value is None:
        return ""

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip()


def make_client():
    load_dotenv(ROOT / ".env")

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 DASHSCOPE_API_KEY，请检查项目主目录下的 .env"
        )

    base_url = os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


# ============================================================
# tests.xlsx
# ============================================================

def load_tests():
    wb = load_workbook(TESTS_PATH, read_only=True, data_only=True)
    ws = wb.active

    headers = [
        normalize_text(cell.value)
        for cell in ws[1]
    ]

    required = [
        "id",
        "file_name",
        "question_type",
        "question",
        "table_hint",
        "answer_format",
    ]

    missing = [
        name for name in required
        if name not in headers
    ]

    if missing:
        raise ValueError(
            f"tests.xlsx 缺少列: {missing}；实际列: {headers}"
        )

    index = {
        name: headers.index(name)
        for name in required
    }

    rows = []

    for excel_row in ws.iter_rows(min_row=2, values_only=True):
        raw_id = excel_row[index["id"]]

        if raw_id is None:
            continue

        item = {
            "id_raw": raw_id,
            "id": normalize_id(raw_id),
            "file_name": normalize_text(
                excel_row[index["file_name"]]
            ),
            "question_type": normalize_text(
                excel_row[index["question_type"]]
            ).lower(),
            "question": normalize_text(
                excel_row[index["question"]]
            ),
            "table_hint": normalize_text(
                excel_row[index["table_hint"]]
            ),
            "answer_format": normalize_text(
                excel_row[index["answer_format"]]
            ).lower(),
        }

        rows.append(item)

    wb.close()

    if not rows:
        raise RuntimeError("tests.xlsx 中没有题目")

    ids = [x["id"] for x in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("tests.xlsx 中存在重复 id")

    return rows


def tests_to_text(tests):
    """
    把 tests.xlsx 转成紧凑 JSONL 文本交给 Qwen。
    这比让模型自己解析 xlsx 更稳定，也不会丢失任何题目字段。
    """
    lines = []

    for item in tests:
        obj = {
            "id": item["id"],
            "file_name": item["file_name"],
            "question_type": item["question_type"],
            "question": item["question"],
            "table_hint": item["table_hint"],
            "answer_format": item["answer_format"],
        }

        lines.append(
            json.dumps(
                obj,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    return "\n".join(lines)


# ============================================================
# submit-template.xlsx
# ============================================================

def open_and_prepare_submit(tests):
    """
    打开 submit-template.xlsx。

    - 必须至少有 id / answer 两列。
    - 如果模板当前只有表头，则自动按 tests.xlsx 顺序补齐 id。
    - 已经存在的答案不删除，因此脚本中断后可以续跑。
    """
    wb = load_workbook(SUBMIT_PATH)
    ws = wb.active

    headers = [
        normalize_text(cell.value)
        for cell in ws[1]
    ]

    if "id" not in headers or "answer" not in headers:
        raise ValueError(
            "submit-template.xlsx 第一行必须包含 id 和 answer"
        )

    id_col = headers.index("id") + 1
    answer_col = headers.index("answer") + 1

    existing = {}

    for row in range(2, ws.max_row + 1):
        key = normalize_id(ws.cell(row, id_col).value)
        if key:
            existing[key] = row

    # 补齐所有题目的 id，但不碰已经填写的 answer。
    for item in tests:
        key = item["id"]

        if key in existing:
            continue

        row = ws.max_row + 1
        ws.cell(row, id_col).value = item["id_raw"]
        ws.cell(row, answer_col).value = ""
        existing[key] = row

    return wb, ws, id_col, answer_col, existing


def save_workbook_atomic(wb):
    """
    先保存临时文件，再原子替换 submit-template.xlsx。
    避免程序恰好在保存过程中被杀导致主文件损坏。
    """
    temp_path = SUBMIT_PATH.with_name(
        SUBMIT_PATH.stem + ".tmp.xlsx"
    )

    wb.save(temp_path)
    os.replace(temp_path, SUBMIT_PATH)


def load_completed_ids(
    tests,
    ws,
    answer_col,
    row_by_id,
):
    """
    读取已完成题目的进度。

    为什么需要独立的 answer_progress.json：
    Excel 里的空答案保存后仍然是空单元格，无法区分：
    - 这题模型已经明确回答为空
    - 这题还没有回答

    所以：
    1. answer_progress.json 记录“已经处理完成的 id”，包括空答案；
    2. 同时把 submit-template.xlsx 中所有非空答案并入完成集合，
       兼容旧版本脚本已经写入的结果。
    """
    valid_ids = {item["id"] for item in tests}
    completed = set()

    if PROGRESS_PATH.is_file():
        try:
            data = json.loads(
                PROGRESS_PATH.read_text(encoding="utf-8")
            )

            for value in data.get("completed_ids", []):
                key = normalize_id(value)
                if key in valid_ids:
                    completed.add(key)

        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(
                f"无法读取进度文件 {PROGRESS_PATH}: {e}"
            ) from e

    # 兼容之前已经写进 Excel 的非空答案。
    for item in tests:
        row = row_by_id[item["id"]]
        value = ws.cell(row, answer_col).value

        if value is not None and str(value) != "":
            completed.add(item["id"])

    return completed


def save_completed_ids(completed_ids, tests):
    """原子保存处理进度。"""
    ordered_ids = [
        item["id"]
        for item in tests
        if item["id"] in completed_ids
    ]

    temp_path = PROGRESS_PATH.with_name(
        PROGRESS_PATH.stem + ".tmp.json"
    )

    temp_path.write_text(
        json.dumps(
            {"completed_ids": ordered_ids},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    os.replace(temp_path, PROGRESS_PATH)


def first_unanswered_index(
    tests,
    completed_ids,
):
    """
    返回第一道尚未完成的题目位置。

    空答案只要已经记录在 answer_progress.json 中，
    就视为“已经回答”，不会再次卡在这里。
    """
    for i, item in enumerate(tests):
        if item["id"] not in completed_ids:
            return i

    return len(tests)


def write_answers(
    pairs,
    ws,
    answer_col,
    row_by_id,
):
    for question, answer in pairs:
        row = row_by_id[question["id"]]
        ws.cell(row, answer_col).value = answer


# ============================================================
# Qwen 输出校验
# ============================================================

NUMBER_RE = re.compile(
    r"^[+-]?"
    r"(?:\d+(?:\.\d*)?|\.\d+)"
    r"(?:[eE][+-]?\d+)?"
    r"%?$"
)


def validate_and_normalize_answer(answer, answer_format):
    """
    返回:
        (True, 规范化后的答案)
        (False, None)

    JSON / JSON_ARRAY 会重新压成单行 JSON，
    方便直接写入 Excel。
    """
    answer = answer.strip()

    # 模型明确给出空答案时，接受它。
    # 这代表“该题回答为空”，不是解析失败。
    if answer == "":
        return True, ""

    if answer_format == "json":
        try:
            obj = json.loads(answer)
        except json.JSONDecodeError:
            return False, None

        if not isinstance(obj, dict):
            return False, None

        required = {
            "row_count",
            "col_count",
            "cells",
        }

        if not required.issubset(obj):
            return False, None

        if (
            not isinstance(obj.get("row_count"), int)
            or not isinstance(obj.get("col_count"), int)
            or not isinstance(obj.get("cells"), list)
        ):
            return False, None

        return True, json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if answer_format == "json_array":
        try:
            obj = json.loads(answer)
        except json.JSONDecodeError:
            return False, None

        if not isinstance(obj, list):
            return False, None

        return True, json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    if answer_format == "number":
        if not NUMBER_RE.fullmatch(answer):
            return False, None

        # 保留模型原始数字表示，只去掉多余空白。
        return True, answer

    if answer_format == "string":
        # 空字符串在比赛规则中也是合法的“无法作答”结果。
        return True, answer

    raise ValueError(
        f"未知 answer_format: {answer_format!r}"
    )


def remove_markdown_fences(text):
    """
    Prompt 已经要求不要输出 Markdown。
    这里仍容忍模型偶尔输出 ``` / ```json。
    """
    result = []

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip().lower()

        if stripped in {
            "```",
            "```text",
            "```json",
            "```jsonl",
        }:
            continue

        result.append(line)

    return result


def parse_model_output(text, expected_tests):
    """
    扫描模型吐出的完整回答，而不是在第一个空答案处停止。

    返回:
        pairs:
            可安全写入 Excel 的 (question, answer)。
            空答案也包含在这里，answer == ""。

        seen_questions:
            模型输出中实际出现过 id 的题目。
            用来判断模型这一轮到底回答到了哪里。

        invalid_questions:
            id 出现了、答案也非空，但答案格式不合法的题目。
            它们不会写入 Excel，也不会标记为完成；
            但解析器会继续扫描后面的题目。

    解析原则：
    - 每个单独一行的题目 id 视为一个答案块的开始；
    - 答案块一直延伸到下一个题目 id；
    - 两个 id 紧挨着 => 前一个答案为空；
    - JSON / JSON_ARRAY 即便意外换行，也会把整个块拼起来解析；
    - 遇到空答案、非法答案、缺失答案都不再 break，
      继续扫完整份模型输出。
    """
    lines = remove_markdown_fences(text)

    id_to_question = {
        question["id"]: question
        for question in expected_tests
    }

    order = {
        question["id"]: i
        for i, question in enumerate(expected_tests)
    }

    # --------------------------------------------------------
    # 找出回答中的题目 id 标记。
    #
    # 模型被要求按题目顺序输出，因此这里只接受严格递增的 id。
    # 这样可以过滤掉大部分“答案本身恰好是一个数字 id”的情况。
    # --------------------------------------------------------
    markers = []
    last_order = -1

    for line_index, line in enumerate(lines):
        key = line.strip()

        if key not in id_to_question:
            continue

        current_order = order[key]

        if current_order <= last_order:
            continue

        markers.append(
            (line_index, key, current_order)
        )
        last_order = current_order

    pairs = []
    seen_questions = []
    invalid_questions = []

    for marker_index, (line_index, key, current_order) in enumerate(markers):
        question = id_to_question[key]
        seen_questions.append(question)

        answer_start = line_index + 1

        if marker_index + 1 < len(markers):
            answer_end = markers[marker_index + 1][0]
        else:
            answer_end = len(lines)

        raw_lines = lines[answer_start:answer_end]

        # 去掉答案块首尾的空行，但保留内容内部换行。
        while raw_lines and raw_lines[0].strip() == "":
            raw_lines.pop(0)

        while raw_lines and raw_lines[-1].strip() == "":
            raw_lines.pop()

        raw_answer = "\n".join(raw_lines).strip()

        # 空答案是明确答案，直接接受。
        if raw_answer == "":
            pairs.append((question, ""))
            continue

        ok, normalized = validate_and_normalize_answer(
            raw_answer,
            question["answer_format"],
        )

        if ok:
            pairs.append((question, normalized))
        else:
            invalid_questions.append(
                (question, raw_answer)
            )

    return pairs, seen_questions, invalid_questions


# ============================================================
# Prompt
# ============================================================

CONTROL_INSTRUCTION = """
【本轮控制要求】

你必须严格遵守 answer_prompt.txt 中的全部规则。

本轮只允许回答从指定 START_ID 开始（包含 START_ID）以及它之后的题目。
START_ID 之前的题目即使出现在 tests.xlsx 内容中，也绝对不要重新回答。

最终只能输出：
id
answer
id
answer
...

不要解释，不要输出标题，不要输出 Markdown，不要输出代码块。
每个 id 单独一行，每个 answer 紧接着单独一行。
json 和 json_array 必须尽量压缩成单行。

必须严格按照 tests.xlsx 中的原始题目顺序连续回答。
不要跳过中间题目。
如果某一道题无法作答，仍然输出该 id，并让 answer 行为空；然后必须继续回答后面的题目，绝对不能因为空答案而停止。
"""


def build_messages(
    answer_prompt,
    documents_text,
    tests_text,
    start_id,
):
    system_text = (
        answer_prompt.rstrip()
        + "\n\n"
        + CONTROL_INSTRUCTION.strip()
    )

    user_text = f"""
START_ID:
{start_id}

下面是完整的 documents.txt 内容：
<documents.txt>
{documents_text}
</documents.txt>

下面是完整 tests.xlsx 的内容。
为了让你稳定读取，程序已经逐行转换成等价 JSONL；
每一行就是 tests.xlsx 中的一道题，字段和值与 Excel 一致：
<tests.xlsx>
{tests_text}
</tests.xlsx>

现在从 START_ID={start_id} 对应的题目开始回答。
START_ID 之前的题目不要输出。
""".strip()

    return [
        {
            "role": "system",
            "content": system_text,
        },
        {
            "role": "user",
            "content": user_text,
        },
    ]


# ============================================================
# 单轮调用
# ============================================================

def ask_qwen(
    client,
    answer_prompt,
    documents_text,
    tests_text,
    start_id,
):
    messages = build_messages(
        answer_prompt=answer_prompt,
        documents_text=documents_text,
        tests_text=tests_text,
        start_id=start_id,
    )

    completion = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=MAX_OUTPUT_TOKENS,
        extra_body={
            "enable_thinking": ENABLE_THINKING,
            "thinking_budget": THINKING_BUDGET,
        },
    )

    if not completion.choices:
        raise RuntimeError("Qwen 返回 choices 为空")

    choice = completion.choices[0]

    content = choice.message.content or ""
    finish_reason = choice.finish_reason

    return content, finish_reason, completion


# ============================================================
# main
# ============================================================

def main():
    for path in (
        DOCUMENTS_PATH,
        TESTS_PATH,
        PROMPT_PATH,
        SUBMIT_PATH,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    print("[读取] documents.txt")
    documents_text = DOCUMENTS_PATH.read_text(
        encoding="utf-8"
    )

    print("[读取] answer_prompt.txt")
    answer_prompt = PROMPT_PATH.read_text(
        encoding="utf-8"
    )

    print("[读取] tests.xlsx")
    tests = load_tests()
    tests_text = tests_to_text(tests)

    print(f"[题目数] {len(tests)}")

    wb, ws, id_col, answer_col, row_by_id = (
        open_and_prepare_submit(tests)
    )

    # 先保存一次，确保模板中已经包含全部 id。
    save_workbook_atomic(wb)

    completed_ids = load_completed_ids(
        tests=tests,
        ws=ws,
        answer_col=answer_col,
        row_by_id=row_by_id,
    )

    # 即便是首次使用新版脚本，也把旧 Excel 中已有的答案写入进度。
    save_completed_ids(completed_ids, tests)

    start_index = first_unanswered_index(
        tests=tests,
        completed_ids=completed_ids,
    )

    if start_index >= len(tests):
        print("[完成] submit-template.xlsx 已经包含全部答案")
        return

    print(
        f"[续跑位置] 第 {start_index + 1}/{len(tests)} 题，"
        f"id={tests[start_index]['id']}"
    )

    client = make_client()

    round_no = 0
    no_progress_rounds = 0

    while start_index < len(tests):
        round_no += 1

        start_id = tests[start_index]["id"]
        remaining = tests[start_index:]

        print()
        print("=" * 72)
        print(
            f"[第 {round_no} 轮] "
            f"从 id={start_id} 开始，"
            f"剩余 {len(remaining)} 题"
        )
        print("=" * 72)

        try:
            content, finish_reason, completion = ask_qwen(
                client=client,
                answer_prompt=answer_prompt,
                documents_text=documents_text,
                tests_text=tests_text,
                start_id=start_id,
            )

        except BadRequestError as e:
            raise RuntimeError(
                "Qwen 请求被拒绝。"
                "如果错误信息提到 context/token length，"
                "说明 documents.txt + tests.xlsx + prompt 已超过输入上下文。"
                f"\n原始错误:\n{e}"
            ) from e

        # 无论解析是否成功，都先保存这一轮模型原始回答。
        log_path = (
            LOG_DIR
            / f"round_{round_no:03d}_from_{start_id}.txt"
        )

        log_path.write_text(
            content,
            encoding="utf-8",
        )

        print(f"[finish_reason] {finish_reason}")
        print(f"[原始回答] {log_path.relative_to(ROOT)}")

        pairs, seen_questions, invalid_questions = (
            parse_model_output(
                content,
                remaining,
            )
        )

        if seen_questions:
            print(
                f"[模型覆盖] 共看到 {len(seen_questions)} 个 id，"
                f"id={seen_questions[0]['id']} ... "
                f"{seen_questions[-1]['id']}"
            )
        else:
            print("[模型覆盖] 没有识别到任何题目 id")

        empty_count = sum(
            1
            for _, answer in pairs
            if answer == ""
        )

        if empty_count:
            print(
                f"[空答案] {empty_count} 条；"
                "按模型明确回答为空处理，并继续扫描后续 id"
            )

        if invalid_questions:
            preview = ", ".join(
                q["id"]
                for q, _ in invalid_questions[:10]
            )

            suffix = (
                " ..."
                if len(invalid_questions) > 10
                else ""
            )

            print(
                f"[格式异常] {len(invalid_questions)} 条，"
                f"id={preview}{suffix}；"
                "这些题暂不写入，但不会阻止扫描后面的答案"
            )

        # ----------------------------------------------------
        # length 安全规则：
        # 最后一个出现的 id 位于物理截断边界附近，
        # 无论它看起来是否完整，都不采纳这一题。
        # 下一轮会从它或更早的未完成题重新回答。
        # ----------------------------------------------------
        risky_id = None

        if finish_reason == "length" and seen_questions:
            risky_id = seen_questions[-1]["id"]

            print(
                "[长度截断] "
                f"边界题 id={risky_id} 不采纳，下一轮重新回答"
            )

            pairs = [
                (question, answer)
                for question, answer in pairs
                if question["id"] != risky_id
            ]

            invalid_questions = [
                (question, answer)
                for question, answer in invalid_questions
                if question["id"] != risky_id
            ]

        # 只写尚未完成的题，避免续跑时覆盖之前已经确认的答案。
        new_pairs = [
            (question, answer)
            for question, answer in pairs
            if question["id"] not in completed_ids
        ]

        if new_pairs:
            write_answers(
                pairs=new_pairs,
                ws=ws,
                answer_col=answer_col,
                row_by_id=row_by_id,
            )

            # 先保存 Excel，再保存进度。
            save_workbook_atomic(wb)

            for question, _ in new_pairs:
                completed_ids.add(question["id"])

            save_completed_ids(
                completed_ids,
                tests,
            )

            print(
                f"[本轮写入] {len(new_pairs)} 条，"
                f"其中空答案 "
                f"{sum(1 for _, a in new_pairs if a == '')} 条"
            )
            print(f"[已保存] {SUBMIT_PATH.name}")
            print(f"[已保存进度] {PROGRESS_PATH.name}")

            no_progress_rounds = 0
        else:
            no_progress_rounds += 1

            print(
                "[警告] 本轮没有新增可安全写入的答案"
            )

        # 下一轮永远从“第一道尚未完成的题”开始。
        # 这样即使模型中间某题格式坏了，但后面几百题都已经答好，
        # 后面的结果也会保留，不会因为前面一题而全部丢弃。
        start_index = first_unanswered_index(
            tests=tests,
            completed_ids=completed_ids,
        )

        if start_index >= len(tests):
            break

        next_id = tests[start_index]["id"]

        if finish_reason == "length":
            print(
                f"[截断后续跑] 下一轮从首个未完成 id={next_id} 开始"
            )
        else:
            print(
                f"[下一轮] 从首个未完成 id={next_id} 开始"
            )

        # 防止模型连续多轮完全不给任何可用结果造成无限循环。
        if no_progress_rounds >= 2:
            raise RuntimeError(
                "连续两轮没有获得任何新增可安全保存的答案，已停止。"
                "请查看 answer_runs/ 中最近两轮原始输出。"
            )

    save_workbook_atomic(wb)
    save_completed_ids(completed_ids, tests)

    print()
    print("=" * 72)
    print(
        f"[全部完成] 共 {len(tests)} 题，"
        f"答案已写入 {SUBMIT_PATH}"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()