import asyncio
from telethon import TelegramClient

# ==========================================
# CREDENTIALS
# ==========================================
API_ID = 39425022               # Your numeric api_id
API_HASH = "8cf5f0beae08a2e5e614b85c23bf3adc"     # Your string api_hash

# Source and Target channel links or IDs:
SOURCE_CHANNEL = -1002184913353
TARGET_CHANNEL = -1004450477002

# ==========================================
# RANGE & TOPIC CONTROLS
# ==========================================
# Set specific message ID ranges
START_FROM_MESSAGE_ID = 14608
END_AT_MESSAGE_ID = 14627

# Source topic to filter (forum/topic)
SOURCE_TOPIC_ID = 7793
# Target topic to send to (forum/topic). Use None if no topic
TARGET_TOPIC_ID = None

# Cooling rate / Time gap in seconds between each forward
COOLING_RATE_SECONDS = 8
# ==========================================


async def main():
    client = TelegramClient("my_user_session", API_ID, API_HASH)

    print("Connecting to Telegram...")
    await client.start()
    print("\n[+] Logged in successfully!")

    try:
        source = await client.get_entity(SOURCE_CHANNEL)
        target = await client.get_entity(TARGET_CHANNEL)
    except Exception as e:
        print(f"[-] Error finding channels. Details: {e}")
        return

    print("[+] Starting video forward (server-side, no download)...")

    count = 0
    async for message in client.iter_messages(
        source,
        min_id=START_FROM_MESSAGE_ID - 1 if START_FROM_MESSAGE_ID else 0,
        max_id=END_AT_MESSAGE_ID,
        reverse=False,
    ):
        count += 1
        if count % 10 == 0:
            print(f"    Scanned {count} messages...")

        if not message.video and not (message.media and hasattr(message.media, 'document')):
            continue

        # Filter by source topic if specified
        if SOURCE_TOPIC_ID and getattr(message, 'reply_to', None):
            r = message.reply_to
            topic_match = False
            if getattr(r, 'reply_to_top_id', None) == SOURCE_TOPIC_ID:
                topic_match = True
            if getattr(r, 'reply_to_msg_id', None) == SOURCE_TOPIC_ID:
                topic_match = True
            if not topic_match:
                continue
        elif SOURCE_TOPIC_ID and not getattr(message, 'reply_to', None):
            continue

        print(f"\n[~] Forwarding message ID: {message.id}...")

        try:
            if TARGET_TOPIC_ID:
                await client.forward_messages(
                    target,
                    message,
                    from_peer=source,
                    reply_to=TARGET_TOPIC_ID,
                )
            else:
                await client.forward_messages(
                    target,
                    message,
                    from_peer=source,
                )
            print(f"    Successfully forwarded message ID: {message.id}!")
        except Exception as e:
            print(f"    [-] Failed to forward message {message.id}: {e}")

        await asyncio.sleep(COOLING_RATE_SECONDS)

    print("\n[+] Forward process finished!")


if __name__ == "__main__":
    asyncio.run(main())

