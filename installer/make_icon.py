# -*- coding: utf-8 -*-
"""从扩展图标生成 Windows 多尺寸 .ico。

Pillow 可用时输出 7 个尺寸；否则用标准库生成只含 128×128 PNG 条目的 ICO
（Vista 起支持 PNG 压缩图标条目），保证构建脚本零硬性第三方依赖。
"""
from __future__ import annotations

import os
import struct

SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _entry(size, png, offset):
    """ICONDIRENTRY：宽高为 0 表示 256。"""
    w = 0 if size >= 256 else size
    return struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), offset)


def build_single_png_ico(png_bytes, size=128):
    header = struct.pack("<HHH", 0, 1, 1)
    return header + _entry(size, png_bytes, 22) + png_bytes


def build_ico(src_png, dest_ico, sizes=SIZES):
    with open(src_png, "rb") as f:
        raw = f.read()
    if raw[:8] != PNG_MAGIC:
        raise ValueError("源文件不是 PNG：%s" % src_png)
    try:
        from PIL import Image
    except ImportError:
        data = build_single_png_ico(raw)
    else:
        import io
        images = []
        with Image.open(src_png) as im:
            im = im.convert("RGBA")
            for size in sizes:
                buf = io.BytesIO()
                im.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
                images.append((size, buf.getvalue()))
        out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
        offset = 6 + 16 * len(images)
        for size, png in images:
            out += _entry(size, png, offset)
            offset += len(png)
        for _size, png in images:
            out += png
        data = bytes(out)
    parent = os.path.dirname(os.path.abspath(dest_ico))
    os.makedirs(parent, exist_ok=True)
    with open(dest_ico, "wb") as f:
        f.write(data)
    return dest_ico


def ico_entry_count(data):
    """供测试/校验用：返回 ICO 里的图像条目数。"""
    if len(data) < 6 or struct.unpack_from("<H", data, 2)[0] != 1:
        return 0
    return struct.unpack_from("<H", data, 4)[0]
