#!/usr/bin/env python3
import argparse
import importlib.util
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

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif",
}
PDF_SUFFIXES = {".pdf"}

# 最终标准表格少于 5 个 cell 时，视为“结果太短”。
MIN_VALID_CELLS = 5


class JsonPipelineError(RuntimeError):
    """
    模型任务已经正常返回，但在：
    - JSON 精简
    - 表格结构寻找
    - 标准 JSON 转换
    - 标准 JSON 校验

    这些阶段出现的错误。

    PDF 遇到这类错误时，不直接终止，
    而是进入“PDF -> 每页图片”的 fallback。
    """
    pass


# ============================================================
# 1. 阿里云文档解析（大模型版）
# ============================================================

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
        raise RuntimeError(
            ".env 中缺少 "
            "ALIBABA_CLOUD_ACCESS_KEY_ID / "
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )

    config = open_api_models.Config(
        access_key_id=ak,
        access_key_secret=sk,
    )
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

    task_id = get_any(
        get_any(body, "Data", "data", default={}),
        "Id",
        "id",
    )

    if not task_id:
        raise RuntimeError(
            "提交成功但未找到任务 ID:\n"
            + json.dumps(body, ensure_ascii=False, indent=2)
        )

    print(f"[提交成功] {path.name}")
    print(f"[任务 ID] {task_id}")

    return task_id, body


def wait_until_done(client, task_id: str):
    for i in range(1, MAX_POLLS + 1):
        req = docmind_models.QueryDocParserStatusRequest(id=task_id)
        body = to_plain(client.query_doc_parser_status(req).body)

        data = get_any(body, "Data", "data", default={}) or {}

        status = str(
            get_any(data, "Status", "status", default="")
        ).lower()

        progress = get_any(data, "Processing", "processing")

        parsed = get_any(
            data,
            "NumberOfSuccessfulParsing",
            "numberOfSuccessfulParsing",
            "number_of_successful_parsing",
        )

        print(
            f"[轮询 {i}] "
            f"status={status or 'unknown'} "
            f"progress={progress} "
            f"parsed={parsed}"
        )

        if status == "success":
            return body

        if status == "fail":
            raise RuntimeError(
                "解析失败:\n"
                + json.dumps(body, ensure_ascii=False, indent=2)
            )

        time.sleep(POLL_SECONDS)

    raise TimeoutError("超过最大轮询次数，任务仍未完成")


def fetch_all_results(
    client,
    task_id: str,
    total_layouts: int,
    step: int = 500,
):
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

        body = to_plain(
            client.get_doc_parser_result(req).body
        )

        chunks.append(body)
        start += step

    return chunks


def fetch_raw_json(
    client,
    path: Path,
    vlm_enhancement: bool,
    chunk_size: int,
):
    task_id, submit_body = submit(
        client,
        path,
        vlm_enhancement,
    )

    status_body = wait_until_done(
        client,
        task_id,
    )

    status_data = get_any(
        status_body,
        "Data",
        "data",
        default={},
    ) or {}

    total = int(
        get_any(
            status_data,
            "NumberOfSuccessfulParsing",
            "numberOfSuccessfulParsing",
            "number_of_successful_parsing",
            default=0,
        ) or 0
    )

    chunks = fetch_all_results(
        client,
        task_id,
        total,
        chunk_size,
    )

    return {
        "task_id": task_id,
        "submit_response": submit_body,
        "status_response": status_body,
        "result_chunks": chunks,
    }


# ============================================================
# 2. 精简阿里云返回 JSON
# ============================================================

def simplify_layout(layout):
    if not isinstance(layout, dict):
        return layout

    result = {}

    for key in (
        "text",
        "markdownContent",
        "numCol",
        "numRow",
    ):
        if key in layout:
            result[key] = layout[key]

    if (
        "cells" in layout
        and isinstance(layout["cells"], list)
    ):
        result["cells"] = [
            simplify_cell(cell)
            for cell in layout["cells"]
        ]

    if (
        "layouts" in layout
        and isinstance(layout["layouts"], list)
    ):
        result["layouts"] = [
            simplify_layout(x)
            for x in layout["layouts"]
        ]

    return result


def simplify_cell(cell):
    if not isinstance(cell, dict):
        return cell

    result = {}

    for key in ("ysc", "yec", "xsc", "xec"):
        if key in cell:
            result[key] = cell[key]

    if (
        "layouts" in cell
        and isinstance(cell["layouts"], list)
    ):
        result["layouts"] = [
            simplify_layout(layout)
            for layout in cell["layouts"]
        ]

    return result


