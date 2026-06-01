import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery import DiscoveryRunner, SourceAdapterContext, TopicProfile, default_source_registry
from backend.agents.discovery.runner import _apply_exclusions, _apply_topic_relevance, _lane_limit, _dedupe_candidates

async def trace_runner_gmail():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    # We want to run with EXACTLY the same runner.run logic
    context = SourceAdapterContext(
        exploration_id="trace-exploration",
        candidate_limit=250,
        lookback_hours=168,
    )
    
    registry = default_source_registry()
    print("Running DiscoveryRunner.run(profile, ...)")
    result = await DiscoveryRunner(registry).run(
        profile,
        source_selection=profile.source_selection,
        context=context
    )
    
    gmail_cands = [c for c in result.candidates if c.adapter == "gmail"]
    print(f"DiscoveryRunner returned {len(result.candidates)} candidates total.")
    print(f"Found {len(gmail_cands)} Gmail candidates in the result.")
    
    # Let's inspect the first few candidates to see their adapters
    from collections import Counter
    adapters_count = Counter([c.adapter for c in result.candidates])
    print("Adapters count in result.candidates:", adapters_count)

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(trace_runner_gmail())
