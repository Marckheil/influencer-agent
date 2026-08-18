
# Reelayer — MULTI-TENANT agentic influencer on Maritime.
#
# One agent, many users. Each user (keyed by email) has their own niche,
# posting frequency, timezone, Instagram profile, and pipeline state.
#
# PER-USER PIPELINE (advanced one step per /run, per due user):
#   idle -> generating(image) -> generating(video) -> awaiting_approval
#        -> posting -> idle
#   The user gets emailed each video; they reply "post" or "skip".
#   Frequency + timezone decide WHEN a new video starts for each user.


import os, re, json, time, pathlib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------- config ----------------
PORT = int(os.environ.get("PORT", "8080"))

INKBOX_API_KEY = os.environ.get("INKBOX_API_KEY", "")
INKBOX_HANDLE  = os.environ.get("AGENT_HANDLE", "reminder")

HF_KEY_ID     = os.environ.get("HF_KEY_ID", "")
HF_KEY_SECRET = os.environ.get("HF_KEY_SECRET", "")
HIGGSFIELD_BASE = os.environ.get("HIGGSFIELD_BASE", "https://platform.higgsfield.ai")
HF_IMAGE_MODEL  = os.environ.get("HF_IMAGE_MODEL", "higgsfield-ai/soul/standard")
HF_VIDEO_MODEL  = os.environ.get("HF_VIDEO_MODEL", "higgsfield-ai/dop/standard")

UPLOADPOST_API_KEY = os.environ.get("UPLOADPOST_API_KEY", "")

# a hard cap so signups can't blow the Higgsfield budget (Stage 3 tunes this)
MAX_USERS  = int(os.environ.get("MAX_USERS", "25"))
AUTO_POST  = os.environ.get("AUTO_POST", "false").lower() == "true"

DATA_DIR   = pathlib.Path(os.environ.get("DATA_DIR", "."))
USERS_FILE = DATA_DIR / "users.json"
SEEN_FILE  = DATA_DIR / "seen.json"

# niche id -> (scene prompt template, motion prompt)
NICHE_PROMPTS = {
    "1977":    ("First-person POV, a day in the life in 1977, {extra}sunlit, warm film grain, "
                "kodachrome colors, nostalgic, photorealistic",
                "slow cinematic push-in, gentle handheld motion, warm 1977 film look, dust in the light"),
    "history": ("First-person POV in ancient Rome, {extra}marble, torchlight, cinematic, "
                "historically detailed, photorealistic",
                "slow cinematic dolly, atmospheric, epic scale"),
    "nature":  ("A serene nature scene, {extra}misty forest at dawn, volumetric light, "
                "ultra detailed, calm, photorealistic",
                "slow drifting camera, gentle wind, peaceful ambient motion"),
    "space":   ("A breathtaking cosmic scene, {extra}nebula and distant planets, sci-fi, "
                "cinematic, ultra detailed",
                "slow orbital drift, twinkling stars, vast and quiet"),
    "food":    ("A cozy kitchen scene, {extra}warm comfort food, steam rising, golden hour light, "
                "appetizing, photorealistic",
                "slow push-in on the food, gentle steam motion, warm and inviting"),
}
DEFAULT_NICHE = "1977"


# ---------------- persistence ----------------
def load(path, default):
    try: return json.loads(path.read_text())
    except Exception: return default

def save(path, obj):
    path.write_text(json.dumps(obj, indent=2))

def load_users(): return load(USERS_FILE, {})
def save_users(u): save(USERS_FILE, u)

def new_user(email, instagram, niche, frequency, tz_offset):
    return {
        "email": email,
        "instagram": instagram,
        "niche": niche,
        "frequency": int(frequency),            # posts per day (1 or 2)
        "tz_offset": float(tz_offset),
        "uploadpost_user": None,                # set when they connect IG (Stage 2)
        # pipeline state:
        "stage": "idle",                        # idle|generating|awaiting_approval|posting
        "substage": None,                       # image|video (within generating)
        "idea": None, "caption": None,
        "job_id": None, "image_url": None, "video_url": None,
        "last_started": None,                   # iso of last generation kickoff
        "history": [],
        "last_error": None,
    }


# ---------------- http helper (Cloudflare-friendly) ----------------
def http_json(method, url, headers=None, body=None, form=None, timeout=120):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
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


# ---------------- Inkbox ----------------
def inkbox_send(to_email, subject, body_text):
    if not INKBOX_API_KEY: return False
    from inkbox import Inkbox
    with Inkbox(api_key=INKBOX_API_KEY) as ink:
        ink.get_identity(INKBOX_HANDLE).send_email(
            to=[to_email], subject=subject, body_text=body_text)
    return True

