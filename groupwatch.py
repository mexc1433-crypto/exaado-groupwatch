#!/usr/bin/env python3
# Exaado group watcher — runs 24/7 on Railway (no phone needed).
# Listens to the marketing group via Telethon and pushes every new message
# to the Base44 telegramWebhook function. Includes a one-time web login page.
import os, json, asyncio, hmac, secrets
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.environ.get("TELEGRAM_API_ID", "26469071"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
GROUP_ID = int(os.environ.get("GROUP_ID", "-1001667723553"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
LOGIN_KEY = os.environ.get("LOGIN_KEY", secrets.token_hex(16))
SESSION_PATH = "/data/session"
LAST_ID_PATH = "/data/last_id"
TZ = ZoneInfo("Africa/Cairo")
DR_M_HINTS = ["مهدي", "المهدي", "mahdi", "mehdi"]

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
state = {"phone": None, "code_hash": None, "logged": False, "started": False}

def now_hhmm():
    return datetime.now(TZ).strftime("%H:%M")

def read_last_id():
    try: return int(open(LAST_ID_PATH).read().strip())
    except Exception: return 24727

def write_last_id(i):
    try:
        os.makedirs("/data", exist_ok=True)
        open(LAST_ID_PATH, "w").write(str(i))
    except Exception: pass

async def push_message(mid, sender, text, date_str):
    payload = {"type": "group", "message": {"id": mid, "sender": sender, "text": text, "date": date_str}}
    for attempt in range(4):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(WEBHOOK_URL, json=payload,
                                  headers={"x-telegram-bot-api-secret-token": WEBHOOK_SECRET},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        return True
        except Exception:
            await asyncio.sleep(5 * (attempt + 1))
    return False

async def heartbeat_loop():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"https://ntfy.sh/{NTFY_TOPIC}", data="GROUPWATCH_ALIVE (railway)",
                             headers={"Title": "GROUPWATCH_ALIVE"}, timeout=aiohttp.ClientTimeout(total=15))
        except Exception:
            pass
        await asyncio.sleep(1800)

def dr_m(name, username):
    n = (name or "").lower()
    u = (username or "").lower()
    return any(h in n or h in u for h in DR_M_HINTS)

async def handle_message(event):
    try:
        await _handle(event)
    except Exception as e:
        print("handler error:", e)
        await report_error(e)

async def _handle(event):
    sender = await event.get_sender()
    name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])).strip()
    username = getattr(sender, "username", None) or ""
    if dr_m(name, username):
        name = "Dr M"
    m = event.message
    if m.voice or m.audio:
        text = "[صوتية]"
    elif m.photo:
        text = "[صورة] " + (m.message or "")
    elif m.video:
        text = "[فيديو] " + (m.message or "")
    elif m.document:
        text = "[ملف] " + (m.message or "")
    else:
        text = m.message or ""
    ok = await push_message(m.id, name, text.strip()[:2000], now_hhmm())
    if ok:
        write_last_id(m.id)

async def catchup():
    try:
        last = read_last_id()
        msgs = await client.get_messages(GROUP_ID, limit=100, min_id=last)
        for m in reversed(msgs):
            await handle_message(type("E", (), {"message": m, "get_sender": lambda mm=m: asyncio.sleep(0) or mm.sender})())
    except Exception as e:
        print("catchup err:", e)

async def start_listener():
    if state["started"]:
        return
    state["started"] = True
    client.add_event_handler(handle_message, events.NewMessage(chats=GROUP_ID))
    asyncio.create_task(heartbeat_loop())
    await catchup()
    print("listener live")

async def authed(_):
    if await client.is_user_authorized():
        state["logged"] = True
        await start_listener()

def check_key(k):
    return k and hmac.compare_digest(k, LOGIN_KEY)

async def index(req):
    return web.Response(text=PAGE_HTML, content_type="text/html")

async def login_start(req):
    if not check_key(req.query.get("k")):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    if await client.is_user_authorized():
        return web.json_response({"ok": True, "already": True})
    phone = req.query.get("phone", "").strip()
    if not phone.startswith("+"):
        return web.json_response({"ok": False, "error": "اكتب الرقم بصيغة دولية زي +2010xxxxxxxx"})
    state["phone"] = phone
    sent = await client.send_code_request(phone)
    state["code_hash"] = sent.phone_code_hash
    return web.json_response({"ok": True, "sent": True})

async def login_verify(req):
    if not check_key(req.query.get("k")):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    code = req.query.get("code", "").strip()
    try:
        await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["code_hash"])
    except SessionPasswordNeededError:
        return web.json_response({"ok": True, "need_password": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]})
    state["logged"] = True
    asyncio.create_task(start_listener())
    return web.json_response({"ok": True, "logged_in": True})

async def login_password(req):
    if not check_key(req.query.get("k")):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    try:
        await client.sign_in(password=req.query.get("p", ""))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]})
    state["logged"] = True
    asyncio.create_task(start_listener())
    return web.json_response({"ok": True, "logged_in": True})

