#!/usr/bin/env python3

import argparse
from pathlib import Path
from openpyxl import load_workbook


def normalize_id(value):
    if value is None:
        return ""

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value).strip()


def read_answer_txt(path: Path):
    """
    txt 格式：
        id
        answer
        id
        answer
        ...

    answer 可以为空字符串。
    文件末尾多余的空行会自动忽略。
    """
    text = path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # splitlines() 能保留中间的空答案，
    # 同时不会因为普通的单个结尾换行多造一行。
    lines = text.splitlines()

    # 如果文件末尾还有额外空行，且导致总行数为奇数，
    # 只删除这些“多余的结尾空行”。
    # 中间的空答案（例如 863 后面的空行）不会被动。
    while len(lines) % 2 != 0 and lines and lines[-1] == "":
        lines.pop()

    if len(lines) % 2 != 0:
        raise ValueError(
            f"txt 行数不是偶数，共 {len(lines)} 行。\n"
            "必须严格满足：一行 id，一行答案。"
        )

    pairs = []

    for i in range(0, len(lines), 2):
        id_value = lines[i].strip()
        answer = lines[i + 1]

        if id_value == "":
            raise ValueError(
                f"第 {i + 1} 行 id 为空，请检查 txt 是否多了空行。"
            )

        pairs.append((id_value, answer))

    return pairs


def main():
    parser = argparse.ArgumentParser(
        description="按 txt 中的一行 id、一行答案，复写 xlsx 第二列"
    )
    parser.add_argument("txt_file", help="答案 txt 文件")
    parser.add_argument("xlsx_file", help="要复写的 xlsx 文件")
    args = parser.parse_args()

    txt_path = Path(args.txt_file)
    xlsx_path = Path(args.xlsx_file)

    if not txt_path.is_file():
        raise FileNotFoundError(f"找不到 txt 文件: {txt_path}")

    if not xlsx_path.is_file():
        raise FileNotFoundError(f"找不到 xlsx 文件: {xlsx_path}")

    pairs = read_answer_txt(txt_path)

    wb = load_workbook(xlsx_path)
    ws = wb.active

    row_by_id = {}

    # 第一行是表头，从第二行开始读取第一列 id。
    for row in range(2, ws.max_row + 1):
        key = normalize_id(ws.cell(row=row, column=1).value)
        if key:
            row_by_id[key] = row

    updated = []
    not_found = []

    for raw_id, answer in pairs:
        key = normalize_id(raw_id)
        row = row_by_id.get(key)

        if row is None:
            not_found.append(key)
            continue

        # 第二列直接覆盖，按字符串原样写入。
        ws.cell(row=row, column=2).value = answer
        updated.append(key)

    wb.save(xlsx_path)
    wb.close()

    print(f"[完成] 已复写 {len(updated)} 个答案")
    print(f"[文件] {xlsx_path}")

    if updated:
        print("[已更新 id]")
        print(" ".join(updated))

    if not_found:
        print()
        print(f"[警告] {len(not_found)} 个 id 在 xlsx 第一列中未找到：")
        print(" ".join(not_found))


if __name__ == "__main__":
    main()