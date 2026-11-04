import re

def on_message(message):
    if re.search(r"\bpat\b", message["content"], re.IGNORECASE):
        for emoji in ("👋", "🫳", "❤️"):
            add_reaction(emoji)
