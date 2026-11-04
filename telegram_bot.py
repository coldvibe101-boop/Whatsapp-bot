import os
import base64
import threading
from collections import deque
from urllib.parse import quote
import requests
from flask import Flask, request

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL", "")

API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"

SYSTEM = (
    "You are a helpful assistant chatting on Telegram. "
    "Keep answers short, clear and friendly. Use plain text with no markdown symbols. "
    "The user edits videos on a phone (Alight Motion, After Motion, CapCut), "
    "so when editing comes up, give phone-friendly advice."
)

EDIT_APPS = {"am": "Alight Motion", "amz": "After Motion", "cc": "CapCut"}

EDIT_SYSTEM = """You are a mobile video-editing coach replying on Telegram.
The user edits only on a phone (no PC). Target app: {app}.
Explain how to recreate the edit they describe (or show in the attached screenshot), step by step.
Format: one line describing the look, then numbered steps, then 2-3 quick tips.
Use exact tool/menu/effect names as best you know them and give specific values
(keyframe times, durations, opacity, blur amount, speed %, easing).
If you are not sure of an exact menu name in this app, say so and describe where to look.
Keep each step short. Plain text only: no markdown symbols, tables or headers."""

HELP = (
    "I can chat, make images and coach your edits:\n"
    "- Type any question to chat\n"
    "- /image a cat astronaut on the moon\n"
    "- /am <edit> = recreate in Alight Motion\n"
    "- /amz <edit> = recreate in After Motion\n"
    "- /cc <edit> = recreate in CapCut\n"
    "Tip: send a screenshot with /am, /amz or /cc in the caption.\n"
    "(Video generation isn't available on the free setup.)"
)

MAX_MESSAGES = 20
histories = {}          # chat_id -> list of Gemini-format messages
seen_ids = deque(maxlen=300)  # avoid handling the same update twice


# ---------- Telegram helpers ----------
def tg(method, **payload):
    r = requests.post(f"{API}/{method}", json=payload, timeout=30)
    return r.json()


def send_text(chat_id, text):
    text = text or "..."
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 3900])


def get_photo_part(msg):
    """Download the photo the user sent (if any) so Gemini can look at it."""
    try:
        photos = msg.get("photo")
        if not photos:
            return None
        file_id = photos[-1]["file_id"]  # biggest size
        info = tg("getFile", file_id=file_id)["result"]
        r = requests.get(f"{FILE_API}/{info['file_path']}", timeout=30)
        r.raise_for_status()
        data = base64.b64encode(r.content).decode()
        return {"inlineData": {"mimeType": "image/jpeg", "data": data}}
    except Exception as e:
        print("Photo download error:", e)
        return None


# ---------- Gemini ----------
def gemini(system, contents, max_tokens=900):
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


# ---------- features ----------
def make_image(chat_id, prompt):
    tg("sendChatAction", chat_id=chat_id, action="upload_photo")
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1024&height=1024&nologo=true"
    r = requests.get(url, timeout=90)
    r.raise_for_status()
    if "image" not in r.headers.get("Content-Type", ""):
        raise RuntimeError("Not an image")
    requests.post(
        f"{API}/sendPhoto",
        data={"chat_id": chat_id, "caption": "Here's your image!"},
        files={"photo": ("image.jpg", r.content)},
        timeout=60,
    )


def edit_help(chat_id, app_key, request_text, image_part):
    tg("sendChatAction", chat_id=chat_id, action="typing")
    parts = []
    if image_part:
        parts.append(image_part)
    parts.append({"text": request_text or "Recreate the edit in this screenshot."})
    answer = gemini(EDIT_SYSTEM.format(app=EDIT_APPS[app_key]),
                    [{"role": "user", "parts": parts}], max_tokens=1800)
    history = histories.setdefault(chat_id, [])
    history.append({"role": "user", "parts": [{"text": f"({EDIT_APPS[app_key]} edit help) {request_text}"}]})
    history.append({"role": "model", "parts": [{"text": answer}]})
    send_text(chat_id, answer)


def chat(chat_id, text, image_part):
    tg("sendChatAction", chat_id=chat_id, action="typing")
    history = histories.setdefault(chat_id, [])
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
    except Exception:
        history.pop()
        raise
    send_text(chat_id, answer)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    try:
        image_part = get_photo_part(msg)
        cmd, _, rest = text.partition(" ")
        cmd, rest = cmd.split("@")[0].lower(), rest.strip()

        if cmd in ("/start", "/help") or (not text and not image_part):
            send_text(chat_id, HELP)
        elif cmd == "/video":
            send_text(chat_id, "Video generation isn't available on the free setup yet. "
                               "Try /image, then animate the picture in CapCut or Alight Motion!")
        elif cmd == "/image":
            if not rest:
                send_text(chat_id, "Add a description, e.g. /image a sunset over mountains")
            else:
                send_text(chat_id, "Making your image... about 20-60 seconds.")
                make_image(chat_id, rest)
        elif cmd in ("/am", "/amz", "/cc"):
            if not rest and not image_part:
                send_text(chat_id, f"Describe the edit (or attach a screenshot), e.g. {cmd} glitch zoom transition on beat")
            else:
                send_text(chat_id, f"Working out the steps for {EDIT_APPS[cmd[1:]]}... one moment.")
                edit_help(chat_id, cmd[1:], rest, image_part)
        else:
            chat(chat_id, text, image_part)
    except Exception as e:
        print("Error handling message:", e)
        send_text(chat_id, "Sorry, something went wrong. Please try again in a minute.")


# ---------- web routes ----------
def set_webhook():
    if not (TOKEN and PUBLIC_URL):
        print("Webhook not set: missing TELEGRAM_BOT_TOKEN or public URL")
        return {"ok": False}
    res = tg("setWebhook", url=f"{PUBLIC_URL}/telegram", secret_token=SECRET,
             allowed_updates=["message"], drop_pending_updates=False)
    print("setWebhook:", res)
    return res


@app.route("/", methods=["GET"])
def home():
    return "Telegram bot is running."


@app.route("/setup", methods=["GET"])
def setup():
    return set_webhook()


@app.route("/telegram", methods=["POST"])
def telegram():
    if SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return "forbidden", 403
    update = request.get_json(silent=True) or {}
    uid = update.get("update_id")
    if uid in seen_ids:
        return "ok"
    seen_ids.append(uid)
    msg = update.get("message")
    if msg:
        threading.Thread(target=handle_message, args=(msg,), daemon=True).start()
    return "ok"


# Register the webhook automatically whenever the bot starts
threading.Thread(target=set_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
