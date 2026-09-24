"""Telegram file-sharing bot: upload center, channel locks, broadcast, stats.
Storage: Supabase (PostgREST over httpx). Framework: python-telegram-bot v21+.
"""
import asyncio
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

import httpx
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, ReplyKeyboardRemove, Update)
from telegram.constants import ChatMemberStatus as S
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SB_URL = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# ───────────────────────── database (Supabase REST) ─────────────────────────
_http = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        headers = {"apikey": SB_KEY}
        if SB_KEY.startswith("eyJ"):  # legacy service_role JWT key
            headers["Authorization"] = f"Bearer {SB_KEY}"
        _http = httpx.AsyncClient(base_url=SB_URL, timeout=20, headers=headers)
    return _http


async def q_select(table, params=None):
    r = await http().get(f"/{table}", params=params or {})
    r.raise_for_status()
    return r.json()


async def q_count(table, params=None):
    p = dict(params or {})
    p.update(select="*", limit="1")
    r = await http().get(f"/{table}", params=p, headers={"Prefer": "count=exact"})
    r.raise_for_status()
    total = r.headers.get("content-range", "*/0").split("/")[-1]
    return int(total) if total.isdigit() else 0


async def q_insert(table, rows, upsert_on=None):
    prefer = "return=minimal" + (",resolution=merge-duplicates" if upsert_on else "")
    r = await http().post(f"/{table}", json=rows,
                          params={"on_conflict": upsert_on} if upsert_on else None,
                          headers={"Prefer": prefer})
    r.raise_for_status()


async def q_update(table, match, values):
    r = await http().patch(f"/{table}", params={k: f"eq.{v}" for k, v in match.items()},
                           json=values, headers={"Prefer": "return=minimal"})
    r.raise_for_status()


async def q_delete(table, match):
    r = await http().delete(f"/{table}", params={k: f"eq.{v}" for k, v in match.items()},
                            headers={"Prefer": "return=minimal"})
    r.raise_for_status()


async def get_setting(key):
    rows = await q_select("settings", {"key": f"eq.{key}"})
    return rows[0]["value"] if rows else None


async def set_setting(key, value):
    if value is None:
        await q_delete("settings", {"key": key})
    else:
        await q_insert("settings", {"key": key, "value": value}, upsert_on="key")


async def is_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    return bool(await q_select("admins", {"id": f"eq.{uid}"}))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── keyboards / labels ────────────────────────────
B_STATS, B_BC = "📊 آمار ربات", "📨 ارسال همگانی"
B_UP, B_LOCKS = "📤 آپلود فایل", "🔒 قفل ها"
B_SET, B_EXIT = "⚙️ تنظیمات", "🔙 خروج از پنل"
B_BACK, B_CANCEL = "🔙 بازگشت به پنل", "❌ لغو"
B_SINGLE, B_GROUP, B_DONE = "📤 آپلود تکی", "📦 آپلود گروهی", "✅ پایان آپلود"
B_START_TXT, B_CAPTION, B_ADMINS = "📝 متن استارت", "🖊 کپشن پیشفرض", "👥 ادمین ها"


