import asyncio
import edge_tts
import os

CACHE_DIR = os.path.join(os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")),
                         "audio_cache")
VOICE = "en-GB-RyanNeural"

# Standard phrases to cache for "Zero-Lag" response
PHRASES = {
    "greeting_1": "Yes, Sir?",
    "greeting_2": "At your service.",
    "greeting_3": "Standing by.",
    "ack_1": "Right away, Sir.",
    "ack_2": "Consider it done.",
    "ack_3": "Very well, Sir.",
    "error_1": "I'm sorry Sir, I missed that.",
    "system_online": "At your service, Sir." # Streamlined Boot
}

async def generate_cache():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
        
    for key, text in PHRASES.items():
        path = os.path.join(CACHE_DIR, f"{key}.mp3")
        print(f"  Caching '{text}' -> {key}.mp3")
        communicate = edge_tts.Communicate(text, VOICE, rate="+5%", pitch="-2Hz")
        await communicate.save(path)

if __name__ == "__main__":
    asyncio.run(generate_cache())