def extract_layouts_from_chunk(chunk):
    if not isinstance(chunk, dict):
        return []

    data = chunk.get("Data")

    if isinstance(data, dict):
        layouts = data.get("layouts", [])
    else:
        layouts = chunk.get("layouts", [])

    if not isinstance(layouts, list):
        return []

    return [
        simplify_layout(layout)
        for layout in layouts
    ]


def simplify_json(data):
    result_chunks = data.get("result_chunks")

    if not isinstance(result_chunks, list):
        raise ValueError(
            "JSON 中没有找到有效的 result_chunks"
        )

    simplified_chunks = []

    for chunk in result_chunks:
        simplified_chunks.append({
            "layouts": extract_layouts_from_chunk(chunk)
        })

    return {
        "result_chunks": simplified_chunks
    }


# ============================================================
# 3. 转换为最终统一表格 JSON
#
# 顶层固定顺序：
# row_count
# col_count
# cells
#
# 每个单元格固定顺序：
# text
# row
# col
# rowspan
# colspan
#
# 不输出 color
# ============================================================

def find_table_layouts(obj):
    """
    递归寻找真正包含表格结构的 layout。

    判定条件：
    - numRow
    - numCol
    - cells
    """
    found = []

    if isinstance(obj, dict):
        if (
            "numRow" in obj
            and "numCol" in obj
            and isinstance(obj.get("cells"), list)
        ):
            found.append(obj)

        for value in obj.values():
            found.extend(
                find_table_layouts(value)
            )

    elif isinstance(obj, list):
        for item in obj:
            found.extend(
                find_table_layouts(item)
            )

    return found


def extract_layout_text(layout):
    """
    从 cell.layouts 中取文字。

    优先使用当前 layout 自己的 text。
    若当前层没有 text，再递归寻找子 layouts。
    """
    if not isinstance(layout, dict):
        return ""

    text = layout.get("text")

    if text is not None:
        return str(text)

    nested = layout.get("layouts")

    if not isinstance(nested, list):
        return ""

    return "".join(
        extract_layout_text(x)
        for x in nested
    )


def extract_cell_text(cell):
    layouts = cell.get("layouts", [])

    if not isinstance(layouts, list):
        return ""

    text = "".join(
        extract_layout_text(layout)
        for layout in layouts
    )

    return text.strip()


def convert_table_to_standard(table):
    row_count = int(table["numRow"])
    col_count = int(table["numCol"])

    if row_count <= 0 or col_count <= 0:
        raise ValueError(
            "numRow / numCol 必须为正整数"
        )

    raw_cells = table["cells"]

    cells = []

    for i, cell in enumerate(raw_cells):
        try:
            ysc = int(cell["ysc"])
            yec = int(cell["yec"])
            xsc = int(cell["xsc"])
            xec = int(cell["xec"])
        except KeyError as e:
            raise ValueError(
                f"第 {i} 个单元格缺少坐标字段: {e}"
            ) from e

        if (
            ysc < 0
            or xsc < 0
            or yec < ysc
            or xec < xsc
            or yec >= row_count
            or xec >= col_count
        ):
            raise ValueError(
                f"第 {i} 个单元格坐标非法: "
                f"ysc={ysc}, yec={yec}, "
                f"xsc={xsc}, xec={xec}"
            )

        # 字段顺序严格按照最终约定写入。
        standard_cell = {
            "text": extract_cell_text(cell),
            "row": ysc,
            "col": xsc,
            "rowspan": yec - ysc + 1,
            "colspan": xec - xsc + 1,
        }

        cells.append(standard_cell)

    # 保证 cells 本身也使用稳定的行优先顺序。
    cells.sort(
        key=lambda c: (
            c["row"],
            c["col"],
        )
    )

    result = {
        "row_count": row_count,
        "col_count": col_count,
        "cells": cells,
    }

    validate_standard_json(result)

    return result


