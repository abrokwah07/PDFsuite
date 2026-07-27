"""
Aggressive Office (OOXML) compression.

Images dominate .docx/.pptx size. We resize, strip metadata, re-encode as JPEG
(where safe), update Content_Types + relationships when extensions change, and
write the package with maximum DEFLATE compression.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from app.security import is_safe_zip_member

logger = logging.getLogger("pdfsuite")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp"}
# Paths that typically hold embedded media
MEDIA_HINTS = ("/media/", "/embeddings/", "word/media", "ppt/media", "xl/media")

LEVELS: dict[str, dict[str, Any]] = {
    # quality is JPEG quality; max_dim is longest side in px
    "low": {"quality": 72, "max_dim": 1600, "min_bytes_to_touch": 8_000},
    "medium": {"quality": 45, "max_dim": 1200, "min_bytes_to_touch": 4_000},
    "high": {"quality": 28, "max_dim": 800, "min_bytes_to_touch": 2_000},
}


def _is_media_path(name: str) -> bool:
    n = name.replace("\\", "/").lower()
    return any(h in n for h in MEDIA_HINTS) or n.startswith("word/media") or n.startswith(
        "ppt/media"
    )


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def compress_image_bytes(
    data: bytes,
    member_name: str,
    *,
    quality: int,
    max_dim: int,
    min_bytes: int,
) -> tuple[bytes, str, bool]:
    """
    Returns (new_bytes, new_member_name, changed).
    May change extension to .jpg for better compression.
    """
    if len(data) < min_bytes:
        return data, member_name, False

    ext = Path(member_name).suffix.lower()
    if ext not in IMAGE_EXTS:
        return data, member_name, False

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        logger.debug("Cannot open image %s: %s", member_name, exc)
        return data, member_name, False

    # Skip tiny icons
    w, h = img.size
    if w * h < 80 * 80 and len(data) < 20_000:
        return data, member_name, False

    img = _flatten_to_rgb(img)
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    img.thumbnail((max_dim, max_dim), resample)

    # Strip EXIF by re-encoding
    out = io.BytesIO()
    # Prefer JPEG for photos; keep PNG only if still smaller after optimize attempt
    use_jpeg = True
    img.save(
        out,
        format="JPEG",
        quality=max(10, min(95, quality)),
        optimize=True,
        progressive=True,
        subsampling=2 if quality < 60 else 0,
    )
    jpeg_bytes = out.getvalue()

    # Try optimized PNG for comparison on small graphics
    if ext == ".png" and len(data) < 200_000:
        png_out = io.BytesIO()
        try:
            # Re-open original for PNG path with palette reduction
            raw = Image.open(io.BytesIO(data))
            raw = _flatten_to_rgb(raw)
            raw.thumbnail((max_dim, max_dim), resample)
            raw_p = raw.convert("P", palette=Image.ADAPTIVE, colors=128)
            raw_p.save(png_out, format="PNG", optimize=True, compress_level=9)
            png_bytes = png_out.getvalue()
            if len(png_bytes) < len(jpeg_bytes) * 0.9:
                use_jpeg = False
                new_data = png_bytes
            else:
                new_data = jpeg_bytes
        except Exception:
            new_data = jpeg_bytes
    else:
        new_data = jpeg_bytes

    if not use_jpeg:
        # Keep .png name
        if len(new_data) >= len(data) * 0.98:
            return data, member_name, False
        return new_data, member_name, True

    # JPEG output — rename non-jpeg members
    new_name = member_name
    if ext not in {".jpg", ".jpeg"}:
        new_name = str(Path(member_name).with_suffix(".jpg"))
    # Only keep if smaller (or significantly smaller for renames)
    threshold = 0.98 if new_name == member_name else 0.99
    if len(new_data) >= len(data) * threshold and new_name == member_name:
        return data, member_name, False
    if len(new_data) >= len(data) and new_name != member_name:
        # Rename not worth it if larger
        return data, member_name, False
    return new_data, new_name, True


def _rewrite_package_xml(text: str, renames: dict[str, str]) -> str:
    """Replace old media paths with new ones in XML (Content_Types, rels, docs)."""
    if not renames:
        return text
    out = text
    for old, new in renames.items():
        if old == new:
            continue
        out = out.replace(old, new)
        # Also bare filenames
        out = out.replace(Path(old).name, Path(new).name)
        # Content type for Extension
        old_ext = Path(old).suffix.lstrip(".").lower()
        new_ext = Path(new).suffix.lstrip(".").lower()
        if old_ext and new_ext and old_ext != new_ext:
            # Override="word/media/image1.png" style Target
            pass
    # Fix Default Content_Type for jpg if missing
    if 'Extension="jpg"' not in out and any(n.endswith(".jpg") for n in renames.values()):
        out = out.replace(
            "</Types>",
            '<Default Extension="jpg" ContentType="image/jpeg"/>\n</Types>',
        )
        if 'Extension="jpeg"' not in out:
            out = out.replace(
                "</Types>",
                '<Default Extension="jpeg" ContentType="image/jpeg"/>\n</Types>',
            )
    # Remove obsolete Override for renamed png if present (optional)
    for old, new in renames.items():
        if old == new:
            continue
        # PartName="/word/media/image1.png")
        part_old = "/" + old.lstrip("/")
        part_new = "/" + new.lstrip("/")
        out = out.replace(part_old, part_new)
    return out


def compress_ooxml(
    data: bytes,
    *,
    level: str = "medium",
) -> tuple[bytes, dict[str, Any]]:
    """
    Compress a .docx/.pptx package in memory.
    Returns (compressed_bytes, stats).
    """
    cfg = LEVELS.get(level.lower(), LEVELS["medium"])
    quality = int(cfg["quality"])
    max_dim = int(cfg["max_dim"])
    min_bytes = int(cfg["min_bytes_to_touch"])

    renames: dict[str, str] = {}
    members: list[tuple[str, bytes, bool]] = []  # name, data, is_xml
    images_touched = 0
    bytes_before_images = 0
    bytes_after_images = 0

    with zipfile.ZipFile(io.BytesIO(data), "r") as zin:
        for item in zin.infolist():
            if not is_safe_zip_member(item.filename):
                continue
            if item.is_dir():
                continue
            name = Path(item.filename).as_posix()
            raw = zin.read(item.filename)
            lower = name.lower()
            is_image = lower.endswith(tuple(IMAGE_EXTS)) and (
                _is_media_path(name) or "media" in lower
            )

            if is_image:
                bytes_before_images += len(raw)
                new_raw, new_name, changed = compress_image_bytes(
                    raw,
                    name,
                    quality=quality,
                    max_dim=max_dim,
                    min_bytes=min_bytes,
                )
                bytes_after_images += len(new_raw)
                if changed:
                    images_touched += 1
                    if new_name != name:
                        renames[name] = new_name
                    members.append((new_name, new_raw, False))
                else:
                    members.append((name, raw, False))
            else:
                is_xml = lower.endswith((".xml", ".rels"))
                members.append((name, raw, is_xml))

    # Apply renames inside XML parts
    final_members: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for name, raw, is_xml in members:
        if is_xml and renames:
            try:
                text = raw.decode("utf-8")
                text2 = _rewrite_package_xml(text, renames)
                raw = text2.encode("utf-8")
            except Exception:
                pass
        # Dedupe if rename collision
        out_name = name
        if out_name in seen:
            continue
        seen.add(out_name)
        final_members.append((out_name, raw))

    out_buf = io.BytesIO()
    # compresslevel=9 for max size reduction (Python 3.7+)
    with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
        for name, raw in final_members:
            info = zipfile.ZipInfo(filename=name)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Store images still deflated; already highly compressed JPEGs
            zout.writestr(info, raw)

    result = out_buf.getvalue()
    # Never return larger than original for "compress"
    if len(result) >= len(data):
        # Fallback: only max-deflate original without re-encoding (sometimes still helps a little)
        out_buf2 = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data), "r") as zin:
            with zipfile.ZipFile(
                out_buf2, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as zout:
                for item in zin.infolist():
                    if item.is_dir() or not is_safe_zip_member(item.filename):
                        continue
                    zout.writestr(item.filename, zin.read(item.filename))
        fallback = out_buf2.getvalue()
        if len(fallback) < len(result):
            result = fallback
        if len(result) >= len(data):
            result = data  # no gain — return original

    stats = {
        "original_bytes": len(data),
        "compressed_bytes": len(result),
        "saved_bytes": max(0, len(data) - len(result)),
        "saved_percent": round(
            (1 - len(result) / len(data)) * 100, 1
        )
        if len(data)
        else 0.0,
        "images_touched": images_touched,
        "image_bytes_before": bytes_before_images,
        "image_bytes_after": bytes_after_images,
        "renames": len(renames),
        "level": level,
    }
    return result, stats
