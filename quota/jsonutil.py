"""Lenient readers for the providers' JSON: wrong types become None instead of exceptions."""

import json
import math
import re
from datetime import datetime, timezone

from .models import ProviderIssue


def obj(data: bytes | str) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, TypeError):
        raise ProviderIssue("invalidResponse") from None
    if not isinstance(value, dict):
        raise ProviderIssue("invalidResponse")
    return value


def try_obj(data: bytes | str | None) -> dict | None:
    if data is None:
        return None
    try:
        return obj(data)
    except ProviderIssue:
        return None


def mapping(value) -> dict | None:
    return value if isinstance(value, dict) else None


def string(value) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def timestamp(value) -> float | None:
    """Seconds or milliseconds since the epoch, or an ISO 8601 string."""
    parsed = number(value)
    if parsed is not None:
        if parsed <= 0:
            return None
        return parsed / 1_000 if parsed > 10_000_000_000 else parsed
    text = string(value)
    return iso(text) if text else None


def iso(text: str) -> float | None:
    without_fraction = re.sub(r"\.\d+", "", text)
    if without_fraction.endswith(("Z", "z")):
        without_fraction = without_fraction[:-1] + "+00:00"
    try:
        date = datetime.fromisoformat(without_fraction)
    except ValueError:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.timestamp()