async def send_dm(req):
    k = req.query.get("k") or req.headers.get("x-secret")
    if not (check_key(k) or (WEBHOOK_SECRET and k == WEBHOOK_SECRET)):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    to = req.query.get("to", "").strip()
    text = req.query.get("text", "").strip()
    if not to or not text:
        return web.json_response({"ok": False, "error": "to & text required"}, status=400)
    try:
        entity = await client.get_entity(to)
        await client.send_message(entity, text)
        return web.json_response({"ok": True, "sent": True, "to": to})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]})


async def read_chat(req):
    k = req.query.get("k") or req.headers.get("x-secret")
    if not (check_key(k) or (WEBHOOK_SECRET and k == WEBHOOK_SECRET)):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    chat = req.query.get("chat", "me").strip() or "me"
    try:
        limit = min(int(req.query.get("limit", "20")), 100)
    except Exception:
        limit = 20
    entity = None
    try:
        entity = await client.get_entity(chat if chat not in ("me", "self") else "me")
    except Exception:
        pass
    if entity is None:
        try:
            want = int(chat)
        except Exception:
            want = None
        async for d in client.iter_dialogs(limit=200):
            if d.id == want or (getattr(d.entity, "username", None) or "").lower() == chat.lstrip("@").lower() or d.name == chat:
                entity = d.entity
                break
    if entity is None:
        return web.json_response({"ok": False, "error": "chat not found"}, status=404)
    out = []
    try:
        try:
            offset_id = int(req.query.get("offset_id", "0"))
        except Exception:
            offset_id = 0
        async for m in client.iter_messages(entity, limit=limit, offset_id=offset_id or 0):
            kind = "text"
            if m.voice or m.audio:
                kind = "voice"
            elif m.photo:
                kind = "photo"
            elif m.video_note:
                kind = "video_note"
            elif m.video:
                kind = "video"
            elif m.sticker:
                kind = "sticker"
            elif m.document:
                kind = "document"
            if m.action:
                kind = "action:" + type(m.action).__name__
            try:
                snd = await m.get_sender()
                sname = " ".join(filter(None, [getattr(snd, "first_name", None), getattr(snd, "last_name", None)])).strip()
            except Exception:
                sname = ""
            dt = m.date.strftime("%m-%d %H:%M") if m.date else ""
            grp = getattr(m, "grouped_id", None)
            out.append({
                "id": m.id, "kind": kind, "sender": sname, "date": dt, "grouped": grp,
                "text": (m.message or "")[:1500],
                "file_id": None,
            })
        return web.json_response({"ok": True, "chat": chat, "messages": out})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=500)

async def list_dialogs(req):
    k = req.query.get("k") or req.headers.get("x-secret")
    if not (check_key(k) or (WEBHOOK_SECRET and k == WEBHOOK_SECRET)):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    try:
        limit = min(int(req.query.get("limit", "40")), 100)
    except Exception:
        limit = 40
    out = []
    try:
        async for d in client.iter_dialogs(limit=limit):
            out.append({
                "id": d.id, "name": d.name,
                "username": getattr(d.entity, "username", None) if hasattr(d.entity, "username") else None,
                "last": (d.message.message or "")[:80] if d.message else "",
                "when": d.message.date.strftime("%m-%d %H:%M") if d.message and d.message.date else "",
            })
        return web.json_response({"ok": True, "dialogs": out})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=500)


async def media(req):
    import io
    k = req.query.get("k") or req.headers.get("x-secret")
    if not (check_key(k) or (WEBHOOK_SECRET and k == WEBHOOK_SECRET)):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    chat = req.query.get("chat", "me").strip() or "me"
    try:
        msg_id = int(req.query.get("msg_id", "0"))
    except Exception:
        msg_id = 0
    entity = None
    try:
        entity = await client.get_entity(chat if chat not in ("me", "self") else "me")
    except Exception:
        pass
    if entity is None:
        try:
            want = int(chat)
        except Exception:
            want = None
        async for d in client.iter_dialogs(limit=200):
            if d.id == want or (getattr(d.entity, "username", None) or "").lower() == chat.lstrip("@").lower() or d.name == chat:
                entity = d.entity
                break
    if entity is None:
        return web.json_response({"ok": False, "error": "chat not found"}, status=404)
    try:
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg or not (msg.photo or msg.video or msg.document or msg.voice):
            return web.json_response({"ok": False, "error": "message has no media"}, status=404)
        data = await client.download_media(msg, file=io.BytesIO())
        data.seek(0)
        ctype = "image/jpeg"
        if msg.document and getattr(msg.document, "mime_type", None):
            ctype = msg.document.mime_type or ctype
        return web.Response(body=data.read(), content_type=ctype, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=500)


