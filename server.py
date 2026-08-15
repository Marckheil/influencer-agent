#!/usr/bin/env python3
# Agentic Influencer — runs on Maritime as a sleep/wake web agent.
#
# THE LOOP (one cycle per wake):
#   1. Pick a content idea (from a rotating themes list, or a queued idea).
#   2. Generate a 9:16 faceless video for it (Higgsfield).
#   3. Email YOU the video + caption for approval (Inkbox).
#   4. You reply "post" (or "skip"). Next cycle, the agent reads that reply
#      and either publishes to Instagram (Upload-Post) or drops it.
#
# WHY human-in-the-loop: keeps you compliant (you approve every post),
# prevents off-brand/ToS-risky posts, and makes a far better demo. Flip
# AUTO_POST=true later to skip approval once you trust it.
#
# WHY this shape fits Maritime: the work is bursty + scheduled (wake, make,
# ask, sleep). Video gen is async, so we kick a job then poll on later wakes.
#
# All external calls are isolated in clearly marked functions so you can
# wire real credentials in one place each.
#
# Endpoints:
#   GET  /run       -> advance the pipeline one step, return JSON summary
#   GET  /health    -> liveness + current pipeline state
#   POST /idea      -> queue a specific content idea  {"idea": "..."}
#   GET  /          -> tiny status page

import os, re, json, time, pathlib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------- config (all via env, set with `maritime env set`) ----------
PORT = int(os.environ.get("PORT", "8080"))

# Inkbox (already working from your other agent)
INKBOX_API_KEY = os.environ.get("INKBOX_API_KEY", "")
INKBOX_HANDLE  = os.environ.get("AGENT_HANDLE", "reminder")
OWNER_EMAIL    = os.environ.get("OWNER_EMAIL", "")

# Higgsfield  (video generation)
HIGGSFIELD_API_KEY = os.environ.get("HIGGSFIELD_API_KEY", "")

# Upload-Post  (Instagram publishing)
UPLOADPOST_API_KEY = os.environ.get("UPLOADPOST_API_KEY", "")
UPLOADPOST_USER    = os.environ.get("UPLOADPOST_USER", "")   # your connected IG profile name in Upload-Post

# behavior
AUTO_POST = os.environ.get("AUTO_POST", "false").lower() == "true"

DATA_DIR   = pathlib.Path(os.environ.get("DATA_DIR", "."))
STATE_FILE = DATA_DIR / "state.json"
SEEN_FILE  = DATA_DIR / "seen.json"

# Rotating content themes. The "day in the life in 1977" idea and friends.
# The agent walks this list; POST /idea can inject a specific one to the front.
DEFAULT_THEMES = [
    "A day in the life in 1977, first-person POV, warm film grain, nostalgic",
    "A day in the life in ancient Rome, first-person POV, cinematic",
    "What a morning routine looked like in 1950s America, cozy vintage",
    "POV: you are a lighthouse keeper in 1890, moody and atmospheric",
    "A day in the life on a 1980s arcade night, neon, synthwave mood",
]


# ================= persistence =================
def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def save(path, obj):
    path.write_text(json.dumps(obj, indent=2))

def get_state():
    # pipeline stages: idle -> generating -> awaiting_approval -> posting -> done
    return load(STATE_FILE, {
        "stage": "idle",
        "theme_index": 0,
        "idea": None,
        "caption": None,
        "job_id": None,      # Higgsfield generation job
        "video_url": None,
        "queued_idea": None,
        "last_error": None,
        "history": [],       # list of {idea, video_url, posted_at}
    })

def set_state(s):
    save(STATE_FILE, s)


