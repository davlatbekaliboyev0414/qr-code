import asyncio
import io
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import qrcode
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ───────────────────────── Sozlamalar ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
CURRENCY = os.getenv("CURRENCY", "XTR").upper()
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "bot.db")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))

PRICE_MULT = 1 if CURRENCY == "XTR" else 100
PLANS = {30: "1 oy", 90: "3 oy", 365: "1 yil"}
COLORS = {
    "black": ("⚫ Qora", "#000000"),
    "blue": ("🔵 Ko'k", "#1d4ed8"),
    "red": ("🔴 Qizil", "#dc2626"),
    "green": ("🟢 Yashil", "#16a34a"),
    "purple": ("🟣 Binafsha", "#7c3aed"),
}
DEFAULT_PRICES = (
    {"price_30": "100", "price_90": "250", "price_365": "800"}
    if CURRENCY == "XTR"
    else {"price_30": "15000", "price_90": "40000", "price_365": "140000"}
)
DEFAULTS = {"free_limit": "5", "premium_limit": "0", **DEFAULT_PRICES}

# Referal bonusi: yangi odam keltirganda +3 QR
REFERRAL_BONUS = 3

BTN_LIMIT = "📊 Limitim"
BTN_PREMIUM = "💎 Premium"
BTN_COLOR = "🎨 QR rangi"
BTN_REFERRAL = "👥 Referal"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_LIMIT), KeyboardButton(text=BTN_PREMIUM)],
        [KeyboardButton(text=BTN_COLOR), KeyboardButton(text=BTN_REFERRAL)],
    ],
    resize_keyboard=True,
)


def now() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return now().date().isoformat()


