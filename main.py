import os
import re
import time
import sqlite3
import asyncio
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from langdetect import detect, LangDetectException

# ---------------- ENV ----------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

# DB path (Render Free may reset; later you can switch to Postgres)
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Admin alert chat (set after /adminid)
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # 0 = disabled

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------------- Regex ----------------
HASHTAG_RE = re.compile(r"#([\w\d_]+)", re.UNICODE)

# Detect typical spam/ads patterns (tune later)
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
        required_tags TEXT,     -- csv, lowercase without '#'
        recommend_tags TEXT,    -- csv, lowercase without '#'
        PRIMARY KEY (chat_id, thread_id)
    );
    """)
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

async def send_spam_alert(source_msg: Message, reason: str):
    """Send alert + copy message to admin chat (if configured)."""
    if not ADMIN_CHAT_ID:
        return

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
    )

    await bot.send_message(ADMIN_CHAT_ID, text)
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
        "🤖 Scitch Bot (Admin mode) активний.\n\n"
        "Команди:\n"
        "• /adminid — показати ID цього чату (використай у Scitch Admin)\n"
        "• /ids — показати ID гілки (topic)\n"
        "• /setrules #tag1 #tag2 | #rec1 #rec2 — правила гілки (адмін)\n"
        "• /rules — правила поточної гілки\n"
        "• /listrules — всі правила (адмін)\n"
        "• /clearrules — стерти правила гілки (адмін)\n"
        "• /search <текст> — пошук\n"
        "• /tag <#тег> — пошук по тегу\n"
    )

@dp.message(Command("adminid"))
async def cmd_adminid(m: Message):
    # Use this inside your private "Scitch Admin" group to get its chat_id
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
        await m.reply("Формат: /setrules #tag1 #tag2 | #rec1 #rec2\nПісля | — рекомендовані теги (необов’язково).")
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

    title = ""  # optional label
    set_topic_rule(m.chat.id, tid, title, req_tags, rec_tags)
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
        await m.reply("Для цієї гілки правила тегів ще не задані. (Адмін: /setrules ...)")
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
            preview = preview[:140] + "…"
        out.append(f"• {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))} | {who} | topic:{thread_id} | msg:{msg_id}\n  {preview}")
    await m.reply("\n".join(out))

# ---------------- Main handler: index + soft moderation ----------------
# IMPORTANT: ignore commands so they don't block /adminid etc.
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return

    text = m.text or ""
    tid = m.message_thread_id
    uid = m.from_user.id if m.from_user else 0
    username = m.from_user.username if m.from_user else ""
    full_name = m.from_user.full_name if m.from_user else ""
    ts = int(time.time())

    tags = extract_tags(text)
    tags_csv = ",".join(tags)

    # Index message for search
    con = db()
    con.execute("""
      INSERT INTO messages(chat_id, msg_id, thread_id, user_id, username, full_name, ts, text, tags)
      VALUES(?,?,?,?,?,?,?,?,?)
    """, (m.chat.id, m.message_id, tid, uid, username, full_name, ts, text, tags_csv))
    con.commit()
    con.close()

    # UA-only (soft)
    if len(text.strip()) >= 12 and not is_ukrainian(text):
        if cooldown_ok(_last_lang, (m.chat.id, uid), LANG_COOLDOWN_SEC):
            await m.reply(
                "🇺🇦 У чаті пости публікуються українською.\n"
                "Будь ласка, продублюйте/перепишіть ваше повідомлення українською.\n\n"
                "🇫🇷 Le chat publie les messages en ukrainien.\n"
                "Merci de republier votre message en ukrainien."
            )

    # Hashtag rules per topic (soft)
    if tid:
        rule = get_topic_rule(m.chat.id, tid)
        if rule and rule["required"]:
            required = set(rule["required"])
            ok = bool(set(tags) & required)
            if not ok and cooldown_ok(_last_tag, (m.chat.id, uid), TAG_COOLDOWN_SEC):
                req_preview = " ".join("#"+t for t in rule["required"][:10])
                rec_preview = " ".join("#"+t for t in rule["recommend"][:8]) if rule["recommend"] else ""
                msg = (
                    "🧩 Підказка по хештегам:\n"
                    f"Додайте 1 тег: {req_preview}\n"
                )
                if rec_preview:
                    msg += f"Рекомендую: {rec_preview}\n"
                msg += "Приклад: #... + короткий опис (ціна/район/дата тощо)."
                await m.reply(msg)

    # Spam/ads link alert to admin chat (soft, no deletion)
    if ADMIN_CHAT_ID and SPAM_RE.search(text):
        # avoid alert flood per user
        if cooldown_ok(_last_spam, (m.chat.id, uid), SPAM_ALERT_COOLDOWN_SEC):
            await send_spam_alert(m, "Лінк/реклама/username або домен у повідомленні")

async def main():
    init_db()
    print("Bot running…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())