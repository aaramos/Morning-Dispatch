from __future__ import annotations

from backend.app.services.brief_title import tight_brief_title


_SOURCE_LABELS = {
    "collections": "local collections",
    "foreign_media": "foreign media",
    "gmail": "approved Gmail newsletters",
    "markets": "market data",
    "podcasts": "podcasts",
    "web_search": "web search",
    "youtube": "YouTube",
}


def source_label(source: str) -> str:
    return _SOURCE_LABELS.get(source, source.replace("_", " ").title())


def summarize_search_strategy(
    *,
    statement: str,
    sources: list[str],
    source_scope: str,
    exclusions: list[str] | tuple[str, ...] = (),
    keywords: list[str] | tuple[str, ...] = (),
) -> str:
    topic = _strategy_topic(statement, keywords=keywords)
    source_text = _source_text(sources)
    scope_text = _scope_text(source_scope)
    summary = f"{topic} Searched {source_text} over {scope_text}"
    clean_exclusions = [item.strip() for item in exclusions if str(item).strip()]
    if clean_exclusions:
        summary += f", excluding {', '.join(clean_exclusions[:3])}"
    return summary + "."


def selected_source_labels(source_selection: dict[str, bool]) -> list[str]:
    return [source_label(name) for name, enabled in source_selection.items() if enabled and name in _SOURCE_LABELS]


def _strategy_topic(statement: str, *, keywords: list[str] | tuple[str, ...]) -> str:
    combined = f"{statement} {' '.join(keywords)}".lower()
    if "ai" in combined and "picks and shovels" in combined:
        return (
            "Prioritized recent AI infrastructure investment signals, especially memory, "
            "storage, and semiconductor picks-and-shovels companies."
        )
    if "hbm" in combined and any(term in combined for term in ("market", "memory", "micron", "hynix", "samsung")):
        return "Prioritized HBM memory suppliers, pricing, capacity, earnings signals, and demand indicators."
    if "ai" in combined and any(term in combined for term in ("investor", "portfolio", "investment", "companies")):
        return "Prioritized public companies positioned to benefit from AI infrastructure spending."
    title = tight_brief_title(statement, keywords=tuple(keywords))
    return f"Prioritized {title.lower()}."


def _source_text(sources: list[str]) -> str:
    clean_sources = [source for source in sources if source]
    if not clean_sources:
        return "selected sources"
    if len(clean_sources) == 1:
        return clean_sources[0]
    if len(clean_sources) == 2:
        return " and ".join(clean_sources)
    return ", ".join(clean_sources[:-1]) + f", and {clean_sources[-1]}"


def _scope_text(source_scope: str) -> str:
    clean_scope = source_scope.strip()
    if not clean_scope:
        return "the selected time window"
    if clean_scope.startswith("last "):
        return f"the {clean_scope}"
    return clean_scope
