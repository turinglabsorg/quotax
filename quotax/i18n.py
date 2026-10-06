"""Tiny message catalog shared with the GNOME Shell extension (`locale/<language>.json`).

Keys are the English strings; `{name}` placeholders are filled after translation.
"""

import json
import os
from functools import cache
from pathlib import Path

LOCALE_DIR = Path(__file__).with_name("locale")


def language() -> str:
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable, "")
        code = value.split(":")[0].split(".")[0].split("@")[0].split("_")[0]
        if code:
            return "en" if code in ("C", "POSIX") else code
    return "en"


@cache
def _catalog(code: str) -> dict[str, str]:
    try:
        return json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _(text: str, **values) -> str:
    translated = _catalog(language()).get(text, text)
    return translated.format(**values) if values else translated
