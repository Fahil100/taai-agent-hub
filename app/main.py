import os, json, time, uuid, threading, subprocess
from typing import Dict, Any
import requests
import redis
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

REDIS_URL = os.getenv("REDIS_URL", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
RENDER_AGENT_DEPLOY_HOOK = os.getenv("RENDER_AGENT_DEPLOY_HOOK", os.getenv("RENDER_DEPLOY_HOOK", ""))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ALLOW_SHELL = os.getenv("ALLOW_SHELL", "false").lower() == "true"

QUEUE_CLOUD = "queue:tasks"   # cloud worker (GitHub/Render/HTTP/Telegram)
QUEUE_LOCAL = "queue:local"   # pulled by your local runner
LOGS_KEY    = "logs:lines"
MAX_LOGS    = 2000

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

# ---------------- Cloud processors ----------------
def do_github_dispatch(payload: Dict[str, Any]):
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN missing")
    owner = payload.get("owner", "Fahil100")
    repo  = payload.get("repo", "TAAI-Automation-agent")
    evt   = payload.get("event_type", "smoke-now")
    url   = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
    hdrs  = {"Authorization": f"token {GITHUB_TOKEN}",
             "Accept": "application/vnd.github+json",
             "User-Agent": "taai-agent-hub"}
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
            item = r.brpop(QUEUE_CLOUD, timeout=5)
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

# ---------------- HTTP API ----------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "agent-hub"}

@app.get("/queue")
def get_queue():
    items_cloud = r.lrange(QUEUE_CLOUD, 0, 200)
    items_local = r.lrange(QUEUE_LOCAL, 0, 200)
    return {
        "cloud": [json.loads(x) for x in items_cloud],
        "local": [json.loads(x) for x in items_local],
    }

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
    r.lpush(QUEUE_CLOUD, json.dumps(item))
    log(f"[ENQ][CLOUD] {t_type} id={t_id}")
    return {"ok": True, "id": t_id}

@app.post("/enqueue-local")
async def enqueue_local(request: Request):
    require_auth(request)
    body = await request.json()
    t_type = body.get("type")
    payload = body.get("payload")
    if not t_type or payload is None:
        raise HTTPException(status_code=400, detail="missing type or payload")
    t_id = uuid.uuid4().hex[:8]
    item = {"id": t_id, "type": t_type, "payload": payload}
    r.lpush(QUEUE_LOCAL, json.dumps(item))
    log(f"[ENQ][LOCAL] {t_type} id={t_id}")
    return {"ok": True, "id": t_id}

@app.post("/runner/poll")
async def runner_poll(request: Request):
    require_auth(request)
    body = await request.json()
    runner = (body.get("runner") or "unknown").strip()
    item = r.brpop(QUEUE_LOCAL, timeout=20)
    if not item:
        return {"ok": True, "task": None}
    _, raw = item
    try:
        task = json.loads(raw)
    except Exception:
        task = {"raw": raw}
    log(f"[RUNNER][{runner}] pulled {task.get('type') or 'unknown'}")
    return {"ok": True, "task": task}

@app.get("/")
def dashboard():
    html = """
<!doctype html><meta charset="utf-8"/><title>TAAI Agent Hub</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial;margin:20px}
  .row{display:flex;gap:16px;flex-wrap:wrap}
  .card{border:1px solid #ddd;border-radius:12px;padding:16px;flex:1;min-width:320px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  pre{white-space:pre-wrap;max-height:420px;overflow:auto}
  small{color:#666}
</style>
<h2>🧠 TAAI Agent Hub</h2>
<p><small>Cloud queue + Local runner queue</small></p>
<div class="row">
  <div class="card"><h3>Queues</h3><pre id="queue"></pre></div>
  <div class="card"><h3>Logs</h3><pre id="logs"></pre></div>
</div>
<script>
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
setInterval(load, 1500); load();
</script>
"""
    return HTMLResponse(html)