# ───────────────────────── Ma'lumotlar bazasi ─────────────────────────
db: aiosqlite.Connection


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            joined_at TEXT, banned INTEGER DEFAULT 0,
            premium_until TEXT, qr_color TEXT DEFAULT 'black',
            referred_by INTEGER DEFAULT NULL,
            referral_bonus INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS usage(
            user_id INTEGER, day TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(user_id, day));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, days INTEGER,
            amount INTEGER, currency TEXT, charge_id TEXT, created_at TEXT);
        """
    )
    # Eski bazaga yangi ustunlar qo'shish (agar yo'q bo'lsa)
    try:
        await db.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN referral_bonus INTEGER DEFAULT 0")
    except Exception:
        pass
    await db.commit()


async def get_setting(key: str) -> int:
    cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return int(row["value"] if row else DEFAULTS[key])


async def set_setting(key: str, value: int):
    await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))
    await db.commit()


async def upsert_user(u, referred_by: int = None):
    await db.execute(
        "INSERT INTO users(user_id, username, full_name, joined_at, referred_by) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (u.id, u.username, u.full_name, now().isoformat(), referred_by),
    )
    await db.commit()


async def get_user(user_id: int):
    cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return await cur.fetchone()


async def is_new_user(user_id: int) -> bool:
    cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    return await cur.fetchone() is None


def is_premium(u) -> bool:
    return bool(u and u["premium_until"] and datetime.fromisoformat(u["premium_until"]) > now())


async def add_premium(user_id: int, days: int) -> datetime:
    await db.execute("INSERT OR IGNORE INTO users(user_id, joined_at) VALUES(?,?)", (user_id, now().isoformat()))
    u = await get_user(user_id)
    base = now()
    if is_premium(u):
        base = datetime.fromisoformat(u["premium_until"])
    new_until = base + timedelta(days=days)
    await db.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until.isoformat(), user_id))
    await db.commit()
    return new_until


async def used_today(user_id: int) -> int:
    cur = await db.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (user_id, today()))
    row = await cur.fetchone()
    return row["count"] if row else 0


async def inc_usage(user_id: int):
    await db.execute(
        "INSERT INTO usage(user_id, day, count) VALUES(?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
        (user_id, today()),
    )
    await db.commit()


async def user_limit(u) -> int:
    """0 = cheksiz"""
    return await get_setting("premium_limit" if is_premium(u) else "free_limit")


async def get_referral_bonus(user_id: int) -> int:
    cur = await db.execute("SELECT referral_bonus FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return row["referral_bonus"] if row else 0


async def add_referral_bonus(user_id: int, amount: int):
    await db.execute(
        "UPDATE users SET referral_bonus=referral_bonus+? WHERE user_id=?",
        (amount, user_id),
    )
    await db.commit()


async def use_referral_bonus(user_id: int) -> int:
    """Bonusni ishlatadi va qancha ishlatilganini qaytaradi"""
    bonus = await get_referral_bonus(user_id)
    if bonus > 0:
        await db.execute("UPDATE users SET referral_bonus=0 WHERE user_id=?", (user_id,))
        await db.commit()
    return bonus


async def count_referrals(user_id: int) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (user_id,))
    row = await cur.fetchone()
    return row[0] if row else 0


# ───────────────────────── QR generatsiya ─────────────────────────
def make_qr(data: str, premium: bool, color: str) -> bytes:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H if premium else qrcode.constants.ERROR_CORRECT_M,
        box_size=16 if premium else 10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color=color if premium else "#000000", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ───────────────────────── Ban middleware ─────────────────────────
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id not in ADMIN_IDS:
            u = await get_user(user.id)
            if u and u["banned"]:
                if isinstance(event, Message):
                    await event.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 Bloklangansiz.", show_alert=True)
                return
        return await handler(event, data)


# ───────────────────────── Admin panel ─────────────────────────
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class A(StatesGroup):
    free_limit = State()
    premium_limit = State()
    price = State()
    grant = State()
    ban = State()
    unban = State()
    broadcast = State()


def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📈 Statistika", callback_data="adm:stats")
    b.button(text="⚙️ Bepul limit", callback_data="adm:free_limit")
    b.button(text="💎 Premium limit", callback_data="adm:premium_limit")
    for d, name in PLANS.items():
        b.button(text=f"💰 Narx: {name}", callback_data=f"adm:price:{d}")
    b.button(text="🎁 Premium berish", callback_data="adm:grant")
    b.button(text="🚫 Ban", callback_data="adm:ban")
    b.button(text="✅ Unban", callback_data="adm:unban")
    b.button(text="📢 Xabar yuborish", callback_data="adm:broadcast")
    b.adjust(1, 2, 3, 1, 2, 1)
    return b.as_markup()


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb(), parse_mode="HTML")


@admin.message(Command("cancel"))
async def admin_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=admin_kb())


@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    async def one(q, *a):
        cur = await db.execute(q, a)
        r = await cur.fetchone()
        return r[0] or 0

    total = await one("SELECT COUNT(*) FROM users")
    banned = await one("SELECT COUNT(*) FROM users WHERE banned=1")
    prem = await one("SELECT COUNT(*) FROM users WHERE premium_until > ?", now().isoformat())
    active = await one("SELECT COUNT(*) FROM usage WHERE day=?", today())
    qr_today = await one("SELECT SUM(count) FROM usage WHERE day=?", today())
    qr_all = await one("SELECT SUM(count) FROM usage")
    rev = await one("SELECT SUM(amount) FROM payments WHERE currency=?", CURRENCY)
    rev_shown = rev // PRICE_MULT
    referred = await one("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL")
    fl, pl = await get_setting("free_limit"), await get_setting("premium_limit")
    text = (
        "📈 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: {total}\n"
        f"💎 Premium: {prem}\n"
        f"🚫 Banlangan: {banned}\n"
        f"🔥 Bugun faol: {active}\n"
        f"🧾 Bugungi QR: {qr_today}\n"
        f"🧾 Jami QR: {qr_all}\n"
        f"👥 Referal orqali kelgan: {referred}\n"
        f"💰 Daromad: {rev_shown} {CURRENCY}\n\n"
        f"⚙️ Bepul limit: {fl}/kun\n"
        f"💎 Premium limit: {'cheksiz' if pl == 0 else str(pl) + '/kun'}"
    )
    await c.message.edit_text(text, reply_markup=admin_kb(), parse_mode="HTML")
    await c.answer()


PROMPTS = {
    "free_limit": (A.free_limit, "Bepul foydalanuvchilar uchun kunlik limitni kiriting (son):"),
    "premium_limit": (A.premium_limit, "Premium uchun kunlik limitni kiriting (0 = cheksiz):"),
    "grant": (A.grant, "Format: <code>user_id kun</code>\nMasalan: <code>123456789 30</code>"),
    "ban": (A.ban, "Banlanadigan foydalanuvchi ID sini yuboring:"),
    "unban": (A.unban, "Unban qilinadigan foydalanuvchi ID sini yuboring:"),
    "broadcast": (A.broadcast, "Barcha foydalanuvchilarga yuboriladigan xabar matnini yozing:"),
}


@admin.callback_query(F.data.startswith("adm:") & ~F.data.in_({"adm:stats"}))
async def adm_prompt(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    key = parts[1]
    if key == "price":
        days = int(parts[2])
        await state.set_state(A.price)
        await state.update_data(days=days)
        await c.message.answer(f"{PLANS[days]} uchun yangi narxni kiriting ({CURRENCY}, butun son):")
    elif key in PROMPTS:
        st, text = PROMPTS[key]
        await state.set_state(st)
        await c.message.answer(text + "\n\nBekor qilish: /cancel", parse_mode="HTML")
    await c.answer()


def _int(text: str):
    try:
        v = int(text.strip())
        return v if v >= 0 else None
    except ValueError:
        return None


@admin.message(A.free_limit)
async def set_free(m: Message, state: FSMContext):
    v = _int(m.text or "")
    if v is None:
        return await m.answer("Iltimos, musbat son kiriting.")
    await set_setting("free_limit", v)
    await state.clear()
    await m.answer(f"✅ Bepul limit: {v}/kun", reply_markup=admin_kb())


@admin.message(A.premium_limit)
async def set_prem(m: Message, state: FSMContext):
    v = _int(m.text or "")
    if v is None:
        return await m.answer("Iltimos, musbat son kiriting.")
    await set_setting("premium_limit", v)
    await state.clear()
    await m.answer(f"✅ Premium limit: {'cheksiz' if v == 0 else str(v) + '/kun'}", reply_markup=admin_kb())


@admin.message(A.price)
async def set_price(m: Message, state: FSMContext):
    v = _int(m.text or "")
    if not v:
        return await m.answer("Iltimos, 0 dan katta son kiriting.")
    days = (await state.get_data())["days"]
    await set_setting(f"price_{days}", v)
    await state.clear()
    await m.answer(f"✅ {PLANS[days]} narxi: {v} {CURRENCY}", reply_markup=admin_kb())


@admin.message(A.grant)
async def do_grant(m: Message, state: FSMContext):
    try:
        uid, days = (int(x) for x in (m.text or "").split())
    except ValueError:
        return await m.answer("Format noto'g'ri. Masalan: <code>123456789 30</code>", parse_mode="HTML")
    until = await add_premium(uid, days)
    await state.clear()
    await m.answer(f"✅ {uid} ga {days} kun premium berildi (gacha: {until:%Y-%m-%d}).", reply_markup=admin_kb())
    try:
        await m.bot.send_message(uid, f"🎁 Sizga {days} kunlik Premium berildi!")
    except Exception:
        pass


@admin.message(A.ban)
@admin.message(A.unban)
async def do_ban(m: Message, state: FSMContext):
    uid = _int(m.text or "")
    if uid is None:
        return await m.answer("ID noto'g'ri.")
    ban = (await state.get_state()) == A.ban.state
    await db.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if ban else 0, uid))
    await db.commit()
    await state.clear()
    await m.answer(("🚫 Banlandi: " if ban else "✅ Unban qilindi: ") + str(uid), reply_markup=admin_kb())


@admin.message(A.broadcast)
async def do_broadcast(m: Message, state: FSMContext):
    await state.clear()
    cur = await db.execute("SELECT user_id FROM users WHERE banned=0")
    ids = [r["user_id"] for r in await cur.fetchall()]
    await m.answer(f"📢 Yuborish boshlandi ({len(ids)} ta)...")
    ok = 0
    for uid in ids:
        try:
            await m.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yuborildi: {ok}/{len(ids)}", reply_markup=admin_kb())


# ───────────────────────── Foydalanuvchi qismi ─────────────────────────
user_router = Router()


@user_router.message(CommandStart())
async def start(m: Message):
    # Referal tekshirish: /start ref_12345678 shaklida
    args = m.text.split() if m.text else []
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1][4:])
            if ref_id != m.from_user.id:  # O'ziga o'zi referal bo'lmasin
                new = await is_new_user(m.from_user.id)
                if new:
                    referred_by = ref_id
        except ValueError:
            pass

    await upsert_user(m.from_user, referred_by=referred_by)

    # Referal bonusini berish
    if referred_by:
        await add_referral_bonus(referred_by, REFERRAL_BONUS)
        try:
            ref_user = await get_user(referred_by)
            ref_count = await count_referrals(referred_by)
            await m.bot.send_message(
                referred_by,
                f"🎉 Sizning referal havolangiz orqali yangi odam keldi!\n"
                f"+{REFERRAL_BONUS} ta qo'shimcha QR bonusi berildi.\n"
                f"Jami keltirilgan: {ref_count} ta odam 👥",
            )
        except Exception:
            pass

    await m.answer(
        "👋 Salom! Menga istalgan matn yoki havola yuboring — men uni QR kodga aylantirib beraman.\n\n"
        "📊 Limit va 💎 Premium bo'limlari pastdagi tugmalarda.\n"
        "👥 Do'stlaringni taklif qil va qo'shimcha QR ol!",
        reply_markup=MAIN_KB,
    )


async def limit_text(user_id: int) -> str:
    u = await get_user(user_id)
    lim = await user_limit(u)
    used = await used_today(user_id)
    bonus = await get_referral_bonus(user_id)
    bonus_text = f"\n🎁 Referal bonusi: {bonus} ta QR" if bonus > 0 else ""
    if is_premium(u):
        until = datetime.fromisoformat(u["premium_until"]).strftime("%Y-%m-%d")
        lim_s = "cheksiz" if lim == 0 else str(lim)
        return f"💎 Premium faol (gacha: {until})\nBugun: {used} / {lim_s}{bonus_text}"
    return f"🆓 Bepul tarif\nBugun: {used} / {lim}{bonus_text}\n\nKo'proq uchun 💎 Premium oling."


@user_router.message(F.text == BTN_LIMIT)
async def my_limit(m: Message):
    await upsert_user(m.from_user)
    await m.answer(await limit_text(m.from_user.id))


async def plans_kb():
    b = InlineKeyboardBuilder()
    for d, name in PLANS.items():
        price = await get_setting(f"price_{d}")
        b.button(text=f"{name} — {price} {CURRENCY}", callback_data=f"buy:{d}")
    b.adjust(1)
    return b.as_markup()


@user_router.message(F.text == BTN_PREMIUM)
@user_router.message(Command("premium"))
async def premium_menu(m: Message):
    await upsert_user(m.from_user)
    pl = await get_setting("premium_limit")
    await m.answer(
        "💎 <b>Premium afzalliklari</b>\n"
        f"• Kunlik limit: {'cheksiz' if pl == 0 else pl}\n"
        "• Yuqori sifatli (katta) QR\n"
        "• QR rangini tanlash\n\n"
        "Tarifni tanlang:",
        reply_markup=await plans_kb(),
        parse_mode="HTML",
    )


@user_router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    days = int(c.data.split(":")[1])
    if days not in PLANS:
        return await c.answer()
    price = await get_setting(f"price_{days}")
    await c.bot.send_invoice(
        chat_id=c.from_user.id,
        title=f"Premium — {PLANS[days]}",
        description=f"QR bot Premium obunasi ({PLANS[days]})",
        payload=f"premium:{days}",
        provider_token=None if CURRENCY == "XTR" else PROVIDER_TOKEN,
        currency=CURRENCY,
        prices=[LabeledPrice(label=f"Premium {PLANS[days]}", amount=price * PRICE_MULT)],
    )
    await c.answer()


@user_router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@user_router.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    days = int(sp.invoice_payload.split(":")[1])
    until = await add_premium(m.from_user.id, days)
    await db.execute(
        "INSERT INTO payments(user_id, days, amount, currency, charge_id, created_at) VALUES(?,?,?,?,?,?)",
        (m.from_user.id, days, sp.total_amount, sp.currency, sp.telegram_payment_charge_id, now().isoformat()),
    )
    await db.commit()
    await m.answer(f"✅ To'lov qabul qilindi! Premium {until:%Y-%m-%d} gacha faol. 🎉")
    for a in ADMIN_IDS:
        try:
            await m.bot.send_message(
                a, f"💰 Yangi to'lov: {m.from_user.full_name} ({m.from_user.id}) — {PLANS.get(days, days)}, "
                   f"{sp.total_amount // PRICE_MULT} {sp.currency}"
            )
        except Exception:
            pass


@user_router.message(F.text == BTN_COLOR)
async def color_menu(m: Message):
    u = await get_user(m.from_user.id)
    if not is_premium(u):
        return await m.answer("🎨 QR rangini tanlash faqat 💎 Premium uchun.")
    b = InlineKeyboardBuilder()
    for key, (label, _) in COLORS.items():
        b.button(text=label, callback_data=f"color:{key}")
    b.adjust(2)
    await m.answer("QR rangini tanlang:", reply_markup=b.as_markup())


@user_router.callback_query(F.data.startswith("color:"))
async def set_color(c: CallbackQuery):
    key = c.data.split(":")[1]
    if key not in COLORS:
        return await c.answer()
    await db.execute("UPDATE users SET qr_color=? WHERE user_id=?", (key, c.from_user.id))
    await db.commit()
    await c.message.edit_text(f"✅ Rang tanlandi: {COLORS[key][0]}")
    await c.answer()


# ───────────────────────── Referal tugmasi ─────────────────────────
@user_router.message(F.text == BTN_REFERRAL)
async def referral_menu(m: Message):
    await upsert_user(m.from_user)
    ref_count = await count_referrals(m.from_user.id)
    bonus = await get_referral_bonus(m.from_user.id)
    bot_info = await m.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{m.from_user.id}"
    await m.answer(
        f"👥 <b>Referal tizimi</b>\n\n"
        f"Do'stingni taklif qil — har bir yangi odam uchun <b>+{REFERRAL_BONUS} ta</b> qo'shimcha QR olasan!\n\n"
        f"📊 Sening natijang:\n"
        f"• Keltirilgan odamlar: <b>{ref_count} ta</b>\n"
        f"• Hozirgi bonus: <b>{bonus} ta QR</b>\n\n"
        f"🔗 Havola:\n<code>{ref_link}</code>\n\n"
        f"Yuqoridagi havolani nusxa olib do'stlaringga yubor!",
        parse_mode="HTML",
    )


# ───────────────────────── QR generatsiya ─────────────────────────
@user_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def generate(m: Message):
    await upsert_user(m.from_user)
    u = await get_user(m.from_user.id)
    lim = await user_limit(u)
    used = await used_today(m.from_user.id)
    bonus = await get_referral_bonus(m.from_user.id)

    # Limit tekshirish: avval oddiy limit, keyin bonus ishlatiladi
    if lim and used >= lim:
        if bonus > 0:
            # Bonusdan foydalanish
            await use_referral_bonus(m.from_user.id)
            await add_referral_bonus(m.from_user.id, bonus - 1)  # 1 tasini ishlatdik
        else:
            return await m.answer(
                f"⛔ Kunlik limit tugadi ({used}/{lim}).\n"
                f"Ertaga yangilanadi yoki 💎 Premium oling.\n"
                f"Yoki do'stlarni taklif qilib bonus ol! 👥",
                reply_markup=await plans_kb() if not is_premium(u) else None,
            )

    text = m.text.strip()
    if len(text) > 1000:
        return await m.answer("Matn juda uzun (maksimum 1000 belgi).")
    color = COLORS.get(u["qr_color"] or "black", COLORS["black"])[1]
    png = await asyncio.to_thread(make_qr, text, is_premium(u), color)
    await inc_usage(m.from_user.id)

    # Bonus ishlatilganligi haqida xabar
    u_updated = await get_user(m.from_user.id)
    new_bonus = await get_referral_bonus(m.from_user.id)
    bonus_note = f" (bonus ishlatildi, qoldi: {new_bonus})" if bonus > 0 and used >= lim else ""

    await m.answer_photo(
        BufferedInputFile(png, "qr.png"),
        caption=f"✅ Tayyor!{bonus_note} ({used + 1}{'/' + str(lim) if lim else ''})"
    )


# ───────────────────────── Ishga tushirish ─────────────────────────
async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env faylida ko'rsatilmagan!")
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.include_router(admin)
    dp.include_router(user_router)
    logging.info("Bot ishga tushdi")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
