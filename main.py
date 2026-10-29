import asyncio
import html
import logging
import os

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ===================== SOZLAMALAR =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "BOT_TOKENNI_SHU_YERGA_YOZING")
# Admin ID lari (vergul bilan ajrating), @userinfobot dan o'z ID ingizni oling
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()
}
# Majburiy obuna kanallari. Bo'sh qoldirsangiz - o'chiriladi. Masalan: ["@kanal1", "@kanal2"]
# Bot shu kanallarda ADMIN bo'lishi kerak!
CHANNELS: list[str] = []
DB_PATH = os.getenv("DB_PATH", "kino.db")
# ======================================================

logging.basicConfig(level=logging.INFO)
BOT_USERNAME = ""


# ===================== BAZA =====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS movies (
                code INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                title TEXT NOT NULL,
                views INTEGER DEFAULT 0
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT
            )"""
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT)"
        )
        await db.commit()


async def add_user(user):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, full_name, username) VALUES (?, ?, ?)",
            (user.id, user.full_name, user.username),
        )
        await db.commit()


async def add_chat(chat):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)",
            (chat.id, chat.title or ""),
        )
        await db.commit()


async def add_movie(file_id: str, file_type: str, title: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO movies (file_id, file_type, title) VALUES (?, ?, ?)",
            (file_id, file_type, title),
        )
        await db.commit()
        return cur.lastrowid


async def get_movie(code: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT code, file_id, file_type, title, views FROM movies WHERE code = ?",
            (code,),
        )
        row = await cur.fetchone()
        if row:
            await db.execute("UPDATE movies SET views = views + 1 WHERE code = ?", (code,))
            await db.commit()
        return row


async def delete_movie(code: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM movies WHERE code = ?", (code,))
        await db.commit()
        return cur.rowcount > 0


async def list_movies():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code, title, views FROM movies ORDER BY code")
        return await cur.fetchall()


async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        chats = (await (await db.execute("SELECT COUNT(*) FROM chats")).fetchone())[0]
        movies = (await (await db.execute("SELECT COUNT(*) FROM movies")).fetchone())[0]
        views = (
            await (await db.execute("SELECT COALESCE(SUM(views), 0) FROM movies")).fetchone()
        )[0]
        return users, chats, movies, views


async def all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]


# ===================== YORDAMCHI =====================
async def not_subscribed(bot: Bot, user_id: int) -> list[str]:
    missing = []
    for ch in CHANNELS:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(ch)
        except Exception as e:
            logging.warning("Obunani tekshirib bo'lmadi (%s): %s", ch, e)
    return missing


def sub_kb():
    b = InlineKeyboardBuilder()
    for ch in CHANNELS:
        b.button(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")
    b.button(text="✅ Tekshirish", callback_data="check_sub")
    b.adjust(1)
    return b.as_markup()


def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Kino qo'shish", callback_data="adm:add")
    b.button(text="🗑 Kino o'chirish", callback_data="adm:del")
    b.button(text="📋 Kinolar ro'yxati", callback_data="adm:list")
    b.button(text="📊 Statistika", callback_data="adm:stats")
    b.button(text="📢 Xabar yuborish", callback_data="adm:bc")
    b.adjust(2)
    return b.as_markup()


def cancel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="❌ Bekor qilish", callback_data="adm:cancel")
    return b.as_markup()


def movie_caption(code: int, title: str, views: int) -> str:
    return (
        f"🎬 <b>{html.escape(title)}</b>\n\n"
        f"🔢 Kod: <b>{code}</b>\n"
        f"👁 Ko'rishlar: {views}\n\n"
        f"🤖 @{BOT_USERNAME}"
    )


# ===================== ADMIN =====================
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS), F.chat.type == ChatType.PRIVATE)
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddMovie(StatesGroup):
    video = State()
    title = State()


class DelMovie(StatesGroup):
    code = State()


class Broadcast(StatesGroup):
    msg = State()


@admin_router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())


@admin_router.message(Command("cancel"), StateFilter("*"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Bekor qilindi.", reply_markup=admin_kb())


@admin_router.callback_query(F.data == "adm:cancel")
async def cancel_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("❌ Bekor qilindi.", reply_markup=admin_kb())
    await call.answer()


# --- kino qo'shish ---
@admin_router.callback_query(F.data == "adm:add")
async def add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddMovie.video)
    await call.message.answer("🎬 Kinoni (video yoki fayl) yuboring:", reply_markup=cancel_kb())
    await call.answer()


@admin_router.message(AddMovie.video, F.video | F.document)
async def add_video(message: Message, state: FSMContext):
    if message.video:
        file_id, file_type = message.video.file_id, "video"
    else:
        file_id, file_type = message.document.file_id, "document"
    await state.update_data(file_id=file_id, file_type=file_type)
    await state.set_state(AddMovie.title)
    await message.answer("✏️ Kino nomini yozing:", reply_markup=cancel_kb())


@admin_router.message(AddMovie.video)
async def add_video_wrong(message: Message):
    await message.answer("❗ Iltimos, video yoki fayl yuboring.", reply_markup=cancel_kb())


@admin_router.message(AddMovie.title, F.text)
async def add_title(message: Message, state: FSMContext):
    data = await state.get_data()
    title = message.text.strip()[:200]
    code = await add_movie(data["file_id"], data["file_type"], title)
    await state.clear()
    await message.answer(
        f"✅ Kino qo'shildi!\n\n🎬 <b>{html.escape(title)}</b>\n🔢 Kodi: <b>{code}</b>",
        reply_markup=admin_kb(),
    )


# --- kino o'chirish ---
@admin_router.callback_query(F.data == "adm:del")
async def del_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(DelMovie.code)
    await call.message.answer("🗑 O'chiriladigan kino kodini yuboring:", reply_markup=cancel_kb())
    await call.answer()


@admin_router.message(DelMovie.code, F.text)
async def del_code(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❗ Faqat raqam yuboring.", reply_markup=cancel_kb())
        return
    ok = await delete_movie(int(message.text.strip()))
    await state.clear()
    await message.answer(
        "✅ O'chirildi." if ok else "❌ Bunday kodli kino topilmadi.", reply_markup=admin_kb()
    )


# --- ro'yxat ---
@admin_router.callback_query(F.data == "adm:list")
async def list_cb(call: CallbackQuery):
    rows = await list_movies()
    if not rows:
        await call.message.answer("📭 Hozircha kino yo'q.", reply_markup=admin_kb())
    else:
        lines = [f"<b>{c}</b> — {html.escape(t)} (👁 {v})" for c, t, v in rows]
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 3800:
                await call.message.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await call.message.answer(chunk, reply_markup=admin_kb())
    await call.answer()


# --- statistika ---
@admin_router.callback_query(F.data == "adm:stats")
async def stats_cb(call: CallbackQuery):
    users, chats, movies, views = await get_stats()
    await call.message.answer(
        f"📊 <b>Statistika</b>\n\n👤 Foydalanuvchilar: {users}\n👥 Guruhlar: {chats}\n"
        f"🎬 Kinolar: {movies}\n👁 Jami ko'rishlar: {views}",
        reply_markup=admin_kb(),
    )
    await call.answer()


# --- xabar yuborish ---
@admin_router.callback_query(F.data == "adm:bc")
async def bc_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await call.message.answer(
        "📢 Hammaga yuboriladigan xabarni yuboring (matn, rasm, video...):",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@admin_router.message(Broadcast.msg)
async def bc_send(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ids = await all_user_ids()
    await message.answer(f"⏳ Yuborilmoqda... ({len(ids)} ta foydalanuvchi)")
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(uid, message.chat.id, message.message_id)
                ok += 1
            except Exception:
                fail += 1
        except (TelegramForbiddenError, Exception):
            fail += 1
        await asyncio.sleep(0.05)
    await message.answer(
        f"✅ Tugadi.\n\nYetkazildi: {ok}\nYetmadi: {fail}", reply_markup=admin_kb()
    )


# ===================== FOYDALANUVCHI =====================
user_router = Router()


async def send_movie(message: Message, movie):
    code, file_id, file_type, title, views = movie
    caption = movie_caption(code, title, views + 1)
    if file_type == "video":
        await message.reply_video(video=file_id, caption=caption)
    else:
        await message.reply_document(document=file_id, caption=caption)


@user_router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, bot: Bot):
    await add_user(message.from_user)
    if CHANNELS and await not_subscribed(bot, message.from_user.id):
        await message.answer(
            "❗ Botdan foydalanish uchun kanallarga obuna bo'ling:", reply_markup=sub_kb()
        )
        return
    await message.answer(
        f"👋 Salom, <b>{html.escape(message.from_user.full_name)}</b>!\n\n"
        "🎬 Kino olish uchun <b>kino kodini</b> (raqam) yuboring.\n"
        "Masalan: <code>1</code>"
    )


@user_router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery, bot: Bot):
    if await not_subscribed(bot, call.from_user.id):
        await call.answer("❌ Hali hamma kanalga obuna bo'lmadingiz!", show_alert=True)
        return
    await call.message.answer("✅ Rahmat! Endi kino kodini yuboring.")
    await call.answer()


@user_router.message(F.text.regexp(r"^\d{1,9}$"))
async def by_code(message: Message, bot: Bot):
    if message.chat.type == ChatType.PRIVATE:
        await add_user(message.from_user)
        if CHANNELS and await not_subscribed(bot, message.from_user.id):
            await message.answer(
                "❗ Avval kanallarga obuna bo'ling:", reply_markup=sub_kb()
            )
            return
    else:
        await add_chat(message.chat)

    movie = await get_movie(int(message.text))
    if not movie:
        await message.reply("❌ Bunday kodli kino topilmadi.")
        return
    await send_movie(message, movie)


@user_router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def other_text(message: Message):
    await message.answer("🔢 Iltimos, kino kodini (faqat raqam) yuboring.")


# ===================== ISHGA TUSHIRISH =====================
async def main():
    global BOT_USERNAME
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(user_router)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot ishga tushdi: @%s", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
