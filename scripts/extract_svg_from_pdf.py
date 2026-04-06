#!/usr/bin/env python3
"""
Extract SVG from a PDF into an output folder (default: ./in).

What this does:
1. Embedded files: Many PDFs attach .svg files; PyMuPDF lists and extracts them.
2. Sniff: Any embedded file whose bytes start with '<svg' or contain an SVG MIME marker is saved as .svg.
3. Optional --pages: If Poppler's pdftocairo is on PATH, export each page as vector SVG
   (reconstructed from PDF drawing operators, not "original" embedded SVG).

Install:
  pip install pymupdf

Optional (page export):
  Install Poppler for Windows and ensure pdftocairo.exe is on PATH.

Paths with spaces: you can quote the path, use --pdf "…", or pass it unquoted —
  the script joins split tokens when the merged path exists (PowerShell often
  splits on spaces).

  python extract_svg_from_pdf.py --pages --pdf "F:\\folder\\Workflow Automation - n8n.pdf"
  python extract_svg_from_pdf.py --pages F:\\folder\\Workflow Automation - n8n.pdf
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Stops "positional path" merging when we hit these (before argparse).
_ARGV_STOP_TOKENS = frozenset(
    {"--pages", "--help", "-h", "--out", "--o", "-p", "--pdf"},
)


def _maybe_join_spaced_pdf_argv(argv: list[str]) -> list[str]:
    """
    If the shell split a path on spaces (e.g. PowerShell), join consecutive
    tokens into one path when the result is an existing file. Skipped when
    -p/--pdf is used (explicit path).
    """
    if "-p" in argv or "--pdf" in argv:
        return argv
    i = 1
    block: list[str] = []
    while i < len(argv):
        t = argv[i]
        if t in _ARGV_STOP_TOKENS:
            break
        if t.startswith("--"):
            break
        # lone "-" is allowed inside titles like "Foo - Bar.pdf"
        if len(t) > 1 and t.startswith("-"):
            break
        block.append(t)
        i += 1
    if len(block) >= 2:
        joined = " ".join(block)
        if Path(joined).is_file():
            return [argv[0], joined, *argv[i:]]
    return argv


def _is_svg_bytes(data: bytes) -> bool:
    head = data[:8000].lstrip()
    return head.startswith(b"<svg") or head.startswith(b"<?xml") and b"<svg" in data[:20000]


def _safe_name(name: str, index: int) -> str:
    name = (name or "").strip() or f"embedded_{index}"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name


def extract_embedded_svgs(pdf_path: Path, out_dir: Path) -> list[Path]:
    import fitz  # PyMuPDF

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    doc = fitz.open(pdf_path)
    try:
        for i in range(doc.embfile_count()):
            info = doc.embfile_info(i)
            raw_name = info.get("filename") or info.get("ufilename") or ""
            data = doc.embfile_get(i)
            if not data:
                continue

            base = _safe_name(Path(raw_name).name if raw_name else "", i)
            lower = base.lower()

            if lower.endswith(".svg") or _is_svg_bytes(data):
                if not lower.endswith(".svg"):
                    base = f"{Path(base).stem}.svg"
                target = out_dir / base
                # avoid overwrite
                n = 0
                while target.exists():
                    n += 1
                    target = out_dir / f"{Path(base).stem}_{n}.svg"
                target.write_bytes(data)
                written.append(target)
    finally:
        doc.close()

    return written


def export_pages_with_pdftocairo(pdf_path: Path, out_dir: Path) -> list[Path]:
    """One SVG per page via Poppler (pdftocairo -svg)."""
    exe = shutil.which("pdftocairo")
    if not exe:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem
    # pdftocairo writes stem-page1.svg, stem-page2.svg, ...
    pattern = out_dir / f"{stem}-page*.svg"
    # remove old matches for this stem to avoid confusion
    for old in out_dir.glob(f"{stem}-page*.svg"):
        old.unlink(missing_ok=True)

    subprocess.run(
        [exe, "-svg", str(pdf_path), str(out_dir / stem)],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(out_dir.glob(f"{stem}-page*.svg"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract SVG from PDF into a folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Example with spaces in filename:\n  %(prog)s --pages --pdf "C:\\docs\\My file name.pdf"',
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        type=Path,
        help="Path to input .pdf (must be quoted if the path contains spaces)",
    )
    parser.add_argument(
        "-p",
        "--pdf",
        dest="pdf_explicit",
        type=Path,
        metavar="PATH",
        help="Path to input .pdf (use when the path has spaces; one quoted token after --pdf)",
    )
    parser.add_argument(
        "--o",
        "--out",
        dest="out",
        type=Path,
        default=Path("in"),
        help="Output directory (default: ./in)",
    )
    parser.add_argument(
        "--pages",
        action="store_true",
        help="Also run pdftocairo -svg (Poppler) to export each page as SVG",
    )
    sys.argv = _maybe_join_spaced_pdf_argv(sys.argv)
    args = parser.parse_args()

    pdf_path = args.pdf_explicit or args.pdf
    if pdf_path is None:
        parser.error("Provide the PDF: positional path or --pdf PATH")
    pdf_path = pdf_path.resolve()
    out_dir = args.out.resolve()

    if not pdf_path.is_file():
        print(f"Not a file: {pdf_path}", file=sys.stderr)
        return 1

    try:
        import fitz  # noqa: F401
    except ImportError:
        print("Install PyMuPDF: pip install pymupdf", file=sys.stderr)
        return 1

    embedded = extract_embedded_svgs(pdf_path, out_dir)
    for p in embedded:
        print(f"embedded -> {p}")

    if not embedded:
        print("No embedded SVG files found in this PDF.")

    page_svgs: list[Path] = []
    if args.pages:
        try:
            page_svgs = export_pages_with_pdftocairo(pdf_path, out_dir)
        except subprocess.CalledProcessError as e:
            print(f"pdftocairo failed: {e.stderr or e}", file=sys.stderr)
            return 1
        if not page_svgs and not shutil.which("pdftocairo"):
            print("pdftocairo not on PATH; install Poppler to use --pages", file=sys.stderr)
        else:
            for p in page_svgs:
                print(f"page -> {p}")

    if not embedded and not page_svgs:
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
