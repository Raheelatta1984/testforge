"""Bound the size of persistent PNG artifacts without changing preview JPEGs."""
import io


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
