"""Stage 1: Google sign-in + PS5 compatibility test page. Nothing is faked:
every button returns the raw YouTube API response or the raw error."""
import os, io, json
from flask import Flask, session, redirect, request, jsonify, render_template_string
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
import requests as rq

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
fernet = Fernet(os.environ["FERNET_KEY"])
BASE = os.environ["BASE_URL"].rstrip("/")
OWNER = os.environ["OWNER_EMAIL"].lower()
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
          "https://www.googleapis.com/auth/youtube"]
CFG = {"web": {"client_id": os.environ["GOOGLE_CLIENT_ID"],
               "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
               "auth_uri": "https://accounts.google.com/o/oauth2/auth",
               "token_uri": "https://oauth2.googleapis.com/token"}}

def flow():
    return Flow.from_client_config(CFG, SCOPES, redirect_uri=BASE + "/oauth2callback",
                                   autogenerate_code_verifier=False)

def yt():
    t = session.get("t")
    if not t:
        return None
    d = json.loads(fernet.decrypt(t.encode()))
    c = Credentials(d["token"], refresh_token=d["refresh_token"], token_uri=CFG["web"]["token_uri"],
                    client_id=CFG["web"]["client_id"], client_secret=CFG["web"]["client_secret"], scopes=SCOPES)
    return build("youtube", "v3", credentials=c, cache_discovery=False)

def run(fn):
    try:
        return jsonify(ok=True, result=fn())
    except HttpError as e:
        return jsonify(ok=False, status=e.resp.status, error=e.content.decode(errors="replace")), 200
    except Exception as e:
        return jsonify(ok=False, error=repr(e)), 200

@app.get("/login")
def login():
    url, state = flow().authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    session["state"] = state
    return redirect(url)

@app.get("/oauth2callback")
def callback():
    f = flow()
    f.fetch_token(authorization_response=request.url.replace("http://", "https://"))
    c = f.credentials
    email = rq.get("https://www.googleapis.com/oauth2/v3/userinfo",
                   headers={"Authorization": f"Bearer {c.token}"}, timeout=10).json().get("email", "").lower()
    if email != OWNER:
        session.clear()
        return "This app is private.", 403
    if not c.refresh_token:
        return "No refresh token returned. Revoke app access in your Google account and sign in again.", 400
    session["t"] = fernet.encrypt(json.dumps({"token": c.token, "refresh_token": c.refresh_token}).encode()).decode()
    return redirect("/debug")

@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.get("/")
def home():
    return render_template_string(PAGE, signed_in=bool(session.get("t")), debug=False)

@app.get("/debug")
def debug():
    if not session.get("t"):
        return redirect("/")
    return render_template_string(PAGE, signed_in=True, debug=True)

def active_broadcast(y):
    r = y.liveBroadcasts().list(part="id,snippet,status,contentDetails", broadcastStatus="active", maxResults=5).execute()
    return r, (r.get("items") or [None])[0]

@app.post("/api/detect")
def detect():
    def f():
        y = yt()
        r, b = active_broadcast(y)
        # Diagnostic: also list recent broadcasts so we can see how PS5 streams are classified
        allb = y.liveBroadcasts().list(part="id,snippet,status", mine=True, maxResults=5).execute()
        return {"active_found": bool(b), "active_response": r,
                "recent_broadcasts_diagnostic": [
                    {"id": i["id"], "title": i["snippet"]["title"], "lifeCycleStatus": i["status"]["lifeCycleStatus"]}
                    for i in allb.get("items", [])]}
    return run(f)

@app.post("/api/video-id")
def video_id():
    def f():
        _, b = active_broadcast(yt())
        if not b: raise Exception("No active broadcast")
        v = yt().videos().list(part="id,snippet,liveStreamingDetails,status", id=b["id"]).execute()
        return {"broadcast_id": b["id"], "video_found_with_same_id": bool(v.get("items")), "video": v}
    return run(f)

@app.post("/api/title")
def title():
    new = (request.json or {}).get("title", "").strip()[:100]
    def f():
        if not new: raise Exception("Enter a title")
        y = yt(); _, b = active_broadcast(y)
        if not b: raise Exception("No active broadcast")
        sn = b["snippet"]
        body = {"id": b["id"], "snippet": {"title": new, "description": sn.get("description", ""),
                                           "scheduledStartTime": sn["scheduledStartTime"]}}
        return y.liveBroadcasts().update(part="snippet", body=body).execute()
    return run(f)

