import asyncio
import os
import sys
import logging

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.db import database
from backend.agents.digestor.gmail import fetch_newsletters

async def debug_gmail_fetch():
    db_path = str(database.database_path())
    senders = database.approved_gmail_senders()
    print("Database path:", db_path)
    print("Approved senders:", senders)
    
    # We will fetch with a dummy digest_id that changes every run to avoid watermarks
    import uuid
    digest_id = f"debug-fetch-{uuid.uuid4().hex[:8]}"
    print(f"Running fetch_newsletters with digest_id: {digest_id}")
    
    payloads = await fetch_newsletters(
        digest_id=digest_id,
        sender_allowlist=senders,
        lookback_hours=168,
        db_path=db_path
    )
    print(f"Fetched {len(payloads)} payloads.")

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    os.environ["MORNING_DISPATCH_SECRETS_DIR"] = "/Users/macstudio/.morning-dispatch/secrets"
    asyncio.run(debug_gmail_fetch())
