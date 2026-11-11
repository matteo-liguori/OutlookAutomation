import argparse
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image
from pypdf import PdfReader


def safe_print(value):
    text = str(value)
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace") + b"\n")


def parse_pages(spec, page_count):
    if not spec:
        return list(range(1, page_count + 1))

    selected = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start = int(left)
            end = int(right)
            selected.extend(range(start, end + 1))
        else:
            selected.append(int(part))

    seen = set()
    valid = []
    for page in selected:
        if page < 1 or page > page_count:
            raise ValueError(f"Page {page} is outside 1-{page_count}")
        if page not in seen:
            valid.append(page)
            seen.add(page)
    return valid


def resize_to_bounds(image, max_width, max_height):
    if not max_width and not max_height:
        return image

    width, height = image.size
    ratios = []
    if max_width:
        ratios.append(max_width / width)
    if max_height:
        ratios.append(max_height / height)
    ratio = min(ratios) if ratios else 1.0

    if ratio >= 1.0:
        return image

    new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def render_with_pdfium(pdf_path, out_dir, pages, scale, max_width, max_height):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    outputs = []
    for page_no in pages:
        page = pdf[page_no - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        image = resize_to_bounds(image, max_width, max_height)
        out_path = out_dir / f"page_{page_no:03d}.png"
        image.save(out_path)
        outputs.append(
            {
                "page": page_no,
                "path": str(out_path),
                "width": image.size[0],
                "height": image.size[1],
            }
        )
    return outputs


def extract_text(pdf_path):
    reader = PdfReader(str(pdf_path))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        pages.append({"page": index, "text": page.extract_text() or ""})
    return pages


def parse_check(spec):
    if ":" not in spec:
        raise ValueError(f"Bad check {spec!r}; use contains:TEXT or not-contains:TEXT")
    kind, text = spec.split(":", 1)
    kind = kind.strip().lower()
    if kind not in {"contains", "not-contains"}:
        raise ValueError(f"Unsupported check kind {kind!r}")
    return kind, text


def run_checks(page_texts, checks):
    full_text = "\n".join(page["text"] for page in page_texts)
    lower_text = full_text.lower()
    results = []
    ok = True
    for raw in checks:
        kind, needle = parse_check(raw)
        found = needle.lower() in lower_text
        passed = found if kind == "contains" else not found
        results.append({"check": raw, "passed": passed, "found": found})
        ok = ok and passed
    return ok, results


def summarize_matching_pages(page_texts, patterns):
    summaries = []
    lowered_patterns = [p.lower() for p in patterns]
    for page in page_texts:
        text = re.sub(r"\s+", " ", page["text"]).strip()
        lower_text = text.lower()
        if any(pattern in lower_text for pattern in lowered_patterns):
            summaries.append({"page": page["page"], "preview": text[:800]})
    return summaries


def main():
    parser = argparse.ArgumentParser(
        description="Render selected PDF pages to PNG and run Unicode-safe text checks."
    )
    parser.add_argument("pdf", help="Input PDF path")
    parser.add_argument("--out", help="Output directory; defaults beside the PDF")
    parser.add_argument("--pages", help="Pages/ranges, e.g. 1,2,15-17. Defaults to all pages.")
    parser.add_argument("--scale", type=float, default=2.0, help="Render scale before resizing")
    parser.add_argument("--max-width", type=int, default=1600)
    parser.add_argument("--max-height", type=int, default=2200)
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        help="Text check: contains:TEXT or not-contains:TEXT. Can be repeated.",
    )
    parser.add_argument(
        "--summary-pattern",
        action="append",
        default=[],
        help="Print previews for pages whose text contains this pattern.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    out_dir = Path(args.out).expanduser().resolve() if args.out else pdf_path.with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(pdf_path))
    pages = parse_pages(args.pages, len(reader.pages))
    rendered = render_with_pdfium(pdf_path, out_dir, pages, args.scale, args.max_width, args.max_height)
    page_texts = extract_text(pdf_path)
    checks_ok, checks = run_checks(page_texts, args.check)
    summaries = summarize_matching_pages(page_texts, args.summary_pattern)

    manifest = {
        "pdf": str(pdf_path),
        "page_count": len(reader.pages),
        "rendered": rendered,
        "checks_ok": checks_ok,
        "checks": checks,
        "summaries": summaries,
    }
    manifest_path = out_dir / "export_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    safe_print(json.dumps(manifest, indent=2, ensure_ascii=False))
    if not checks_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
