import asyncio
import os
import sys
import uuid

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery import DiscoveryRunner, SourceAdapterContext, TopicProfile, default_source_registry

async def trace_runner_random_id():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    exploration_id = f"test-{uuid.uuid4().hex[:8]}"
    print(f"Generating new exploration ID: {exploration_id}")
    
    context = SourceAdapterContext(
        exploration_id=exploration_id,
        candidate_limit=250,
        lookback_hours=168,
    )
    
    registry = default_source_registry()
    print("Running DiscoveryRunner.run...")
    result = await DiscoveryRunner(registry).run(
        profile,
        source_selection=profile.source_selection,
        context=context
    )
    
    print("\nStatuses from Discovery runner:")
    for status in result.statuses:
        print(f" - {status.name}: status={status.status}, candidate_count={status.candidate_count}, message={status.message}")

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(trace_runner_random_id())