async def debug_msg(req):
    k = req.query.get("k") or req.headers.get("x-secret")
    if not (check_key(k) or (WEBHOOK_SECRET and k == WEBHOOK_SECRET)):
        return web.json_response({"ok": False, "error": "bad key"}, status=403)
    try:
        msg_id = int(req.query.get("msg_id", "0"))
    except Exception:
        msg_id = 0
    chat = req.query.get("chat", "8990872009")
    try:
        entity = await client.get_entity(chat)
    except Exception:
        entity = None
        async for d in client.iter_dialogs(limit=200):
            if str(d.id) == chat:
                entity = d.entity
                break
    if entity is None:
        return web.json_response({"ok": False, "error": "chat not found"}, status=404)
    try:
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            return web.json_response({"ok": False, "error": "no message"}, status=404)
        raw = msg.to_dict()
        raw.pop("peer", None); raw.pop("_client", None)
        sraw = str(raw)
        i = sraw.find("'rich_message'")
        if i >= 0:
            sraw = "..." + sraw[i:]
        return web.json_response({"ok": True, "raw": sraw[:9000]})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=500)

async def status(req):
    auth = False
    try: auth = await client.is_user_authorized()
    except Exception: pass
    return web.json_response({"logged": auth, "listening": state["started"]})

PAGE_HTML = """<!doctype html><html dir=rtl lang=ar><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1><title>Exaado — تسجيل الدخول</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:60px auto;padding:0 16px;direction:rtl">
<h2>🔐 تسجيل دخول التليجرام — مرة واحدة بس</h2>
<p>اكتب رقمك بالصيغة الدولية (مثال: +201012345678). هيوصلك كود على تليجرام، اكتبه في الخانة اللي بعدها.</p>
<input id=phone placeholder="+2010xxxxxxxx" style="width:100%;padding:12px;font-size:18px;margin:8px 0">
<button onclick="sendCode()" style="padding:12px 24px;font-size:16px">إرسال الكود</button>
<p id=msg1></p>
<input id=code placeholder="الكود من تليجرام" style="width:100%;padding:12px;font-size:18px;margin:8px 0;display:none">
<button id=b2 onclick="verify()" style="padding:12px 24px;font-size:16px;display:none">تأكيد</button>
<p id=msg2></p>
<input id=pass placeholder="كلمة سر التحقق بخطوتين (لو مفعلة)" style="width:100%;padding:12px;font-size:18px;margin:8px 0;display:none">
<button id=b3 onclick="pw()" style="padding:12px 24px;font-size:16px;display:none">تأكيد</button>
<script>
const K=new URLSearchParams(location.search).get('k')||'';
async function sendCode(){msg1.textContent='جاري الإرسال...';
const r=await fetch(`/login/start?k=${K}&phone=${encodeURIComponent(phone.value)}`).then(r=>r.json());
if(r.need_password){pass.style.display='';b3.style.display='';msg1.textContent='المطلوب كلمة سر التحقق';return}
if(r.already){msg1.textContent='✅ مسجل دخول بالفعل!';return}
if(!r.ok){msg1.textContent='❌ '+r.error;return}
msg1.textContent='✅ الكود اتذهب على تليجرام';code.style.display='';b2.style.display='';}
async function verify(){const r=await fetch(`/login/verify?k=${K}&code=${code.value}`).then(r=>r.json());
if(r.need_password){pass.style.display='';b3.style.display='';msg2.textContent='اكتب كلمة السر';return}
if(r.ok&&r.logged_in){msg2.textContent='✅ تم الدخول! المستمع اشتغل — تقدر تقفل الصفحة';return}
msg2.textContent='❌ '+(r.error||'حاول تاني');}
async function pw(){const r=await fetch(`/login/password?k=${K}&p=${encodeURIComponent(pass.value)}`).then(r=>r.json());
msg2.textContent=r.ok&&r.logged_in?'✅ تم الدخول! المستمع اشتغل':'❌ '+(r.error||'حاول تاني');}
</script></body></html>"""

async def report_error(err):
    if not NTFY_TOPIC:
        return
    try:
        import traceback
        tb = traceback.format_exc()[-1200:] or str(err)
        async with aiohttp.ClientSession() as s:
            await s.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=f"GW_ERROR: {tb}",
                         headers={"Title": "GW_ERROR"}, timeout=aiohttp.ClientTimeout(total=15))
    except Exception:
        pass

async def telethon_loop():
    import traceback
    while True:
        try:
            if not client.is_connected():
                await client.connect()
            if await client.is_user_authorized():
                state["logged"] = True
                await start_listener()
                return
            print("waiting for telegram login (unauthorized)...")
            return
        except Exception as e:
            print("telethon_loop error:", e)
            traceback.print_exc()
            await report_error(e)
            await asyncio.sleep(60)

async def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/login/start", login_start)
    app.router.add_get("/login/verify", login_verify)
    app.router.add_get("/login/password", login_password)
    app.router.add_get("/status", status)
    app.router.add_post("/send", send_dm)
    app.router.add_get("/send", send_dm)
    app.router.add_get("/read", read_chat)
    app.router.add_get("/dialogs", list_dialogs)
    app.router.add_get("/media", media)
    app.router.add_get("/debug", debug_msg)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"http server on {port}")
    asyncio.create_task(telethon_loop())
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
