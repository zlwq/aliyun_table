#!/usr/bin/env python3
import sys
from pathlib import Path
from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "submit-template.xlsx"
OUTPUT = ROOT / "submit-final_v1.xlsx"


def main():
    # 用法：
    # python fill_submission_v1.py answer_runs/round_001_from_1.txt
    if len(sys.argv) != 2:
        print(
            "用法: python fill_submission_v1.py "
            "answer_runs/round_001_from_1.txt"
        )
        raise SystemExit(1)

    answer_path = Path(sys.argv[1])

    if not answer_path.is_file():
        raise FileNotFoundError(answer_path)

    if not TEMPLATE.is_file():
        raise FileNotFoundError(TEMPLATE)

    # 模型输出格式：
    # id
    # answer
    # id
    # answer
    # ...
    lines = (
        answer_path.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    )

    # 只去掉文件最后的换行，不碰中间的空答案。
    if lines and lines[-1] == "":
        lines.pop()

    if len(lines) % 2 != 0:
        raise RuntimeError(
            f"答案文件行数异常：{len(lines)}，不是偶数"
        )

    pairs = []

    for i in range(0, len(lines), 2):
        qid = lines[i].strip()
        answer = lines[i + 1]

        if not qid.isdigit():
            raise RuntimeError(
                f"第 {i + 1} 行不是合法 id: {qid!r}"
            )

        pairs.append((qid, answer))

    ids = [qid for qid, _ in pairs]

    if len(ids) != len(set(ids)):
        raise RuntimeError("答案文件中存在重复 id")

    # 本次题目固定为 1..908。
    expected = [str(i) for i in range(1, 909)]

    missing = [x for x in expected if x not in ids]
    extra = [x for x in ids if x not in expected]

    if missing or extra:
        raise RuntimeError(
            f"id 不完整。\n"
            f"缺失: {missing}\n"
            f"异常: {extra}"
        )

    # 再保证顺序也是 1..908。
    if ids != expected:
        raise RuntimeError("id 顺序不是严格的 1..908")

    wb = load_workbook(TEMPLATE)
    ws = wb.active

    headers = {
        str(cell.value).strip(): cell.column
        for cell in ws[1]
        if cell.value is not None
    }

    if "id" not in headers or "answer" not in headers:
        raise RuntimeError(
            "submit-template.xlsx 第一行必须包含 id 和 answer"
        )

    id_col = headers["id"]
    answer_col = headers["answer"]

    # 从第2行开始写 908 道题。
    # 所有答案都按字符串原样写入，不擅自改数字、JSON、百分号等内容。
    for row_index, (qid, answer) in enumerate(
        pairs,
        start=2,
    ):
        ws.cell(row_index, id_col).value = qid
        ws.cell(row_index, answer_col).value = answer

    wb.save(OUTPUT)

    print(f"[完成] 共写入 {len(pairs)} 道题")
    print("[检查] id=1..908 全部存在，无重复、无遗漏")
    print(f"[输出] {OUTPUT}")


if __name__ == "__main__":
    main()
