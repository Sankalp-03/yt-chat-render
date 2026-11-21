import os
import json
import time
import random
import threading
from flask import Flask, request, jsonify, render_template_string
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import googleapiclient.discovery
from google import genai
from google.genai import types

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
MODEL_ID = "gemini-3.5-flash-lite"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Paste the full JSON string of your authorized OAuth token into YOUTUBE_TOKEN_JSON env var
YOUTUBE_TOKEN_JSON = os.environ.get("YOUTUBE_TOKEN_JSON", "")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

state_lock = threading.Lock()
stop_event = threading.Event()
worker_thread = None

agent_state = {
    "running": False,
    "video_id": "",
    "interval": 300,
    "fallback_messages": [
        "Great stream today!",
        "Loving the content so far!",
        "Keep it up!",
    ],
    "logs": [],
}


def add_log(msg):
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    with state_lock:
        agent_state["logs"].append(line)
        agent_state["logs"] = agent_state["logs"][-100:]


def get_youtube_client():
    if not YOUTUBE_TOKEN_JSON:
        raise RuntimeError("YOUTUBE_TOKEN_JSON environment variable is not set.")
    token_data = json.loads(YOUTUBE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)


def get_live_chat_id(youtube, video_id):
    res = youtube.videos().list(part="liveStreamingDetails", id=video_id).execute()
    items = res.get("items", [])
    if not items or "liveStreamingDetails" not in items[0]:
        raise ValueError("Live stream not found or not currently live.")
    chat_id = items[0]["liveStreamingDetails"].get("activeLiveChatId")
    if not chat_id:
        raise ValueError("No active live chat ID found for this stream.")
    return chat_id


def get_recent_messages(youtube, live_chat_id, limit=15):
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
            text = (
                snippet.get("textMessageDetails", {})
                .get("messageText", "")
                .strip()
            )
            if text:
                lines.append(f"{author}: {text}")
    return lines[-limit:]


def generate_reply(recent_lines, fallbacks):
    pool = fallbacks if fallbacks else ["Great stream today!"]
    if not recent_lines or not gemini_client:
        return random.choice(pool)

    prompt = (
        "Recent messages in a live stream chat:\n"
        + "\n".join(recent_lines)
        + "\n\nWrite one short, natural chat message that fits the conversation."
    )
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


def send_chat_message(youtube, live_chat_id, text):
    youtube.liveChatMessages().insert(
        part="snippet",
        body={
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": text},
            }
        },
    ).execute()


def agent_loop(video_id, interval, fallbacks):
    try:
        add_log("Authenticating with YouTube...")
        yt = get_youtube_client()
        chat_id = get_live_chat_id(yt, video_id)
        add_log(f"Connected to chat: {chat_id}")

        while not stop_event.is_set():
            try:
                recent = get_recent_messages(yt, chat_id, limit=15)
                msg = generate_reply(recent, fallbacks)
                send_chat_message(yt, chat_id, msg)
                add_log(f"Sent: {msg}")
            except Exception as exc:
                add_log(f"Loop error: {exc}")

            wait_seconds = max(10, int(interval)) + random.randint(0, 20)
            if stop_event.wait(timeout=wait_seconds):
                break
    except Exception as exc:
        add_log(f"Startup error: {exc}")
    finally:
        with state_lock:
            agent_state["running"] = False
        add_log("Agent stopped.")


HTML_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>YT Chat Agent</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; max-width: 680px; margin: 40px auto; padding: 0 16px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 20px; margin-bottom: 16px; }
    label { display: block; font-size: 13px; color: #94a3b8; margin-top: 12px; margin-bottom: 4px; }
    input, textarea { width: 100%; box-sizing: border-box; background: #0f172a; color: #f8fafc; border: 1px solid #475569; border-radius: 6px; padding: 10px; font-size: 14px; }
    textarea { min-height: 90px; resize: vertical; }
    .row { display: flex; gap: 10px; margin-top: 16px; align-items: center; }
    button { padding: 10px 18px; border-radius: 6px; border: none; font-weight: 600; cursor: pointer; }
    #startBtn { background: #16a34a; color: white; }
    #stopBtn { background: #dc2626; color: white; }
    .status { margin-left: auto; font-size: 13px; font-weight: 600; }
    pre { background: #020617; border: 1px solid #1e293b; border-radius: 8px; padding: 12px; height: 220px; overflow-y: auto; font-size: 12px; color: #cbd5e1; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <h2 style="margin: 0 0 8px 0;">YouTube Live Chat Agent</h2>
    <label for="videoId">YouTube Video ID</label>
    <input id="videoId" type="text" placeholder="e.g. Ysc9P6jAOfk" />

    <label for="interval">Interval (seconds)</label>
    <input id="interval" type="number" min="15" value="300" />

    <label for="fallbacks">Fallback Messages (one per line)</label>
    <textarea id="fallbacks">Great stream today!
Loving the content so far!
Keep it up!</textarea>

    <div class="row">
      <button id="startBtn" onclick="startAgent()">Start</button>
      <button id="stopBtn" onclick="stopAgent()">Stop</button>
      <span id="statusText" class="status">Status: Stopped</span>
    </div>
  </div>

  <div class="card">
    <div style="font-size: 13px; color: #94a3b8; margin-bottom: 8px;">Activity Log</div>
    <pre id="logs"></pre>
  </div>

  <script>
    async function startAgent() {
      const video_id = document.getElementById("videoId").value.trim();
      const interval = parseInt(document.getElementById("interval").value, 10) || 300;
      const fallback_messages = document.getElementById("fallbacks").value
        .split("\\n").map(s => s.trim()).filter(Boolean);

      const res = await fetch("/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_id, interval, fallback_messages })
      });
      const data = await res.json();
      if (!res.ok) alert(data.error || "Failed to start");
      refreshStatus();
    }

    async function stopAgent() {
      await fetch("/stop", { method: "POST" });
      refreshStatus();
    }

    async function refreshStatus() {
      const res = await fetch("/status");
      const data = await res.json();
      const st = document.getElementById("statusText");
      st.textContent = data.running ? "Status: Running" : "Status: Stopped";
      st.style.color = data.running ? "#4ade80" : "#f87171";
      const logBox = document.getElementById("logs");
      logBox.textContent = (data.logs || []).join("\\n");
      logBox.scrollTop = logBox.scrollHeight;
    }

    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/status")
def status():
    with state_lock:
        return jsonify({
            "running": agent_state["running"],
            "logs": list(agent_state["logs"]),
        })


@app.route("/start", methods=["POST"])
def start():
    global worker_thread
    payload = request.get_json(force=True) or {}
    video_id = (payload.get("video_id") or "").strip()
    interval = int(payload.get("interval") or 300)
    fallbacks = payload.get("fallback_messages") or []

    if not video_id:
        return jsonify({"error": "YouTube Video ID is required."}), 400

    with state_lock:
        if agent_state["running"]:
            return jsonify({"error": "Agent is already running."}), 400
        agent_state["video_id"] = video_id
        agent_state["interval"] = interval
        agent_state["fallback_messages"] = fallbacks
        agent_state["running"] = True
        stop_event.clear()

    worker_thread = threading.Thread(
        target=agent_loop,
        args=(video_id, interval, fallbacks),
        daemon=True,
    )
    worker_thread.start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    stop_event.set()
    add_log("Stop requested...")
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)