def validate_standard_json(data):
    """
    检查最终 JSON：
    - 单元格不能越界
    - 单元格不能重叠
    - 必须完整覆盖整个基础网格
    """
    rows = data["row_count"]
    cols = data["col_count"]

    occupied = [
        [None for _ in range(cols)]
        for _ in range(rows)
    ]

    for i, cell in enumerate(data["cells"]):
        r0 = cell["row"]
        c0 = cell["col"]
        rs = cell["rowspan"]
        cs = cell["colspan"]

        if (
            r0 < 0
            or c0 < 0
            or rs <= 0
            or cs <= 0
            or r0 + rs > rows
            or c0 + cs > cols
        ):
            raise ValueError(
                f"最终 JSON 中 cell#{i} 越界"
            )

        for r in range(r0, r0 + rs):
            for c in range(c0, c0 + cs):
                if occupied[r][c] is not None:
                    raise ValueError(
                        f"最终 JSON 出现单元格重叠: "
                        f"基础格 ({r}, {c}) 同时属于 "
                        f"cell#{occupied[r][c]} 和 cell#{i}"
                    )

                occupied[r][c] = i

    missing = [
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if occupied[r][c] is None
    ]

    if missing:
        raise ValueError(
            f"最终 JSON 有 {len(missing)} 个基础格未覆盖，"
            f"例如: {missing[:10]}"
        )


def simplified_to_standard_tables(simplified):
    """
    将精简 JSON 中检测到的所有表格
    全部转换为最终统一标准格式。
    """
    tables = find_table_layouts(simplified)

    if not tables:
        raise ValueError(
            "精简 JSON 中没有找到表格结构 "
            "(numRow / numCol / cells)"
        )

    print(
        f"[表格检测] 共找到 {len(tables)} 个表格"
    )

    standard_tables = []

    for i, table in enumerate(
        tables,
        1,
    ):
        standard = convert_table_to_standard(
            table
        )

        standard_tables.append(
            standard
        )

        print(
            f"[表格 {i}] "
            f"{standard['row_count']} × "
            f"{standard['col_count']}, "
            f"cells={len(standard['cells'])}"
        )

    return standard_tables


# ============================================================
# 4. 保存 + fallback 工具
# ============================================================

def save_json(path: Path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )


def is_valid_standard_table(
    standard,
    min_cells=MIN_VALID_CELLS,
):
    """
    用最终标准 JSON 中 cells 的数量判断是否“太短”。
    """
    cells = standard.get("cells", [])

    return (
        isinstance(cells, list)
        and len(cells) >= min_cells
    )


def split_valid_tables(
    standard_tables,
    min_cells=MIN_VALID_CELLS,
):
    valid = []
    short = []

    for table in standard_tables:
        if is_valid_standard_table(
            table,
            min_cells=min_cells,
        ):
            valid.append(table)
        else:
            short.append(table)

    return valid, short


def save_standard_tables(
    output_dir: Path,
    stem: str,
    standard_tables,
):
    """
    只保存 cells >= MIN_VALID_CELLS 的最终标准表格。
    返回实际保存的文件路径。
    """
    valid_tables, short_tables = split_valid_tables(
        standard_tables
    )

    for i, table in enumerate(
        short_tables,
        1,
    ):
        print(
            f"[结果太短] 丢弃候选表格 #{i}: "
            f"cells={len(table.get('cells', []))} "
            f"< {MIN_VALID_CELLS}"
        )

    if not valid_tables:
        return []

    saved_paths = []

    if len(valid_tables) == 1:
        standard_path = (
            output_dir
            / f"{stem}.table_standard.json"
        )

        save_json(
            standard_path,
            valid_tables[0],
        )

        saved_paths.append(
            standard_path
        )

        print(
            f"[最终标准 JSON] {standard_path}"
        )

        return saved_paths

    print(
        f"[最终标准 JSON] 共 {len(valid_tables)} 个有效表格："
    )

    for i, standard in enumerate(
        valid_tables,
        1,
    ):
        standard_path = (
            output_dir
            / f"{stem}.table_{i}_standard.json"
        )

        save_json(
            standard_path,
            standard,
        )

        saved_paths.append(
            standard_path
        )

        print(
            f"  - {standard_path}"
        )

    return saved_paths