# ================= tiny HTTP helper (stdlib only) =================
def http_json(method, url, headers=None, body=None, form=None, timeout=120):
    """Returns (status_code, parsed_json_or_text)."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        # multipart-ish: Upload-Post accepts application/x-www-form-urlencoded too
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    # Higgsfield sits behind Cloudflare, which blocks the default python UA.
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try: return r.status, json.loads(raw)
            except Exception: return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, raw
    except Exception as e:
        return 0, str(e)


# ================= INKBOX (email you + read your replies) =================
# Uses the same Inkbox SDK pattern as your reminder agent.
def inkbox_send(subject, body_text):
    if not (INKBOX_API_KEY and OWNER_EMAIL):
        return False
    from inkbox import Inkbox
    with Inkbox(api_key=INKBOX_API_KEY) as ink:
        ink.get_identity(INKBOX_HANDLE).send_email(
            to=[OWNER_EMAIL], subject=subject, body_text=body_text)
    return True

def inkbox_latest_reply():
    """Return (message_id, body_text) of the newest inbound email, or (None, None)."""
    if not INKBOX_API_KEY:
        return None, None
    from inkbox import Inkbox
    with Inkbox(api_key=INKBOX_API_KEY) as ink:
        identity = ink.get_identity(INKBOX_HANDLE)
        for msg in identity.iter_emails(direction="inbound"):
            detail = identity.get_message(str(msg.id))
            return str(msg.id), (detail.body_text or "")
    return None, None


# ================= HIGGSFIELD (generate the video) =================
# CONFIRMED + TESTED against the real API (docs.higgsfield.ai):
#   auth:         Authorization: Key {key_id}:{key_secret}
#   text->image:  POST https://platform.higgsfield.ai/higgsfield-ai/soul/standard
#   image->video: POST https://platform.higgsfield.ai/higgsfield-ai/dop/standard
#   poll:         GET  https://platform.higgsfield.ai/requests/{request_id}/status
#   done when status=="completed"; image at images[0].url, video at video.url
#
# It's a TWO-STEP pipeline: text prompt -> still image -> animated video.
# DoP outputs a fixed 5s clip (a valid Reel length). To go longer later,
# swap the video model path + add a "duration" param.
HIGGSFIELD_BASE = os.environ.get("HIGGSFIELD_BASE", "https://platform.higgsfield.ai")
# credentials: set BOTH of these with `maritime env set`
HF_KEY_ID     = os.environ.get("HF_KEY_ID", "")
HF_KEY_SECRET = os.environ.get("HF_KEY_SECRET", "")
HF_IMAGE_MODEL = os.environ.get("HF_IMAGE_MODEL", "higgsfield-ai/soul/standard")
HF_VIDEO_MODEL = os.environ.get("HF_VIDEO_MODEL", "higgsfield-ai/dop/standard")

def _hf_auth():
    return {"Authorization": f"Key {HF_KEY_ID}:{HF_KEY_SECRET}", "Accept": "application/json"}

def _hf_submit(model_path, payload):
    status, data = http_json("POST", f"{HIGGSFIELD_BASE}/{model_path}",
                             headers=_hf_auth(), body=payload)
    if status in (200, 201) and isinstance(data, dict) and data.get("request_id"):
        return data["request_id"]
    raise RuntimeError(f"higgsfield submit {model_path} failed: {status} {data}")

def _hf_status(request_id):
    """Return (status_str, data). status in queued|in_progress|completed|failed|nsfw|canceled."""
    status, data = http_json("GET", f"{HIGGSFIELD_BASE}/requests/{request_id}/status",
                             headers=_hf_auth())
    if status == 200 and isinstance(data, dict):
        return (data.get("status") or "").lower(), data
    raise RuntimeError(f"higgsfield status failed: {status} {data}")

# --- step 1: kick off the IMAGE generation ---
def higgsfield_start_image(prompt):
    return _hf_submit(HF_IMAGE_MODEL, {
        "prompt": prompt, "aspect_ratio": "9:16", "resolution": "720p"})

# --- poll the image; returns None (running) or image_url (done) ---
def higgsfield_poll_image(request_id):
    st, data = _hf_status(request_id)
    if st == "completed":
        imgs = data.get("images") or []
        if imgs:
            return imgs[0]["url"]
        raise RuntimeError(f"image completed but no url: {data}")
    if st in ("failed", "nsfw", "canceled"):
        raise RuntimeError(f"image generation {st}: {data.get('error')}")
    return None

# --- step 2: kick off the VIDEO generation from the image ---
def higgsfield_start_video(image_url, motion_prompt):
    return _hf_submit(HF_VIDEO_MODEL, {
        "image_url": image_url, "prompt": motion_prompt})

# --- poll the video; returns None (running) or video_url (done) ---
def higgsfield_poll_video(request_id):
    st, data = _hf_status(request_id)
    if st == "completed":
        vid = data.get("video") or {}
        if vid.get("url"):
            return vid["url"]
        raise RuntimeError(f"video completed but no url: {data}")
    if st in ("failed", "nsfw", "canceled"):
        raise RuntimeError(f"video generation {st}: {data.get('error')}")
    return None

MOTION_PROMPT = ("slow cinematic push-in, gentle handheld motion, "
                 "warm film look, dust motes drifting in the light")


# ================= UPLOAD-POST (publish to Instagram) =================
# Confirmed API: POST https://api.upload-post.com/api/upload
#   Authorization: Apikey KEY ; platform[]=instagram ; media_type=REELS
#   video_url=<public mp4> ; user=<connected profile> ; title=<caption>
def uploadpost_publish(video_url, caption, first_comment=None):
    form = {
        "video_url": video_url,
        "user": UPLOADPOST_USER,
        "title": caption,
        "platform[]": "instagram",
        "media_type": "REELS",
        "share_mode": "FEED_AND_REELS",
    }
    if first_comment:
        form["instagram_first_comment"] = first_comment
    status, data = http_json(
        "POST", "https://api.upload-post.com/api/upload",
        headers={"Authorization": f"Apikey {UPLOADPOST_API_KEY}"},
        form=form,
    )
    if status == 200 and isinstance(data, dict) and data.get("success"):
        return data.get("job_id")
    raise RuntimeError(f"uploadpost_publish failed: {status} {data}")


# ================= caption writing (no LLM dependency needed) =================
def make_caption(idea):
    # Simple, effective. Swap for an LLM call later if you want punchier copy.
    hook = idea.split(",")[0].strip()
    return f"{hook} 👀\n\nwhich era would you actually want to live in?"

def make_hashtags(idea):
    base = "#ai #aivideo #dayinthelife #history #nostalgia #fyp #viral #edit"
    return base


# ================= the pipeline =================
def advance_pipeline():
    """One wake = one step forward. Returns a summary dict."""
    s = get_state()
    now = datetime.now(timezone.utc).isoformat()
    summary = {"stage_before": s["stage"]}

    # --- first: always check for an approval reply if we're awaiting one ---
    if s["stage"] == "awaiting_approval":
        mid, body = inkbox_latest_reply()
        seen = load(SEEN_FILE, {}).get("id")
        if mid and mid != seen:
            save(SEEN_FILE, {"id": mid})
            decision = (body or "").strip().lower()
            if decision.startswith("post"):
                s["stage"] = "posting"
            elif decision.startswith("skip"):
                s["history"].append({"idea": s["idea"], "video_url": s["video_url"],
                                     "posted_at": None, "skipped": True})
                s["stage"] = "idle"
                summary["note"] = "skipped by owner"
        # if no new reply, stay awaiting_approval (do nothing else this wake)

    if s["stage"] == "idle":
        # pick the next idea
        idea = s.get("queued_idea")
        if idea:
            s["queued_idea"] = None
        else:
            themes = DEFAULT_THEMES
            idea = themes[s["theme_index"] % len(themes)]
            s["theme_index"] = (s["theme_index"] + 1) % len(themes)
        s["idea"] = idea
        s["caption"] = make_caption(idea) + "\n\n" + make_hashtags(idea)
        s["image_url"] = None
        try:
            s["job_id"] = higgsfield_start_image(idea)   # step 1: image
            s["substage"] = "image"
            s["stage"] = "generating"
            s["last_error"] = None
        except Exception as e:
            s["last_error"] = str(e)[:300]
        summary["idea"] = idea

    elif s["stage"] == "generating":
        try:
            if s.get("substage") == "image":
                image_url = higgsfield_poll_image(s["job_id"])
                if image_url:
                    s["image_url"] = image_url
                    # step 2: animate the image into a video
                    s["job_id"] = higgsfield_start_video(image_url, MOTION_PROMPT)
                    s["substage"] = "video"
                # else image still rendering; try next wake
            elif s.get("substage") == "video":
                video_url = higgsfield_poll_video(s["job_id"])
                if video_url:
                    s["video_url"] = video_url
                    if AUTO_POST:
                        s["stage"] = "posting"
                    else:
                        inkbox_send(
                            subject="Approve today's Reel?",
                            body_text=(
                                f"Your agent made a video.\n\n"
                                f"Idea: {s['idea']}\n\n"
                                f"Caption:\n{s['caption']}\n\n"
                                f"Watch it: {video_url}\n\n"
                                f"Reply 'post' to publish to Instagram, or 'skip' to drop it."
                            ),
                        )
                        s["stage"] = "awaiting_approval"
                # else video still rendering; try next wake
        except Exception as e:
            s["last_error"] = str(e)[:300]

    elif s["stage"] == "posting":
        try:
            caption_only = s["caption"].split("\n\n#")[0]
            hashtags = make_hashtags(s["idea"])
            post_id = uploadpost_publish(s["video_url"], caption_only, first_comment=hashtags)
            s["history"].append({"idea": s["idea"], "video_url": s["video_url"],
                                 "posted_at": now, "post_id": post_id})
            inkbox_send("Posted ✅", f"Your Reel is live.\nIdea: {s['idea']}\n{s['video_url']}")
            s["stage"] = "idle"
            summary["posted"] = post_id
        except Exception as e:
            s["last_error"] = str(e)[:300]

    set_state(s)
    summary["stage_after"] = s["stage"]
    summary["last_error"] = s["last_error"]
    return summary


# ================= HTTP =================
class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/idea"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n) or b"{}")
                idea = data.get("idea")
                if not idea:
                    return self._json(400, {"ok": False, "error": "idea required"})
                s = get_state(); s["queued_idea"] = idea; set_state(s)
                self._json(200, {"ok": True, "queued": idea})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:300]})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        if self.path.startswith("/run"):
            try:
                self._json(200, {"ok": True, **advance_pipeline()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:400]})
        elif self.path.startswith("/health"):
            s = get_state()
            self._json(200, {"status": "ok", "stage": s["stage"],
                             "posts_made": len([h for h in s["history"] if h.get("posted_at")])})
        else:
            s = get_state()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                (f"<h1>Agentic Influencer</h1><p>Stage: {s['stage']}</p>"
                 f"<p>Posts made: {len([h for h in s['history'] if h.get('posted_at')])}</p>"
                 ).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"influencer agent listening on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
