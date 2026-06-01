import asyncio
import os
import sys
import uuid
from collections import Counter

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery import DiscoveryRunner, SourceAdapterContext, TopicProfile, default_source_registry
from backend.agents.discovery.runner import _apply_exclusions, _apply_topic_relevance, _lane_limit, _dedupe_candidates

async def trace_runner_run():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    exploration_id = f"test-{uuid.uuid4().hex[:8]}"
    
    context = SourceAdapterContext(
        exploration_id=exploration_id,
        candidate_limit=250,
        lookback_hours=168,
    )
    
    registry = default_source_registry()
    adapters = registry.selected(profile.source_selection)
    
    results = await asyncio.gather(
        *[
            DiscoveryRunner(registry)._run_adapter(adapter, profile, context)
            for adapter in adapters
        ],
    )
    
    all_raw_candidates = [candidate for adapter_candidates, _status in results for candidate in adapter_candidates]
    candidates, exclusions = _apply_exclusions(profile, all_raw_candidates)
    candidates, relevance_exclusions = _apply_topic_relevance(profile, candidates)
    
    # 1. Gmail candidates surviving topic relevance
    gmail_rel = [c for c in candidates if c.adapter == "gmail"]
    rel_senders = Counter([c.payload.metadata.get("sender_email") or c.payload.source_name for c in gmail_rel])
    print("Gmail candidates surviving topic relevance:", rel_senders)
    
    # 2. Gmail candidates surviving lane limit (deduplication)
    deduped = _dedupe_candidates(
        sorted(gmail_rel, key=lambda c: c.score, reverse=True),
        limit=25,
    )
    deduped_senders = Counter([c.payload.metadata.get("sender_email") or c.payload.source_name for c in deduped])
    print("Gmail candidates surviving lane limit (25):", deduped_senders)

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(trace_runner_run())