def run_pipeline_once(
    client,
    path: Path,
    output_dir: Path,
    vlm_enhancement: bool,
    chunk_size: int,
    artifact_stem=None,
):
    """
    对一个具体文件只跑一次既定 pipeline：
      原始 JSON -> 精简 JSON -> 标准表格

    注意：
    - 阿里云提交/轮询/下载结果阶段的错误原样抛出。
      这样网络、鉴权、欠费等问题不会被误判成表格问题。
    - 模型已经返回以后，如果 JSON/表格结构转换阶段报错，
      统一包装成 JsonPipelineError。
    """
    print()
    print(
        f"========== 解析 {path.name} =========="
    )

    stem = (
        artifact_stem
        if artifact_stem is not None
        else path.stem
    )

    # ---------- 第 1 步：调用模型 ----------
    # 这一段如果失败，说明不是 JSON 结构问题，直接原样抛出。
    raw = fetch_raw_json(
        client=client,
        path=path,
        vlm_enhancement=vlm_enhancement,
        chunk_size=chunk_size,
    )

    raw_path = (
        output_dir
        / f"{stem}.doc_parser_large.json"
    )

    save_json(
        raw_path,
        raw,
    )

    print(
        f"[1/3 原始 JSON] {raw_path}"
    )

    # ---------- 第 2/3 步：JSON / 结构处理 ----------
    try:
        simplified = simplify_json(
            raw
        )

        simplified_path = (
            output_dir
            / f"{stem}.doc_parser_large.simplified.json"
        )

        save_json(
            simplified_path,
            simplified,
        )

        print(
            f"[2/3 精简 JSON] {simplified_path}"
        )

        tables = find_table_layouts(
            simplified
        )

        if not tables:
            print(
                "[3/3] 没有检测到表格结构"
            )
            return []

        standard_tables = []

        print(
            f"[表格检测] 共找到 {len(tables)} 个表格"
        )

        for i, table in enumerate(
            tables,
            1,
        ):
            standard = convert_table_to_standard(
                table
            )

            standard_tables.append(
                standard
            )

            print(
                f"[表格 {i}] "
                f"{standard['row_count']} × "
                f"{standard['col_count']}, "
                f"cells={len(standard['cells'])}"
            )

        return standard_tables

    except (
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        json.JSONDecodeError,
    ) as e:
        raise JsonPipelineError(
            f"{path.name} 的 JSON/表格结构处理失败: {e}"
        ) from e

