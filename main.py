import os
import re
import time
import sqlite3
import asyncio
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from langdetect import detect, LangDetectException

# ---------------- ENV ----------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Set after you run /adminid in your private admin group
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # 0 = disabled

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------------- Regex ----------------
HASHTAG_RE = re.compile(r"#([\w\d_]+)", re.UNICODE)

SPAM_RE = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|joinchat/\S+|@\w{4,}|"
    r"\b\S+\.(com|net|org|ru|ua|ca|io|gg|me|shop)\b)",
    re.IGNORECASE
)

# ---------------- Cooldowns ----------------
LANG_COOLDOWN_SEC = 120
TAG_COOLDOWN_SEC = 90
SPAM_ALERT_COOLDOWN_SEC = 180

_last_lang: Dict[tuple, int] = {}
_last_tag: Dict[tuple, int] = {}
_last_spam: Dict[tuple, int] = {}

# ---------------- DB ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        msg_id INTEGER NOT NULL,
        thread_id INTEGER,
        user_id INTEGER,
        username TEXT,
        full_name TEXT,
        ts INTEGER NOT NULL,
        text TEXT,
        tags TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON messages(chat_id, ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat_thread_ts ON messages(chat_id, thread_id, ts);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat_text ON messages(chat_id, text);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat_tags ON messages(chat_id, tags);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS topic_rules (
        chat_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        title TEXT,
        required_tags TEXT,
        recommend_tags TEXT,
        PRIMARY KEY (chat_id, thread_id)
    );
    """)

    # Store pending alerts so we can act on them
    cur.execute("""
    CREATE TABLE IF NOT EXISTS spam_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts INTEGER NOT NULL,
        source_chat_id INTEGER NOT NULL,
        source_msg_id INTEGER NOT NULL,
        source_thread_id INTEGER,
        source_user_id INTEGER,
        source_username TEXT,
        source_full_name TEXT,
        reason TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spam_alerts_ts ON spam_alerts(created_ts);")

    con.commit()
    con.close()

def extract_tags(text: str) -> List[str]:
    if not text:
        return []
    return sorted({m.group(1).lower() for m in HASHTAG_RE.finditer(text)})

def detect_lang(text: str) -> str:
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"

def is_ukrainian(text: str) -> bool:
    return detect_lang(text) == "uk"

def cooldown_ok(store: dict, key: tuple, cooldown: int) -> bool:
    now = int(time.time())
    last = store.get(key, 0)
    if now - last < cooldown:
        return False
    store[key] = now
    return True

async def is_admin_async(m: Message) -> bool:
    try:
        member = await bot.get_chat_member(m.chat.id, m.from_user.id)
        return member.status in ("creator", "administrator")
    except:
        return False

def get_topic_rule(chat_id: int, thread_id: int) -> Optional[dict]:
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT title, required_tags, recommend_tags
        FROM topic_rules
        WHERE chat_id=? AND thread_id=?
    """, (chat_id, thread_id))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    title, required_csv, rec_csv = row
    required = [t for t in (required_csv or "").split(",") if t]
    rec = [t for t in (rec_csv or "").split(",") if t]
    return {"title": title or "", "required": required, "recommend": rec}

def set_topic_rule(chat_id: int, thread_id: int, title: str, required: List[str], recommend: List[str]):
    con = db()
    cur = con.cursor()
    cur.execute("""
    INSERT INTO topic_rules(chat_id, thread_id, title, required_tags, recommend_tags)
    VALUES(?,?,?,?,?)
    ON CONFLICT(chat_id, thread_id) DO UPDATE SET
      title=excluded.title,
      required_tags=excluded.required_tags,
      recommend_tags=excluded.recommend_tags
    """, (chat_id, thread_id, title, ",".join(required), ",".join(recommend)))
    con.commit()
    con.close()

def clear_topic_rule(chat_id: int, thread_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM topic_rules WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))
    con.commit()
    con.close()

def list_topic_rules(chat_id: int) -> List[tuple]:
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT thread_id, title, required_tags, recommend_tags
        FROM topic_rules
        WHERE chat_id=?
        ORDER BY thread_id ASC
    """, (chat_id,))
    rows = cur.fetchall()
    con.close()
    return rows

