def on_message(message):
    if message["content"] == "!meido":
        send("hi I am the server meido")
        add_reaction("☕")
    elif message["content"] == "!party":
        send("hi I am the server meido")
