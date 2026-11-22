import os
import re
import json
import uuid
import time
import base64
import hashlib
import secrets
import random
import threading
from urllib.parse import urlencode
import requests
from flask import Flask, request, jsonify, redirect, session, render_template_string
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
import googleapiclient.discovery
from google import genai
from google.genai import types

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# --- YouTube Config ---
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
GOOGLE_CLIENT_CONFIG_JSON = os.environ.get("GOOGLE_CLIENT_CONFIG_JSON", "")

# --- Kick Config ---
KICK_CLIENT_ID = os.environ.get("KICK_CLIENT_ID", "")
KICK_CLIENT_SECRET = os.environ.get("KICK_CLIENT_SECRET", "")
KICK_SCOPES = ["chat:write", "channel:read", "user:read"]
KICK_AUTH_URL = "https://id.kick.com/oauth/authorize"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_API_BASE = "https://api.kick.com/public/v1"

# --- Gemini Config ---
MODEL_ID = "gemini-3.5-flash-lite"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if os.environ.get("FLASK_ENV") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    app.config["SESSION_COOKIE_SECURE"] = False

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

state_lock = threading.Lock()
session_logs = {}     # {(sid, platform): [log_lines...]}
session_tokens = {}   # {(sid, platform): token_json_str}
session_configs = {}  # {(sid, platform): {"mode": "gemini"|"manual", "manual_message": str, "fallbacks": list}}
agents = {}           # {(sid, platform): {"running": bool, "stop_event": Event, "thread": Thread}}

YT_VIDEO_PATTERNS = [
    r"(?:v=|/live/|/embed/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
    r"^([A-Za-z0-9_-]{11})$",
]

KICK_SLUG_PATTERNS = [
    r"(?:https?://)?(?:www\.|m\.)?kick\.com/([A-Za-z0-9_-]+)/?(?:[?#].*)?$",
    r"^([A-Za-z0-9_-]+)$",
]


def normalize_platform(raw):
    val = (raw or "youtube").strip().lower()
    return val if val in ("youtube", "kick") else "youtube"


def normalize_mode(raw):
    val = (raw or "gemini").strip().lower()
    return val if val in ("gemini", "manual") else "gemini"


def extract_yt_video_id(raw_value):
    val = (raw_value or "").strip()
    if not val:
        return ""
    for pattern in YT_VIDEO_PATTERNS:
        match = re.search(pattern, val)
        if match:
            return match.group(1)
    return val


def extract_kick_slug(raw_value):
    val = (raw_value or "").strip()
    if not val:
        return ""
    for pattern in KICK_SLUG_PATTERNS:
        match = re.match(pattern, val, re.IGNORECASE)
        if match:
            slug = match.group(1).strip("/")
            if slug.lower() not in ("category", "categories", "video", "videos", "clip", "clips", "search"):
                return slug.lower()
    return val.lower()


def get_or_create_sid():
    sid = session.get("sid")
    if not sid:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    with state_lock:
        for plat in ("youtube", "kick"):
            session_logs.setdefault((sid, plat), [])
            session_configs.setdefault((sid, plat), {
                "mode": "gemini",
                "manual_message": "",
                "fallbacks": ["Great stream today!", "Loving the content so far!", "Keep it up!"],
            })
    return sid


def add_log(msg, sid, platform="youtube"):
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    key = (sid, normalize_platform(platform))
    with state_lock:
        session_logs.setdefault(key, [])
        session_logs[key].append(line)
        session_logs[key] = session_logs[key][-100:]


def base_https_url():
    base = request.url_root.rstrip("/")
    if base.startswith("http://") and "onrender.com" in base:
        base = "https://" + base[len("http://"):]
    return base


def yt_callback_url():
    configured = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return f"{base_https_url()}/oauth2callback"