def rotate_image_clockwise_90(
    image_path: Path,
    output_dir: Path,
):
    """
    顺时针旋转 90 度，统一保存为 PNG。
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "图片旋转需要 Pillow。"
            "请先执行: pip install Pillow"
        ) from e

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    rotated_path = (
        output_dir
        / f"{image_path.stem}_rot90.png"
    )

    with Image.open(
        image_path
    ) as image:
        rotated = image.rotate(
            -90,
            expand=True,
        )

        # 避免某些模式保存 PNG 时出问题。
        if rotated.mode not in (
            "RGB",
            "RGBA",
            "L",
        ):
            rotated = rotated.convert(
                "RGB"
            )

        rotated.save(
            rotated_path,
            "PNG",
        )

    print(
        f"[顺时针旋转 90°] {rotated_path}"
    )

    return rotated_path


def find_images_under(
    directory: Path,
):
    result = []

    for path in directory.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower()
            in IMAGE_SUFFIXES
        ):
            result.append(path)

    result.sort(
        key=lambda p: str(p)
    )

    return result


def pdf_to_images_with_sibling_script(
    pdf_path: Path,
    work_dir: Path,
):
    """
    直接导入与当前 pipeline 同目录的 pdf2image.py，
    调用其中的：

        pdf_to_images(pdf_path, out_dir, dpi=300)

    pdf2image.py 的输出目录统一使用项目当前目录下的：
        rendered_pages/

    它会生成：
        rendered_pages/<pdf_stem>_page_1.png
        rendered_pages/<pdf_stem>_page_2.png
        ...

    work_dir 参数保留，是为了不改动外部 fallback 调用结构；
    实际 PDF 页图目录按用户现有脚本固定为 rendered_pages。
    """
    script_path = (
        Path(__file__)
        .resolve()
        .with_name("pdf2image.py")
    )

    if not script_path.is_file():
        raise FileNotFoundError(
            "没有找到与当前 pipeline 同目录的 pdf2image.py: "
            f"{script_path}"
        )

    rendered_dir = Path("rendered_pages")
    rendered_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 只清理当前 PDF 以前留下的同名页图，
    # 避免本次 PDF 页数变少时误读旧页面。
    old_pages = list(
        rendered_dir.glob(
            f"{pdf_path.stem}_page_*.png"
        )
    )

    for old_page in old_pages:
        old_page.unlink()

    spec = importlib.util.spec_from_file_location(
        "local_pdf2image",
        script_path,
    )

    if (
        spec is None
        or spec.loader is None
    ):
        raise RuntimeError(
            f"无法加载 pdf2image.py: {script_path}"
        )

    module = importlib.util.module_from_spec(
        spec
    )
    spec.loader.exec_module(
        module
    )

    pdf_to_images = getattr(
        module,
        "pdf_to_images",
        None,
    )

    if not callable(pdf_to_images):
        raise RuntimeError(
            "pdf2image.py 中没有找到可调用的 "
            "pdf_to_images(pdf_path, out_dir, dpi=300)"
        )

    print(
        f"[PDF 转图片] {pdf_path} "
        f"-> {rendered_dir}/"
    )

    pdf_to_images(
        str(pdf_path),
        str(rendered_dir),
        dpi=300,
    )

    images = sorted(
        rendered_dir.glob(
            f"{pdf_path.stem}_page_*.png"
        ),
        key=lambda p: p.name,
    )

    if not images:
        raise RuntimeError(
            "pdf2image.py 已执行，但 rendered_pages "
            f"中没有找到 {pdf_path.stem}_page_*.png"
        )

    print(
        f"[PDF 转图片完成] 共 {len(images)} 页"
    )

    for image in images:
        print(
            f"  - {image}"
        )

    return images

def process_image_with_rotation_fallback(
    client,
    image_path: Path,
    output_dir: Path,
    retry_dir: Path,
    vlm_enhancement: bool,
    chunk_size: int,
    result_stem=None,
):
    """
    图片 fallback：

      原图
        ├─ 成功且存在 >=5-cell 表格 -> 收录
        └─ 报错 或 结果太短
              ↓
          顺时针旋转 90°
              ├─ 成功且存在 >=5-cell 表格 -> 收录
              └─ 报错 或 结果太短 -> 放弃

    这里故意把“报错”和“太短”放在同一个 fallback 逻辑中。
    """
    base_stem = (
        result_stem
        if result_stem is not None
        else image_path.stem
    )

    # ---------- 第一次：原图 ----------
    try:
        standard_tables = run_pipeline_once(
            client=client,
            path=image_path,
            output_dir=output_dir,
            vlm_enhancement=vlm_enhancement,
            chunk_size=chunk_size,
            artifact_stem=base_stem,
        )

        saved = save_standard_tables(
            output_dir=output_dir,
            stem=base_stem,
            standard_tables=standard_tables,
        )

        if saved:
            print(
                f"[图片成功] {image_path.name}"
            )
            return saved

        print(
            f"[图片结果太短] {image_path.name}: "
            f"没有 >= {MIN_VALID_CELLS} cells 的有效表格"
        )

    except Exception as e:
        print(
            f"[图片解析报错] {image_path.name}: "
            f"{type(e).__name__}: {e}"
        )

    # ---------- 第二次：顺时针旋转 90° ----------
    print(
        f"[图片 fallback] {image_path.name} "
        "-> 顺时针旋转 90° 后再试一次"
    )

    try:
        rotated_path = rotate_image_clockwise_90(
            image_path=image_path,
            output_dir=retry_dir,
        )

        rotated_stem = (
            f"{base_stem}_rot90"
        )

        rotated_tables = run_pipeline_once(
            client=client,
            path=rotated_path,
            output_dir=output_dir,
            vlm_enhancement=vlm_enhancement,
            chunk_size=chunk_size,
            artifact_stem=rotated_stem,
        )

        saved = save_standard_tables(
            output_dir=output_dir,
            stem=rotated_stem,
            standard_tables=rotated_tables,
        )

        if saved:
            print(
                f"[旋转后成功] {rotated_path.name}"
            )
            return saved

        print(
            f"[旋转后结果仍太短] {rotated_path.name}: "
            f"没有 >= {MIN_VALID_CELLS} cells 的有效表格"
        )

    except Exception as e:
        print(
            f"[旋转后仍报错] {image_path.name}: "
            f"{type(e).__name__}: {e}"
        )

    print(
        f"[放弃] {image_path.name}: "
        "原图与顺时针 90° 两次尝试均未得到有效表格"
    )

    return []


# ============================================================
# 5. 主程序
#
# fallback 规则：
#
# PDF:
#   原 PDF
#     ↓ 如果 JSON/结构处理报错，或没有任何 >=5-cell 的表格
#   pdf2image.py 拆成每页图片
#     ↓
#   每页原图
#     ↓ 如果报错或仍然太短
#   每页顺时针旋转 90°
#     ↓ 如果仍报错或仍然太短
#   放弃该页
#
# 图片:
#   原图
#     ↓ 如果报错或没有任何 >=5-cell 的表格
#   顺时针旋转 90°
#     ↓ 如果仍报错或仍然太短
#   放弃
#
# 其它文件:
#   只跑原有 pipeline，不做 fallback
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "阿里云文档解析大模型版："
            "原始 JSON -> 精简 JSON -> "
            "统一标准表格 JSON；"
            "PDF/图片结果过短时自动拆页/旋转重试"
        )
    )

    parser.add_argument(
        "file",
        type=Path,
        help="一个受支持的本地文件",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs"),
    )

    parser.add_argument(
        "--vlm-enhancement",
        action="store_true",
        help="额外开启 VLM 增强",
    )

    parser.add_argument(
        "--chunk-size",
        type=int,
        default=500,
        help="GetDocParserResult 每次获取的 layout 数，1~3000",
    )

    args = parser.parse_args()

    path = args.file

    if not path.is_file():
        raise FileNotFoundError(
            path
        )

    if path.suffix.lower() not in SUPPORTED:
        print(
            f"[警告] 扩展名 {path.suffix!r} "
            "不在当前脚本已知支持列表中；"
            "仍尝试提交，由服务端最终判断。"
        )

    if not 1 <= args.chunk_size <= 3000:
        raise ValueError(
            "--chunk-size 必须在 1~3000"
        )

    output_dir = args.output_dir
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    client = make_client()

    suffix = path.suffix.lower()

    # --------------------------------------------------------
    # 1. PDF：先原 PDF，太短才拆页
    # --------------------------------------------------------
    if suffix in PDF_SUFFIXES:
        pdf_fallback_reason = None

        try:
            direct_tables = run_pipeline_once(
                client=client,
                path=path,
                output_dir=output_dir,
                vlm_enhancement=args.vlm_enhancement,
                chunk_size=args.chunk_size,
            )

            saved = save_standard_tables(
                output_dir=output_dir,
                stem=path.stem,
                standard_tables=direct_tables,
            )

            if saved:
                print(
                    "[完成] 原 PDF 已得到有效表格，"
                    "不再拆页。"
                )
                return

            pdf_fallback_reason = (
                "原 PDF 没有得到 "
                f">= {MIN_VALID_CELLS} cells 的有效表格"
            )

        except JsonPipelineError as e:
            pdf_fallback_reason = str(e)

            print()
            print(
                "[PDF JSON/结构报错] "
                f"{e}"
            )

        print()
        print(
            "[PDF fallback] "
            f"{pdf_fallback_reason}；"
            "开始逐页转图片。"
        )

        pdf_work_dir = (
            output_dir
            / "_fallback"
            / f"{path.stem}_pdf"
        )

        page_images = (
            pdf_to_images_with_sibling_script(
                pdf_path=path,
                work_dir=pdf_work_dir,
            )
        )

        retry_dir = (
            pdf_work_dir
            / "rotated"
        )

        collected = []

        for page_index, image_path in enumerate(
            page_images,
            1,
        ):
            print()
            print(
                f"========== PDF 第 {page_index} 页 fallback =========="
            )

            collected.extend(
                process_image_with_rotation_fallback(
                    client=client,
                    image_path=image_path,
                    output_dir=output_dir,
                    retry_dir=retry_dir,
                    vlm_enhancement=args.vlm_enhancement,
                    chunk_size=args.chunk_size,
                    result_stem=(
                        f"{path.stem}_page_{page_index}"
                    ),
                )
            )

        print()
        print(
            f"[PDF fallback 完成] "
            f"共收录 {len(collected)} 个标准 JSON"
        )

        return

    # --------------------------------------------------------
    # 2. 图片：原图太短 -> 顺时针 90° 再试一次
    # --------------------------------------------------------
    if suffix in IMAGE_SUFFIXES:
        retry_dir = (
            output_dir
            / "_fallback"
            / f"{path.stem}_image"
        )

        collected = (
            process_image_with_rotation_fallback(
                client=client,
                image_path=path,
                output_dir=output_dir,
                retry_dir=retry_dir,
                vlm_enhancement=args.vlm_enhancement,
                chunk_size=args.chunk_size,
            )
        )

        print()
        print(
            f"[图片处理完成] "
            f"共收录 {len(collected)} 个标准 JSON"
        )

        return

    # --------------------------------------------------------
    # 3. 其它格式：保持原有逻辑，不做 fallback
    # --------------------------------------------------------
    standard_tables = run_pipeline_once(
        client=client,
        path=path,
        output_dir=output_dir,
        vlm_enhancement=args.vlm_enhancement,
        chunk_size=args.chunk_size,
    )

    saved = save_standard_tables(
        output_dir=output_dir,
        stem=path.stem,
        standard_tables=standard_tables,
    )

    if not saved:
        print(
            "[完成] 没有收录任何有效表格"
        )


if __name__ == "__main__":
    main()