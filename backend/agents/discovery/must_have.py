from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from backend.agents.discovery.types import TopicProfile, fold_text


_DENIED_METADATA_PARTS = {
    "query",
    "queries",
    "prompt",
    "profile",
    "strategy",
    "user_request",
    "interest",
    "scope",
}
_SAFE_METADATA_PARTS = {
    "article",
    "body",
    "caption",
    "comment",
    "comments",
    "content",
    "description",
    "excerpt",
    "headline",
    "link_text",
    "post",
    "selftext",
    "snippet",
    "subject",
    "summary",
    "text",
    "title",
    "transcript",
}


@dataclass(frozen=True)
class MustHaveMatch:
    anchor: str
    alias: str
    field: str


@dataclass(frozen=True)
class MustHaveEvaluation:
    enabled: bool
    matches: tuple[MustHaveMatch, ...] = ()
    missed_terms: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.enabled or not self.missed_terms


def must_have_enabled(profile: TopicProfile) -> bool:
    return bool(must_have_alias_sets(profile))


def must_have_alias_sets(profile: TopicProfile) -> list[tuple[str, set[str]]]:
    alias_sets: list[tuple[str, set[str]]] = []
    aliases_by_key = {
        normalize_must_have_text(key): {
            normalize_must_have_text(alias)
            for alias in aliases
            if normalize_must_have_text(alias)
        }
        for key, aliases in (profile.must_have_aliases or {}).items()
    }
    for term in profile.must_have_terms:
        anchor = str(term or "").strip()
        folded_anchor = normalize_must_have_text(anchor)
        if not folded_anchor:
            continue
        term_aliases = {folded_anchor, *aliases_by_key.get(folded_anchor, set())}
        term_aliases = {alias for alias in term_aliases if alias}

        merged_into_existing = False
        for _idx, (_existing_anchor, existing_set) in enumerate(alias_sets):
            if folded_anchor in existing_set:
                existing_set.update(term_aliases)
                merged_into_existing = True
                break
        if not merged_into_existing:
            alias_sets.append((anchor, term_aliases))
    return alias_sets


def evaluate_must_have_fields(
    profile: TopicProfile,
    fields: Iterable[tuple[str, Any]],
) -> MustHaveEvaluation:
    alias_sets = must_have_alias_sets(profile)
    if not alias_sets:
        return MustHaveEvaluation(enabled=False)

    normalized_fields = [
        (name, normalize_must_have_text(value))
        for name, value in fields
        if normalize_must_have_text(value)
    ]
    matches: list[MustHaveMatch] = []
    missed: list[str] = []
    for anchor, aliases in alias_sets:
        match = _first_alias_match(anchor, aliases, normalized_fields)
        if match is None:
            missed.append(anchor)
        else:
            matches.append(match)
    return MustHaveEvaluation(
        enabled=True,
        matches=tuple(matches),
        missed_terms=tuple(missed),
    )


def candidate_must_have_evaluation(profile: TopicProfile, candidate: Any) -> MustHaveEvaluation:
    return evaluate_must_have_fields(profile, candidate_must_have_fields(candidate))


def article_result_must_have_evaluation(profile: TopicProfile, result: Any) -> MustHaveEvaluation:
    return evaluate_must_have_fields(profile, article_result_must_have_fields(result))


def must_have_reason(evaluation: MustHaveEvaluation) -> str:
    if not evaluation.enabled or evaluation.passed:
        return ""
    return f"Missing required term(s): {', '.join(evaluation.missed_terms)}."


def must_have_evidence(evaluation: MustHaveEvaluation) -> list[dict[str, str]]:
    return [
        {
            "anchor": match.anchor,
            "alias": match.alias,
            "field": match.field,
        }
        for match in evaluation.matches
    ]


def normalize_must_have_text(value: object) -> str:
    text = fold_text(value)
    text = re.sub(r"[_\W]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def candidate_must_have_fields(candidate: Any) -> list[tuple[str, Any]]:
    payload = getattr(candidate, "payload", None)
    if payload is None:
        return []
    fields: list[tuple[str, Any]] = [
        ("payload.raw_text", getattr(payload, "raw_text", "")),
    ]
    fields.extend(_safe_metadata_fields(getattr(payload, "metadata", {}) or {}, prefix="payload.metadata"))
    return fields


def article_result_must_have_fields(result: Any) -> list[tuple[str, Any]]:
    payload = getattr(result, "payload", None)
    fields: list[tuple[str, Any]] = [
        ("title", getattr(result, "title", "")),
        ("text", getattr(result, "text", "")),
    ]
    if not bool(getattr(result, "fetched", False)) or not str(getattr(result, "text", "") or "").strip():
        fields.append(("excerpt", getattr(result, "excerpt", "")))
    if payload is not None:
        fields.append(("payload.raw_text", getattr(payload, "raw_text", "")))
        fields.extend(_safe_metadata_fields(getattr(payload, "metadata", {}) or {}, prefix="payload.metadata"))
    fields.extend(_safe_metadata_fields(getattr(result, "metadata", {}) or {}, prefix="metadata"))
    return fields


def _first_alias_match(
    anchor: str,
    aliases: set[str],
    fields: list[tuple[str, str]],
) -> MustHaveMatch | None:
    for field_name, field_text in fields:
        for alias in sorted(aliases, key=len, reverse=True):
            if _contains_alias(field_text, alias):
                return MustHaveMatch(anchor=anchor, alias=alias, field=field_name)
    return None


def _contains_alias(field_text: str, alias: str) -> bool:
    if not field_text or not alias:
        return False
    if any(ord(char) > 127 for char in alias):
        return alias in field_text
    pattern = re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", flags=re.UNICODE)
    return bool(pattern.search(field_text))


def _safe_metadata_fields(metadata: Any, *, prefix: str) -> list[tuple[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    fields: list[tuple[str, Any]] = []
    for raw_key, value in metadata.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        key_parts = _metadata_key_parts(key)
        if _metadata_key_denied(key_parts):
            continue
        child_prefix = f"{prefix}.{key}"
        if _metadata_key_safe(key_parts):
            fields.extend((child_prefix, item) for item in _flatten_metadata_value(value))
        if isinstance(value, dict):
            fields.extend(_safe_metadata_fields(value, prefix=child_prefix))
        elif isinstance(value, (list, tuple, set)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    fields.extend(_safe_metadata_fields(item, prefix=f"{child_prefix}.{index}"))
    return fields


def _metadata_key_parts(key: str) -> set[str]:
    return {part for part in re.split(r"[^a-z0-9]+", fold_text(key)) if part}


def _metadata_key_denied(parts: set[str]) -> bool:
    return bool(parts & _DENIED_METADATA_PARTS)


def _metadata_key_safe(parts: set[str]) -> bool:
    return bool(parts & _SAFE_METADATA_PARTS)


def _flatten_metadata_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bool):
        return [str(value)]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_flatten_metadata_value(item))
        return values
    if isinstance(value, dict):
        values: list[str] = []
        for nested in value.values():
            values.extend(_flatten_metadata_value(nested))
        return values
    return [str(value)]