def kick_callback_url():
    configured = os.environ.get("KICK_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return f"{base_https_url()}/kick/oauth2callback"


# --- YouTube Helpers ---
def build_yt_oauth_flow(state=None):
    if not GOOGLE_CLIENT_CONFIG_JSON:
        raise RuntimeError("GOOGLE_CLIENT_CONFIG_JSON is not configured.")
    client_config = json.loads(GOOGLE_CLIENT_CONFIG_JSON)
    return Flow.from_client_config(
        client_config,
        scopes=YT_SCOPES,
        state=state,
        redirect_uri=yt_callback_url(),
    )


def get_youtube_client(token_json_str, sid):
    if not token_json_str:
        raise RuntimeError("Not authenticated for YouTube. Click 'Authenticate Google' first.")
    token_data = json.loads(token_json_str)
    creds = Credentials.from_authorized_user_info(token_data, YT_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        refreshed_json = creds.to_json()
        with state_lock:
            session_tokens[(sid, "youtube")] = refreshed_json
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)


def get_yt_live_chat_id(youtube, video_id):
    res = youtube.videos().list(part="liveStreamingDetails", id=video_id).execute()
    items = res.get("items", [])
    if not items or "liveStreamingDetails" not in items[0]:
        raise ValueError("YouTube live stream not found or not currently live.")
    chat_id = items[0]["liveStreamingDetails"].get("activeLiveChatId")
    if not chat_id:
        raise ValueError("No active live chat ID found for this YouTube stream.")
    return chat_id


def get_yt_recent_messages(youtube, live_chat_id, limit=15):
    res = (
        youtube.liveChatMessages()
        .list(liveChatId=live_chat_id, part="snippet,authorDetails", maxResults=50)
        .execute()
    )
    lines = []
    for item in res.get("items", []):
        snippet = item.get("snippet", {})
        if snippet.get("type") == "textMessageEvent":
            author = item.get("authorDetails", {}).get("displayName", "Viewer")
            text = snippet.get("textMessageDetails", {}).get("messageText", "").strip()
            if text:
                lines.append(f"{author}: {text}")
    return lines[-limit:]


def send_yt_chat_message(youtube, live_chat_id, text):
    clean_text = " ".join((text or "").split()).strip()[:200] or "Great stream today!"
    youtube.liveChatMessages().insert(
        part="snippet",
        body={
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": clean_text},
            }
        },
    ).execute()
    return clean_text


# --- Kick Helpers ---
def generate_pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return verifier, challenge


def get_kick_access_token(token_json_str, sid):
    if not token_json_str:
        raise RuntimeError("Not authenticated for Kick. Click 'Authenticate Kick' first.")
    data = json.loads(token_json_str)
    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expires_at = float(data.get("expires_at", 0))

    if access_token and expires_at and time.time() > (expires_at - 60) and refresh_token and KICK_CLIENT_ID and KICK_CLIENT_SECRET:
        resp = requests.post(
            KICK_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": KICK_CLIENT_ID,
                "client_secret": KICK_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
            timeout=20,
        )
        if resp.ok:
            tok = resp.json()
            updated = {
                "access_token": tok.get("access_token", access_token),
                "refresh_token": tok.get("refresh_token", refresh_token),
                "expires_at": time.time() + int(tok.get("expires_in", 3600)),
            }
            with state_lock:
                session_tokens[(sid, "kick")] = json.dumps(updated)
            return updated["access_token"]

    if not access_token:
        raise RuntimeError("Kick access token missing. Please re-authenticate.")
    return access_token


def get_kick_broadcaster_id(access_token, slug):
    resp = requests.get(
        f"{KICK_API_BASE}/channels",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"slug": slug},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(items, list) and items:
        ch = items[0]
        broadcaster_id = ch.get("broadcaster_user_id") or ch.get("user_id") or ch.get("id")
        if broadcaster_id is not None:
            return broadcaster_id
    if isinstance(payload, dict):
        broadcaster_id = payload.get("broadcaster_user_id") or payload.get("user_id")
        if broadcaster_id is not None:
            return broadcaster_id
    raise ValueError(f"Could not resolve Kick channel '{slug}' to a broadcaster ID.")


