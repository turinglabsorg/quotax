import time

from .i18n import _


def countdown(to: float, now: float) -> str:
    seconds = int(to - now)
    if seconds <= 0:
        return _("now")
    days, hours, minutes = seconds // 86_400, (seconds % 86_400) // 3_600, (seconds % 3_600) // 60
    if days > 0:
        return _("{days}d {hours}h", days=days, hours=hours) if hours > 0 else _("{days}d", days=days)
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"
    return f"{max(minutes, 1)}m"


def relative(date: float, now: float) -> str:
    seconds = int(now - date)
    if seconds < 60:
        return _("now")
    if seconds < 3_600:
        return _("{minutes} min ago", minutes=seconds // 60)
    return _("{hours} h ago", hours=seconds // 3_600)


def duration(minutes: int) -> str:
    if minutes % 1_440 == 0:
        return _("{days} d", days=minutes // 1_440)
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min"


def absolute(date: float) -> str:
    return time.strftime("%a %d %b %H:%M", time.localtime(date))


def capitalized(value: str) -> str:
    trimmed = value.strip()
    return trimmed[:1].upper() + trimmed[1:]


def rounded(value: float) -> int:
    """Half away from zero, like Swift's `rounded()` (Python's `round` is banker's rounding)."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)