def inkbox_latest_reply():
    """(message_id, sender_email, body) of the newest inbound email, or (None,None,None)."""
    if not INKBOX_API_KEY: return None, None, None
    from inkbox import Inkbox
    with Inkbox(api_key=INKBOX_API_KEY) as ink:
        identity = ink.get_identity(INKBOX_HANDLE)
        for msg in identity.iter_emails(direction="inbound"):
            detail = identity.get_message(str(msg.id))
            sender = (msg.from_address or "").strip().lower()
            return str(msg.id), sender, (detail.body_text or "")
    return None, None, None


# ---------------- Higgsfield (confirmed two-step API) ----------------
def _hf_auth():
    return {"Authorization": f"Key {HF_KEY_ID}:{HF_KEY_SECRET}", "Accept": "application/json"}

def _hf_submit(model_path, payload):
    status, data = http_json("POST", f"{HIGGSFIELD_BASE}/{model_path}",
                             headers=_hf_auth(), body=payload)
    if status in (200, 201) and isinstance(data, dict) and data.get("request_id"):
        return data["request_id"]
    raise RuntimeError(f"higgsfield submit {model_path} failed: {status} {data}")

def _hf_status(request_id):
    status, data = http_json("GET", f"{HIGGSFIELD_BASE}/requests/{request_id}/status",
                             headers=_hf_auth())
    if status == 200 and isinstance(data, dict):
        return (data.get("status") or "").lower(), data
    raise RuntimeError(f"higgsfield status failed: {status} {data}")

def higgsfield_start_image(prompt):
    return _hf_submit(HF_IMAGE_MODEL, {"prompt": prompt, "aspect_ratio": "9:16", "resolution": "720p"})

def higgsfield_poll_image(rid):
    st, data = _hf_status(rid)
    if st == "completed":
        imgs = data.get("images") or []
        if imgs: return imgs[0]["url"]
        raise RuntimeError(f"image completed but no url: {data}")
    if st in ("failed","nsfw","canceled"): raise RuntimeError(f"image {st}: {data.get('error')}")
    return None

def higgsfield_start_video(image_url, motion_prompt):
    return _hf_submit(HF_VIDEO_MODEL, {"image_url": image_url, "prompt": motion_prompt})

def higgsfield_poll_video(rid):
    st, data = _hf_status(rid)
    if st == "completed":
        vid = data.get("video") or {}
        if vid.get("url"): return vid["url"]
        raise RuntimeError(f"video completed but no url: {data}")
    if st in ("failed","nsfw","canceled"): raise RuntimeError(f"video {st}: {data.get('error')}")
    return None


# ---------------- Upload-Post ----------------
UPLOADPOST_BASE = "https://api.upload-post.com/api/uploadposts"

def _up_headers():
    return {"Authorization": f"Apikey {UPLOADPOST_API_KEY}"}

def uploadpost_profile_name(email):
    """A safe Upload-Post username derived from the user's email (their profile id)."""
    return "reelayer_" + re.sub(r"[^a-zA-Z0-9]", "_", email.lower())

def uploadpost_create_profile(email):
    """Create the user's Upload-Post profile. Idempotent (409 = already exists = fine)."""
    uname = uploadpost_profile_name(email)
    status, data = http_json("POST", f"{UPLOADPOST_BASE}/users",
                             headers=_up_headers(), body={"username": uname})
    if status in (200, 201, 409):
        return uname
    raise RuntimeError(f"create_profile failed: {status} {data}")

def uploadpost_connect_link(email, redirect_url=None):
    """Generate the 1-hour connect URL the user visits to link their Instagram."""
    uname = uploadpost_profile_name(email)
    body = {"username": uname, "platforms": ["instagram"]}
    if redirect_url:
        body["redirect_url"] = redirect_url
    status, data = http_json("POST", f"{UPLOADPOST_BASE}/users/generate-jwt",
                             headers=_up_headers(), body=body)
    if status == 200 and isinstance(data, dict) and data.get("access_url"):
        return data["access_url"]
    raise RuntimeError(f"connect_link failed: {status} {data}")

def uploadpost_is_connected(email):
    """True if this user's profile has Instagram connected."""
    uname = uploadpost_profile_name(email)
    status, data = http_json("GET", f"{UPLOADPOST_BASE}/users/{uname}", headers=_up_headers())
    if status == 200 and isinstance(data, dict):
        ig = (data.get("profile") or {}).get("social_accounts", {}).get("instagram")
        return bool(ig)
    return False


def uploadpost_publish(video_url, caption, uploadpost_user):
    form = {
        "video": video_url,
        "user": uploadpost_user,
        "title": caption,
        "platform[]": "instagram",
        "media_type": "REELS",
    }
    status, data = http_json("POST", "https://api.upload-post.com/api/upload",
                             headers={"Authorization": f"Apikey {UPLOADPOST_API_KEY}"}, form=form)
    if status == 200 and isinstance(data, dict) and data.get("success"):
        return data.get("job_id") or data.get("request_id") or "posted"
    raise RuntimeError(f"uploadpost_publish failed: {status} {data}")