def send_kick_chat_message(access_token, broadcaster_user_id, text):
    clean_text = " ".join((text or "").split()).strip()[:500] or "Great stream today!"
    resp = requests.post(
        f"{KICK_API_BASE}/chat",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "broadcaster_user_id": broadcaster_user_id,
            "content": clean_text,
            "type": "user",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return clean_text


# --- Message Selection (Checked Fresh Every Cycle) ---
def generate_gemini_reply(recent_lines, fallbacks, stream_context=""):
    pool = fallbacks if fallbacks else ["Great stream today!"]
    if not gemini_client:
        return random.choice(pool)

    if recent_lines:
        prompt = (
            "Recent messages in a live stream chat:\n"
            + "\n".join(recent_lines)
            + "\n\nWrite one short, natural chat message that fits the conversation."
        )
    else:
        ctx = f" on Kick channel @{stream_context}" if stream_context else " on a live stream"
        prompt = (
            f"You are watching{ctx} where recent chat messages aren't available. "
            "Write one short, natural viewer chat message reacting to the live stream."
        )

    try:
        response = gemini_client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are a casual live stream viewer. "
                    "Reply in 1 short line under 12 words. "
                    "No markdown, no hashtags, never mention AI or bots. "
                    "Output only the message text."
                ),
                max_output_tokens=40,
                temperature=0.8,
            ),
        )
        lines = [
            line.strip().strip('"').strip("'")
            for line in (response.text or "").splitlines()
            if line.strip()
        ]
        return lines[0] if lines else random.choice(pool)
    except Exception:
        return random.choice(pool)


def get_live_message_for_tick(sid, platform, recent_lines=None, stream_context=""):
    key = (sid, normalize_platform(platform))
    with state_lock:
        cfg = dict(session_configs.get(key, {}))
    mode = normalize_mode(cfg.get("mode"))
    fallbacks = cfg.get("fallbacks") or ["Great stream today!"]

    if mode == "manual":
        manual_text = " ".join((cfg.get("manual_message") or "").split()).strip()
        if manual_text:
            return manual_text
        raise ValueError("Manual Chat selected, but Manual Chat Message is empty.")

    return generate_gemini_reply(recent_lines or [], fallbacks, stream_context=stream_context)


# --- Agent Loops ---
def youtube_agent_loop(video_id, interval, token_json_str, sid, stop_evt):
    try:
        add_log("Authenticating with session YouTube token...", sid, "youtube")
        yt = get_youtube_client(token_json_str, sid)
        chat_id = get_yt_live_chat_id(yt, video_id)
        add_log(f"Connected to YouTube chat: {chat_id}", sid, "youtube")

        while not stop_evt.is_set():
            try:
                with state_lock:
                    mode = normalize_mode(session_configs.get((sid, "youtube"), {}).get("mode"))
                recent = get_yt_recent_messages(yt, chat_id, limit=15) if mode == "gemini" else []
                msg = get_live_message_for_tick(sid, "youtube", recent_lines=recent, stream_context=video_id)
                sent_msg = send_yt_chat_message(yt, chat_id, msg)
                add_log(f"[YT | {mode}] Sent: {sent_msg}", sid, "youtube")
            except Exception as exc:
                add_log(f"[YT] Loop error: {exc}", sid, "youtube")

            wait_seconds = max(15, int(interval)) + random.randint(0, 20)
            if stop_evt.wait(timeout=wait_seconds):
                break
    except Exception as exc:
        add_log(f"[YT] Startup error: {exc}", sid, "youtube")
    finally:
        with state_lock:
            if (sid, "youtube") in agents:
                agents[(sid, "youtube")]["running"] = False
        add_log("[YT] Agent stopped.", sid, "youtube")


def kick_agent_loop(slug, interval, token_json_str, sid, stop_evt):
    try:
        add_log("Authenticating with session Kick token...", sid, "kick")
        access_token = get_kick_access_token(token_json_str, sid)
        broadcaster_id = get_kick_broadcaster_id(access_token, slug)
        add_log(f"Connected to Kick channel @{slug} (ID: {broadcaster_id})", sid, "kick")

        while not stop_evt.is_set():
            try:
                current_token = get_kick_access_token(token_json_str, sid)
                with state_lock:
                    mode = normalize_mode(session_configs.get((sid, "kick"), {}).get("mode"))
                msg = get_live_message_for_tick(sid, "kick", recent_lines=[], stream_context=slug)
                sent_msg = send_kick_chat_message(current_token, broadcaster_id, msg)
                add_log(f"[Kick | {mode}] Sent to @{slug}: {sent_msg}", sid, "kick")
            except Exception as exc:
                add_log(f"[Kick] Loop error: {exc}", sid, "kick")

            wait_seconds = max(15, int(interval)) + random.randint(0, 20)
            if stop_evt.wait(timeout=wait_seconds):
                break
    except Exception as exc:
        add_log(f"[Kick] Startup error: {exc}", sid, "kick")
    finally:
        with state_lock:
            if (sid, "kick") in agents:
                agents[(sid, "kick")]["running"] = False
        add_log("[Kick] Agent stopped.", sid, "kick")


HTML_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Live Chat Agent — YouTube & Kick</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --accent: #ef4444;
      --text: #e2e8f0;
      --muted: #94a3b8;
    }
    body.kick-mode {
      --bg: #090d0a;
      --card: #111a13;
      --border: #1e3323;
      --accent: #53fc18;
      --text: #ecfdf5;
      --muted: #86efac;
    }
    body { font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); max-width: 720px; margin: 36px auto; padding: 0 16px; transition: background 0.2s ease, color 0.2s ease; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
    .header-top { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .pill-group { display: inline-flex; background: rgba(0,0,0,0.35); border: 1px solid var(--border); border-radius: 8px; padding: 4px; gap: 6px; margin-top: 6px; }
    .pill-group label { margin: 0; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
    .pill-group input[type="radio"] { display: none; }
    .pill-group label.active-yt { background: #dc2626; color: #ffffff; }
    .pill-group label.active-kick { background: #53fc18; color: #052e16; }
    .pill-group label.active-mode { background: #2563eb; color: #ffffff; }
    label.field-label { display: block; font-size: 13px; color: var(--muted); margin-top: 12px; margin-bottom: 4px; }
    .hint { font-size: 12px; color: var(--muted); opacity: 0.9; margin-top: 5px; line-height: 1.4; }
    input, textarea { width: 100%; box-sizing: border-box; background: rgba(2,6,23,0.75); color: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 10px; font-size: 14px; }
    input:disabled, textarea:disabled { opacity: 0.45; cursor: not-allowed; }
    textarea { min-height: 85px; resize: vertical; }
    .row { display: flex; gap: 10px; margin-top: 16px; align-items: center; flex-wrap: wrap; }
    button, a.btn { padding: 10px 16px; border-radius: 6px; border: none; font-weight: 600; cursor: pointer; text-decoration: none; font-size: 14px; display: inline-block; }
    .authBtn { background: var(--accent); color: #ffffff; }
    body.kick-mode .authBtn { color: #052e16; }
    #startBtn { background: #16a34a; color: white; }
    #stopBtn { background: #dc2626; color: white; }
    #syncBtn { background: #334155; color: white; }
    .status { margin-left: auto; font-size: 13px; font-weight: 600; }
    .auth-badge { font-size: 12px; color: var(--muted); margin: 8px 0 10px 0; }
    pre { background: #020617; border: 1px solid var(--border); border-radius: 8px; padding: 12px; height: 220px; overflow-y: auto; font-size: 12px; color: #cbd5e1; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="header-top">
      <h2 id="pageTitle" style="margin: 0;">YouTube Live Chat Agent</h2>
      <div class="pill-group" role="radiogroup" aria-label="Select Platform" style="margin-top: 0;">
        <label id="lblYt" class="active-yt">
          <input type="radio" name="platform" value="youtube" checked onchange="onPlatformChange()" />
          YouTube
        </label>
        <label id="lblKick">
          <input type="radio" name="platform" value="kick" onchange="onPlatformChange()" />
          Kick
        </label>
      </div>
    </div>

    <div id="authBadge" class="auth-badge">Auth: Checking...</div>
    <a id="authLink" class="btn authBtn" href="/auth">Authenticate / Re-auth Google</a>

    <label id="targetLabel" class="field-label" for="targetInput">YouTube Stream Link or Video ID</label>
    <input id="targetInput" type="text" autocomplete="off" placeholder="Paste link from Share or browser, or enter Video ID" />
    <div id="targetHint" class="hint">Paste directly from the YouTube mobile app's <strong>Share</strong> button (<code>youtu.be/...</code> or <code>.../live/...</code>) or from your <strong>browser</strong> address bar (<code>youtube.com/watch?v=...</code>).</div>

    <label class="field-label">Chat Message Source (checked before every message)</label>
    <div class="pill-group" role="radiogroup" aria-label="Select Chat Message Mode">
      <label id="lblModeGemini" class="active-mode">
        <input type="radio" name="chatMode" value="gemini" checked onchange="onModeChange()" />
        Gemini Chat
      </label>
      <label id="lblModeManual">
        <input type="radio" name="chatMode" value="manual" onchange="onModeChange()" />
        Manual Chat
      </label>
    </div>

    <label class="field-label" for="manualMsg">Manual Chat Message</label>
    <input id="manualMsg" type="text" autocomplete="off" placeholder="Type the exact message to send in Manual Chat mode" disabled />
    <div class="hint">Disabled when <strong>Gemini Chat</strong> is selected. When <strong>Manual Chat</strong> is selected, you can edit this text anytime and the next scheduled message will use the updated text.</div>

    <label class="field-label" for="interval">Interval (seconds)</label>
    <input id="interval" type="number" min="15" value="300" />

    <label class="field-label" for="fallbacks">Fallback Messages (used in Gemini Chat if chat is quiet or Gemini is unavailable)</label>
    <textarea id="fallbacks">Great stream today!
Loving the content so far!
Keep it up!</textarea>

    <div class="row">
      <button id="startBtn" onclick="startAgent()">Start</button>
      <button id="stopBtn" onclick="stopAgent()">Stop</button>
      <button id="syncBtn" type="button" onclick="syncConfigLive(true)">Apply Message Now</button>
      <span id="statusText" class="status">Status: Stopped</span>
    </div>
  </div>

  <div class="card">
    <div id="logTitle" style="font-size: 13px; color: var(--muted); margin-bottom: 8px;">YouTube Activity Log</div>
    <pre id="logs"></pre>
  </div>

  <script>
    const DEFAULT_FALLBACKS_TEXT = "Great stream today!\\nLoving the content so far!\\nKeep it up!";
    const STORAGE_KEY = "yt_kick_platform_forms_v1";
    const PLATFORM_STORAGE_KEY = "yt_kick_active_platform_v1";

    function createDefaultForms() {
      return {
        youtube: {
          target: "",
          interval: 300,
          mode: "gemini",
          manual_message: "",
          fallbacksText: DEFAULT_FALLBACKS_TEXT,
        },
        kick: {
          target: "",
          interval: 300,
          mode: "gemini",
          manual_message: "",
          fallbacksText: DEFAULT_FALLBACKS_TEXT,
        },
      };
    }

    function loadSavedForms() {
      const base = createDefaultForms();
      try {
        const raw = sessionStorage.getItem(STORAGE_KEY);
        if (!raw) return base;
        const parsed = JSON.parse(raw);
        for (const plat of ["youtube", "kick"]) {
          if (parsed && typeof parsed[plat] === "object" && parsed[plat] !== null) {
            base[plat] = {
              target: typeof parsed[plat].target === "string" ? parsed[plat].target : "",
              interval: Number(parsed[plat].interval) || 300,
              mode: parsed[plat].mode === "manual" ? "manual" : "gemini",
              manual_message: typeof parsed[plat].manual_message === "string" ? parsed[plat].manual_message : "",
              fallbacksText: typeof parsed[plat].fallbacksText === "string" ? parsed[plat].fallbacksText : DEFAULT_FALLBACKS_TEXT,
            };
          }
        }
      } catch (_) {}
      return base;
    }

    const platformForms = loadSavedForms();
    let activePlatform = sessionStorage.getItem(PLATFORM_STORAGE_KEY) === "kick" ? "kick" : "youtube";

    function persistForms() {
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(platformForms));
        sessionStorage.setItem(PLATFORM_STORAGE_KEY, activePlatform);
      } catch (_) {}
    }

    function getPlatform() {
      const checked = document.querySelector('input[name="platform"]:checked');
      return checked ? checked.value : "youtube";
    }

    function getChatMode() {
      const checked = document.querySelector('input[name="chatMode"]:checked');
      return checked ? checked.value : "gemini";
    }

    function setChatModeRadio(mode) {
      const targetVal = mode === "manual" ? "manual" : "gemini";
      const radio = document.querySelector(`input[name="chatMode"][value="${targetVal}"]`);
      if (radio) radio.checked = true;
      applyModeState();
    }

    function saveCurrentFormToState(plat = activePlatform) {
      platformForms[plat] = {
        target: document.getElementById("targetInput").value,
        interval: parseInt(document.getElementById("interval").value, 10) || 300,
        mode: getChatMode(),
        manual_message: document.getElementById("manualMsg").value,
        fallbacksText: document.getElementById("fallbacks").value,
      };
      persistForms();
    }

    function loadStateIntoForm(plat) {
      const st = platformForms[plat];
      document.getElementById("targetInput").value = st.target || "";
      document.getElementById("interval").value = st.interval || 300;
      document.getElementById("manualMsg").value = st.manual_message || "";
      document.getElementById("fallbacks").value =
        typeof st.fallbacksText === "string" ? st.fallbacksText : DEFAULT_FALLBACKS_TEXT;
      setChatModeRadio(st.mode || "gemini");
    }

    function applyModeState() {
      const mode = getChatMode();
      const isManual = mode === "manual";
      document.getElementById("lblModeGemini").classList.toggle("active-mode", !isManual);
      document.getElementById("lblModeManual").classList.toggle("active-mode", isManual);
      document.getElementById("manualMsg").disabled = !isManual;
    }

    function applyPlatformTheme(plat) {
      const isKick = plat === "kick";
      document.body.classList.toggle("kick-mode", isKick);
      document.getElementById("lblYt").classList.toggle("active-yt", !isKick);
      document.getElementById("lblKick").classList.toggle("active-kick", isKick);

      document.getElementById("pageTitle").textContent = isKick
        ? "Kick Live Chat Agent"
        : "YouTube Live Chat Agent";

      const authLink = document.getElementById("authLink");
      authLink.href = isKick ? "/kick/auth" : "/auth";
      authLink.textContent = isKick
        ? "Authenticate / Re-auth Kick"
        : "Authenticate / Re-auth Google";

      const targetLabel = document.getElementById("targetLabel");
      const targetInput = document.getElementById("targetInput");
      const targetHint = document.getElementById("targetHint");

      if (isKick) {
        targetLabel.textContent = "Kick Channel Link or Channel Slug";
        targetInput.placeholder = "Paste kick.com link from Share or browser, or enter channel name";
        targetHint.innerHTML = "Paste directly from the Kick mobile app's <strong>Share</strong> button or your <strong>browser</strong> (<code>https://kick.com/channelname</code> or <code>https://m.kick.com/channelname</code>), or enter the channel slug.";
      } else {
        targetLabel.textContent = "YouTube Stream Link or Video ID";
        targetInput.placeholder = "Paste link from Share or browser, or enter Video ID";
        targetHint.innerHTML = "Paste directly from the YouTube mobile app's <strong>Share</strong> button (<code>youtu.be/...</code> or <code>.../live/...</code>) or from your <strong>browser</strong> address bar (<code>youtube.com/watch?v=...</code>).";
      }

      document.getElementById("logTitle").textContent = isKick
        ? "Kick Activity Log"
        : "YouTube Activity Log";
    }

    async function syncConfigLive(showToast = false) {
      const platform = getPlatform();
      saveCurrentFormToState(platform);
      const st = platformForms[platform];
      const fallback_messages = st.fallbacksText
        .split("\\n").map(s => s.trim()).filter(Boolean);

      const res = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform,
          mode: st.mode,
          manual_message: st.manual_message,
          fallback_messages,
        })
      });
      if (showToast && res.ok) {
        refreshStatus();
      }
    }

    function onFieldInput() {
      saveCurrentFormToState(getPlatform());
      syncConfigLive();
    }

    function onModeChange() {
      applyModeState();
      saveCurrentFormToState(getPlatform());
      syncConfigLive();
    }

    function onPlatformChange() {
      saveCurrentFormToState(activePlatform);
      const nextPlatform = getPlatform();
      activePlatform = nextPlatform;
      persistForms();
      applyPlatformTheme(nextPlatform);
      loadStateIntoForm(nextPlatform);
      refreshStatus();
    }

    async function startAgent() {
      const platform = getPlatform();
      saveCurrentFormToState(platform);
      const st = platformForms[platform];
      const fallback_messages = st.fallbacksText
        .split("\\n").map(s => s.trim()).filter(Boolean);

      const res = await fetch("/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform,
          target: st.target.trim(),
          interval: st.interval,
          mode: st.mode,
          manual_message: st.manual_message,
          fallback_messages,
        })
      });
      const data = await res.json();
      if (!res.ok) alert(data.error || "Failed to start");
      refreshStatus();
    }

    async function stopAgent() {
      const platform = getPlatform();
      await fetch("/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform })
      });
      refreshStatus();
    }

    async function refreshStatus() {
      const platform = getPlatform();
      const res = await fetch("/status?platform=" + encodeURIComponent(platform));
      const data = await res.json();

      const st = document.getElementById("statusText");
      st.textContent = data.running ? "Status: Running" : "Status: Stopped";
      st.style.color = data.running ? "#4ade80" : "#f87171";

      const ab = document.getElementById("authBadge");
      if (data.authenticated) {
        ab.textContent = platform === "kick"
          ? "Kick Auth: Ready (session token active)"
          : "YouTube Auth: Ready (session token active)";
        ab.style.color = "#4ade80";
      } else {
        ab.textContent = platform === "kick"
          ? "Kick Auth: Required (click Authenticate / Re-auth Kick)"
          : "YouTube Auth: Required (click Authenticate / Re-auth Google)";
        ab.style.color = "#fbbf24";
      }

      const logBox = document.getElementById("logs");
      logBox.textContent = (data.logs || []).join("\\n");
      logBox.scrollTop = logBox.scrollHeight;
    }

    document.getElementById("targetInput").addEventListener("input", () => saveCurrentFormToState(getPlatform()));
    document.getElementById("interval").addEventListener("input", () => saveCurrentFormToState(getPlatform()));
    document.getElementById("manualMsg").addEventListener("input", onFieldInput);
    document.getElementById("fallbacks").addEventListener("input", onFieldInput);

    const initialRadio = document.querySelector(`input[name="platform"][value="${activePlatform}"]`);
    if (initialRadio) initialRadio.checked = true;

    applyPlatformTheme(activePlatform);
    loadStateIntoForm(activePlatform);
    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    get_or_create_sid()
    return render_template_string(HTML_PAGE)


