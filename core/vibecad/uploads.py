"""Files the user attaches to a prompt in the GUI: saved under <root>/uploads/, then handed to the agent as
content blocks (images and PDFs the model reads directly, text files inline, anything else by name only)."""
from __future__ import annotations

import base64
import io
import re
import time
from pathlib import Path

MAX_BYTES = 30 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".step", ".stp", ".dxf", ".svg", ".py", ".scad",
            ".kcl", ".ini", ".toml", ".log", ".html", ".obj"}
TEXT_LIMIT = 100_000     # characters of a text file given to the model
IMAGE_MAX_SIDE = 1568    # the API downsamples beyond this anyway
IMAGE_MAX_BYTES = 3_500_000


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_TYPES:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    return "other"


def save(root: Path, name: str, data: bytes) -> dict:
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name} is {len(data) / 1e6:.1f} MB; the limit is {MAX_BYTES // 1024 // 1024} MB")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "file"
    d = root / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
    n = 1
    while p.exists():
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}-{safe}"
        n += 1
    p.write_bytes(data)
    return {"id": str(p.relative_to(root)), "name": Path(name).name, "kind": kind_of(name), "size": len(data)}


def resolve(root: Path, file_id: str) -> Path:
    p = (root / file_id).resolve()
    if p.parent != (root / "uploads").resolve() or not p.is_file():
        raise ValueError(f"no uploaded file {file_id!r}")
    return p


def _image_block(p: Path) -> dict:
    data, media = p.read_bytes(), IMAGE_TYPES[p.suffix.lower()]
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) > IMAGE_MAX_SIDE or len(data) > IMAGE_MAX_BYTES or media == "image/webp":
            im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            buf = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P"):
                im.convert("RGBA").save(buf, "PNG", optimize=True)
                media = "image/png"
            else:
                im.convert("RGB").save(buf, "JPEG", quality=88)
                media = "image/jpeg"
            data = buf.getvalue()
    except Exception:
        pass  # send as is; the API reports what it can't take
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(data).decode()}}


def blocks(root: Path, ids: list[str]) -> tuple[str, list[dict]]:
    """(a context line for the prompt, content blocks) for the attached files."""
    lines, out = [], []
    for fid in ids:
        p = resolve(root, fid)
        name = p.name.split("-", 2)[-1]
        k = kind_of(p.name)
        if k == "image":
            out.append(_image_block(p))
            lines.append(f"{name} (image, below)")
        elif k == "pdf":
            out.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                        "data": base64.b64encode(p.read_bytes()).decode()}, "title": name})
            lines.append(f"{name} (PDF, below)")
        else:
            raw = p.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and (k == "text" or "\x00" not in text[:4000]):
                cut = len(text) > TEXT_LIMIT
                out.append({"type": "text", "text": f"Attached file {name} ({len(raw):,} bytes"
                            + (f", first {TEXT_LIMIT:,} characters" if cut else "") + f"):\n```\n{text[:TEXT_LIMIT]}\n```"})
                lines.append(f"{name} (text, below)")
            else:
                lines.append(f"{name} ({len(raw):,} bytes, saved as {fid}; a binary format you can't read directly)")
    ctx = f"[The user attached {len(ids)} file(s): " + "; ".join(lines) + "]\n" if ids else ""
    return ctx, out
