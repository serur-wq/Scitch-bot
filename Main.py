import os
import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from langdetect import detect

TOKEN = os.environ.get("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- DATABASE ----------
conn = sqlite3.connect("messages.db")
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS messages(
id INTEGER PRIMARY KEY,
chat INTEGER,
text TEXT
)
"""
)
conn.commit()

# ---------- LANGUAGE CHECK ----------
def is_ukrainian(text):
    try:
        return detect(text) == "uk"
    except:
        return True

# ---------- START ----------
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("🤖 Scitch Bot активний.")

# ---------- SEARCH ----------
@dp.message(Command("search"))
async def search(message: Message):
    query = message.text.replace("/search",""").strip()

    rows = cur.execute(
        "SELECT text FROM messages WHERE text LIKE ? LIMIT 5",
        (f"%{query}%,")
    ).fetchall()

    if not rows:
        await message.answer("Нічого не знайдено.")
        return

    result = "\n\n".join(r[0][:200] for r in rows)
    await message.answer(f"🔎 Результати:\n\n{result}")

# ---------- TAG SEARCH ----------
@dp.message(Command("tag"))
async def tag(message: Message):
    tag = message.text.replace("/tag",""").strip()

    rows = cur.execute(
        "SELECT text FROM messages WHERE text LIKE ? LIMIT 5",
        (f"%{tag}%,")
    ).fetchall()

    if not rows:
        await message.answer("Тег не знайдено.")
        return

    result = "\n\n".join(r[0][:200] for r in rows)
    await message.answer(result)

# ---------- MAIN MESSAGE HANDLER ----------
@dp.message(F.text)
async def handle(message: Message):

    text = message.text

    # save message
    cur.execute(
        "INSERT INTO messages(chat,text) VALUES(?,?)",
        (message.chat.id, text)
    )
    conn.commit()

    # language rule
    if len(text) > 12 and not is_ukrainian(text):
        await message.reply(
            "🇺🇦 У чаті використовується українська мова.\n"
            "Будь ласка, продублюйте повідомлення українською.\n\n"
            "🇫🇷 Merci d'écrire en ukrainien."
        )

    # hashtag hint
    if "квартира" in text.lower():
        await message.reply("💡 Рекомендований тег: #оренда")

async def main():
    print("Bot running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
