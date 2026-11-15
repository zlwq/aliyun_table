#!/usr/bin/env python3
"""
build_documents_txt_v2.py

流程：
1. 遍历 documents/ 中的 PDF / 图片。
2. 对每个文件执行：
       python pipeline_parser_large.py <file>
3. 收集 pipeline 本次生成的有效标准表格 JSON（cells >= 5）。
4. 如果 pipeline 日志出现 [放弃] xxx.png，说明该图片已经是 fallback 链路中
   没能成功识别的对象；把它对应的“最后一次尝试图片”（优先 rot90）交给 Qwen：
       python qwen_file_cli.py --file <图片> --prompt <prompt.txt全文>
5. 收集 Qwen 生成的标准 JSON。
6. 最终写入 documents.txt：

       === 001.pdf ===
       {一张表的标准 JSON}

       === 002.png ===
       {一张表的标准 JSON}

说明：Qwen fallback 只接图片，不再考虑 PDF/其它文件类型。
"""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DOCUMENTS_DIR = ROOT / "documents"
OUTPUTS_DIR = ROOT / "outputs"
RENDERED_DIR = ROOT / "rendered_pages"
PROMPT_PATH = ROOT / "prompt.txt"
PIPELINE_SCRIPT = ROOT / "pipeline_parser_large.py"
QWEN_SCRIPT = ROOT / "qwen_file_cli.py"
DOCUMENTS_TXT = ROOT / "documents.txt"

MIN_VALID_CELLS = 5
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
INPUT_SUFFIXES = IMAGE_SUFFIXES | {".pdf"}


# ============================================================
# 标准 JSON
# ============================================================

def is_standard_table(obj):
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("row_count"), int)
        and isinstance(obj.get("col_count"), int)
        and isinstance(obj.get("cells"), list)
        and len(obj["cells"]) >= MIN_VALID_CELLS
    )


def find_standard_tables(obj):
    if is_standard_table(obj):
        return [obj]

    found = []

    if isinstance(obj, dict):
        for value in obj.values():
            found.extend(find_standard_tables(value))

    elif isinstance(obj, list):
        for value in obj:
            found.extend(find_standard_tables(value))

    return found


def table_key(table):
    return json.dumps(table, ensure_ascii=False, sort_keys=True)


def dedupe_tables(tables):
    result = []
    seen = set()

    for table in tables:
        key = table_key(table)
        if key not in seen:
            seen.add(key)
            result.append(table)

    return result


# ============================================================
# 只收集“本次命令新生成/更新”的 JSON
# ============================================================

def json_files():
    files = []

    if OUTPUTS_DIR.exists():
        files.extend(OUTPUTS_DIR.rglob("*.json"))

    files.extend(ROOT.glob("*.json"))
    return [p for p in files if p.is_file()]


