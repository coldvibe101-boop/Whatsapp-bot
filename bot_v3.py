import os
import time
import base64
import threading
from collections import deque
from urllib.parse import quote
import requests
from flask import Flask, request

app = Flask(__name__)


def log(*args):
    print(*args, flush=True)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
VIDEO_MODEL = os.environ.get("GEMINI_VIDEO_MODEL", GEMINI_MODEL)
CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
CF_TOKEN = os.environ.get("CF_API_TOKEN", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL", "")

API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
GEMINI_BASE = "https://generativelanguage.googleapis.com"

EDIT_APPS = {"am": "Alight Motion", "amz": "After Motion", "cc": "CapCut"}

SYSTEM = (
    "You are a helpful assistant chatting on Telegram. "
    "Keep answers short, clear and friendly. Use plain text with no markdown symbols. "
    "The user edits videos on a phone (Alight Motion, After Motion, CapCut), "
    "so when editing comes up, give phone-friendly advice."
)

EDIT_SYSTEM = """You are a mobile video-editing coach replying on Telegram.
The user edits only on a phone (no PC). Target app: {app}.
Explain how to recreate the edit they describe (or show in the attached screenshot), step by step.
Format: one line describing the look, then numbered steps, then 2-3 quick tips.
Use exact tool/menu/effect names as best you know them and give specific values
(keyframe times, durations, opacity, blur amount, speed %, easing).
If you are not sure of an exact menu name in this app, say so and describe where to look.
Keep each step short. Plain text only: no markdown symbols, tables or headers."""

VIDEO_SYSTEM = """You are an expert mobile video editor who reverse-engineers edits from a finished video.
The user edits only on a phone (no PC). Target app: {app}.
Watch the WHOLE video (visuals and audio) and work out exactly how it was made. Reply in plain text
(no markdown symbols, tables or headers) in this order:
1. LOOK: the overall style in 1-2 lines.
2. TIMELINE: go through the video by seconds (for example 0:00-0:02). For each part note the clip or
   scene, transitions, speed changes or slow-mo, zooms or shakes, color grade, text or captions
   (style, font feel, animation), overlays or effects, and how cuts or effects match the music beat.
3. RECREATE IN {app}: numbered steps using exact tool, menu and effect names as best you know them,
   with specific values (keyframe times, easing, speed %, opacity, blur amount, duration).
4. YOU WILL NEED: any assets, fonts, overlays, sounds or music to find.
5. NOT SURE: anything you could not tell for certain, and what to try instead.
If you are unsure of an exact menu name in {app}, say so and describe where to look.
Be specific and honest. Do not invent details that are not in the video."""

HELP = (
    "I can chat, make images and reverse-engineer edits:\n"
    "- Type any question to chat\n"
    "- /image a cat astronaut on the moon\n"
    "- Send me a VIDEO (an edit or reel) and I'll break down how it was made and how to recreate it. "
    "Add /am, /amz or /cc in the caption to pick the app, or I'll ask you.\n"
    "- /am <edit>, /amz <edit>, /cc <edit> = recreate an edit you describe (a screenshot works too)\n"
    "(Video generation isn't available on the free setup.)"
)

MAX_MESSAGES = 20
histories = {}                 # chat_id -> Gemini-format messages
pending_videos = {}            # chat_id -> (file_id, mime, note)
seen_ids = deque(maxlen=300)   # avoid handling the same update twice


def safe(e):
    """Error text with secrets removed, safe to show in chat/logs."""
    text = f"{type(e).__name__}: {e}"
    for secret in (TOKEN, GEMINI_KEY, CF_TOKEN, CF_ACCOUNT):
        if secret:
            text = text.replace(secret, "***")
    return text[:220]


# ---------- Telegram helpers ----------
def tg(method, **payload):
    r = requests.post(f"{API}/{method}", json=payload, timeout=30)
    return r.json()


def send_text(chat_id, text):
    text = text or "..."
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 3900])


def download_file(file_id):
    info = tg("getFile", file_id=file_id)
    if not info.get("ok"):
        raise RuntimeError("Telegram would not give me that file (bots can only read files up to 20 MB)")
    r = requests.get(f"{FILE_API}/{info['result']['file_path']}", timeout=120)
    r.raise_for_status()
    return r.content


def get_photo_part(msg):
    try:
        photos = msg.get("photo")
        if not photos:
            return None
        data = base64.b64encode(download_file(photos[-1]["file_id"])).decode()
        return {"inlineData": {"mimeType": "image/jpeg", "data": data}}
    except Exception as e:
        log("Photo download error:", safe(e))
        return None


