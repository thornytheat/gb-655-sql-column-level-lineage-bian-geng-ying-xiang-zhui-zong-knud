"""归档包的规范化 JSON 与摘要。

同一个事实只允许一种字节写法：键排序、无多余空白、拒绝 NaN/Infinity。
摘要算在规范化字节上，所以导出 → 校验 → 再导出 必然得到同一个值。
"""

from __future__ import annotations

import hashlib
import json

from app import errors


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def encode(value) -> bytes:
    text = dumps(value)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise errors.ArchiveRejected(
            "归档内容含无法编码的字符", detail={"reason": "BAD_ENCODING", "pos": exc.start}
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value) -> str:
    """对任意 JSON 值取规范化摘要。"""
    return sha256_bytes(encode(value))
