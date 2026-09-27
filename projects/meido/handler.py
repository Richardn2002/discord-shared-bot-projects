# Server Meido — the server's helpful maid.
#
# Commands:
#   !meido          greeting + coffee reaction
#   !party          plain text (joins the aggregated message)
#   !menu           today's cafe menu as a rich embed
#   !note <text>    files your text away and hands it back as a .txt attachment


def on_message(message):
    content = message["content"]

    if content == "!meido":
        send("hi I am the server meido")
        add_reaction("☕")

    elif content == "!party":
        send("hi I am the server meido")

    elif content == "!menu":
        send_embed(
            title="🍰 Meido Café — today's menu",
            description="Welcome home, master! Here is what I can serve you:",
            color=0xFF88AA,
            fields=[
                {"name": "☕ Coffee", "value": "hand-dripped, with latte art", "inline": True},
                {"name": "🍓 Shortcake", "value": "one slice per master", "inline": True},
                {"name": "🍛 Omelet curry", "value": "moe moe kyun spell included", "inline": True},
            ],
        )
    
    elif content == "!note":
        reply("please tell me what to write down, master!")

    elif content.startswith("!note "):
        text = content[6:].strip()
        n = kv_get("notes_filed", 0) + 1
        kv_set("notes_filed", n)
        log(f"filing note #{n} for {message['author']['name']}")
        send_file(
            f"note-{n}.txt",
            f"Meido Café — filed note #{n}\n"
            f"for: {message['author']['display_name']}\n"
            f"---\n{text}\n",
        )


def on_failure(event_name, event_data, error):
    log(f"meido tripped while handling {event_name}: {error}")
