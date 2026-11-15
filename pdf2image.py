
#!/usr/bin/env python3

import argparse
import fitz  # PyMuPDF
from pathlib import Path


def pdf_to_images(pdf_path, out_dir, dpi=300):
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)

    if not pdf_path.is_file():
        raise FileNotFoundError(f"找不到 PDF: {pdf_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)

    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi)

        out_path = out_dir / f"{pdf_path.stem}_page_{i + 1}.png"
        pix.save(out_path)

        print(f"已保存: {out_path}")

    doc.close()


def main():
    parser = argparse.ArgumentParser(
        description="将 PDF 每一页转换为 PNG 图片"
    )

    parser.add_argument(
        "pdf",
        help="输入 PDF 文件路径",
    )

    parser.add_argument(
        "out_dir",
        nargs="?",
        default="rendered_pages",
        help="输出目录，默认 rendered_pages",
    )

    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="图片 DPI，默认 300",
    )

    args = parser.parse_args()

    pdf_to_images(
        pdf_path=args.pdf,
        out_dir=args.out_dir,
        dpi=args.dpi,
    )


if __name__ == "__main__":
    main()