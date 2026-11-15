#!/usr/bin/env python3
import argparse
import json
import unicodedata
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import is_color_like
from matplotlib.patches import Rectangle


DEFAULT_COLOR = "white"


def get_chinese_font():
    """
    自动寻找可显示中文的字体。
    优先使用常见 Linux / Windows / macOS 中文字体。
    """
    candidates = [
        "Noto Sans CJK SC",
        "Noto Sans CJK TC",
        "Source Han Sans CN",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "Microsoft YaHei",
        "SimHei",
        "PingFang SC",
        "Arial Unicode MS",
    ]

    installed = {f.name for f in font_manager.fontManager.ttflist}

    for name in candidates:
        if name in installed:
            print(f"[字体] 使用中文字体: {name}")
            return font_manager.FontProperties(family=name)

    print(
        "[警告] 没有找到常见中文字体，中文可能显示为方块。\\n"
        "Ubuntu/Debian 可安装：sudo apt install fonts-noto-cjk"
    )
    return None



# 标准 JSON：\n# 顶层字段：row_count, col_count, cells\n# 单元格结构字段：text, row, col, rowspan, colspan\n# 可选显示字段：color\n# 注意：color 不参与单元格位置/合并判断；JSON 不区分表头和正文。\n

def load_table(json_path: Path):
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    rows = int(data["row_count"])
    cols = int(data["col_count"])
    cells = data["cells"]

    if rows <= 0 or cols <= 0:
        raise ValueError("row_count 和 col_count 必须为正整数")
    if not isinstance(cells, list):
        raise ValueError("cells 必须是数组")

    return rows, cols, cells


def normalize_cell(cell):
    """
    标准单元格格式：

    必需/结构字段：
    - row
    - col
    - rowspan
    - colspan

    内容字段：
    - text

    可选显示字段：
    - color

    只有 row / col / rowspan / colspan 决定位置和合并关系。
    color 仅用于显示，缺失或非法时使用默认背景色。
    JSON 不区分表头、正文等语义角色。
    """
    row = int(cell["row"])
    col = int(cell["col"])
    rowspan = int(cell.get("rowspan", 1))
    colspan = int(cell.get("colspan", 1))

    if rowspan <= 0 or colspan <= 0:
        raise ValueError(
            f"非法跨度: row={row}, col={col}, "
            f"rowspan={rowspan}, colspan={colspan}"
        )

    text = str(cell.get("text", ""))

    # color 与位置无关，只是可选显示属性。
    color = cell.get("color")
    if not color or not is_color_like(color):
        color = DEFAULT_COLOR

    return {
        "text": text,
        "row": row,
        "col": col,
        "rowspan": rowspan,
        "colspan": colspan,
        "color": color,
    }


def validate_cells(rows, cols, cells):
    occupied = [[None for _ in range(cols)] for _ in range(rows)]

    for i, cell in enumerate(cells):
        r0 = cell["row"]
        c0 = cell["col"]
        rs = cell["rowspan"]
        cs = cell["colspan"]

        if r0 < 0 or c0 < 0 or r0 + rs > rows or c0 + cs > cols:
            print(
                f"[警告] cell#{i} 越界: "
                f"row={r0}, col={c0}, rowspan={rs}, colspan={cs}"
            )
            continue

        for r in range(r0, r0 + rs):
            for c in range(c0, c0 + cs):
                if occupied[r][c] is not None:
                    print(
                        f"[警告] 基础格 ({r}, {c}) 重叠: "
                        f"cell#{occupied[r][c]} 与 cell#{i}"
                    )
                else:
                    occupied[r][c] = i

    missing = [
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if occupied[r][c] is None
    ]

    if missing:
        print(f"[警告] 有 {len(missing)} 个基础格未被任何单元格覆盖")
    else:
        print("[检查] 所有基础格均被覆盖")


def char_width(ch):
    """
    粗略估计字符显示宽度。
    中文/全角字符按 2 个 ASCII 宽度计算。
    """
    if ch == "\t":
        return 4
    if unicodedata.east_asian_width(ch) in ("W", "F", "A"):
        return 2
    return 1


def text_width_units(text):
    if not text:
        return 0
    return max(
        sum(char_width(ch) for ch in line)
        for line in str(text).splitlines() or [""]
    )


def wrap_text_by_units(text, max_units):
    """
    按估算显示宽度换行。
    对中文、英文、数字混排都能工作。
    """
    text = str(text or "")

    if not text:
        return ""

    max_units = max(4, int(max_units))
    lines = []

    for raw_line in text.splitlines() or [""]:
        if raw_line == "":
            lines.append("")
            continue

        current = []
        current_units = 0

        for ch in raw_line:
            w = char_width(ch)

            if current and current_units + w > max_units:
                lines.append("".join(current))
                current = [ch]
                current_units = w
            else:
                current.append(ch)
                current_units += w

        if current:
            lines.append("".join(current))

    return "\n".join(lines)


