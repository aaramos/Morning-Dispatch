import asyncio
import os
import sys

# Ensure backend is in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.core.config import get_settings
from backend.app.db import database
from backend.agents.digestor import gmail

async def test_gmail():
    print("Database path:", database.database_path())
    print("Gmail credentials path:", get_settings().gmail_credentials_path)
    
    # Check approved senders
    senders = database.approved_gmail_senders()
    print("Approved Gmail senders in DB:", senders)
    
    # Check if credentials file exists
    creds_path = gmail._credentials_path()
    print("Resolved credentials path:", creds_path, "Exists:", creds_path.exists())
    
    if not creds_path.exists():
        print("Credentials file does not exist. Authentication will fail.")
        return
        
    try:
        service = gmail.get_gmail_service()
        print("Successfully authenticated and got Gmail service client!")
    except Exception as exc:
        print("Gmail authentication FAILED:", exc)
        import traceback
        traceback.print_exc()
        return

    # Try listing messages for each approved sender in last 7 days (168 hours)
    lookback_hours = 168
    import time
    after_timestamp = int(time.time() - (lookback_hours * 3600))
    print(f"Testing lookback of {lookback_hours} hours. Unix timestamp: {after_timestamp}")
    
    for sender in senders:
        query = gmail.build_query(sender, after_timestamp)
        print(f"\nQuerying sender: {sender} with query: '{query}'")
        try:
            messages = gmail._list_messages(service, query)
            print(f"Found {len(messages)} messages.")
            for msg_ref in messages[:3]:
                msg_id = msg_ref.get("id")
                msg = gmail._get_message(service, msg_id)
                payload = msg.get("payload", {})
                subject = gmail.header_value(payload, "Subject")
                published_at = gmail.message_published_at(msg)
                print(f" - Msg ID: {msg_id}, Published: {published_at}, Subject: {subject}")
        except Exception as exc:
            print(f"Failed to query {sender}: {exc}")

if __name__ == "__main__":
    os.environ["MORNING_DISPATCH_HOME"] = "runtime"
    asyncio.run(test_gmail())
