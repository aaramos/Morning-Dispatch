from __future__ import annotations

import re


_DISPATCH_SUFFIX_RE = re.compile(r"\s*[-–—]\s*Morning Dispatch Issue\s*$", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&/.'+-]*")


def tight_brief_title(text: str, *, keywords: tuple[str, ...] = (), max_words: int = 8) -> str:
    """Return a short editorial title from a user prompt or refined scope."""

    cleaned = _clean_title_seed(text)
    lowered = cleaned.lower()
    keyword_text = " ".join(keywords).lower()
    combined = f"{lowered} {keyword_text}"

    if "ai" in combined and "picks and shovels" in combined:
        return "AI Picks-and-Shovels Investment Signals"
    if "hbm" in combined and any(term in combined for term in ("market", "memory", "micron", "hynix", "samsung")):
        return "HBM Memory Market Signals"
    if "ai" in combined and any(term in combined for term in ("investor", "portfolio", "investment", "companies")):
        return "AI Buildout Investment Signals"

    candidate = _extract_action_phrase(cleaned) or cleaned
    candidate = _strip_prompt_language(candidate)
    words = _WORD_RE.findall(candidate)
    if not words:
        return "Morning Brief"
    title = " ".join(words[:max_words])
    return _title_case(title)


def _clean_title_seed(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    cleaned = _DISPATCH_SUFFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+[-–—]\s*$", "", cleaned)
    return cleaned.strip()


def _extract_action_phrase(text: str) -> str:
    patterns = (
        r"\bhelp me identify\s+(?P<value>.+?)(?:\.|$)",
        r"\bidentify\s+(?P<value>.+?)(?:\.|$)",
        r"\bfocus(?:ing)? on\s+(?P<value>.+?)(?:\.|$)",
        r"\bcover(?:ing)?\s+(?P<value>.+?)(?:\.|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group("value").strip()
    first_sentence = re.split(r"[.!?]", text, maxsplit=1)[0]
    return first_sentence.strip()


def _strip_prompt_language(text: str) -> str:
    replacements = (
        r"^companies\s+poised\s+to\s+benefit\s+(?:greatly\s+)?from\s+",
        r"^the\s+",
        r"^a\s+",
        r"^an\s+",
    )
    cleaned = text.strip()
    for pattern in replacements:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _title_case(text: str) -> str:
    small_words = {"a", "an", "and", "as", "for", "from", "in", "of", "on", "or", "the", "to", "vs", "with"}
    words = []
    for index, word in enumerate(text.split()):
        lower = word.lower()
        if word.isupper() or any(char.isdigit() for char in word):
            words.append(word)
        elif index > 0 and lower in small_words:
            words.append(lower)
        else:
            words.append(lower.capitalize())
    return " ".join(words)