@app.post("/api/thumbnail")
def thumb():
    def f():
        file = request.files.get("file")
        if not file: raise Exception("Choose an image")
        y = yt(); _, b = active_broadcast(y)
        if not b: raise Exception("No active broadcast")
        m = MediaIoBaseUpload(io.BytesIO(file.read()), mimetype=file.mimetype or "image/jpeg")
        return y.thumbnails().set(videoId=b["id"], media_body=m).execute()
    return run(f)

@app.post("/api/playlist")
def playlist():
    name = (request.json or {}).get("name", "").strip()
    def f():
        if not name: raise Exception("Enter a playlist name")
        y = yt(); _, b = active_broadcast(y)
        if not b: raise Exception("No active broadcast")
        pid, created, tok = None, False, None
        while not pid:
            r = y.playlists().list(part="id,snippet", mine=True, maxResults=50, pageToken=tok).execute()
            pid = next((p["id"] for p in r["items"] if p["snippet"]["title"] == name), None)
            tok = r.get("nextPageToken")
            if not tok: break
        if not pid:
            pid = y.playlists().insert(part="snippet,status", body={"snippet": {"title": name},
                  "status": {"privacyStatus": "public"}}).execute()["id"]
            created = True
        tok, dup = None, False
        while True:
            r = y.playlistItems().list(part="snippet", playlistId=pid, maxResults=50, pageToken=tok).execute()
            if any(i["snippet"]["resourceId"]["videoId"] == b["id"] for i in r["items"]): dup = True; break
            tok = r.get("nextPageToken")
            if not tok: break
        if dup: return {"playlist_id": pid, "created": created, "already_in_playlist": True}
        item = y.playlistItems().insert(part="snippet", body={"snippet": {"playlistId": pid,
               "resourceId": {"kind": "youtube#video", "videoId": b["id"]}}}).execute()
        return {"playlist_id": pid, "created": created, "already_in_playlist": False, "item": item}
    return run(f)

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>PS5 Stream Manager</title>
<style>:root{color-scheme:dark}body{font-family:system-ui;background:#0e1016;color:#e8eaf0;max-width:640px;margin:0 auto;padding:20px}
h1{font-size:1.3rem}button,a.b{display:block;width:100%;box-sizing:border-box;text-align:center;background:#6c5ce7;color:#fff;border:0;
border-radius:12px;padding:14px;margin:8px 0;font-size:1rem;text-decoration:none}input{width:100%;box-sizing:border-box;padding:12px;
border-radius:10px;border:1px solid #333;background:#181b25;color:#fff;margin:6px 0}pre{background:#181b25;padding:12px;border-radius:10px;
overflow:auto;font-size:.75rem;white-space:pre-wrap;word-break:break-word}.s{color:#9aa}</style>
<h1>PS5 Stream Manager · Stage 1</h1>
{% if not signed_in %}<a class=b href=/login>Sign in with Google</a>
{% elif not debug %}<a class=b href=/debug>Open compatibility test</a><a class=b href=/logout>Sign out</a>
{% else %}<p class=s>Start a stream from the PS5 first, then run steps in order. Raw responses appear below.</p>
<button onclick="go('/api/detect')">1-2. Detect active broadcast</button>
<button onclick="go('/api/video-id')">3. Get video ID</button>
<input id=t placeholder="Test title (e.g. TEST | PS5 Pro Gameplay | Part 1)">
<button onclick="go('/api/title',{title:t.value})">4. Update title</button>
<input id=f type=file accept="image/jpeg,image/png">
<button onclick="up()">5. Upload thumbnail</button>
<input id=p placeholder="Test playlist name">
<button onclick="go('/api/playlist',{name:p.value})">6. Find/create playlist + add video</button>
<pre id=o>Output…</pre>
<script>
async function go(u,b){o.textContent='Working…';const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});o.textContent=JSON.stringify(await r.json(),null,2)}
async function up(){const d=new FormData();d.append('file',f.files[0]);o.textContent='Working…';const r=await fetch('/api/thumbnail',{method:'POST',body:d});o.textContent=JSON.stringify(await r.json(),null,2)}
</script>{% endif %}"""
