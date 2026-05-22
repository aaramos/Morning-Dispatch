from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


SOURCE_STATES = {"active", "search_only", "candidate", "retired"}

INTEREST_TERMS = {
    "agent": 1.0,
    "agentic": 1.0,
    "agents": 1.0,
    "ai": 0.8,
    "coding": 0.9,
    "code": 0.8,
    "cursor": 0.9,
    "claude": 0.9,
    "codex": 0.9,
    "gemini": 0.8,
    "chatgpt": 0.8,
    "local": 0.9,
    "llm": 1.0,
    "llms": 1.0,
    "mlx": 1.0,
    "omlx": 1.0,
    "ollama": 0.9,
    "lmstudio": 0.9,
    "mcp": 1.0,
    "model": 0.7,
    "models": 0.7,
    "openai": 0.8,
    "product": 0.8,
    "privacy": 0.7,
    "selfhosted": 0.7,
    "vibe": 0.9,
    "workflow": 0.9,
    "workflows": 0.9,
}

NOISE_TERMS = {
    "meme",
    "shitpost",
    "joke",
    "karma",
    "giveaway",
    "removed",
    "rant",
    "drama",
    "wallpaper",
}

SUBREDDIT_ALIASES = {
    "artificialinteligence": "ArtificialIntelligence",
}

DISCOVERY_BLOCKLIST = {
    "programmerhumor",
}

DEFAULT_REDDIT_COMMUNITIES: tuple[dict[str, Any], ...] = (
    {
        "subreddit": "LocalLLaMA",
        "state": "active",
        "category": "Privacy & Infrastructure",
        "tags": ["local", "llm", "ollama", "mlx", "lmstudio", "models"],
        "reason": "Core signal for local models, inference tooling, and hardware constraints.",
    },
    {
        "subreddit": "Cursor",
        "state": "active",
        "category": "Emerging Workflows",
        "tags": ["cursor", "coding", "agent", "workflow", "vibe"],
        "reason": "Core signal for AI-native IDE workflows and pain points.",
    },
    {
        "subreddit": "ChatGPTCoding",
        "state": "active",
        "category": "Emerging Workflows",
        "tags": ["chatgpt", "coding", "agents", "workflow", "mcp"],
        "reason": "Core signal for AI-assisted development and agentic coding workflows.",
    },
    {
        "subreddit": "ClaudeAI",
        "state": "active",
        "category": "Competitive Intelligence",
        "tags": ["claude", "coding", "agents", "model", "workflow"],
        "reason": "Core signal for Claude product shifts, Claude Code, and user complaints.",
    },
    {
        "subreddit": "ChatGPT",
        "state": "active",
        "category": "Competitive Intelligence",
        "tags": ["chatgpt", "openai", "model", "product", "workflow"],
        "reason": "Core signal for broad OpenAI product sentiment and user-facing changes.",
    },
    {
        "subreddit": "MachineLearning",
        "state": "active",
        "category": "Foundational R&D",
        "tags": ["machine", "learning", "models", "research", "llm"],
        "reason": "Core signal for research and model-release discussion.",
    },
    {
        "subreddit": "AI_Agents",
        "state": "active",
        "category": "Emerging Workflows",
        "tags": ["ai", "agents", "agentic", "workflow", "automation"],
        "reason": "Core signal for agent frameworks and multi-agent workflows.",
    },
    {
        "subreddit": "ollama",
        "state": "active",
        "category": "Privacy & Infrastructure",
        "tags": ["ollama", "local", "llm", "models", "selfhosted"],
        "reason": "Core signal for local model serving and practical setup problems.",
    },
    {
        "subreddit": "LMStudio",
        "state": "active",
        "category": "Privacy & Infrastructure",
        "tags": ["lmstudio", "local", "llm", "models", "inference"],
        "reason": "Core signal for local model runtime usability and constraints.",
    },
    {
        "subreddit": "Replit",
        "state": "search_only",
        "category": "Emerging Workflows",
        "tags": ["replit", "agent", "coding", "workflow", "builder"],
        "reason": "Useful for AI app-builder pain points, but too noisy to browse blindly.",
    },
    {
        "subreddit": "AIProgramming",
        "state": "search_only",
        "category": "Emerging Workflows",
        "tags": ["ai", "programming", "coding", "agents", "workflow"],
        "reason": "Useful for AI programming workflows; quality should be proven before daily watching.",
    },
    {
        "subreddit": "selfhosted",
        "state": "search_only",
        "category": "Privacy & Infrastructure",
        "tags": ["selfhosted", "privacy", "local", "infrastructure"],
        "reason": "Useful for privacy-first deployment and local infrastructure signals.",
    },
    {
        "subreddit": "singularity",
        "state": "search_only",
        "category": "Foundational R&D",
        "tags": ["ai", "agi", "model", "research"],
        "reason": "Broad trend signal, but hype-heavy; search only unless product signal improves.",
    },
    {
        "subreddit": "ArtificialIntelligence",
        "state": "search_only",
        "category": "Foundational R&D",
        "tags": ["artificial", "intelligence", "ai", "models", "product"],
        "reason": "Broad AI trend signal; search only to control noise.",
    },
    {
        "subreddit": "GeminiAI",
        "state": "search_only",
        "category": "Competitive Intelligence",
        "tags": ["gemini", "google", "model", "product"],
        "reason": "Gemini product sentiment source; monitor with noise control.",
    },
    {
        "subreddit": "GoogleGeminiAI",
        "state": "search_only",
        "category": "Competitive Intelligence",
        "tags": ["gemini", "google", "model", "product"],
        "reason": "Alternate Gemini community; compare against GeminiAI before promoting.",
    },
    {
        "subreddit": "StableDiffusion",
        "state": "search_only",
        "category": "Multi-modal & Creative",
        "tags": ["image", "video", "multimodal", "generation", "workflow"],
        "reason": "Useful for creative AI workflow and UX signals when multimodal topics matter.",
    },
    {
        "subreddit": "Midjourney",
        "state": "search_only",
        "category": "Multi-modal & Creative",
        "tags": ["image", "multimodal", "generation", "workflow"],
        "reason": "Useful for visual-generation workflow and user pain points.",
    },
    {
        "subreddit": "BetterOffline",
        "state": "candidate",
        "category": "Privacy & Infrastructure",
        "tags": ["offline", "privacy", "local", "ai"],
        "reason": "Candidate source for offline/privacy-first AI; needs evidence before promotion.",
    },
    {
        "subreddit": "raspberry_pi",
        "state": "candidate",
        "category": "Privacy & Infrastructure",
        "tags": ["edge", "hardware", "local", "offline"],
        "reason": "Candidate edge-computing source; likely too broad unless AI-specific posts appear.",
    },
)