# ---------------- content helpers ----------------
def prompts_for(user):
    niche = user.get("niche") or DEFAULT_NICHE
    if niche in NICHE_PROMPTS:
        scene, motion = NICHE_PROMPTS[niche]
        return scene.format(extra=""), motion
    # custom free-text niche
    scene = (f"First-person POV, {niche}, cinematic, atmospheric, "
             f"ultra detailed, photorealistic")
    motion = "slow cinematic push-in, gentle motion, filmic"
    return scene, motion

def make_caption(user):
    niche = user.get("niche") or DEFAULT_NICHE
    label = {"1977":"a day in 1977","history":"ancient Rome","nature":"nature's calm",
             "space":"the cosmos","food":"cozy comfort"}.get(niche, niche)
    return (f"{label} 👀\n\nwhich era would you actually live in?\n\n"
            "#ai #aivideo #dayinthelife #history #nostalgia #fyp #viral")


# ---------------- scheduling ----------------
def user_is_due(user, now):
    """True if this idle user should start a new video now, per frequency+timezone."""
    if user["stage"] != "idle":
        return False  # already mid-pipeline
    last = user.get("last_started")
    if not last:
        return True   # never made one yet
    gap_hours = 24.0 / max(1, user["frequency"])
    elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 3600.0
    return elapsed >= gap_hours


# ---------------- per-user pipeline step ----------------
def advance_user(email, user, now):
    """Advance one user's pipeline by one step. Mutates `user`. Returns a small summary."""
    out = {"email": email, "from": user["stage"]}

    if user["stage"] == "idle":
        if not user_is_due(user, now):
            out["skip"] = "not due"; out["to"] = "idle"; return out
        scene, _ = prompts_for(user)
        user["idea"] = scene
        user["caption"] = make_caption(user)
        user["image_url"] = None; user["video_url"] = None
        try:
            user["job_id"] = higgsfield_start_image(scene)
            user["substage"] = "image"; user["stage"] = "generating"
            user["last_started"] = now.isoformat(); user["last_error"] = None
        except Exception as e:
            user["last_error"] = str(e)[:300]

    elif user["stage"] == "generating":
        try:
            if user["substage"] == "image":
                img = higgsfield_poll_image(user["job_id"])
                if img:
                    user["image_url"] = img
                    _, motion = prompts_for(user)
                    user["job_id"] = higgsfield_start_video(img, motion)
                    user["substage"] = "video"
            elif user["substage"] == "video":
                vid = higgsfield_poll_video(user["job_id"])
                if vid:
                    user["video_url"] = vid
                    if AUTO_POST and user.get("uploadpost_user"):
                        user["stage"] = "posting"
                    else:
                        inkbox_send(email, "Approve today's Reel?",
                            f"Your agent made a video.\n\nNiche: {user['niche']}\n\n"
                            f"Caption:\n{user['caption']}\n\nWatch it: {vid}\n\n"
                            f"Reply 'post' to publish to Instagram, or 'skip' to drop it.")
                        user["stage"] = "awaiting_approval"
        except Exception as e:
            user["last_error"] = str(e)[:300]

    elif user["stage"] == "posting":
        try:
            # Check live connection status with Upload-Post (don't trust stale flag).
            if not user.get("uploadpost_user"):
                if uploadpost_is_connected(email):
                    user["uploadpost_user"] = uploadpost_profile_name(email)

            if not user.get("uploadpost_user"):
                # Still not connected — hold the video, send a fresh connect link, go idle.
                try:
                    link = uploadpost_connect_link(email)
                except Exception:
                    link = None
                msg = ("Your video is ready, but your Instagram isn't connected yet, so I "
                       "can't publish it.")
                if link:
                    msg += f"\n\nConnect here (1 hour):\n{link}\n\nThen future videos auto-post."
                inkbox_send(email, "Connect Instagram to publish", msg)
                user["history"].append({"idea": user["idea"], "video_url": user["video_url"],
                                        "posted_at": None, "reason": "no_ig"})
                user["stage"] = "idle"
                out["note"] = "no IG connected"
            else:
                pid = uploadpost_publish(user["video_url"], user["caption"], user["uploadpost_user"])
                user["history"].append({"idea": user["idea"], "video_url": user["video_url"],
                                        "posted_at": now.isoformat(), "post_id": pid})
                inkbox_send(email, "Posted ✅", f"Your Reel is live.\n{user['video_url']}")
                user["stage"] = "idle"; out["posted"] = pid
        except Exception as e:
            user["last_error"] = str(e)[:300]

    out["to"] = user["stage"]
    return out


