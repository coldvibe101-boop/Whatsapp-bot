import os
import base64
import threading
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
import anthropic
import replicate

app = Flask(__name__)
claude = anthropic.Anthropic()  # ANTHROPIC_API_KEY
TWILIO_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
# replicate reads REPLICATE_API_TOKEN automatically

CHAT_MODEL = "claude-sonnet-5-5"
# Replicate model IDs change over time. Override with env vars if one stops working.
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "black-forest-labs/flux-schnell")
VIDEO_MODEL = os.environ.get("VIDEO_MODEL", "minimax/video-01")

SYSTEM = (
    "You are a helpful assistant chatting on WhatsApp. "
    "Keep answers short, clear and friendly. Avoid heavy formatting. "
    "The user edits videos on a phone (Alight Motion, After Motion, CapCut), "
    "so when editing comes up, give phone-friendly advice."
)

EDIT_APPS = {"am": "Alight Motion", "amz": "After Motion", "cc": "CapCut"}

EDIT_SYSTEM = """You are a mobile video-editing coach replying on WhatsApp.
The user edits only on a phone (no PC). Target app: {app}.
Explain how to recreate the edit they describe (or show in the attached screenshot), step by step.
Format: one line describing the look, then numbered steps, then 2-3 quick tips.
Use exact tool/menu/effect names as best you know them and give specific values
(keyframe times, durations, opacity, blur amount, speed %, easing).
If you are not sure of an exact menu name in this app, say so and describe where to look.
Keep each step short. Plain text only: no tables, no headers. Use *bold* sparingly."""

HELP = (
    "I can chat, make media and coach your edits:\n"
    "• Type any question to chat\n"
    "• /image a cat astronaut on the moon\n"
    "• /video a paper boat sailing in the rain\n"
    "• /am <edit> = recreate in Alight Motion\n"
    "• /amz <edit> = recreate in After Motion\n"
    "• /cc <edit> = recreate in CapCut\n"
    "Tip: send a screenshot of the edit with /am, /amz or /cc as the caption."
)

MAX_MESSAGES = 20
histories = {}


def split_message(text, limit=1500):
    """Split long text into WhatsApp-sized chunks at line breaks."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]


def send_text(text, to, from_):
    for chunk in split_message(text):
        twilio.messages.create(from_=from_, to=to, body=chunk)


def get_image_block(form):
    """Download the first image the user sent (if any) for Claude to look at."""
    try:
        for i in range(int(form.get("NumMedia", 0))):
            ctype = form.get(f"MediaContentType{i}", "")
            if ctype in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                r = requests.get(form[f"MediaUrl{i}"], auth=(TWILIO_SID, TWILIO_TOKEN), timeout=20)
                r.raise_for_status()
                data = base64.b64encode(r.content).decode()
                return {"type": "image", "source": {"type": "base64", "media_type": ctype, "data": data}}
    except Exception as e:
        print("Image download error:", e)
    return None


def media_url(output):
    if isinstance(output, (list, tuple)):
        output = output[0]
    return getattr(output, "url", None) or str(output)


def generate_and_send(kind, prompt, to, from_):
    try:
        model = IMAGE_MODEL if kind == "image" else VIDEO_MODEL
        out = replicate.run(model, input={"prompt": prompt})
        twilio.messages.create(
            from_=from_, to=to, body=f"Here's your {kind}!", media_url=[media_url(out)]
        )
    except Exception as e:
        print("Generation error:", e)
        twilio.messages.create(
            from_=from_, to=to, body=f"Sorry, I couldn't make that {kind}. Try a different prompt."
        )


def edit_help_and_send(app_key, request_text, image_block, to, from_):
    try:
        content = []
        if image_block:
            content.append(image_block)
        content.append({"type": "text", "text": request_text or "Recreate the edit in this screenshot."})
        resp = claude.messages.create(
            model=CHAT_MODEL,
            max_tokens=1500,
            system=EDIT_SYSTEM.format(app=EDIT_APPS[app_key]),
            messages=[{"role": "user", "content": content}],
        )
        answer = "".join(b.text for b in resp.content if b.type == "text")
        # Save to chat history so follow-up questions have context
        history = histories.setdefault(to, [])
        history.append({"role": "user", "content": f"({EDIT_APPS[app_key]} edit help) {request_text}"})
        history.append({"role": "assistant", "content": answer})
        send_text(answer, to, from_)
    except Exception as e:
        print("Edit help error:", e)
        send_text("Sorry, I couldn't work that out. Please try again.", to, from_)


@app.route("/", methods=["GET"])
def home():
    return "WhatsApp bot is running."


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender = request.form.get("From", "unknown")
    bot_number = request.form.get("To", "")
    text = request.form.get("Body", "").strip()
    image_block = get_image_block(request.form)
    reply = MessagingResponse()

    if not text and not image_block:
        reply.message(HELP)
        return str(reply)
    if text.lower() in ("/help", "help", "hi", "hello", "menu"):
        reply.message(HELP)
        return str(reply)

    cmd, _, rest = text.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()

    # Image / video generation
    if cmd in ("/image", "/video"):
        kind = cmd[1:]
        if not rest:
            reply.message(f"Add a description, e.g. {cmd} a sunset over mountains")
            return str(reply)
        wait = "30 seconds" if kind == "image" else "2-5 minutes"
        reply.message(f"Making your {kind}... this can take up to {wait}.")
        threading.Thread(
            target=generate_and_send, args=(kind, rest, sender, bot_number), daemon=True
        ).start()
        return str(reply)

    # Edit recreation help (Alight Motion / After Motion / CapCut)
    if cmd in ("/am", "/amz", "/cc"):
        if not rest and not image_block:
            reply.message(f"Describe the edit (or attach a screenshot), e.g. {cmd} glitch zoom transition on beat")
            return str(reply)
        reply.message(f"Working out the steps for {EDIT_APPS[cmd[1:]]}... one moment.")
        threading.Thread(
            target=edit_help_and_send,
            args=(cmd[1:], rest, image_block, sender, bot_number),
            daemon=True,
        ).start()
        return str(reply)

    # Normal chat (can also look at an image if one is attached)
    history = histories.setdefault(sender, [])
    user_text = text or "Please look at this image."
    history.append({"role": "user", "content": user_text})
    del history[:-MAX_MESSAGES]
    while history and history[0]["role"] != "user":
        history.pop(0)

    messages = list(history)
    if image_block:
        messages[-1] = {"role": "user", "content": [image_block, {"type": "text", "text": user_text}]}

    try:
        response = claude.messages.create(
            model=CHAT_MODEL, max_tokens=700, system=SYSTEM, messages=messages
        )
        answer = "".join(b.text for b in response.content if b.type == "text")
        history.append({"role": "assistant", "content": answer})
    except Exception as e:
        print("Chat error:", e)
        history.pop()
        answer = "Sorry, something went wrong. Please try again."

    for chunk in split_message(answer):
        reply.message(chunk)
    return str(reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
