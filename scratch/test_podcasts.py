import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery.adapters import PodcastSourceAdapter
from backend.agents.discovery.types import TopicProfile, SourceAdapterContext

async def test_podcasts():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    context = SourceAdapterContext(
        exploration_id="debug-podcasts",
        candidate_limit=250,
        lookback_hours=168,
    )
    
    adapter = PodcastSourceAdapter()
    print("Running PodcastSourceAdapter.query()...")
    try:
        candidates = await adapter.query(profile, context)
        print(f"Returned {len(candidates)} candidates.")
        for idx, c in enumerate(candidates[:5]):
            print(f"Candidate {idx}: {c.payload.source_name} - {c.payload.metadata.get('title') or c.payload.original_url}")
    except Exception as exc:
        print("Podcast adapter failed with exception:", exc)

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(test_podcasts())
