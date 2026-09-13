import random

# Acknowledgments for normal commands
RESPONSES_ACK = [
    "Right away, Sir.",
    "At once, Sir.",
    "Consider it done.",
    "Very well, Sir.",
    "Proceeding accordingly.",
    "Working on it now.",
    "Understood, Sir."
]

# Greetings / Wake reactions (Cleaned up for Sequential Intelligence)
RESPONSES_GREETING = [
    "Yes, Sir?",
    "Standing by.",
    "How can I help, Sir?",
    "I'm here.",
    "Always listening."
]

# Emotional Contexts
EMOTIONAL_TRIGGERS = {
    "sad": ["feeling sad", "crying", "depressed", "lonely", "breakup"],
    "happy": ["party", "happy", "celebrate", "pumped", "gym"],
    "chill": ["relax", "chill", "lofi", "sleep", "tired"]
}

def get_emotional_response(query: str, default: str = None):
    """Returns a witty or emotional response based on the query context."""
    query = query.lower()
    
    if any(word in query for word in EMOTIONAL_TRIGGERS["sad"]):
        return "I'm sorry to hear that, Sir. Let's find something to match the mood."
    
    if any(word in query for word in EMOTIONAL_TRIGGERS["happy"]):
        return "That's the spirit, Sir! Powering up the atmosphere."
        
    if any(word in query for word in EMOTIONAL_TRIGGERS["chill"]):
        return "Setting a calm ambiance for you, Sir. Relax."
    
    return default if default else random.choice(RESPONSES_ACK)

def get_random_greeting():
    return random.choice(RESPONSES_GREETING)

def get_random_ack():
    return random.choice(RESPONSES_ACK)
