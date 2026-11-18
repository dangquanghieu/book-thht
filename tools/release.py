# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdf>=4", "pypdfium2>=4", "pillow>=10"]
# ///
"""Cập nhật trang chủ sách theo một bản phát hành (release) trên GitHub.

Việc script làm:
  1. Thay phiên bản và số trang trong index.html (các vị trí cố định, đếm chính xác).
  2. Render trang 1 của PDF thành cover.png (600x850).
  3. So mục lục (outline) PDF mới với tools/toc-snapshot.txt, in ra mục thêm/bỏ/đổi tên,
     rồi cập nhật bản chụp. Mục lục trong index.html KHÔNG bị sửa: bạn tự sửa theo báo cáo.
Script không commit; cuối cùng in `git diff --stat`.

Cách dùng (từ thư mục gốc repo):
  uv run tools/release.py                      # tag v* mới nhất trên GitHub
  uv run tools/release.py --tag v2026-12-05
  uv run tools/release.py --tag v2026-12-05 --pdf ~/path/TinHieuHeThong.pdf
  uv run tools/release.py --dry-run            # chỉ báo cáo, không ghi file
Tuỳ chọn --force: cho phép chạy lại cùng phiên bản hoặc lùi về phiên bản cũ hơn.

Bìa được render bằng pdftoppm (poppler) nếu có trên máy, giống cách làm bìa các bản trước;
nếu không có thì dùng pypdfium2 (khác vài mức xám ở viền nét chữ, mắt thường không thấy).
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ASSET = "TinHieuHeThong.pdf"
COVER_SIZE = (600, 850)
TITLE_CHECK = "Tín hiệu và hệ thống"
TAG_RE = re.compile(r"^v(\d{4})-(\d{1,2})-(\d{1,2})$")

ROOT_DEFAULT = Path(__file__).resolve().parent.parent


class ReleaseError(Exception):
    pass


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------- phiên bản / tag
def tag_to_version(tag: str) -> str:
    m = TAG_RE.match(tag)
    if not m:
        raise ReleaseError(f"Tag '{tag}' không đúng dạng vYYYY-M-D.")
    y, mo, d = (int(x) for x in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}"


def version_key(v: str) -> tuple[int, int, int]:
    return tuple(int(x) for x in v.split("-"))  # type: ignore[return-value]


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} lỗi:\n{r.stderr.strip()}")
    return r.stdout


def github_slug(root: Path) -> str:
    url = git(root, "remote", "get-url", "origin").strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise ReleaseError(f"Remote origin không phải GitHub: {url}")
    return m.group(1)


def latest_tag(root: Path) -> str:
    out = git(root, "ls-remote", "--tags", "origin")
    tags = {line.split("refs/tags/")[1].removesuffix("^{}") for line in out.splitlines() if "refs/tags/" in line}
    tags = [t for t in tags if TAG_RE.match(t)]
    if not tags:
        raise ReleaseError("Không tìm thấy tag dạng vYYYY-M-D trên origin.")
    return max(tags, key=lambda t: version_key(tag_to_version(t)))


def download_pdf(slug: str, tag: str, dest: Path) -> None:
    url = f"https://github.com/{slug}/releases/download/{tag}/{ASSET}"
    print(f"Tải {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            dest.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ReleaseError(f"Release {tag} chưa có file {ASSET} (HTTP 404). Tag có tồn tại và đã đính kèm PDF chưa?")
        raise ReleaseError(f"Tải PDF lỗi: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise ReleaseError(f"Không tải được PDF ({e.reason}). Có thể dùng --pdf với file trên máy.")


# ---------------------------------------------------------------- PDF
def read_pdf(path: Path):
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        r = PdfReader(str(path))
        n = len(r.pages)
    except Exception as e:  # noqa: BLE001
        raise ReleaseError(f"Không đọc được PDF {path}: {e}")
    title = nfc((r.metadata or {}).get("/Title", "") or "")
    page1 = nfc(r.pages[0].extract_text() or "")
    if TITLE_CHECK not in title and TITLE_CHECK not in page1:
        raise ReleaseError(f"PDF không giống sách (không thấy '{TITLE_CHECK}' ở tiêu đề hay trang 1).")
    toc: list[tuple[int, str, int]] = []

    def walk(items, depth=0):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
            else:
                toc.append((depth, nfc(it.title).strip(), r.get_destination_page_number(it) + 1))

    walk(r.outline)
    if not toc:
        raise ReleaseError("PDF không có mục lục (outline/bookmark).")
    return n, toc


def render_cover(pdf: Path) -> tuple[bytes, str]:
    exe = shutil.which("pdftoppm")
    if exe:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "cover"
            subprocess.run(
                [exe, "-f", "1", "-l", "1", "-png", "-singlefile",
                 "-scale-to-x", str(COVER_SIZE[0]), "-scale-to-y", str(COVER_SIZE[1]), str(pdf), str(out)],
                check=True,
            )
            return (out.with_suffix(".png")).read_bytes(), "pdftoppm"
    import pypdfium2 as pdfium
    from PIL import Image
    doc = pdfium.PdfDocument(str(pdf))
    img = doc[0].render(scale=4).to_pil().convert("RGB").resize(COVER_SIZE, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), "pypdfium2"


# ---------------------------------------------------------------- index.html
def current_values(html: str) -> tuple[str, int]:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if not m:
        raise ReleaseError("index.html không có khối JSON-LD.")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ReleaseError(f"JSON-LD trong index.html lỗi cú pháp: {e}")
    try:
        return str(data["version"]), int(data["numberOfPages"])
    except KeyError as e:
        raise ReleaseError(f"JSON-LD thiếu trường {e}.")


def update_html(html: str, old_v: str, new_v: str, old_p: int, new_p: int) -> str:
    """Thay ở các vị trí cố định; mỗi mẫu phải khớp đúng 1 lần."""
    ov, op = re.escape(old_v), re.escape(str(old_p))
    rules = [
        (rf'("version": "){ov}(")', new_v),
        (rf'("dateModified": "){ov}(")', new_v),
        (rf"(Phiên bản ){ov}( · )", new_v),
        (rf"(note\s*=\s*\{{Phiên bản ){ov}(\}})", new_v),
        (rf'("numberOfPages": ){op}(,)', str(new_p)),
        (rf"( · ){op}( trang A4 · )", str(new_p)),
    ]
    for pat, val in rules:
        n = len(re.findall(pat, html))
        if n != 1:
            raise ReleaseError(f"Mẫu {pat!r} khớp {n} lần (cần đúng 1). Cấu trúc index.html đã đổi? Không ghi gì.")
        html = re.sub(pat, lambda m: m.group(1) + val + m.group(2), html)
    # Dòng chú thích nhắc việc: không bắt buộc có
    html = re.sub(rf'(tìm "){ov}(" và "){op}(")',
                  lambda m: f"{m.group(1)}{new_v}{m.group(2)}{new_p}{m.group(3)}", html)
    return html


# ---------------------------------------------------------------- mục lục
NUM_RE = re.compile(r"^(Chương\s+\d+\.|Phụ lục\s+[A-Z]\.|\d+(?:\.\d+)*\.)\s*")


def toc_key(entry: tuple[int, str, int]) -> tuple[int, str]:
    depth, title, _ = entry
    return depth, NUM_RE.sub("", title)


def format_snapshot(tag: str, pages: int, toc) -> str:
    lines = [f"# Mục lục (outline) PDF {tag}, {pages} trang. File do tools/release.py sinh; đừng sửa tay.",
             "# <thụt lề theo cấp><tiêu đề>\\t<trang>"]
    lines += ["  " * d + f"{t}\t{p}" for d, t, p in toc]
    return "\n".join(lines) + "\n"


def parse_snapshot(text: str):
    toc = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        body, _, page = line.rpartition("\t")
        depth = (len(body) - len(body.lstrip(" "))) // 2
        toc.append((depth, nfc(body.strip()), int(page)))
    return toc


def toc_report(old, new) -> list[str]:
    a, b = [toc_key(e) for e in old], [toc_key(e) for e in new]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    show = lambda e: "  " * e[0] + e[1] + f"  (tr. {e[2]})"  # noqa: E731
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        # Ghép cặp "đổi tên": cùng cấp, tiêu đề (bỏ số thứ tự) giống nhau ≥ 75%
        olds, news = list(old[i1:i2]), list(new[j1:j2])
        pairs: dict[int, int] = {}
        for jn, y in enumerate(news):
            best, best_r = None, 0.75
            for io_, x in enumerate(olds):
                if io_ in pairs.values() or x[0] != y[0]:
                    continue
                r = difflib.SequenceMatcher(a=toc_key(x)[1], b=toc_key(y)[1]).ratio()
                if r >= best_r:
                    best, best_r = io_, r
            if best is not None:
                pairs[jn] = best
        for io_, x in enumerate(olds):
            if io_ not in pairs.values():
                out.append(f"  BỎ       {show(x).strip()}")
        for jn, y in enumerate(news):
            if jn in pairs:
                out.append(f"  ĐỔI TÊN  {show(olds[pairs[jn]]).strip()}\n        →  {show(y).strip()}")
            else:
                out.append(f"  THÊM     {show(y).strip()}")
    return out


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", help="tag release, ví dụ v2026-12-05 (mặc định: tag mới nhất trên origin)")
    ap.add_argument("--pdf", type=Path, help="dùng file PDF có sẵn thay vì tải từ GitHub")
    ap.add_argument("--repo", type=Path, default=ROOT_DEFAULT, help="thư mục gốc repo (mặc định: thư mục cha của tools/)")
    ap.add_argument("--force", action="store_true", help="cho phép cùng phiên bản hoặc phiên bản cũ hơn")
    ap.add_argument("--dry-run", action="store_true", help="chỉ báo cáo, không ghi file")
    args = ap.parse_args(argv)

    root = args.repo.resolve()
    html_path, cover_path = root / "index.html", root / "cover.png"
    snap_path = root / "tools" / "toc-snapshot.txt"
    html = html_path.read_text(encoding="utf-8")
    old_v, old_p = current_values(html)

    tag = args.tag or latest_tag(root)
    new_v = tag_to_version(tag)
    print(f"Trang hiện tại: phiên bản {old_v}, {old_p} trang. Bản phát hành: {tag} → {new_v}.")
    if version_key(new_v) < version_key(old_v) and not args.force:
        raise ReleaseError(f"{new_v} cũ hơn phiên bản đang có trên trang ({old_v}). Dùng --force nếu thật sự muốn lùi.")
    if new_v == old_v and not args.force:
        print(f"Trang đã ở phiên bản {new_v}; không làm gì (dùng --force để chạy lại).")
        return 0

    with tempfile.TemporaryDirectory() as td:
        if args.pdf:
            pdf = args.pdf.expanduser().resolve()
            if not pdf.is_file():
                raise ReleaseError(f"Không có file {pdf}")
        else:
            pdf = Path(td) / ASSET
            download_pdf(github_slug(root), tag, pdf)
        new_p, toc = read_pdf(pdf)
        cover, renderer = render_cover(pdf)

    new_html = update_html(html, old_v, new_v, old_p, new_p)
    current_values(new_html)  # JSON-LD vẫn hợp lệ
    new_snap = format_snapshot(tag, new_p, toc)

    print(f"Số trang: {old_p} → {new_p}. Bìa render bằng {renderer}.")
    print("\nMục lục PDF so với bản chụp trước:")
    if snap_path.exists():
        rep = toc_report(parse_snapshot(snap_path.read_text(encoding="utf-8")), toc)
        print("\n".join(rep) if rep else "  Không có mục nào thêm, bỏ hay đổi tên (chỉ có thể đổi số trang).")
        if rep:
            print("  → Sửa mục lục trong index.html (section#muc-luc) theo danh sách trên.")
    else:
        print("  Chưa có bản chụp trước; sẽ tạo tools/toc-snapshot.txt để so cho lần sau.")

    if args.dry_run:
        print("\n--dry-run: không ghi file.")
        return 0
    html_path.write_text(new_html, encoding="utf-8")
    cover_path.write_bytes(cover)
    snap_path.parent.mkdir(exist_ok=True)
    snap_path.write_text(new_snap, encoding="utf-8")
    print("\nĐã ghi index.html, cover.png, tools/toc-snapshot.txt. Kiểm tra rồi commit:")
    try:
        print(git(root, "diff", "--stat"))
    except ReleaseError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as e:
        print(f"LỖI: {e}", file=sys.stderr)
        sys.exit(1)