def insert_spam_alert(source_msg: Message, reason: str) -> int:
    con = db()
    cur = con.cursor()
    u = source_msg.from_user
    cur.execute("""
        INSERT INTO spam_alerts(
            created_ts, source_chat_id, source_msg_id, source_thread_id,
            source_user_id, source_username, source_full_name, reason
        ) VALUES (?,?,?,?,?,?,?,?)
    """, (
        int(time.time()),
        source_msg.chat.id,
        source_msg.message_id,
        source_msg.message_thread_id,
        u.id if u else None,
        u.username if u else None,
        u.full_name if u else None,
        reason
    ))
    con.commit()
    alert_id = cur.lastrowid
    con.close()
    return alert_id

def get_spam_alert(alert_id: int) -> Optional[dict]:
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT id, source_chat_id, source_msg_id, source_thread_id,
               source_user_id, source_username, source_full_name, reason
        FROM spam_alerts
        WHERE id=?
    """, (alert_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return {
        "id": row[0],
        "chat_id": row[1],
        "msg_id": row[2],
        "thread_id": row[3],
        "user_id": row[4],
        "username": row[5],
        "full_name": row[6],
        "reason": row[7],
    }

def build_moderation_kb(alert_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Allow", callback_data=f"mod:allow:{alert_id}")
    kb.button(text="🗑 Delete", callback_data=f"mod:del:{alert_id}")
    kb.button(text="🔇 Mute 24h", callback_data=f"mod:mute24:{alert_id}")
    kb.button(text="⛔ Ban", callback_data=f"mod:ban:{alert_id}")
    kb.adjust(2, 2)
    return kb.as_markup()

async def send_spam_alert(source_msg: Message, reason: str):
    if not ADMIN_CHAT_ID:
        return

    alert_id = insert_spam_alert(source_msg, reason)

    u = source_msg.from_user
    who = f"{u.full_name} (@{u.username})" if u and u.username else (u.full_name if u else "unknown")
    chat_title = source_msg.chat.title or str(source_msg.chat.id)
    thread = source_msg.message_thread_id

    text = (
        "🚨 Можлива реклама/лінк\n"
        f"Причина: {reason}\n"
        f"Чат: {chat_title}\n"
        f"Гілка(topic): {thread}\n"
        f"Користувач: {who}\n"
        f"user_id: {u.id if u else '—'}\n"
        f"msg_id: {source_msg.message_id}\n"
        f"alert_id: {alert_id}\n"
    )

    await bot.send_message(ADMIN_CHAT_ID, text, reply_markup=build_moderation_kb(alert_id))
    try:
        await bot.copy_message(
            chat_id=ADMIN_CHAT_ID,
            from_chat_id=source_msg.chat.id,
            message_id=source_msg.message_id
        )
    except:
        pass

# ---------------- Commands ----------------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "🤖 Scitch Bot активний.\n\n"
        "Команди:\n"
        "• /adminid — ID цього чату (використай у Scitch Admin)\n"
        "• /ids — ID гілки (topic)\n"
        "• /setrules #tag1 #tag2 | #rec1 #rec2 — правила гілки (адмін)\n"
        "• /rules — правила гілки\n"
        "• /listrules — всі правила (адмін)\n"
        "• /clearrules — стерти правила (адмін)\n"
        "• /search <текст> — пошук\n"
        "• /tag <#тег> — пошук по тегу\n"
    )

@dp.message(Command("adminid"))
async def cmd_adminid(m: Message):
    await m.reply(f"ADMIN_CHAT_ID: `{m.chat.id}`", parse_mode="Markdown")

@dp.message(Command("ids"))
async def cmd_ids(m: Message):
    tid = m.message_thread_id
    if not tid:
        await m.reply("Це працює всередині гілки (topic). Зайди в гілку і напиши /ids.")
        return
    await m.reply(f"🧵 thread_id цієї гілки: `{tid}`", parse_mode="Markdown")

@dp.message(Command("setrules"))
async def cmd_setrules(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin_async(m):
        await m.reply("Тільки адміністратор може змінювати правила.")
        return
    tid = m.message_thread_id
    if not tid:
        await m.reply("Використай /setrules всередині потрібної гілки (topic).")
        return

    raw = m.text.split(maxsplit=1)
    if len(raw) < 2:
        await m.reply("Формат: /setrules #tag1 #tag2 | #rec1 #rec2")
        return

    text = raw[1].strip()
    parts = [p.strip() for p in text.split("|", 1)]
    req_part = parts[0]
    rec_part = parts[1] if len(parts) > 1 else ""

    req_tags = [t.lower().lstrip("#") for t in req_part.split() if t.strip().startswith("#")]
    rec_tags = [t.lower().lstrip("#") for t in rec_part.split() if t.strip().startswith("#")]

    if not req_tags:
        await m.reply("Потрібно вказати хоча б 1 обов’язковий тег, напр. /setrules #оренда #здам")
        return

    set_topic_rule(m.chat.id, tid, "", req_tags, rec_tags)
    await m.reply(
        "✅ Правила гілки збережено.\n"
        f"Обов’язкові: {' '.join('#'+t for t in req_tags)}\n"
        + (f"Рекомендовані: {' '.join('#'+t for t in rec_tags)}\n" if rec_tags else "")
    )

@dp.message(Command("rules"))
async def cmd_rules(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    tid = m.message_thread_id
    if not tid:
        await m.reply("Відкрий гілку (topic) і напиши /rules.")
        return
    rule = get_topic_rule(m.chat.id, tid)
    if not rule:
        await m.reply("Для цієї гілки правила ще не задані. (Адмін: /setrules ...)")
        return

    req = " ".join("#"+t for t in rule["required"]) if rule["required"] else "—"
    rec = " ".join("#"+t for t in rule["recommend"]) if rule["recommend"] else "—"
    await m.reply(f"🏷 Правила тегів:\nОбов’язкові: {req}\nРекомендовані: {rec}")

@dp.message(Command("listrules"))
async def cmd_listrules(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin_async(m):
        await m.reply("Тільки адміністратор може дивитись список правил.")
        return

    rows = list_topic_rules(m.chat.id)
    if not rows:
        await m.reply("Правила ще не задані.")
        return

    lines = ["🧵 Правила по гілках:"]
    for thread_id, _title, req, rec in rows:
        req_s = " ".join("#"+t for t in (req or "").split(",") if t) or "—"
        rec_s = " ".join("#"+t for t in (rec or "").split(",") if t) or "—"
        lines.append(f"• topic:{thread_id}\n  Обов’язк.: {req_s}\n  Рек.: {rec_s}")
    await m.reply("\n".join(lines))

@dp.message(Command("clearrules"))
async def cmd_clearrules(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return
    if not await is_admin_async(m):
        await m.reply("Тільки адміністратор може стирати правила.")
        return
    tid = m.message_thread_id
    if not tid:
        await m.reply("Використай /clearrules всередині гілки.")
        return
    clear_topic_rule(m.chat.id, tid)
    await m.reply("🗑 Правила цієї гілки видалено.")

@dp.message(Command("search"))
async def cmd_search(m: Message):
    q = m.text.split(maxsplit=1)
    if len(q) < 2 or not q[1].strip():
        await m.reply("Використання: /search текст")
        return
    query = q[1].strip()

    con = db()
    cur = con.cursor()
    cur.execute("""
      SELECT ts, thread_id, username, full_name, msg_id, text
      FROM messages
      WHERE chat_id=? AND text LIKE ?
      ORDER BY ts DESC
      LIMIT 8
    """, (m.chat.id, f"%{query}%"))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await m.reply("Нічого не знайдено (або бот ще мало проіндексував).")
        return

    out = ["🔎 Результати:"]
    for ts, thread_id, username, full_name, msg_id, text in rows:
        who = f"@{username}" if username else (full_name or "user")
        preview = (text or "").replace("\n", " ")
        if len(preview) > 140:
            preview = preview[:140] + "…"
        out.append(f"• {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))} | {who} | topic:{thread_id} | msg:{msg_id}\n  {preview}")
    await m.reply("\n".join(out))

@dp.message(Command("tag"))
async def cmd_tag(m: Message):
    q = m.text.split(maxsplit=1)
    if len(q) < 2 or not q[1].strip():
        await m.reply("Використання: /tag #оренда")
        return
    tag = q[1].strip().lstrip("#").lower()

    con = db()
    cur = con.cursor()
    cur.execute("""
      SELECT ts, thread_id, username, full_name, msg_id, text, tags
      FROM messages
      WHERE chat_id=? AND tags LIKE ?
      ORDER BY ts DESC
      LIMIT 8
    """, (m.chat.id, f"%{tag}%"))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await m.reply("Нічого не знайдено по цьому тегу.")
        return

    out = [f"🏷 Результати по тегу #{tag}:"]
    for ts, thread_id, username, full_name, msg_id, text, _tags in rows:
        who = f"@{username}" if username else (full_name or "user")
        preview = (text or "").replace("\n", " ")
        if len(preview) > 140: