import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.discovery import DiscoveryRunner, SourceAdapterContext, TopicProfile, default_source_registry

async def debug_discovery():
    topic_id = "674ce39e-6d93-4211-882f-a4cd5e91578a"
    record = database.get_topic_profile(topic_id)
    profile = TopicProfile.from_dict(record["profile"])
    
    context = SourceAdapterContext(
        exploration_id="debug-exploration",
        candidate_limit=250,
        lookback_hours=168,
    )
    
    registry = default_source_registry()
    adapters = registry.selected(profile.source_selection)
    
    print("Running Gmail adapter query directly:")
    gmail_adapter = [a for a in adapters if a.name == "gmail"][0]
    candidates = await gmail_adapter.query(profile, context)
    print(f"Direct query returned {len(candidates)} candidates.")
    
    # Let's count candidates by sender_email in metadata
    senders = {}
    for c in candidates:
        sender = c.payload.metadata.get("sender_email") or c.payload.source_name
        senders[sender] = senders.get(sender, 0) + 1
    print("Candidates by sender:", senders)

    print("\nRunning the full DiscoveryRunner...")
    result = await DiscoveryRunner(registry).run(
        profile,
        source_selection=profile.source_selection,
        context=context
    )
    
    print(f"Discovery result has {len(result.candidates)} candidates.")
    gmail_result_candidates = [c for c in result.candidates if c.adapter == "gmail"]
    print(f"Of which {len(gmail_result_candidates)} are Gmail candidates.")
    
    result_senders = {}
    for c in gmail_result_candidates:
        sender = c.payload.metadata.get("sender_email") or c.payload.source_name
        result_senders[sender] = result_senders.get(sender, 0) + 1
    print("Final selected Gmail candidates by sender:", result_senders)
    
    print("\nRequested sources status:")
    for requested in profile.requested_sources:
        adapter = requested.get("adapter")
        ref = requested.get("ref")
        # Check if found
        from backend.app.services.explore import _requested_source_found
        found = _requested_source_found(adapter=adapter, source_name=ref, discovery=result)
        print(f" - {ref} (adapter: {adapter}): Found={found}")

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(debug_discovery())
