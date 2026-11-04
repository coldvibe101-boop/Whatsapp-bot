import os
import base64
import threading
import uuid
from urllib.parse import quote
import requests
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

app = Flask(__name__)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)

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
    "I can chat, make images and coach your edits:\n"
    "• Type any question to chat\n"
    "• /image a cat astronaut on the moon\n"
    "• /am <edit> = recreate in Alight Motion\n"
    "• /amz <edit> = recreate in After Motion\n"
    "• /cc <edit> = recreate in CapCut\n"
    "Tip: send a screenshot of the edit with /am, /amz or /cc as the caption.\n"
    "(Video generation isn't available on the free setup.)"
)

MAX_MESSAGES = 20
histories = {}   # phone number -> list of Gemini-format messages
media_store = {} # id -> (bytes, content_type) for images we send back


# ---------- helpers ----------
def split_message(text, limit=1500):
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


def gemini(system, contents, max_tokens=800):
    """Call the free Gemini API and return the reply text."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=60)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip() or "Sorry, I have no answer for that."


def get_image_part(form):
    """Download the first image the user sent (if any)."""
    try:
        for i in range(int(form.get("NumMedia", 0))):
            ctype = form.get(f"MediaContentType{i}", "")
            if ctype.startswith("image/"):
                r = requests.get(form[f"MediaUrl{i}"], auth=(TWILIO_SID, TWILIO_TOKEN), timeout=20)
                r.raise_for_status()
                data = base64.b64encode(r.content).decode()
                return {"inlineData": {"mimeType": ctype, "data": data}}
    except Exception as e:
        print("Image download error:", e)
    return None


def generate_image_and_send(prompt, to, from_, base_url):
    """Free image generation via Pollinations, then send it back."""
    try:
        url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1024&height=1024&nologo=true"
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        media_id = uuid.uuid4().hex
        media_store[media_id] = (r.content, r.headers.get("Content-Type", "image/jpeg"))
        while len(media_store) > 20:  # keep memory small
            media_store.pop(next(iter(media_store)))
        twilio.messages.create(
            from_=from_, to=to, body="Here's your image!",
            media_url=[f"{base_url}media/{media_id}"],
        )
    except Exception as e:
        print("Image error:", e)
        send_text("Sorry, I couldn't make that image. Try a different prompt in a minute.", to, from_)


def edit_help_and_send(app_key, request_text, image_part, to, from_):
    try:
        parts = []
        if image_part:
            parts.append(image_part)
        parts.append({"text": request_text or "Recreate the edit in this screenshot."})
        answer = gemini(EDIT_SYSTEM.format(app=EDIT_APPS[app_key]),
                        [{"role": "user", "parts": parts}], max_tokens=1500)
        history = histories.setdefault(to, [])
        history.append({"role": "user", "parts": [{"text": f"({EDIT_APPS[app_key]} edit help) {request_text}"}]})
        history.append({"role": "model", "parts": [{"text": answer}]})
        send_text(answer, to, from_)
    except Exception as e:
        print("Edit help error:", e)
        send_text("Sorry, I couldn't work that out. Please try again.", to, from_)


# ---------- routes ----------
@app.route("/", methods=["GET"])
def home():
    return "WhatsApp bot is running."


@app.route("/media/<media_id>", methods=["GET"])
def media(media_id):
    item = media_store.get(media_id)
    if not item:
        return "Not found", 404
    return Response(item[0], content_type=item[1])


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender = request.form.get("From", "unknown")
    bot_number = request.form.get("To", "")
    text = request.form.get("Body", "").strip()
    image_part = get_image_part(request.form)
    base_url = request.host_url.replace("http://", "https://")
    reply = MessagingResponse()

    if (not text and not image_part) or text.lower() in ("/help", "help", "hi", "hello", "menu"):
        reply.message(HELP)
        return str(reply)

    cmd, _, rest = text.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()

    if cmd == "/video":
        reply.message("Video generation isn't available on the free setup yet. "
                      "Try /image, then animate the picture in CapCut or Alight Motion!")
        return str(reply)

    if cmd == "/image":
        if not rest:
            reply.message("Add a description, e.g. /image a sunset over mountains")
            return str(reply)
        reply.message("Making your image... about 20-60 seconds.")
        threading.Thread(target=generate_image_and_send,
                         args=(rest, sender, bot_number, base_url), daemon=True).start()
        return str(reply)

    if cmd in ("/am", "/amz", "/cc"):
        if not rest and not image_part:
            reply.message(f"Describe the edit (or attach a screenshot), e.g. {cmd} glitch zoom transition on beat")
            return str(reply)
        reply.message(f"Working out the steps for {EDIT_APPS[cmd[1:]]}... one moment.")
        threading.Thread(target=edit_help_and_send,
                         args=(cmd[1:], rest, image_part, sender, bot_number), daemon=True).start()
        return str(reply)

    # Normal chat
    history = histories.setdefault(sender, [])
    user_text = text or "Please look at this image."
    history.append({"role": "user", "parts": [{"text": user_text}]})
    del history[:-MAX_MESSAGES]
    while history and history[0]["role"] != "user":
        history.pop(0)

    contents = list(history)
    if image_part:
        contents[-1] = {"role": "user", "parts": [image_part, {"text": user_text}]}

    try:
        answer = gemini(SYSTEM, contents)
        history.append({"role": "model", "parts": [{"text": answer}]})
    except Exception as e:
        print("Chat error:", e)
        history.pop()
        answer = "Sorry, something went wrong. Please try again."

    for chunk in split_message(answer):
        reply.message(chunk)
    return str(reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