def extract_video(msg):
    """Return (file_id, mime, size) for a video the user sent, else None."""
    for key in ("video", "animation", "video_note"):
        v = msg.get(key)
        if v:
            return v["file_id"], v.get("mime_type", "video/mp4"), v.get("file_size", 0)
    d = msg.get("document")
    if d and d.get("mime_type", "").startswith("video/"):
        return d["file_id"], d["mime_type"], d.get("file_size", 0)
    return None


# ---------- Gemini ----------
def gemini(system, contents, max_tokens=900, model=None):
    url = f"{GEMINI_BASE}/v1beta/models/{model or GEMINI_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=180)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip() or "Sorry, I have no answer for that."


def gemini_upload(data, mime):
    """Upload a video to Gemini's file storage and wait until it is ready."""
    key = {"x-goog-api-key": GEMINI_KEY}
    start = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files",
        headers={**key,
                 "X-Goog-Upload-Protocol": "resumable",
                 "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(len(data)),
                 "X-Goog-Upload-Header-Content-Type": mime,
                 "Content-Type": "application/json"},
        json={"file": {"display_name": "edit-video"}}, timeout=60)
    start.raise_for_status()
    upload_url = start.headers["X-Goog-Upload-URL"]
    up = requests.post(
        upload_url,
        headers={"Content-Length": str(len(data)),
                 "X-Goog-Upload-Offset": "0",
                 "X-Goog-Upload-Command": "upload, finalize"},
        data=data, timeout=240)
    up.raise_for_status()
    info = up.json()["file"]
    for _ in range(50):
        state = info.get("state", "ACTIVE")
        if state == "ACTIVE":
            break
        if state == "FAILED":
            raise RuntimeError("Gemini could not process this video")
        time.sleep(3)
        r = requests.get(f"{GEMINI_BASE}/v1beta/{info['name']}", headers=key, timeout=30)
        r.raise_for_status()
        info = r.json()
    else:
        raise RuntimeError("Video processing took too long")
    return {"fileData": {"mimeType": info.get("mimeType", mime), "fileUri": info["uri"]}}, info["name"]


def gemini_delete_file(name):
    try:
        requests.delete(f"{GEMINI_BASE}/v1beta/{name}", headers={"x-goog-api-key": GEMINI_KEY}, timeout=20)
    except Exception:
        pass


# ---------- images ----------
def cloudflare_image(prompt):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}"
           f"/ai/run/@cf/black-forest-labs/flux-1-schnell")
    r = requests.post(url, headers={"Authorization": f"Bearer {CF_TOKEN}"},
                      json={"prompt": prompt[:2000], "steps": 6}, timeout=120)
    if r.ok and "image" in r.headers.get("Content-Type", ""):
        return r.content
    r.raise_for_status()
    j = r.json()
    b64 = (j.get("result") or {}).get("image") or j.get("image")
    if not b64:
        raise RuntimeError("Cloudflare sent no image")
    return base64.b64decode(b64)


