import os, json, time, uuid, threading, subprocess
from typing import Dict, Any
import requests
import redis
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

REDIS_URL = os.getenv("REDIS_URL", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
RENDER_AGENT_DEPLOY_HOOK = os.getenv("RENDER_AGENT_DEPLOY_HOOK", os.getenv("RENDER_DEPLOY_HOOK", ""))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ALLOW_SHELL = os.getenv("ALLOW_SHELL", "false").lower() == "true"

QUEUE_KEY = "queue:tasks"
LOGS_KEY  = "logs:lines"
MAX_LOGS  = 2000

if not REDIS_URL:
    raise RuntimeError("REDIS_URL not set")

r = redis.from_url(REDIS_URL, decode_responses=True)
app = FastAPI()

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}"
    print(line, flush=True)
    p = r.pipeline()
    p.lpush(LOGS_KEY, line)
    p.ltrim(LOGS_KEY, 0, MAX_LOGS - 1)
    p.execute()

def require_auth(request: Request):
    if not AGENT_API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if token != AGENT_API_KEY:
        raise HTTPException(status_code=403, detail="invalid token")

def do_github_dispatch(payload: Dict[str, Any]):
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing")
    owner = payload.get("owner", "Fahil100")
    repo  = payload.get("repo", "TAAI-Automation-agent")
    evt   = payload.get("event_type", "smoke-now")
    url   = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
    hdrs  = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "taai-agent-hub"
    }
    body = {"event_type": evt}
    resp = requests.post(url, headers=hdrs, json=body, timeout=15)
    log(f"[GH] {owner}/{repo} dispatch '{evt}' → {resp.status_code}")
    resp.raise_for_status()

def do_render_deploy(payload: Dict[str, Any]):
    hook = payload.get("hook") or RENDER_AGENT_DEPLOY_HOOK
    if not hook:
        raise RuntimeError("Render deploy hook missing")
    resp = requests.post(hook, timeout=20)
    log(f"[RENDER] deploy → {resp.status_code}")
    resp.raise_for_status()

def do_http_request(payload: Dict[str, Any]):
    method  = (payload.get("method") or "GET").upper()
    url     = payload.get("url")
    headers = payload.get("headers") or {}
    body    = payload.get("body")
    if not url:
        raise RuntimeError("http.url missing")
    resp = requests.request(method, url, headers=headers, json=body, timeout=20)
    log(f"[HTTP] {method} {url} → {resp.status_code}")

def do_telegram_send(payload: Dict[str, Any]):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise RuntimeError("Telegram envs missing")
    text = payload.get("text") or "TAAI: (empty)"
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    log("[TG] sent")
    resp.raise_for_status()

def do_shell_run(payload: Dict[str, Any]):
    if not ALLOW_SHELL:
        log("[SHELL][SKIP] not allowed in cloud")
        return
    cmd = payload.get("command")
    if not cmd:
        raise RuntimeError("shell.command missing")
    proc = subprocess.run(cmd, shell=True)
    log(f"[SHELL] exit {proc.returncode}")

PROCESSORS = {
    "github.dispatch": do_github_dispatch,
    "render.deploy":   do_render_deploy,
    "http.request":    do_http_request,
    "telegram.send":   do_telegram_send,
    "shell.run":       do_shell_run,
}

def worker_loop():
    log("[WORKER] started")
    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=5)
            if not item:
                continue
            _, raw = item
            task = json.loads(raw)
            t_id = task.get("id")
            t_tp = task.get("type")
            payload = task.get("payload") or {}
            fn = PROCESSORS.get(t_tp)
            if not fn:
                log(f"[TASK][{t_id}][SKIP] unknown type '{t_tp}'")
                continue
            fn(payload)
            log(f"[TASK][{t_id}] done")
        except Exception as e:
            log(f"[TASK][ERR] {e}")

threading.Thread(target=worker_loop, daemon=True).start()

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "agent-hub"}

@app.get("/queue")
def get_queue():
    items = r.lrange(QUEUE_KEY, 0, 200)
    return [json.loads(x) for x in items]

@app.get("/api/logs")
def get_logs():
    return {"lines": r.lrange(LOGS_KEY, 0, 200)}

@app.post("/enqueue")
async def enqueue(request: Request):
    require_auth(request)
    body = await request.json()
    t_type = body.get("type")
    payload = body.get("payload")
    if not t_type or payload is None:
        raise HTTPException(status_code=400, detail="missing type or payload")
    t_id = uuid.uuid4().hex[:8]
    item = {"id": t_id, "type": t_type, "payload": payload}
    r.lpush(QUEUE_KEY, json.dumps(item))
    log(f"[ENQ] {t_type} id={t_id}")
    return {"ok": True, "id": t_id}