def calculate_layout(rows, cols, cells, font_size):
    """
    根据文字内容计算列宽和行高。

    结构仍完全由 row / col / rowspan / colspan 决定。
   这里只改变可视化尺寸，不改变表格拓扑。
    """
    # Matplotlib 数据坐标中的基础尺寸。
    min_col_width = 1.8
    max_col_width = 7.0
    min_row_height = 1.15

    col_widths = [min_col_width for _ in range(cols)]

    # 先用非合并列单元格估计各列适合的宽度。
    for cell in cells:
        if cell["colspan"] != 1:
            continue

        text = cell["text"]
        if not text:
            continue

        units = text_width_units(text)

        # 一个数据宽度大约容纳 7~8 个 ASCII 宽度。
        preferred = 1.2 + units / 7.5
        preferred = min(max_col_width, max(min_col_width, preferred))

        col = cell["col"]
        col_widths[col] = max(col_widths[col], preferred)

    # 再照顾横向合并单元格。
    for cell in cells:
        if not cell["text"]:
            continue

        c0 = cell["col"]
        cs = cell["colspan"]

        current_width = sum(col_widths[c0:c0 + cs])
        units = text_width_units(cell["text"])

        preferred = min(
            max_col_width * cs,
            max(min_col_width * cs, 1.2 + units / 7.5),
        )

        if preferred > current_width:
            extra = (preferred - current_width) / cs
            for c in range(c0, c0 + cs):
                col_widths[c] += extra

    # 根据最终列宽对文本换行，并据此计算行高。
    row_heights = [min_row_height for _ in range(rows)]
    wrapped_texts = []

    # 每个数据宽度大约对应 7.5 个 ASCII 显示单位。
    units_per_width = 7.5

    for cell in cells:
        c0 = cell["col"]
        cs = cell["colspan"]

        cell_width = sum(col_widths[c0:c0 + cs])

        # 留出左右内边距。
        max_units = max(
            4,
            int((cell_width - 0.35) * units_per_width),
        )

        wrapped = wrap_text_by_units(
            cell["text"],
            max_units,
        )
        wrapped_texts.append(wrapped)

        if not wrapped:
            continue

        line_count = wrapped.count("\n") + 1

        # 字体越大，每一行需要的高度越高。
        line_height = max(
            0.55,
            font_size / 15.0,
        )

        required_height = max(
            min_row_height * cell["rowspan"],
            0.55 + line_count * line_height,
        )

        r0 = cell["row"]
        rs = cell["rowspan"]
        current_height = sum(row_heights[r0:r0 + rs])

        if required_height > current_height:
            extra = (required_height - current_height) / rs

            for r in range(r0, r0 + rs):
                row_heights[r] += extra

    return col_widths, row_heights, wrapped_texts


def render_table(
    json_path: Path,
    output_path: Path,
    dpi: int = 180,
    font_size: float = 10,
    show: bool = False,
):
    rows, cols, raw_cells = load_table(json_path)
    cells = [normalize_cell(cell) for cell in raw_cells]

    validate_cells(rows, cols, cells)
    chinese_font = get_chinese_font()

    col_widths, row_heights, wrapped_texts = calculate_layout(
        rows,
        cols,
        cells,
        font_size,
    )

    # 累计坐标。
    x_edges = [0.0]
    for width in col_widths:
        x_edges.append(x_edges[-1] + width)

    y_edges_top = [0.0]
    for height in row_heights:
        y_edges_top.append(y_edges_top[-1] + height)

    total_width = x_edges[-1]
    total_height = y_edges_top[-1]

    # figsize 与数据宽高同步，避免文字挤成一团。
    fig_width = max(6.0, total_width * 0.78)
    fig_height = max(4.0, total_height * 0.72)

    fig, ax = plt.subplots(
        figsize=(fig_width, fig_height)
    )

    for cell, wrapped_text in zip(cells, wrapped_texts):
        row = cell["row"]
        col = cell["col"]
        rowspan = cell["rowspan"]
        colspan = cell["colspan"]

        x = x_edges[col]
        width = x_edges[col + colspan] - x

        top = y_edges_top[row]
        bottom_from_top = y_edges_top[row + rowspan]
        height = bottom_from_top - top

        # Matplotlib y 轴向上，所以把“从顶部算”的坐标翻转。
        y = total_height - bottom_from_top

        rect = Rectangle(
            (x, y),
            width,
            height,
            facecolor=cell["color"],
            edgecolor="black",
            linewidth=1.2,
        )
        ax.add_patch(rect)

        if wrapped_text:
            ax.text(
                x + width / 2,
                y + height / 2,
                wrapped_text,
                ha="center",
                va="center",
                fontsize=font_size,
                color="black",
                fontproperties=chinese_font,
                linespacing=1.15,
            )

    ax.set_xlim(0, total_width)
    ax.set_ylim(0, total_height)
    ax.set_aspect("equal")
    ax.axis("off")

    plt.subplots_adjust(
        left=0,
        right=1,
        top=1,
        bottom=0,
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fig.savefig(
        output_path,
        dpi=dpi,
        bbox_inches="tight",
        pad_inches=0.03,
    )

    print(f"[完成] 已保存: {output_path}")

    if show:
        plt.show()

    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(
        description="按统一标准 JSON（row_count/col_count/cells）使用 Matplotlib 重建表格"
    )
    parser.add_argument(
        "input_json",
        type=Path,
        help="输入统一标准表格 JSON",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出 PNG；默认使用 <输入文件名>_rendered.png",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=180,
        help="输出 DPI，默认 180",
    )
    parser.add_argument(
        "--font-size",
        type=float,
        default=10,
        help="文字字号，默认 10",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="保存后同时弹出窗口显示",
    )

    args = parser.parse_args()

    if not args.input_json.is_file():
        raise FileNotFoundError(args.input_json)

    output = args.output
    if output is None:
        output = args.input_json.with_name(
            args.input_json.stem + "_rendered.png"
        )

    render_table(
        json_path=args.input_json,
        output_path=output,
        dpi=args.dpi,
        font_size=args.font_size,
        show=args.show,
    )


if __name__ == "__main__":
    main()