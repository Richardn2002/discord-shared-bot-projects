def on_message(message):
    content = message["content"]
    if content.startswith("!echo "):
        send(content[6:])
    elif content == "!stats":
        send(f'Messages seen so far: {kv_get("seen", 0)}')
    elif content == "!party":
        send("hello hello")
    elif content == "!slow":
        send("fast reply")
    else:
        kv_set("seen", kv_get("seen", 0) + 1)


def on_failure(event_name, event_data, error):
    log(f"handler {event_name} crashed: {error}")