DISCOVERY_QUERIES = (
    "agentic AI",
    "\"AI agents\"",
    "\"Claude Code\"",
    "\"vibe coding\"",
    "\"local LLM\"",
    "oMLX OR MLX",
    "Ollama model release",
    "MCP server",
    "Cursor agent",
)


@dataclass(frozen=True)
class SourceObservation:
    subreddit: str
    sampled_posts: int = 0
    relevant_posts: int = 0
    fresh_posts: int = 0
    noisy_posts: int = 0
    avg_comments: float = 0.0
    avg_score: float = 0.0
    last_seen_post_at: str | None = None
    sample_titles: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SourceUpdate:
    subreddit: str
    state: str
    category: str | None
    score: float
    reason: str
    last_seen_post_at: str | None = None
    consecutive_stale_runs: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoutDecision:
    subreddit: str
    decision: str
    action: str
    confidence: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoutReview:
    updates: list[SourceUpdate]
    decisions: list[ScoutDecision]
    summary: str
    sampled_count: int
    active_count: int
    candidate_count: int
    retired_count: int
    partial: bool = False


def seed_communities() -> list[dict[str, Any]]:
    return [dict(source) for source in DEFAULT_REDDIT_COMMUNITIES]


def discovery_queries() -> tuple[str, ...]:
    return DISCOVERY_QUERIES


def legacy_alias_names() -> tuple[str, ...]:
    return tuple(SUBREDDIT_ALIASES.keys())


def discovery_blocklist() -> tuple[str, ...]:
    return tuple(DISCOVERY_BLOCKLIST)


