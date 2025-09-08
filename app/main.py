from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI()

OPENAI_API_KEY = ""  # Make sure this is set by env var in Render

@app.post("/chat")
async def chat(request: Request):
    data = await request.json()
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return {"reply": "(empty)"}

    reply = "(LLM not configured)"
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are TAAI Agent, a helpful AI assistant."},
                    {"role": "user", "content": user_msg}
                ]
            )
            reply = resp.choices[0].message.content
        except Exception as e:
            reply = f"(error: {e})"

    return {"reply": reply}

@app.get("/")
def dashboard():
    html = """
    <!doctype html>
    <meta charset="utf-8"/>
    <title>TAAI Agent Hub</title>

    <div class="card">
      <h3>Chat</h3>
      <textarea id="msg" rows="3" placeholder="Say something..."></textarea><br/><br/>
      <button onclick="sendChat()">Send</button>
      <p id="reply"><small>Reply will appear here...</small></p>
    </div>

    <script>
    async function sendChat(){
      const msg = document.getElementById('msg').value;
      const r = await fetch('/chat',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({message:msg})
      });
      const j = await r.json();
      document.getElementById('reply').innerText = j.reply;
    }
    </script>
    """
    return HTMLResponse(html)
import os

# make sure OPENAI_API_KEY is loaded
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY is not set in environment")
