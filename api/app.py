"""PS5 Stream Manager - Stage 2: game library + one-tap Apply."""
import os, io, json, time
from functools import wraps
from datetime import timedelta
from flask import Flask, session, redirect, request, jsonify, render_template_string, Response
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
import requests as rq, psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=365))
fernet = Fernet(os.environ["FERNET_KEY"])
BASE = os.environ["BASE_URL"].rstrip("/")
OWNER = os.environ["OWNER_EMAIL"].lower()
DB = os.environ.get("DATABASE_URL") or os.environ["POSTGRES_URL"]
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/youtube"]
CFG = {"web": {"client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
               "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}

SCHEMA = """
create table if not exists games(id serial primary key, name text unique not null, playlist_id text,
  next_part int not null default 1, last_done int not null default 0);
create table if not exists thumbs(game_id int references games(id) on delete cascade, part text not null,
  data bytea not null, primary key(game_id, part));
create table if not exists processed(video_id text primary key, game_id int references games(id) on delete cascade,
  label text, at timestamptz default now());
"""
_ready = False

def conn():
    global _ready
    c = psycopg.connect(DB, row_factory=dict_row, prepare_threshold=None)
    if not _ready:
        c.execute(SCHEMA); c.commit(); _ready = True
    return c

def q(sql, args=(), mode=None):
    with conn() as c:
        cur = c.execute(sql, args)
        if mode == "one": return cur.fetchone()
        if mode == "all": return cur.fetchall()

# ---------- auth ----------
def flow():
    return Flow.from_client_config(CFG, SCOPES, redirect_uri=BASE + "/oauth2callback", autogenerate_code_verifier=False)

def yt():
    d = json.loads(fernet.decrypt(session["t"].encode()))
    c = Credentials(d["token"], refresh_token=d["refresh_token"], token_uri=CFG["web"]["token_uri"],
                    client_id=CFG["web"]["client_id"], client_secret=CFG["web"]["client_secret"], scopes=SCOPES)
    return build("youtube", "v3", credentials=c, cache_discovery=False)

def need(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("t"):
            return (jsonify(ok=False, error="Not signed in"), 401) if request.path.startswith("/api") else redirect("/")
        return f(*a, **k)
    return w

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
    email = rq.get("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {c.token}"},
                   timeout=10).json().get("email", "").lower()
    if email != OWNER:
        session.clear(); return "This app is private.", 403
    if not c.refresh_token:
        return "No refresh token returned. Revoke app access in your Google account and sign in again.", 400
    session.permanent = True
    session["t"] = fernet.encrypt(json.dumps({"token": c.token, "refresh_token": c.refresh_token}).encode()).decode()
    return redirect("/")

@app.get("/logout")
def logout():
    session.clear(); return redirect("/")

# ---------- YouTube helpers ----------
class Fail(Exception): pass

def yerr(e):
    try:
        j = json.loads(e.content)["error"]
        if j.get("errors", [{}])[0].get("reason") in ("quotaExceeded", "dailyLimitExceeded"):
            return "YouTube API quota exceeded, try again tomorrow"
        return f"{e.resp.status}: {j['message']}"
    except Exception:
        return str(e.resp.status)

def mk_title(name, n, final):
    s = " | PS5 Pro Gameplay | " + ("Final Part" if final else f"Part-{n}")
    return name[:100 - len(s)] + s

def get_playlist(y, g):
    if g["playlist_id"]:
        if y.playlists().list(part="id", id=g["playlist_id"]).execute().get("items"):
            return g["playlist_id"]
    tok = None
    while True:
        r = y.playlists().list(part="id,snippet", mine=True, maxResults=50, pageToken=tok).execute()
        for p in r["items"]:
            if p["snippet"]["title"] == g["name"]: return p["id"]
        tok = r.get("nextPageToken")
        if not tok: break
    return y.playlists().insert(part="snippet,status", body={"snippet": {"title": g["name"]},
                                "status": {"privacyStatus": "public"}}).execute()["id"]

def add_item(y, pid, vid):
    tok = None
    try:
        while True:
            r = y.playlistItems().list(part="snippet", playlistId=pid, maxResults=50, pageToken=tok).execute()
            if any(i["snippet"]["resourceId"]["videoId"] == vid for i in r["items"]): return "duplicate"
            tok = r.get("nextPageToken")
            if not tok: break
    except HttpError as e:
        if e.resp.status != 404: raise
    body = {"snippet": {"playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}
    for a in range(4):
        try:
            return y.playlistItems().insert(part="snippet", body=body).execute()
        except HttpError as e:
            if e.resp.status in (404, 409) and a < 3:
                time.sleep(2 + a * 2)
                try:
                    r = y.playlistItems().list(part="snippet", playlistId=pid, videoId=vid, maxResults=1).execute()
                    if r.get("items"): return "added-despite-error"
                except HttpError:
                    pass
                continue
            raise

@app.post("/api/apply")
@need
def apply():
    d, steps = request.json or {}, []
    def S(name, fn):
        try: r = fn()
        except HttpError as e:
            steps.append({"name": name, "ok": False}); raise Fail(f"{name} failed ({yerr(e)})")
        steps.append({"name": name, "ok": True}); return r
    try:
        g = q("select * from games where id=%s", (int(d.get("id", 0)),), "one")
        if not g: raise Fail("Game not found")
        final = bool(d.get("final")); n = int(d.get("part") or g["next_part"])
        part, label = ("final", "Final Part") if final else (str(n), f"Part-{n}")
        y = yt()
        def detect():
            b = (y.liveBroadcasts().list(part="id,snippet,status", broadcastStatus="active", maxResults=1)
                 .execute().get("items") or [None])[0]
            if not b: raise Fail("No active livestream found. Start the stream on your PS5 first.")
            return b
        b = S("Detect active livestream", detect); vid = b["id"]
        if q("select 1 from processed where video_id=%s", (vid,), "one"):
            return jsonify(ok=True, already=True, steps=steps, message="This livestream was already processed. Nothing changed.")
        row = q("select data from thumbs where game_id=%s and part=%s", (g["id"], part), "one")
        if not row: raise Fail(f"No thumbnail saved for {label}. Add it first, nothing was changed.")
        sn, title = b["snippet"], mk_title(g["name"], n, final)
        S("Update title", lambda: y.liveBroadcasts().update(part="snippet", body={"id": vid, "snippet": {
            "title": title, "description": sn.get("description", ""), "scheduledStartTime": sn["scheduledStartTime"]}}).execute())
        S("Upload thumbnail", lambda: y.thumbnails().set(videoId=vid, media_body=MediaIoBaseUpload(
            io.BytesIO(bytes(row["data"])), mimetype="image/jpeg")).execute())
        pid = S("Find or create playlist", lambda: get_playlist(y, g))
        S("Add to playlist", lambda: add_item(y, pid, vid))
        with conn() as c:
            c.execute("insert into processed(video_id,game_id,label) values(%s,%s,%s)", (vid, g["id"], label))
            c.execute("update games set playlist_id=%s, last_done=case when %s then last_done else %s end, "
                      "next_part=case when %s then next_part else %s end where id=%s",
                      (pid, final, n, final, n + 1, g["id"]))
        return jsonify(ok=True, steps=steps, message=f"Done: {title}")
    except Fail as e:
        return jsonify(ok=False, steps=steps, error=str(e))
    except Exception as e:
        msg = "Session expired, sign in again" if "invalid_grant" in repr(e) or "RefreshError" in repr(e) else repr(e)
        return jsonify(ok=False, steps=steps, error=msg)

# ---------- games API ----------
@app.post("/api/games")
@need
def add_game():
    name = ((request.json or {}).get("name") or "").strip()
    if not name: return jsonify(ok=False, error="Enter a game name")
    try:
        return jsonify(ok=True, id=q("insert into games(name) values(%s) returning id", (name,), "one")["id"])
    except psycopg.errors.UniqueViolation:
        return jsonify(ok=False, error="A game with that name already exists")

@app.post("/api/games/<int:gid>/thumb")
@need
def add_thumb(gid):
    part, f = request.form.get("part", ""), request.files.get("file")
    if not f or not (part == "final" or part.isdigit()): return jsonify(ok=False, error="Bad upload")
    q("insert into thumbs(game_id,part,data) values(%s,%s,%s) on conflict(game_id,part) do update set data=excluded.data",
      (gid, part, f.read()))
    return jsonify(ok=True)

@app.post("/api/games/<int:gid>/delete")
@need
def del_game(gid):
    q("delete from games where id=%s", (gid,))  # thumbs + processed rows cascade
    return jsonify(ok=True)

@app.get("/thumb/<int:gid>/<part>")
@need
def thumb(gid, part):
    r = q("select data from thumbs where game_id=%s and part=%s", (gid, part), "one")
    if not r: return "", 404
    return Response(bytes(r["data"]), mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=60"})

# ---------- pages ----------
def page(tpl, **kw):
    return render_template_string(BASE_HTML, body=render_template_string(tpl, **kw))

@app.get("/")
def home():
    return page(HOME, signed_in=bool(session.get("t")))

@app.get("/games")
@need
def games():
    rows = q("select g.*, (select count(*) from thumbs t where t.game_id=g.id) c from games g order by lower(name)", (), "all")
    return page(GAMES, games=rows)

@app.get("/new")
@need
def new():
    gid = request.args.get("id", type=int)
    return page(NEW, game=q("select * from games where id=%s", (gid,), "one") if gid else None)

@app.get("/game/<int:gid>")
@need
def game(gid):
    g = q("select * from games where id=%s", (gid,), "one")
    return page(GAME, game=g) if g else redirect("/games")

BASE_HTML = r"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>PS5 Stream Manager</title>
<style>:root{color-scheme:dark}body{font-family:system-ui;background:#0e1016;color:#e8eaf0;max-width:640px;margin:0 auto;padding:20px}
h1{font-size:1.4rem}a{color:inherit;text-decoration:none}
.big,button,.b2{display:block;width:100%;box-sizing:border-box;text-align:center;background:#6c5ce7;color:#fff;border:0;border-radius:14px;padding:16px;margin:10px 0;font-size:1.05rem;cursor:pointer}
.big{padding:30px;font-size:1.3rem}button:disabled{opacity:.5}.del{background:#c0392b}.b2{background:#2d3142}
input[type=text],input[type=number],input[type=file]{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #333;background:#181b25;color:#fff;margin:6px 0}
input[type=number]{width:90px}.card{display:flex;gap:12px;align-items:center;background:#181b25;border-radius:14px;padding:10px;margin:10px 0}
.card img{width:96px;border-radius:8px}.card div{display:flex;flex-direction:column;gap:4px}.pv{width:100%;border-radius:12px;background:#181b25;min-height:60px}
.s{color:#9aa}.warn{color:#f5b041}ul{padding:0}li{margin:6px 0;list-style:none}label{display:flex;gap:8px;align-items:center;margin:10px 0}
pre{white-space:pre-wrap}.row{display:flex;gap:8px}.row button{flex:1}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.grid img{width:100%;border-radius:8px}</style>{{ body|safe }}"""

HOME = r"""<h1>PS5 Stream Manager</h1>
{% if signed_in %}<a class=big href=/new>New Game</a><a class=big href=/games>Existing Game</a><a class=s href=/logout>Sign out</a>
{% else %}<a class=big href=/login>Sign in with Google</a>{% endif %}"""

GAMES = r"""<a class=s href=/>← Home</a><h1>Your games</h1>
{% for g in games %}<a class=card href=/game/{{g.id}}><img src=/thumb/{{g.id}}/{{g.next_part}} onerror="this.style.display='none'">
<div><b>{{g.name}}</b><span class=s>Next: Part-{{g.next_part}} · {{g.c}} thumbnails</span></div></a>
{% else %}<p class=s>No games yet.</p>{% endfor %}"""

NEW = r"""<a class=s href={{ '/game/'~game.id if game else '/' }}>← Back</a>
<h1>{{ 'Add thumbnails: '~game.name if game else 'New game' }}</h1>
{% if not game %}<input type=text id=n placeholder="Game name">{% endif %}
<div class=row><button class=b2 onclick="tab(1)">From one image</button><button class=b2 onclick="tab(2)">Existing images</button></div>
<div id=m1>
<p class=s>Pick one base image. The app stamps PART - 1, PART - 2 … and FINAL PART on the last one.</p>
<input type=file id=bf accept="image/png,image/jpeg,image/webp">
<label>How many parts? <input type=number id=np min=1 max=200 value=10></label>
<button onclick=gen()>Generate preview</button>
<div id=pv class=grid></div>
<button id=sv style="display:none" onclick=saveGen()>Looks good, save all</button>
</div>
<div id=m2 style="display:none">
<p class=s>Select the images from your folder (part-1, part-2 … final-part).</p>
<input type=file id=f accept="image/png,image/jpeg" multiple>
<p class=s id=l style="white-space:pre-line"></p>
<button id=b onclick=save()>Save</button>
</div>
<pre id=o></pre>
<script>
let gid={{ game.id if game else 'null' }},items=[];
function tab(k){m1.style.display=k==1?'':'none';m2.style.display=k==2?'':'none'}
function parse(s){s=s.toLowerCase();if(s.includes('final'))return'final';const m=s.match(/(\d+)/);return m?String(+m[1]):null}
f.onchange=()=>{l.textContent=[...f.files].map(x=>{const p=parse(x.name);return x.name+' → '+(p?(p=='final'?'Final Part':'Part '+p):'skipped (no part number)')}).join('\n')};
function stamp(bmp,text){const c=document.createElement('canvas');c.width=1280;c.height=720;const x=c.getContext('2d');
 const s=Math.max(1280/bmp.width,720/bmp.height),w=bmp.width*s,h=bmp.height*s;x.drawImage(bmp,(1280-w)/2,(720-h)/2,w,h);
 if(text){const fs=Math.round(1280*.055),sw=Math.max(2,Math.round(fs*.08));
  x.font='bold '+fs+'px Arial,Helvetica,sans-serif';x.textBaseline='top';x.lineJoin='round';
  const px=1280-Math.round(1280*.03)-x.measureText(text).width,py=Math.round(720*.03);
  x.lineWidth=sw*2;x.strokeStyle='#000';x.strokeText(text,px,py);x.fillStyle='#fff';x.fillText(text,px,py)}
 return new Promise(r=>c.toBlob(r,'image/jpeg',.88))}
async function ensure(){if(gid)return gid;const nm=n.value.trim();if(!nm)throw 'Enter a game name';
 const r=await(await fetch('/api/games',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:nm})})).json();
 if(!r.ok)throw r.error;gid=r.id;return gid}
async function up(list){const id=await ensure();let i=0;
 for(const [p,x] of list){o.textContent='Uploading '+(++i)+'/'+list.length+'…';
  const d=new FormData();d.append('part',p);d.append('file',x,'t.jpg');
  const r=await(await fetch('/api/games/'+id+'/thumb',{method:'POST',body:d})).json();if(!r.ok)throw r.error}
 location='/game/'+id}
async function gen(){const file=bf.files[0],N=+np.value;
 if(!file||!(N>=1&&N<=200)){o.textContent='Choose an image and a part count (1-200)';return}
 o.textContent='Generating…';pv.innerHTML='';sv.style.display='none';items=[];
 const bmp=await createImageBitmap(file);
 for(let i=1;i<=N;i++){const last=i==N,blob=await stamp(bmp,last?'FINAL PART':'PART - '+i);
  items.push([last?'final':String(i),blob]);const im=document.createElement('img');im.src=URL.createObjectURL(blob);pv.appendChild(im)}
 o.textContent=N+' thumbnails ready. Check them, then save.';sv.style.display='block'}
async function saveGen(){sv.disabled=true;try{await up(items)}catch(e){o.textContent='Error: '+e;sv.disabled=false}}
async function save(){const fl=[...f.files].map(x=>[parse(x.name),x]).filter(a=>a[0]);
 if(!fl.length){o.textContent='Pick at least one valid image';return}
 b.disabled=true;try{const list=[];for(const [p,x] of fl)list.push([p,await stamp(await createImageBitmap(x),null)]);await up(list)}
 catch(e){o.textContent='Error: '+e;b.disabled=false}}
</script>"""

GAME = r"""<a class=s href=/games>← Games</a><h1>{{game.name}}</h1>
<p class=s>Last completed: {{ 'Part-'~game.last_done if game.last_done else 'none' }} · Next: Part-{{game.next_part}}</p>
<img id=im class=pv><p id=ti><b></b></p><p id=w class=warn></p>
<label><input type=checkbox id=fin onchange=upd()> This is the final part</label>
<label>Part number <input type=number id=pn min=1 value={{game.next_part}} oninput=upd()></label>
<button id=ap onclick=go()>Start / Apply</button><ul id=st></ul>
<a class=b2 href=/new?id={{game.id}}>Add / replace thumbnails</a>
<button class=del onclick=del()>Delete game</button>
<script>
const ID={{game.id}},NAME={{game.name|tojson}};
function mk(){const fp=fin.checked,n=+pn.value||1,s=' | PS5 Pro Gameplay | '+(fp?'Final Part':'Part-'+n);return{t:NAME.slice(0,100-s.length)+s,p:fp?'final':String(n)}}
function upd(){const m=mk();ti.textContent=m.t;im.src='/thumb/'+ID+'/'+m.p}
im.onerror=()=>{im.style.visibility='hidden';w.textContent='⚠ No thumbnail saved for this part'};
im.onload=()=>{im.style.visibility='visible';w.textContent=''};
upd();
async function go(){ap.disabled=true;st.innerHTML='';
 const r=await fetch('/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:ID,final:fin.checked,part:+pn.value||null})});
 let j;try{j=await r.json()}catch(e){j={ok:false,error:'Server error '+r.status}}
 for(const s of j.steps||[]){const li=document.createElement('li');li.textContent=(s.ok?'✓ ':'✗ ')+s.name;st.appendChild(li)}
 const li=document.createElement('li');li.textContent=j.ok?j.message:'⚠ '+j.error;st.appendChild(li);
 ap.disabled=false;if(j.ok&&!j.already)setTimeout(()=>location.reload(),3000)}
async function del(){if(!confirm('Delete '+NAME+' and ALL its thumbnails? This cannot be undone.'))return;
 const r=await(await fetch('/api/games/'+ID+'/delete',{method:'POST'})).json();if(r.ok)location='/games';else alert(r.error)}
</script>"""