# --- YouTube OAuth Routes ---
@app.route("/auth")
def yt_auth():
    get_or_create_sid()
    flow = build_yt_oauth_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["yt_oauth_state"] = state
    session["yt_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth2callback")
def yt_oauth2callback():
    sid = get_or_create_sid()
    state = session.get("yt_oauth_state")
    flow = build_yt_oauth_flow(state=state)
    flow.code_verifier = session.get("yt_code_verifier")

    auth_response = request.url
    if "onrender.com" in auth_response and auth_response.startswith("http://"):
        auth_response = "https://" + auth_response[len("http://"):]

    flow.fetch_token(authorization_response=auth_response)
    creds = flow.credentials
    with state_lock:
        session_tokens[(sid, "youtube")] = creds.to_json()
    session.pop("yt_code_verifier", None)
    add_log("Generated new session YouTube token via Google OAuth.", sid, "youtube")
    return redirect("/")


# --- Kick OAuth Routes ---
@app.route("/kick/auth")
def kick_auth():
    sid = get_or_create_sid()
    if not KICK_CLIENT_ID:
        add_log("Missing KICK_CLIENT_ID environment variable.", sid, "kick")
        return redirect("/")

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    session["kick_oauth_state"] = state
    session["kick_code_verifier"] = verifier

    params = urlencode({
        "response_type": "code",
        "client_id": KICK_CLIENT_ID,
        "redirect_uri": kick_callback_url(),
        "scope": " ".join(KICK_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return redirect(f"{KICK_AUTH_URL}?{params}")


@app.route("/kick/oauth2callback")
def kick_oauth2callback():
    sid = get_or_create_sid()
    code = request.args.get("code")
    state = request.args.get("state")
    expected_state = session.get("kick_oauth_state")
    verifier = session.get("kick_code_verifier")

    if not code or not state or state != expected_state or not verifier:
        add_log("Kick OAuth failed: invalid state or missing code/verifier.", sid, "kick")
        return redirect("/")

    resp = requests.post(
        KICK_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": KICK_CLIENT_ID,
            "client_secret": KICK_CLIENT_SECRET,
            "redirect_uri": kick_callback_url(),
            "code": code,
            "code_verifier": verifier,
        },
        timeout=20,
    )
    session.pop("kick_code_verifier", None)
    session.pop("kick_oauth_state", None)

    if not resp.ok:
        add_log(f"Kick token exchange failed ({resp.status_code}): {resp.text}", sid, "kick")
        return redirect("/")

    tok = resp.json()
    token_payload = json.dumps({
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
    })
    with state_lock:
        session_tokens[(sid, "kick")] = token_payload
    add_log("Generated new session Kick token via OAuth 2.1 PKCE.", sid, "kick")
    return redirect("/")


# --- Live Config / Status / Start / Stop ---
@app.route("/config", methods=["POST"])
def update_config():
    sid = get_or_create_sid()
    payload = request.get_json(force=True) or {}
    platform = normalize_platform(payload.get("platform"))
    key = (sid, platform)

    with state_lock:
        cfg = session_configs.setdefault(key, {
            "mode": "gemini",
            "manual_message": "",
            "fallbacks": ["Great stream today!", "Loving the content so far!", "Keep it up!"],
        })
        if "mode" in payload:
            cfg["mode"] = normalize_mode(payload.get("mode"))
        if "manual_message" in payload:
            cfg["manual_message"] = payload.get("manual_message") or ""
        if "fallback_messages" in payload and isinstance(payload.get("fallback_messages"), list):
            cfg["fallbacks"] = payload.get("fallback_messages")

    return jsonify({"ok": True, "mode": cfg["mode"]})


@app.route("/status")
def status():
    sid = get_or_create_sid()
    platform = normalize_platform(request.args.get("platform"))
    key = (sid, platform)
    with state_lock:
        has_token = bool(session_tokens.get(key))
        running = bool(agents.get(key, {}).get("running", False))
        logs = list(session_logs.get(key, []))
        cfg = dict(session_configs.get(key, {}))
    return jsonify({
        "platform": platform,
        "running": running,
        "authenticated": has_token,
        "mode": cfg.get("mode", "gemini"),
        "manual_message": cfg.get("manual_message", ""),
        "fallback_messages": cfg.get("fallbacks", ["Great stream today!", "Loving the content so far!", "Keep it up!"]),
        "logs": logs,
    })


@app.route("/start", methods=["POST"])
def start():
    sid = get_or_create_sid()
    payload = request.get_json(force=True) or {}
    platform = normalize_platform(payload.get("platform"))
    raw_target = (payload.get("target") or payload.get("video_id") or "").strip()
    interval = int(payload.get("interval") or 300)
    mode = normalize_mode(payload.get("mode"))
    manual_message = payload.get("manual_message") or ""
    fallbacks = payload.get("fallback_messages") or []

    if not raw_target:
        return jsonify({"error": "Stream link, channel link, or ID is required."}), 400
    if mode == "manual" and not manual_message.strip():
        return jsonify({"error": "Please enter a Manual Chat Message when Manual Chat is selected."}), 400

    key = (sid, platform)

    if platform == "youtube":
        target_id = extract_yt_video_id(raw_target)
        loop_fn = youtube_agent_loop
    else:
        target_id = extract_kick_slug(raw_target)
        loop_fn = kick_agent_loop

    with state_lock:
        token_json = session_tokens.get(key, "")
        if not token_json:
            return jsonify({"error": f"Please authenticate for {platform.capitalize()} first."}), 400
        if agents.get(key, {}).get("running", False):
            return jsonify({"error": f"{platform.capitalize()} agent is already running for this session."}), 400

        session_configs[key] = {
            "mode": mode,
            "manual_message": manual_message,
            "fallbacks": fallbacks,
        }
        session_logs[key] = []
        stop_evt = threading.Event()
        thread = threading.Thread(
            target=loop_fn,
            args=(target_id, interval, token_json, sid, stop_evt),
            daemon=True,
        )
        agents[key] = {
            "running": True,
            "stop_event": stop_evt,
            "thread": thread,
        }

    thread.start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    sid = get_or_create_sid()
    payload = request.get_json(silent=True) or {}
    platform = normalize_platform(payload.get("platform"))
    key = (sid, platform)

    with state_lock:
        agent_info = agents.get(key)
        if agent_info:
            agent_info["stop_event"].set()
    add_log("Stop requested...", sid, platform)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)