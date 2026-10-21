"""Bound the size of persistent PNG artifacts without changing preview JPEGs.

Screenshot *bytes* are compacted by :func:`compact_png`; screenshot
*placement* (which logical shard, which machine) lives in
:mod:`app.image_shards` and is re-exported here so existing imports keep
working.
"""
import io

from app.image_shards import (  # noqa: F401  (re-exported API)
    ImageShardStore,
    Machine,
    MachineMap,
    default_store,
    safe_filename,
    shard_count,
    shard_for,
)


def compact_png(data: bytes) -> bytes:
    """Palette-encode screenshots when it saves space; preserve PNG otherwise.

    Run this off the event loop. Browser preview uses JPEG and is not affected.
    """
    if not data or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            palette = image.convert("RGB").quantize(
                colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE
            )
            output = io.BytesIO()
            palette.save(output, format="PNG", compress_level=3)
            candidate = output.getvalue()
            return candidate if len(candidate) < len(data) else data
    except Exception:
        return data
