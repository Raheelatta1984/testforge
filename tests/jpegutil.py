"""Read a JPEG's pixel size without an image library."""

import struct


def jpeg_size(blob: bytes):
    if not blob or not blob.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(blob):
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", blob[index + 5:index + 9])
            return width, height
        if marker == 0xD9 or index + 4 > len(blob):
            break
        length = struct.unpack(">H", blob[index + 2:index + 4])[0]
        index += 2 + length
    return None
