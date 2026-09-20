#!/usr/bin/env python3
# Exaado group watcher — GitHub Actions edition (free, no Railway, no Termux).
# Phases:
#   request PHONE  -> start Telegram login (code is sent to the user's Telegram)
#   finish         -> complete login with TG_CODE secret, print encrypted session, dump last 50 msgs to ntfy
#   backfill N     -> using TG_SESSION secret, dump last N messages to ntfy
#   watch          -> using TG_SESSION secret, fetch new messages since last_id, push to ntfy, update last_id
import os, sys, json, hashlib, asyncio, base64

API_ID = int(os.environ.get("TG_API_ID", "26469071"))
API_HASH = os.environ.get("TG_API_HASH", "")
GROUP_ID = int(os.environ.get("TG_GROUP_ID", "-1001667723553"))
NTFY_TOPIC = os.environ.get("TG_NTFY", "")          # where fetched messages are posted
NTFY_CMD = os.environ.get("TG_NTFY_CMD", "")        # error/heartbeat topic
TG_PASS = os.environ.get("TG_PASS", "")             # passphrase for session encryption
TG_CODE = os.environ.get("TG_CODE", "")
TG_PASSWORD = os.environ.get("TG_PASSWORD", "")      # 2FA if needed
PHONE = os.environ.get("TG_PHONE", "")
LAST_ID_FILE = os.environ.get("GW_LAST_ID_FILE", "gw_state/last_id")
PENDING_SESSION = os.environ.get("GW_PENDING_FILE", "/tmp/pending_session")

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

def enc(secret: str, text: str) -> str:
    from nacl.secret import SecretBox
    key = hashlib.sha256(secret.encode()).digest()
    box = SecretBox(key)
    return base64.b64encode(box.encrypt(text.encode())).decode()

def dec(secret: str, blob: str) -> str:
    from nacl.secret import SecretBox
    key = hashlib.sha256(secret.encode()).digest()
    box = SecretBox(key)
    return box.decrypt(base64.b64decode(blob)).decode()

def ntfy_post(topic, title, body, prio="default"):
    import urllib.request
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode(),
        headers={"Title": title, "Priority": prio})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print("ntfy error:", e)

def read_last_id():
    try:
        return int(open(LAST_ID_FILE).read().strip())
    except Exception:
        return 24727

def write_last_id(i):
    os.makedirs(os.path.dirname(LAST_ID_FILE) or ".", exist_ok=True)
    open(LAST_ID_FILE, "w").write(str(i))

def sender_name(m):
    try:
        s = m.sender
        if s is None:
            return str(m.sender_id)
        if getattr(s, "username", None):
            return f"@{s.username}"
        nm = " ".join(filter(None, [getattr(s, "first_name", ""), getattr(s, "last_name", "")]))
        return nm or str(m.sender_id)
    except Exception:
        return str(m.sender_id)

def dump_msgs(client, limit, since_id=None):
    """Fetch messages and post them to ntfy in chunks. Returns (count, max_id)."""
    async def run():
        if since_id is not None:
            msgs = await client.get_messages(GROUP_ID, limit=limit, min_id=since_id)
        else:
            msgs = await client.get_messages(GROUP_ID, limit=limit)
        return list(msgs)
    msgs = asyncio.run(run())
    msgs = [m for m in msgs if m.text or m.media]
    lines, max_id, count = [], since_id or 0, 0
    for m in sorted(msgs, key=lambda x: x.id):
        text = (m.text or "").replace("\n", " ⏎ ")
        if len(text) > 1500:
            text = text[:1500] + "…"
        rec = {"id": m.id, "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else "",
               "sender": sender_name(m), "text": text}
        line = json.dumps(rec, ensure_ascii=False)
        if lines and sum(len(l) for l in lines) + len(line) > 3000:
            ntfy_post(NTFY_TOPIC, "TGFETCH_JSON", "\n".join(lines))
            lines = []
        lines.append(line)
        count += 1
        max_id = max(max_id, m.id)
    if lines:
        ntfy_post(NTFY_TOPIC, "TGFETCH_JSON", "\n".join(lines))
    ntfy_post(NTFY_TOPIC, "TGFETCH_DONE", f"count={count} max_id={max_id}")
    return count, max_id

def main():
    phase = sys.argv[1]
    if phase == "request":
        if not PHONE:
            print("ERR: no phone"); sys.exit(1)
        client = TelegramClient(PENDING_SESSION, API_ID, API_HASH)
        asyncio.run(client.connect())
        sent = asyncio.run(client.send_code_request(PHONE))
        print("CODE_SENT", "needs:", getattr(sent, "type", "?"))
    elif phase == "finish":
        code = TG_CODE.strip().replace(" ", "")
        client = TelegramClient(PENDING_SESSION, API_ID, API_HASH)
        asyncio.run(client.connect())
        try:
            asyncio.run(client.sign_in(phone=PHONE, code=code))
        except SessionPasswordNeededError:
            print("PASSWORD_NEEDED")
            if TG_PASSWORD:
                asyncio.run(client.sign_in(password=TG_PASSWORD))
            else:
                sys.exit(3)
        me = asyncio.run(client.get_me())
        print("LOGGED_IN as", me.first_name, me.username or "")
        sess_str = StringSession.save(client.session)
        print("SESSION_ENC:")
        print(enc(TG_PASS, sess_str))
        count, max_id = dump_msgs(client, 50)
        print("DUMPED", count, "messages, max_id", max_id)
    elif phase == "backfill":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 35
        client = TelegramClient(StringSession(dec(TG_PASS, os.environ["TG_SESSION"])), API_ID, API_HASH)
        asyncio.run(client.connect())
        count, max_id = dump_msgs(client, limit)
        print("DUMPED", count, "max_id", max_id)
    elif phase == "watch":
        client = TelegramClient(StringSession(dec(TG_PASS, os.environ["TG_SESSION"])), API_ID, API_HASH)
        asyncio.run(client.connect())
        last = read_last_id()
        count, max_id = dump_msgs(client, 100, since_id=last)
        if max_id and max_id > last:
            write_last_id(max_id)
        print(f"WATCH_OK new={count} last_id={max(max_id, last)}")
        rn = int(os.environ.get("GW_RUN_NUMBER", "0"))
        if rn % 24 == 0:
            ntfy_post(NTFY_CMD, "GROUPWATCH_ALIVE", f"alive (github-actions) run={rn}")
    else:
        print("unknown phase"); sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        ntfy_post(NTFY_CMD, "GW_ERROR", f"{type(e).__name__}: {e}"[:500], prio="high")
        print("FATAL", type(e).__name__, str(e)[:300])
        sys.exit(1)