def review_reddit_sources(
    *,
    digest_interest: str,
    current_sources: list[dict[str, Any]],
    observations: dict[str, SourceObservation] | None = None,
    discovered_subreddits: dict[str, int] | None = None,
) -> ScoutReview:
    observations = observations or {}
    discovered_subreddits = discovered_subreddits or {}
    existing_by_name = _dedupe_sources_by_name(current_sources)
    sources = [dict(source) for source in existing_by_name.values()]
    sources.extend(_candidate_sources(existing_by_name, discovered_subreddits))

    updates: list[SourceUpdate] = []
    decisions: list[ScoutDecision] = []
    interest_tokens = _keywords(digest_interest)
    sampled_count = 0
    partial = False

    for source in sources:
        subreddit = _normalize_subreddit(str(source.get("subreddit") or ""))
        if not subreddit:
            continue
        observation = observations.get(subreddit.lower())
        if observation and observation.error:
            partial = True
        sampled_count += observation.sampled_posts if observation else 0
        score = _score_source(source, interest_tokens, observation)
        state, decision, action, reason, stale_runs = _state_for_source(source, score, observation)
        update = SourceUpdate(
            subreddit=subreddit,
            state=state,
            category=source.get("category"),
            score=score,
            reason=reason,
            last_seen_post_at=observation.last_seen_post_at if observation else source.get("last_seen_post_at"),
            consecutive_stale_runs=stale_runs,
            metadata={
                **_json_metadata(source.get("metadata")),
                "sampled_posts": observation.sampled_posts if observation else 0,
                "relevant_posts": observation.relevant_posts if observation else 0,
                "fresh_posts": observation.fresh_posts if observation else 0,
                "noisy_posts": observation.noisy_posts if observation else 0,
                "sample_titles": list(observation.sample_titles[:3]) if observation else [],
            },
        )
        updates.append(update)
        decisions.append(
            ScoutDecision(
                subreddit=subreddit,
                decision=decision,
                action=action,
                confidence=round(score, 3),
                reason=reason,
                metadata=update.metadata,
            )
        )

    updates.sort(key=lambda row: (state_rank(row.state), row.score, row.subreddit.lower()), reverse=True)
    decisions.sort(key=lambda row: (state_rank(_action_state(row.action)), row.confidence), reverse=True)
    active_count = sum(1 for update in updates if update.state == "active")
    candidate_count = sum(1 for update in updates if update.state == "candidate")
    retired_count = sum(1 for update in updates if update.state == "retired")
    summary = (
        f"Source Scout reviewed {len(updates)} Reddit communit"
        f"{'y' if len(updates) == 1 else 'ies'}: {active_count} active, "
        f"{candidate_count} candidate, {retired_count} retired."
    )
    if partial:
        summary += " Some live Reddit sampling failed, so affected sources were kept conservative."
    return ScoutReview(
        updates=updates,
        decisions=decisions,
        summary=summary,
        sampled_count=sampled_count,
        active_count=active_count,
        candidate_count=candidate_count,
        retired_count=retired_count,
        partial=partial,
    )


def observation_from_posts(subreddit: str, posts: list[dict[str, Any]], *, digest_interest: str) -> SourceObservation:
    interest_tokens = _keywords(digest_interest)
    now_ts = datetime.now(UTC).timestamp()
    sampled = 0
    relevant = 0
    fresh = 0
    noisy = 0
    total_comments = 0.0
    total_score = 0.0
    latest_ts = 0.0
    titles: list[str] = []
    for post in posts:
        title = str(post.get("title") or "")
        content = str(post.get("content") or "")
        if not title.strip():
            continue
        sampled += 1
        titles.append(title)
        text_tokens = _keywords(f"{title} {content}")
        if _post_relevance(text_tokens, interest_tokens) >= 0.18:
            relevant += 1
        if text_tokens & NOISE_TERMS:
            noisy += 1
        created_utc = _float(post.get("created_utc"))
        if created_utc:
            latest_ts = max(latest_ts, created_utc)
            if now_ts - created_utc <= 7 * 24 * 60 * 60:
                fresh += 1
        total_comments += _float(post.get("num_comments"))
        total_score += _float(post.get("score"))

    return SourceObservation(
        subreddit=_normalize_subreddit(subreddit),
        sampled_posts=sampled,
        relevant_posts=relevant,
        fresh_posts=fresh,
        noisy_posts=noisy,
        avg_comments=round(total_comments / sampled, 2) if sampled else 0.0,
        avg_score=round(total_score / sampled, 2) if sampled else 0.0,
        last_seen_post_at=datetime.fromtimestamp(latest_ts, UTC).isoformat(timespec="seconds") if latest_ts else None,
        sample_titles=tuple(titles[:5]),
    )


def state_rank(state: str) -> int:
    return {"active": 4, "search_only": 3, "candidate": 2, "retired": 1}.get(state, 0)


