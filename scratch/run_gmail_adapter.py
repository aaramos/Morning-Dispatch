import asyncio
import os
import sys

# Ensure backend is in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery.adapters import GmailSourceAdapter
from backend.agents.discovery.types import TopicProfile, SourceAdapterContext

async def run_adapter():
    # Load profile from DB
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    if not record:
        print(f"Profile {topic_id} not found in DB!")
        return
        
    profile = TopicProfile.from_dict(record["profile"])
    print("Loaded TopicProfile:", profile)
    print("Requested sources:", profile.requested_sources)
    print("Source selection:", profile.source_selection)
    print("Lookback hours:", profile.lookback_hours)

    context = SourceAdapterContext(
        exploration_id="test-exploration-id",
        candidate_limit=250,
        lookback_hours=168,
    )
    
    adapter = GmailSourceAdapter()
    print("\nRunning GmailSourceAdapter.query()...")
    candidates = await adapter.query(profile, context)
    print(f"GmailSourceAdapter returned {len(candidates)} candidates.")
    
    for idx, c in enumerate(candidates):
        payload = c.payload
        print(f"\nCandidate {idx}:")
        print(f"  Source type: {payload.source_type}")
        print(f"  Source name: {payload.source_name}")
        print(f"  Original URL: {payload.original_url}")
        print(f"  Published at: {payload.published_at}")
        print(f"  Metadata: {payload.metadata}")
        print(f"  Excerpt: {payload.raw_text[:120]}...")

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(run_adapter())