def pollinations_image(prompt):
    q = quote(prompt)
    urls = [
        f"https://image.pollinations.ai/prompt/{q}?width=1024&height=1024&nologo=true&model=flux",
        f"https://image.pollinations.ai/prompt/{q}?width=768&height=768&nologo=true&model=turbo",
        f"https://image.pollinations.ai/prompt/{q}",
    ]
    errors = []
    for url in urls:
        try:
            r = requests.get(url, timeout=70, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                return r.content
            errors.append(f"HTTP {r.status_code}")
        except Exception as e:
            errors.append(type(e).__name__)
    raise RuntimeError("backup image service said: " + ", ".join(errors))


def make_image(chat_id, prompt):
    tg("sendChatAction", chat_id=chat_id, action="upload_photo")
    caption = "Here's your image!"
    img = None
    if CF_ACCOUNT and CF_TOKEN:
        try:
            img = cloudflare_image(prompt)
        except Exception as e:
            log("Cloudflare image error:", safe(e))
            caption = "Here's your image (backup service, may have a watermark)."
    else:
        caption = "Here's your image (backup service, may have a watermark)."
    if img is None:
        img = pollinations_image(prompt)
    res = requests.post(f"{API}/sendPhoto", data={"chat_id": chat_id, "caption": caption},
                        files={"photo": ("image.jpg", img)}, timeout=60)
    if not res.ok:
        raise RuntimeError(f"Telegram rejected the photo (HTTP {res.status_code})")


# ---------- edit help ----------
def edit_help(chat_id, app_key, request_text, image_part):
    tg("sendChatAction", chat_id=chat_id, action="typing")
    parts = []
    if image_part:
        parts.append(image_part)
    parts.append({"text": request_text or "Recreate the edit in this screenshot."})
    answer = gemini(EDIT_SYSTEM.format(app=EDIT_APPS[app_key]),
                    [{"role": "user", "parts": parts}], max_tokens=2500)
    history = histories.setdefault(chat_id, [])
    history.append({"role": "user", "parts": [{"text": f"({EDIT_APPS[app_key]} edit help) {request_text}"}]})
    history.append({"role": "model", "parts": [{"text": answer}]})
    send_text(chat_id, answer)


def ask_which_app(chat_id):
    tg("sendMessage", chat_id=chat_id,
       text="Got your video! Which app do you want to recreate it in?",
       reply_markup={"inline_keyboard": [[
           {"text": "Alight Motion", "callback_data": "app:am"},
           {"text": "After Motion", "callback_data": "app:amz"},
           {"text": "CapCut", "callback_data": "app:cc"},
       ]]})


def analyze_video(chat_id, app_key):
    item = pending_videos.get(chat_id)
    if not item:
        send_text(chat_id, "I lost that video (the free server restarts sometimes). Please send it again.")
        return
    file_id, mime, note = item
    app_name = EDIT_APPS[app_key]
    send_text(chat_id, f"Watching your video and working out the {app_name} steps... "
                       "this can take 1-3 minutes.")
    tg("sendChatAction", chat_id=chat_id, action="typing")
    file_name = None
    try:
        data = download_file(file_id)
        part, file_name = gemini_upload(data, mime)
        prompt = f"Reverse-engineer this edit and tell me how to recreate it in {app_name}."
        if note:
            prompt += f" Extra notes from me: {note}"
        answer = gemini(VIDEO_SYSTEM.format(app=app_name),
                        [{"role": "user", "parts": [part, {"text": prompt}]}],
                        max_tokens=4500, model=VIDEO_MODEL)
        history = histories.setdefault(chat_id, [])
        history.append({"role": "user", "parts": [{"text": f"(sent a video to recreate in {app_name})"}]})
        history.append({"role": "model", "parts": [{"text": answer}]})
        send_text(chat_id, answer)
    finally:
        if file_name:
            gemini_delete_file(file_name)


# ---------- chat ----------
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
        cmd, _, rest = text.partition(" ")
        cmd, rest = cmd.split("@")[0].lower(), rest.strip()

        video = extract_video(msg)
        if video:
            file_id, mime, size = video
            if size and size > 20 * 1024 * 1024:
                send_text(chat_id, "That video is over 20 MB, which is the most Telegram lets bots read. "
                                   "Please trim it or export a smaller version and send it again.")
                return
            if cmd in ("/am", "/amz", "/cc"):
                pending_videos[chat_id] = (file_id, mime, rest)
                analyze_video(chat_id, cmd[1:])
            else:
                pending_videos[chat_id] = (file_id, mime, text)
                ask_which_app(chat_id)
            return

        image_part = get_photo_part(msg)

        if cmd in ("/start", "/help") or (not text and not image_part):
            send_text(chat_id, HELP)
        elif cmd == "/video":
            send_text(chat_id, "I can't generate videos on the free setup, but you can SEND me a video "
                               "and I'll reverse-engineer how it was edited!")
        elif cmd == "/image":
            if not rest:
                send_text(chat_id, "Add a description, e.g. /image a sunset over mountains")
            else:
                send_text(chat_id, "Making your image... about 10-60 seconds.")
                make_image(chat_id, rest)
        elif cmd in ("/am", "/amz", "/cc"):
            if not rest and not image_part:
                send_text(chat_id, f"Describe the edit, attach a screenshot, or send the video itself, "
                                   f"e.g. {cmd} glitch zoom transition on beat")
            else:
                send_text(chat_id, f"Working out the steps for {EDIT_APPS[cmd[1:]]}... one moment.")
                edit_help(chat_id, cmd[1:], rest, image_part)
        else:
            chat(chat_id, text, image_part)
    except Exception as e:
        log("Error handling message:", safe(e))
        send_text(chat_id, f"Sorry, something went wrong ({safe(e)}). Please try again in a minute.")


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    data = cq.get("data", "")
    try:
        if data.startswith("app:") and data[4:] in EDIT_APPS:
            analyze_video(chat_id, data[4:])
    except Exception as e:
        log("Error handling button:", safe(e))
        send_text(chat_id, f"Sorry, something went wrong ({safe(e)}). Please try again in a minute.")


# ---------- web routes ----------
def set_webhook():
    if not (TOKEN and PUBLIC_URL):
        log("Webhook not set: missing TELEGRAM_BOT_TOKEN or public URL")
        return {"ok": False}
    res = tg("setWebhook", url=f"{PUBLIC_URL}/telegram", secret_token=SECRET,
             allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    log("setWebhook:", res)
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
    if update.get("message"):
        threading.Thread(target=handle_message, args=(update["message"],), daemon=True).start()
    elif update.get("callback_query"):
        threading.Thread(target=handle_callback, args=(update["callback_query"],), daemon=True).start()
    return "ok"


threading.Thread(target=set_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
