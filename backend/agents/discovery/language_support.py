from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

FOREIGN_LANGUAGE_CONFIG = Path(__file__).resolve().parents[3] / "config" / "foreign_languages.json"

SCRIPT_PATTERNS: dict[str, re.Pattern[str]] = {
    "Arab": re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]"),
    "Beng": re.compile(r"[\u0980-\u09ff]"),
    "Cyrl": re.compile(r"[\u0400-\u04ff]"),
    "Deva": re.compile(r"[\u0900-\u097f]"),
    "Ethi": re.compile(r"[\u1200-\u137f]"),
    "Hang": re.compile(r"[\uac00-\ud7af]"),
    "Hans": re.compile(r"[\u4e00-\u9fff]"),
    "Hant": re.compile(r"[\u4e00-\u9fff]"),
    "Jpan": re.compile(r"[\u3040-\u30ff]"),
    "Taml": re.compile(r"[\u0b80-\u0bff]"),
    "Telu": re.compile(r"[\u0c00-\u0c7f]"),
    "Thai": re.compile(r"[\u0e00-\u0e7f]"),
}

SCRIPT_DETECTION_PRIORITY = {
    "Jpan": 0,
    "Hang": 0,
    "Thai": 0,
    "Deva": 0,
    "Beng": 0,
    "Telu": 0,
    "Taml": 0,
    "Ethi": 0,
    "Arab": 0,
    "Cyrl": 0,
    "Hans": 10,
    "Hant": 10,
}


def trusted_language_options() -> tuple[dict[str, Any], ...]:
    payload = _read_language_config()
    languages = payload.get("trusted_languages")
    if not isinstance(languages, list):
        return ()
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in languages:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        scripts = _string_list(item.get("scripts"))
        if not code or not name or code in seen:
            continue
        cleaned.append({"code": code, "name": name, "scripts": scripts})
        seen.add(code)
    return tuple(cleaned)


def trusted_script_language_patterns() -> tuple[tuple[str, str, str, re.Pattern[str]], ...]:
    entries: list[tuple[int, int, str, str, str, re.Pattern[str]]] = []
    for index, language in enumerate(trusted_language_options()):
        code = str(language["code"])
        if code == "en":
            continue
        for script in language.get("scripts") or []:
            pattern = SCRIPT_PATTERNS.get(str(script))
            if pattern is None:
                continue
            priority = SCRIPT_DETECTION_PRIORITY.get(str(script), 5)
            entries.append((priority, index, code, str(language["name"]), str(script), pattern))
    entries.sort(key=lambda item: (item[0], item[1]))
    return tuple((code, name, script, pattern) for _, _, code, name, script, pattern in entries)


def _read_language_config() -> dict[str, Any]:
    try:
        payload = json.loads(FOREIGN_LANGUAGE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"trusted_languages": []}
    return payload if isinstance(payload, dict) else {"trusted_languages": []}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        cleaned.append(text)
        seen.add(key)
    return cleaned
