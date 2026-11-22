"""时间解析工具：统一使用带时区的 ISO 8601，内部规范化为 UTC ``...Z`` 文本。

数仓在 SQLite 中以文本保存时间。规范化后的 UTC 文本长度固定、按字典序
比较等价于按时间先后比较，因此 as-of 查询可以直接使用字符串比较，
无需在 SQL 层引入时间函数。
"""
from __future__ import annotations

from datetime import datetime, timezone

_INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_instant(text: str) -> datetime:
    """解析带时区的 ISO 8601 时间值，返回 UTC 感知对象。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("时间值不能为空")
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"无法解析的时间值: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"时间值必须携带时区: {text!r}")
    return parsed.astimezone(timezone.utc)


def format_instant(moment: datetime) -> str:
    """把带时区的时间格式化为 UTC ``YYYY-MM-DDTHH:MM:SSZ`` 文本。"""
    if moment.tzinfo is None:
        raise ValueError("format_instant 需要带时区的时间")
    return moment.astimezone(timezone.utc).strftime(_INSTANT_FORMAT)


def normalize_instant(text: str) -> str:
    """校验并规范化时间值为 UTC 文本，非法值抛出 ValueError。"""
    return format_instant(parse_instant(text))


def parse_optional(text: str | None) -> str | None:
    """可空时间字段：空串或 None 返回 None，否则规范化。"""
    if text is None or not text.strip():
        return None
    return normalize_instant(text)