def _candidate_sources(existing_by_name: dict[str, dict[str, Any]], discovered: dict[str, int]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for subreddit, count in sorted(discovered.items(), key=lambda item: item[1], reverse=True):
        normalized = _normalize_subreddit(subreddit)
        key = normalized.lower()
        if not normalized or key in DISCOVERY_BLOCKLIST or key in existing_by_name or count < 3:
            continue
        candidates.append(
            {
                "subreddit": normalized,
                "state": "candidate",
                "category": "Discovered",
                "tags": [normalized.lower()],
                "reason": f"Discovered in {count} Reddit search result(s) matching the digest interest.",
                "metadata": {"discovered_count": count},
            }
        )
        if len(candidates) >= 8:
            break
    return candidates


def _dedupe_sources_by_name(current_sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for source in current_sources:
        subreddit = _normalize_subreddit(str(source.get("subreddit") or ""))
        if not subreddit:
            continue
        key = subreddit.lower()
        normalized_source = {**source, "subreddit": subreddit}
        existing = sources.get(key)
        if existing is None or _source_priority(normalized_source) > _source_priority(existing):
            sources[key] = normalized_source
    return sources


def _source_priority(source: dict[str, Any]) -> tuple[int, float, int]:
    category = str(source.get("category") or "")
    is_seeded = int(category and category != "Discovered")
    return (
        is_seeded,
        float(source.get("score") or 0),
        state_rank(str(source.get("state") or "candidate")),
    )


def _score_source(
    source: dict[str, Any],
    interest_tokens: set[str],
    observation: SourceObservation | None,
) -> float:
    tags = _keywords(" ".join(str(tag) for tag in source.get("tags", [])))
    name_tokens = _keywords(str(source.get("subreddit") or ""))
    category_tokens = _keywords(str(source.get("category") or ""))
    source_tokens = tags | name_tokens | category_tokens
    seed_fit = _post_relevance(source_tokens, interest_tokens) if interest_tokens else 0.5
    seed_floor = {"active": 0.7, "search_only": 0.52, "candidate": 0.35, "retired": 0.15}.get(
        str(source.get("state") or "candidate"),
        0.35,
    )
    seed_score = max(seed_fit, seed_floor)
    if observation is None or observation.sampled_posts == 0:
        return round(min(0.86, seed_score), 3)

    relevance_rate = observation.relevant_posts / max(1, observation.sampled_posts)
    freshness_rate = observation.fresh_posts / max(1, observation.sampled_posts)
    noise_rate = observation.noisy_posts / max(1, observation.sampled_posts)
    engagement = min(1.0, ((observation.avg_comments / 30) + (observation.avg_score / 120)) / 2)
    score = (seed_score * 0.35) + (relevance_rate * 0.38) + (freshness_rate * 0.17) + (engagement * 0.1)
    score -= min(0.25, noise_rate * 0.25)
    return round(max(0.0, min(1.0, score)), 3)


def _state_for_source(
    source: dict[str, Any],
    score: float,
    observation: SourceObservation | None,
) -> tuple[str, str, str, str, int]:
    previous_state = str(source.get("state") or "candidate")
    previous_stale = int(source.get("consecutive_stale_runs") or 0)
    stale_runs = previous_stale
    if observation is not None:
        stale_runs = previous_stale + 1 if observation.sampled_posts == 0 or observation.relevant_posts == 0 else 0
    if observation and observation.error:
        return (
            previous_state,
            "kept",
            "no_change",
            f"Kept r/{source.get('subreddit')} as {previous_state}; live Reddit sampling failed: {observation.error}",
            stale_runs,
        )

    if previous_state == "active" and score >= 0.52 and stale_runs < 2:
        new_state = "active"
    elif score >= 0.66:
        new_state = "active"
    elif score >= 0.48:
        new_state = "search_only"
    elif score >= 0.28 and stale_runs < 3:
        new_state = "candidate"
    else:
        new_state = "retired"

    if previous_state == new_state:
        decision = "kept"
        action = "no_change"
    elif state_rank(new_state) > state_rank(previous_state):
        decision = "promoted"
        action = f"promote_to_{new_state}"
    else:
        decision = "demoted"
        action = f"move_to_{new_state}"

    evidence = _evidence_sentence(observation)
    reason = f"{decision.title()} r/{source.get('subreddit')} as {new_state}; score {score:.2f}. {evidence}"
    return new_state, decision, action, reason.strip(), stale_runs


def _evidence_sentence(observation: SourceObservation | None) -> str:
    if observation is None:
        return "No live sample yet, so the seed profile is used."
    if observation.sampled_posts == 0:
        return "No recent posts were sampled."
    return (
        f"Sampled {observation.sampled_posts} post(s), "
        f"{observation.relevant_posts} matched interest terms, "
        f"{observation.fresh_posts} were fresh."
    )


def _post_relevance(tokens: set[str], interest_tokens: set[str]) -> float:
    if not tokens:
        return 0.0
    weighted_hits = sum(INTEREST_TERMS.get(token, 0.5) for token in tokens & interest_tokens)
    weighted_interest = sum(INTEREST_TERMS.get(token, 0.5) for token in interest_tokens) or 1.0
    weighted_source = sum(INTEREST_TERMS.get(token, 0.25) for token in tokens) or 1.0
    return max(weighted_hits / weighted_interest, weighted_hits / weighted_source)


def _keywords(text: str) -> set[str]:
    normalized = text.lower().replace("_", " ")
    return {token for token in re.findall(r"[a-z0-9]+", normalized) if len(token) > 1}


def _normalize_subreddit(value: str) -> str:
    cleaned = value.strip().removeprefix("r/").removeprefix("/r/")
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", cleaned)
    alias = SUBREDDIT_ALIASES.get(cleaned.lower())
    return (alias or cleaned)[:80]


def _json_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _action_state(action: str) -> str:
    if action.startswith("promote_to_"):
        return action.replace("promote_to_", "", 1)
    if action.startswith("move_to_"):
        return action.replace("move_to_", "", 1)
    return "candidate"
