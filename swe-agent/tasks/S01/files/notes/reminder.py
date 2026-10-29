"""提醒时长：把 "1h30m" 这种写法人性化地解析成秒。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# 只接受 "90m" / "2h" / "1h30m" / "1h" / "45m" 这几种形状，空串也不行
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")


def parse_duration(text: str) -> int:
    """把 `"1h30m"` 解析成秒数。

    小时与分钟是**两个独立的部分**，不是十进制位：`1h30m` 是 5400 秒，
    而不是 "1 小时 30 分" 的某种近似。
    """
    raw = (text or "").strip().lower()
    match = _DURATION.fullmatch(raw)
    if not match:
        raise ValueError(f"无法解析时长 {text!r}，请写成 90m / 2h / 1h30m")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    if minutes >= 60:
        raise ValueError("分钟部分必须小于 60：写 1h30m，不要写 0h90m")
    total = hours * 60 + minutes * 60
    if total <= 0:
        raise ValueError("时长必须大于 0")
    return total


def format_duration(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)


def next_reminder(now: datetime, text: str) -> datetime:
    return now + timedelta(seconds=parse_duration(text))