# ---------------- the /run cycle: process replies, then advance everyone ----------------
def run_cycle():
    now = datetime.now(timezone.utc)
    users = load_users()
    summary = {"users": len(users), "reply": None, "advanced": []}

    # 1) process the newest inbound email, match to a user by sender
    mid, sender, body = inkbox_latest_reply()
    seen = load(SEEN_FILE, {}).get("id")
    if mid and mid != seen:
        save(SEEN_FILE, {"id": mid})
        if sender in users:
            u = users[sender]
            decision = (body or "").strip().lower()
            if u["stage"] == "awaiting_approval":
                if decision.startswith("post"):
                    u["stage"] = "posting"; summary["reply"] = f"{sender}:post"
                elif decision.startswith("skip"):
                    u["history"].append({"idea": u["idea"], "video_url": u["video_url"],
                                         "posted_at": None, "skipped": True})
                    u["stage"] = "idle"; summary["reply"] = f"{sender}:skip"
            # frequency change by reply
            if decision.startswith("once"): u["frequency"] = 1
            elif decision.startswith("twice"): u["frequency"] = 2
            # fresh connect link on request
            if decision.startswith("connect"):
                try:
                    uploadpost_create_profile(sender)
                    link = uploadpost_connect_link(sender)
                    inkbox_send(sender, "Connect your Instagram",
                        f"Here's a fresh link (valid 1 hour):\n{link}")
                    summary["reply"] = f"{sender}:connect"
                except Exception as e:
                    u["last_error"] = f"connect: {str(e)[:150]}"
            save_users(users)

    # 2) advance every user one step (due idles start; in-flight ones progress)
    for email, u in users.items():
        res = advance_user(email, u, now)
        if res.get("from") != res.get("to") or res.get("posted") or res.get("note"):
            summary["advanced"].append(res)
    save_users(users)
    return summary


# ---------------- signup ----------------
def register_user(email, instagram, niche, frequency, tz_offset):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return 400, {"ok": False, "error": "valid email required"}
    users = load_users()
    if email not in users and len(users) >= MAX_USERS:
        return 403, {"ok": False, "error": "signups are full right now — try later"}
    users[email] = new_user(email, (instagram or "").strip().lstrip("@"),
                            (niche or DEFAULT_NICHE).strip(), frequency or 1, tz_offset or 0)
    save_users(users)

    # Stage 2: create their Upload-Post profile + generate a connect link, email it.
    connect_url = None
    try:
        uploadpost_create_profile(email)
        connect_url = uploadpost_connect_link(email)
    except Exception as e:
        # profile/link failure shouldn't block signup — they still get videos by email
        users[email]["last_error"] = f"connect setup: {str(e)[:150]}"
        save_users(users)

    # welcome + connect instructions
    try:
        body = (f"Welcome! Your agent will start making {users[email]['niche']} videos and "
                f"email each one here for you to approve.\n\n")
        if connect_url:
            body += ("One thing first — connect your Instagram so it can publish for you:\n"
                     f"{connect_url}\n\n(link is valid for 1 hour; reply CONNECT for a fresh one)\n\n")
        body += "First video is generating now."
        inkbox_send(email, "Your Reelayer agent is live 🎬", body)
    except Exception:
        pass
    return 200, {"ok": True, "email": email, "connect_url": connect_url}


# ---------------- HTTP ----------------
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
        if self.path.startswith("/signup"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
                code, resp = register_user(d.get("email"), d.get("instagram"),
                                           d.get("niche"), d.get("frequency"), d.get("tz_offset"))
                self._json(code, resp)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:300]})
        elif self.path.startswith("/connect"):
            # Frontend "Connect Instagram" button: returns a fresh connect URL.
            try:
                n = int(self.headers.get("Content-Length", 0))
                d = json.loads(self.rfile.read(n) or b"{}")
                email = (d.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    return self._json(400, {"ok": False, "error": "email required"})
                uploadpost_create_profile(email)
                url = uploadpost_connect_link(email)
                self._json(200, {"ok": True, "connect_url": url})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:300]})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        if self.path.startswith("/run"):
            try: self._json(200, {"ok": True, **run_cycle()})
            except Exception as e: self._json(500, {"ok": False, "error": str(e)[:400]})
        elif self.path.startswith("/health"):
            users = load_users()
            posts = sum(len([h for h in u["history"] if h.get("posted_at")]) for u in users.values())
            self._json(200, {"status": "ok", "users": len(users), "posts_made": posts})
        else:
            users = load_users()
            self.send_response(200)
            self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write((f"<h1>Reelayer</h1><p>{len(users)} agents running.</p>").encode())

    def log_message(self, *a): pass


if __name__ == "__main__":
    print(f"reelayer (multi-tenant) listening on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