def snapshot_jsons():
    return {
        p.resolve(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in json_files()
    }


def collect_changed_tables(before):
    tables = []

    for path in json_files():
        stat = path.stat()
        current = (stat.st_mtime_ns, stat.st_size)

        if before.get(path.resolve()) == current:
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        found = find_standard_tables(data)
        if found:
            print(f"[收集] {path.relative_to(ROOT)} -> {len(found)} 张表")
            tables.extend(found)

    return dedupe_tables(tables)


# ============================================================
# 执行子程序
# ============================================================

def run(args):
    print("\n$ " + " ".join(map(str, args)))

    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")

    return result


# ============================================================
# pipeline 最终失败图片 -> Qwen 图片
# ============================================================

def abandoned_image_names(log_text):
    """读取 pipeline 输出中的：[放弃] xxx.png: ..."""
    names = []

    for line in log_text.splitlines():
        if "[放弃]" not in line:
            continue

        name = line.split("[放弃]", 1)[1].split(":", 1)[0].strip()

        if Path(name).suffix.lower() in IMAGE_SUFFIXES and name not in names:
            names.append(name)

    return names


def final_image_path(source, abandoned_name):
    """
    pipeline 对图片的最后一次尝试是顺时针 90° 图。
    如果旋转图确实存在，就把旋转图交给 Qwen；否则退回那张原始 fallback 图片。
    """
    abandoned = Path(abandoned_name)

    if source.suffix.lower() == ".pdf":
        rotated = (
            OUTPUTS_DIR
            / "_fallback"
            / f"{source.stem}_pdf"
            / "rotated"
            / f"{abandoned.stem}_rot90.png"
        )
        original = RENDERED_DIR / abandoned.name

    else:
        rotated = (
            OUTPUTS_DIR
            / "_fallback"
            / f"{source.stem}_image"
            / f"{source.stem}_rot90.png"
        )
        original = source

    if rotated.is_file():
        return rotated

    if original.is_file():
        return original

    raise FileNotFoundError(
        f"找不到 pipeline 最后尝试的图片：{abandoned_name}"
    )


def qwen_on_image(image_path, prompt_text):
    if image_path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"Qwen fallback 只允许图片：{image_path}")

    before = snapshot_jsons()

    result = run([
        sys.executable,
        str(QWEN_SCRIPT),
        "--file",
        str(image_path),
        "--prompt",
        prompt_text,
    ])

    tables = collect_changed_tables(before)

    if result.returncode != 0:
        print(f"[Qwen 警告] 退出码={result.returncode}")

    return tables


# ============================================================
# 单个原始文件
# ============================================================

def process_file(source, prompt_text):
    print("\n" + "=" * 72)
    print(f"[处理] {source.relative_to(ROOT)}")
    print("=" * 72)

    before = snapshot_jsons()

    result = run([
        sys.executable,
        str(PIPELINE_SCRIPT),
        str(source),
    ])

    tables = collect_changed_tables(before)

    # pipeline 明确放弃的图片，再交给 Qwen。
    for image_name in abandoned_image_names(result.stdout or ""):
        image_path = final_image_path(source, image_name)
        print(f"[Qwen fallback] {image_path.relative_to(ROOT)}")
        tables.extend(qwen_on_image(image_path, prompt_text))

    tables = dedupe_tables(tables)

    print(f"[结果] {source.name}: {len(tables)} 张有效表格")
    return tables


# ============================================================
# documents.txt
# ============================================================

def write_documents_txt(records):
    parts = []

    for source, tables in records:
        for table in tables:
            parts.append(
                f"=== {source.name} ===\n"
                + json.dumps(
                    table,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

    DOCUMENTS_TXT.write_text(
        "\n\n".join(parts) + ("\n" if parts else ""),
        encoding="utf-8",
    )

    print(f"\n[完成] {DOCUMENTS_TXT}")


# ============================================================
# main
# ============================================================

def main():
    for required in (
        DOCUMENTS_DIR,
        PROMPT_PATH,
        PIPELINE_SCRIPT,
        QWEN_SCRIPT,
    ):
        if not required.exists():
            raise FileNotFoundError(required)

    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")

    sources = sorted(
        p for p in DOCUMENTS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in INPUT_SUFFIXES
    )

    if not sources:
        raise RuntimeError("documents/ 中没有 PDF 或图片")

    records = []
    failures = []

    for i, source in enumerate(sources, 1):
        print(f"\n######## {i}/{len(sources)} ########")

        try:
            tables = process_file(source, prompt_text)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[失败] {source.name}: {type(e).__name__}: {e}")
            tables = []

        records.append((source, tables))

        if not tables:
            failures.append(source)

    write_documents_txt(records)

    if failures:
        failed_names = "\n".join(f"  - {p.name}" for p in failures)
        raise RuntimeError(
            "以下文件最终没有得到有效标准表格：\n"
            + failed_names
        )

    print(
        f"[全部完成] 文件={len(records)}, "
        f"表格={sum(len(tables) for _, tables in records)}"
    )


if __name__ == "__main__":
    main()