@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if TELEGRAM_WEBHOOK_SECRET and secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret")
    upd = await request.json()
    msg = (((upd.get("message") or {}).get("text")) or "").strip().lower()
    if not msg:
        return {"ok": True}
    if "deploy" in msg:
        r.lpush(QUEUE_KEY, json.dumps({"id": uuid.uuid4().hex[:8], "type":"github.dispatch",
                                       "payload":{"owner":"Fahil100","repo":"TAAI-Automation-agent","event_type":"smoke-now"}}))
        r.lpush(QUEUE_KEY, json.dumps({"id": uuid.uuid4().hex[:8], "type":"render.deploy","payload":{}}))
        if TELEGRAM_CHAT_ID:
            do_telegram_send({"text":"Queued deploy via Telegram ✅"})
    elif "health" in msg or "ping" in msg:
        r.lpush(QUEUE_KEY, json.dumps({"id": uuid.uuid4().hex[:8], "type":"http.request",
                                       "payload":{"method":"GET","url":"https://taai-automation-agent.onrender.com/healthz"}}))
    else:
        if TELEGRAM_CHAT_ID:
            do_telegram_send({"text":"Commands: deploy | health"})
    return {"ok": True}

@app.post("/chat")
async def chat(request: Request):
    data = await request.json()
    user = data.get("message","").strip()
    if not user:
        return {"reply":"(empty)"}
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":"You are TAAI ops agent. Keep replies short."},
                          {"role":"user","content":user}]
            )
            text = resp.choices[0].message.content
        except Exception as e:
            text = f"(LLM error: {e})"
    else:
        text = "(LLM not configured) You can still click quick actions."

    lower = user.lower()
    if "deploy" in lower:
        r.lpush(QUEUE_KEY, json.dumps({"id": uuid.uuid4().hex[:8], "type":"github.dispatch",
                                       "payload":{"owner":"Fahil100","repo":"TAAI-Automation-agent","event_type":"smoke-now"}}))
        r.lpush(QUEUE_KEY, json.dumps({"id": uuid.uuid4().hex[:8], "type":"render.deploy","payload":{}}))
        log("[CHAT] queued deploy")
    if "health" in lower or "ping" in lower:
        r.lpush(QUEUE_KEY, json.dumps({"id": uuid.uuid4().hex[:8], "type":"http.request",
                                       "payload":{"method":"GET","url":"https://taai-automation-agent.onrender.com/healthz"}}))
        log("[CHAT] queued health")
    return {"reply": text}

@app.get("/")
def dashboard():
    html = """
<!doctype html><meta charset="utf-8"/><title>TAAI Agent Hub</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial;margin:20px}
  .row{display:flex;gap:16px;flex-wrap:wrap}
  .card{border:1px solid #ddd;border-radius:12px;padding:16px;flex:1;min-width:320px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  button{padding:8px 12px;border-radius:10px;border:1px solid #ccc;cursor:pointer}
  textarea,input{width:100%;padding:8px;border-radius:8px;border:1px solid #ccc}
  pre{white-space:pre-wrap;max-height:420px;overflow:auto}
  small{color:#666}
</style>
<h2>🧠 TAAI Agent Hub</h2>
<div class="row">
  <div class="card">
    <h3>Chat</h3>
    <textarea id="msg" rows="3" placeholder="Try: deploy | health"></textarea><br/><br/>
    <button onclick="send()">Send</button>
    <p id="reply"><small>Reply will appear here…</small></p>
  </div>
  <div class="card">
    <h3>Quick actions</h3>
    <button onclick="enqueue('github.dispatch', {owner:'Fahil100',repo:'TAAI-Automation-agent',event_type:'smoke-now'})">GitHub smoke</button>
    <button onclick="enqueue('render.deploy', {})">Render deploy</button>
    <button onclick="enqueue('http.request', {method:'GET',url:'https://taai-automation-agent.onrender.com/healthz'})">Ping health</button>
    <button onclick="enqueue('telegram.send', {text:'TAAI: hello from dashboard'})">Telegram ping</button>
    <p><small>/enqueue is protected with Bearer token. Use CLI/Postman to call it.</small></p>
  </div>
</div>
<div class="row">
  <div class="card"><h3>Queue</h3><pre id="queue"></pre></div>
  <div class="card"><h3>Logs</h3><pre id="logs"></pre></div>
</div>
<script>
async function send(){
  const r = await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:document.getElementById('msg').value})});
  const j = await r.json(); document.getElementById('reply').innerText = j.reply || '(no reply)';
}
async function load(){
  try{
    const q = await (await fetch('/queue')).json();
    document.getElementById('queue').innerText = JSON.stringify(q, null, 2);
  }catch(e){}
  try{
    const l = await (await fetch('/api/logs')).json();
    document.getElementById('logs').innerText = (l.lines||[]).join('\\n');
  }catch(e){}
}
async function enqueue(type, payload){
  alert('Use CLI/Postman to call /enqueue with your Bearer token.');
}
setInterval(load, 1500); load();
</script>
"""
    return HTMLResponse(html)