def kb(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


ADMIN_KB = kb([[B_STATS, B_BC], [B_UP, B_LOCKS], [B_SET, B_EXIT]])
DEFAULT_START = "سلام 👋\nبرای دریافت فایل از لینک اختصاصی استفاده کن."


async def panel(msg):
    await msg.reply_text("به پنل مدیریت خوش اومدی 🌹", reply_markup=ADMIN_KB)


# ─────────────────────────────── locks / delivery ────────────────────────────
JOINED = {S.MEMBER, S.ADMINISTRATOR, S.OWNER}


async def is_joined(bot, chat_id, uid) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
    except TelegramError:
        return True  # can't verify (bot removed etc.) -> don't block the user
    return m.status in JOINED or (m.status == S.RESTRICTED and getattr(m, "is_member", False))


async def check_locks(bot, uid):
    locks = await q_select("locks")
    missing, optional = [], []
    for lk in locks:
        if lk["mandatory"]:
            if not await is_joined(bot, lk["chat_id"], uid):
                missing.append(lk)
        else:
            optional.append(lk)
    return missing, optional


async def serve(chat_id, uid, code, ctx, query=None):
    link = await q_select("links", {"code": f"eq.{code}"})
    if not link:
        if query:
            await query.answer("❌ لینک نامعتبره", show_alert=True)
        else:
            await ctx.bot.send_message(chat_id, "❌ این لینک نامعتبره یا حذف شده.")
        return

    missing, optional = await check_locks(ctx.bot, uid)
    if missing:
        if query:
            await query.answer("❗️ هنوز تو همه کانال‌ها عضو نشدی", show_alert=True)
            return
        rows = [[InlineKeyboardButton(f"📢 {lk['title']}", url=lk["link"])]
                for lk in missing + optional]
        rows.append([InlineKeyboardButton("✅ عضو شدم", callback_data=f"chk:{code}")])
        await ctx.bot.send_message(
            chat_id, "برای دریافت فایل اول تو کانال‌های زیر عضو شو، بعد دکمه «عضو شدم» رو بزن 👇",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if query:
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError:
            pass

    files = await q_select("files", {"code": f"eq.{code}", "order": "position.asc"})
    default_cap = await get_setting("default_caption")
    for f in files:
        cap = f["caption"] or default_cap
        try:
            await getattr(ctx.bot, "send_" + f["file_type"])(chat_id, f["file_id"], caption=cap)
        except TelegramError:
            log.exception("send failed")
        await asyncio.sleep(0.05)
    await q_update("links", {"code": code}, {"downloads": link[0]["downloads"] + 1})


# ────────────────────────────────── handlers ─────────────────────────────────
async def track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u, c = update.effective_user, update.effective_chat
    if u and c and c.type == "private":
        try:
            await q_update("users", {"id": u.id}, {"last_seen": now_iso(), "active": True})
        except Exception:
            log.exception("track failed")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await q_insert("users", {"id": u.id, "first_name": u.first_name,
                             "last_seen": now_iso(), "active": True}, upsert_on="id")
    if ctx.args:
        await serve(update.effective_chat.id, u.id, ctx.args[0], ctx)
        return
    text = await get_setting("start_text") or DEFAULT_START
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await is_admin(update.effective_user.id):
        ctx.user_data.clear()
        await panel(update.message)


def extract(msg):
    if msg.document:
        return msg.document.file_id, "document"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.audio:
        return msg.audio.file_id, "audio"
    if msg.voice:
        return msg.voice.file_id, "voice"
    if msg.animation:
        return msg.animation.file_id, "animation"
    return None


async def make_link(ctx, items) -> str:
    code = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    await q_insert("links", {"code": code})
    await q_insert("files", [{"code": code, "file_id": fid, "file_type": ft,
                              "caption": cap, "position": i}
                             for i, (fid, ft, cap) in enumerate(items)])
    return f"https://t.me/{ctx.bot.username}?start={code}"


async def stats_text() -> str:
    def ago(h):
        return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()

    (total, active, d1, d7, d30, h1, h4, h12, files) = await asyncio.gather(
        q_count("users"), q_count("users", {"active": "eq.true"}),
        q_count("users", {"joined_at": f"gte.{ago(24)}"}),
        q_count("users", {"joined_at": f"gte.{ago(24 * 7)}"}),
        q_count("users", {"joined_at": f"gte.{ago(24 * 30)}"}),
        q_count("users", {"last_seen": f"gte.{ago(1)}"}),
        q_count("users", {"last_seen": f"gte.{ago(4)}"}),
        q_count("users", {"last_seen": f"gte.{ago(12)}"}),
        q_count("links"))
    return (f"📊 آمار ربات\n\n"
            f"👥 کل کاربران: {total:,}\n"
            f"✅ کاربران فعال: {active:,}\n\n"
            f"📥 عضو جدید روز/هفته/ماه:\n{d1:,} / {d7:,} / {d30:,}\n\n"
            f"🕐 فعال در ۱/۴/۱۲ ساعت گذشته:\n{h1:,} / {h4:,} / {h12:,}\n\n"
            f"📁 تعداد لینک‌های فایل: {files:,}")


async def show_locks(msg):
    locks = await q_select("locks", {"order": "chat_id.asc"})
    rows = [[InlineKeyboardButton(f"{'🔒' if lk['mandatory'] else '🔓'} {lk['title']}  ✖️",
                                  callback_data=f"lk_del:{lk['chat_id']}")] for lk in locks]
    rows.append([InlineKeyboardButton("➕ افزودن قفل", callback_data="lk_add")])
    await msg.reply_text(
        "🔒 = اجباری | 🔓 = اختیاری\nروی هر قفل بزنی حذف می‌شه.\n\n"
        "قبل از افزودن، ربات رو ادمین کانال/گروه کن.",
        reply_markup=InlineKeyboardMarkup(rows))


async def show_admins(msg, uid):
    rows = await q_select("admins")
    owner = uid == OWNER_ID
    btns = [[InlineKeyboardButton(f"✖️ {r['id']}", callback_data=f"adm_del:{r['id']}")]
            for r in rows] if owner else []
    if owner:
        btns.append([InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm_add")])
    ids = ", ".join(str(r["id"]) for r in rows) or "—"
    await msg.reply_text(
        f"👥 مالک: {OWNER_ID}\nادمین‌ها: {ids}\n\n"
        + ("روی آیدی بزنی حذف می‌شه." if owner else "فقط مالک می‌تونه ادمین اضافه/حذف کنه."),
        reply_markup=InlineKeyboardMarkup(btns) if btns else None)


async def broadcast(ctx, admin_chat, src_chat, src_msg):
    ok = fail = 0
    last = 0
    while True:
        rows = await q_select("users", {"select": "id", "active": "eq.true",
                                        "id": f"gt.{last}", "order": "id.asc", "limit": "1000"})
        if not rows:
            break
        for r in rows:
            last = r["id"]
            for attempt in range(2):
                try:
                    await ctx.bot.copy_message(r["id"], src_chat, src_msg)
                    ok += 1
                    break
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Forbidden:
                    fail += 1
                    await q_update("users", {"id": r["id"]}, {"active": False})
                    break
                except TelegramError:
                    fail += 1
                    break
            await asyncio.sleep(0.05)
    await ctx.bot.send_message(admin_chat, f"📨 ارسال همگانی تموم شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, uid = update.effective_message, update.effective_user.id
    if not await is_admin(uid):
        return
    text = (msg.text or "").strip()
    ud = ctx.user_data
    st = ud.get("state")

    # ---- menu buttons ----
    if text == B_EXIT:
        ud.clear()
        await msg.reply_text("از پنل خارج شدی. برای برگشت /admin رو بزن.",
                             reply_markup=ReplyKeyboardRemove())
        return
    if text in (B_BACK, B_CANCEL):
        ud.clear()
        await panel(msg)
        return
    if text == B_STATS:
        await msg.reply_text(await stats_text())
        return
    if text == B_UP:
        ud.clear()
        await msg.reply_text("به بخش آپلود خوش اومدی. یکی رو انتخاب کن:",
                             reply_markup=kb([[B_SINGLE, B_GROUP], [B_BACK]]))
        return
    if text == B_SINGLE:
        ud.clear()
        ud["state"] = "up_single"
        await msg.reply_text("فایلت رو بفرست 📎 (هر فایل یه لینک جدا می‌گیره)",
                             reply_markup=kb([[B_CANCEL]]))
        return
    if text == B_GROUP:
        ud.clear()
        ud.update(state="up_group", batch=[])
        await msg.reply_text("فایل‌ها رو یکی‌یکی بفرست، آخرش «پایان آپلود» رو بزن.",
                             reply_markup=kb([[B_DONE, B_CANCEL]]))
        return
    if text == B_DONE and st == "up_group":
        batch = ud.get("batch") or []
        if not batch:
            await msg.reply_text("هنوز فایلی نفرستادی.")
            return
        link = await make_link(ctx, batch)
        ud.clear()
        await msg.reply_text(f"✅ {len(batch)} فایل ذخیره شد.\n\n🔗 {link}", reply_markup=ADMIN_KB)
        return
    if text == B_LOCKS:
        await show_locks(msg)
        return
    if text == B_BC:
        ud.clear()
        ud["state"] = "broadcast"
        await msg.reply_text("پیامی که می‌خوای برای همه بره رو بفرست (متن، عکس، فایل...).",
                             reply_markup=kb([[B_CANCEL]]))
        return
    if text == B_SET:
        ud.clear()
        await msg.reply_text("تنظیمات:", reply_markup=kb([[B_START_TXT, B_CAPTION], [B_ADMINS], [B_BACK]]))
        return
    if text == B_START_TXT:
        ud.clear()
        ud["state"] = "set_start"
        cur = await get_setting("start_text") or "(تنظیم نشده)"
        await msg.reply_text(f"متن فعلی:\n{cur}\n\nمتن جدید رو بفرست. برای حذف بفرست: -",
                             reply_markup=kb([[B_CANCEL]]))
        return
    if text == B_CAPTION:
        ud.clear()
        ud["state"] = "set_caption"
        cur = await get_setting("default_caption") or "(تنظیم نشده)"
        await msg.reply_text(f"کپشن پیشفرض فعلی:\n{cur}\n\nکپشن جدید رو بفرست. برای حذف بفرست: -",
                             reply_markup=kb([[B_CANCEL]]))
        return
    if text == B_ADMINS:
        await show_admins(msg, uid)
        return

    # ---- states ----
    if st in ("up_single", "up_group"):
        item = extract(msg)
        if not item:
            await msg.reply_text("فقط فایل/عکس/ویدیو/صدا بفرست.")
            return
        item = (item[0], item[1], msg.caption)
        if st == "up_single":
            link = await make_link(ctx, [item])
            await msg.reply_text(f"✅ ذخیره شد.\n\n🔗 {link}")
        else:
            ud["batch"].append(item)
            await msg.reply_text(f"✅ فایل {len(ud['batch'])} اضافه شد.")
        return

    if st == "broadcast":
        ud.clear()
        ctx.application.create_task(broadcast(ctx, msg.chat_id, msg.chat_id, msg.message_id))
        await msg.reply_text("⏳ ارسال شروع شد. وقتی تموم شد خبرت می‌کنم.", reply_markup=ADMIN_KB)
        return

    if st in ("set_start", "set_caption") and text:
        key = "start_text" if st == "set_start" else "default_caption"
        await set_setting(key, None if text == "-" else msg.text)
        ud.clear()
        await msg.reply_text("✅ ذخیره شد.", reply_markup=ADMIN_KB)
        return

    if st == "add_admin":
        if uid == OWNER_ID and text.isdigit():
            await q_insert("admins", {"id": int(text)}, upsert_on="id")
            ud.clear()
            await msg.reply_text("✅ ادمین اضافه شد.", reply_markup=ADMIN_KB)
        else:
            await msg.reply_text("آیدی عددی کاربر رو بفرست.")
        return

    if st == "lock_add":
        fo = getattr(msg, "forward_origin", None)
        target = fo.chat.id if fo is not None and getattr(fo, "chat", None) else text
        if not target:
            await msg.reply_text("یوزرنیم (@channel) یا آیدی عددی رو بفرست، یا یه پیام از کانال فوروارد کن.")
            return
        try:
            chat = await ctx.bot.get_chat(target)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in (S.ADMINISTRATOR, S.OWNER):
                await msg.reply_text("❌ ربات تو اون چت ادمین نیست. اول ادمینش کن.")
                return
            link = (f"https://t.me/{chat.username}" if chat.username
                    else chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id))
        except TelegramError as e:
            await msg.reply_text(f"❌ نتونستم چت رو پیدا کنم یا لینک بگیرم: {e.message}")
            return
        ud["pending_lock"] = {"chat_id": chat.id, "title": chat.title or str(chat.id), "link": link}
        ud["state"] = None
        await msg.reply_text(
            f"«{chat.title}» پیدا شد. نوع قفل؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔒 اجباری", callback_data="lk_mode:1"),
                InlineKeyboardButton("🔓 اختیاری", callback_data="lk_mode:0")]]))
        return


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d, uid, ud = q.data, q.from_user.id, ctx.user_data

    if d.startswith("chk:"):
        await serve(q.message.chat_id, uid, d[4:], ctx, query=q)
        return
    if not await is_admin(uid):
        await q.answer("⛔️", show_alert=True)
        return

    if d == "lk_add":
        ud["state"] = "lock_add"
        await q.answer()
        await q.message.reply_text("یوزرنیم کانال/گروه (@name) یا آیدی عددی رو بفرست، "
                                   "یا یه پیام از کانال فوروارد کن.",
                                   reply_markup=kb([[B_CANCEL]]))
    elif d.startswith("lk_mode:"):
        p = ud.pop("pending_lock", None)
        if not p:
            await q.answer("منقضی شد، دوباره امتحان کن.", show_alert=True)
            return
        await q_insert("locks", {**p, "mandatory": d.endswith(":1")}, upsert_on="chat_id")
        await q.answer("✅ ذخیره شد")
        await q.message.edit_text("✅ قفل اضافه شد.")
    elif d.startswith("lk_del:"):
        await q_delete("locks", {"chat_id": d.split(":", 1)[1]})
        await q.answer("🗑 حذف شد")
        await q.message.edit_text("🗑 قفل حذف شد. برای دیدن لیست دوباره «قفل ها» رو بزن.")
    elif d == "adm_add" and uid == OWNER_ID:
        ud["state"] = "add_admin"
        await q.answer()
        await q.message.reply_text("آیدی عددی ادمین جدید رو بفرست.", reply_markup=kb([[B_CANCEL]]))
    elif d.startswith("adm_del:") and uid == OWNER_ID:
        await q_delete("admins", {"id": d.split(":", 1)[1]})
        await q.answer("🗑 حذف شد")
        await q.message.edit_text("🗑 ادمین حذف شد.")
    else:
        await q.answer()


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(TypeHandler(Update, track), group=-1)
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("admin", admin_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    url = os.getenv("RENDER_EXTERNAL_URL")  # set automatically by Render
    if url:
        app.run_webhook(listen="0.0.0.0", port=int(os.getenv("PORT", "10000")),
                        url_path=TOKEN, webhook_url=f"{url.rstrip('/')}/{TOKEN}",
                        drop_pending_updates=True)
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
