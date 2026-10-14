
# -*- coding: utf-8 -*-
# =====================================================================
#  OILAVIY HISOBCHI — Telegram bot (Google Colab uchun, bitta katak)
# =====================================================================

# ---------- 0. KUTUBXONALARNI O‘RNATISH (kodning o‘zida) ----------
import subprocess
import sys

subprocess.run(
    [sys.executable, "-m", "pip", "install", "-q",
     "python-telegram-bot==21.10", "nest_asyncio"],
    check=False,
)

import logging
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

try:
    import fcntl  # faqat Linux/Colab'da bor
except ImportError:
    fcntl = None

import nest_asyncio
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import Conflict, InvalidToken, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------- 1. SOZLAMALAR ----------
ADMIN_ID = 1935206106            # Administrator Telegram ID
CURRENCY = "so‘m"                # Pul birligi
TZ = timezone(timedelta(hours=5))  # O‘zbekiston vaqti (UTC+5)
MAX_AMOUNT = Decimal("1000000000000")  # Eng katta ruxsat etilgan summa
MAX_CATEGORY_LEN = 40
MAX_NOTE_LEN = 200

# Ma’lumotlar bazasi joyi. Colab sessiyasi o‘chsa /content tozalanadi.
# Doimiy saqlash uchun USE_GOOGLE_DRIVE = True qiling (Google Drive ulanadi).
USE_GOOGLE_DRIVE = False
DB_PATH = "/content/oilaviy_hisobchi.db" if os.path.isdir("/content") else "oilaviy_hisobchi.db"
if USE_GOOGLE_DRIVE:
    try:
        from google.colab import drive  # type: ignore
        drive.mount("/content/drive")
        os.makedirs("/content/drive/MyDrive/OilaviyHisobchi", exist_ok=True)
        DB_PATH = "/content/drive/MyDrive/OilaviyHisobchi/oilaviy_hisobchi.db"
    except Exception as exc:
        print("⚠️ Google Drive ulanmadi, baza vaqtinchalik joyda saqlanadi:", type(exc).__name__)

LOCK_PATH = "/tmp/oilaviy_hisobchi.lock"

# Ovozni matnga aylantirish xizmati ulanganmi? (Hozir ulanmagan)
STT_ENABLED = False

# Tugma matnlari
BTN_INCOME = "💰 Daromad qo‘shish"
BTN_EXPENSE = "💸 Xarajat qo‘shish"
BTN_BALANCE = "🏦 Mening balansim"
BTN_REPORT = "📊 Mening hisobotim"
BTN_FAMILY = "👨‍👩‍👦 Oilaviy hisobot"
BTN_CANCEL = "❌ Bekor qilish"
BTN_SKIP = "⏭ O‘tkazib yuborish"
MENU_TEXTS = {BTN_INCOME, BTN_EXPENSE, BTN_BALANCE, BTN_REPORT, BTN_FAMILY}

INCOME_CATS = ["Maosh", "Biznes", "Sovg‘a", "Boshqa daromad"]
EXPENSE_CATS = ["Oziq-ovqat", "Transport", "Kommunal", "Kiyim-kechak",
                "Sog‘liq", "Ta’lim", "Ko‘ngil ochish", "Boshqa xarajat"]

KIND_LABEL = {"income": "💰 Daromad", "expense": "💸 Xarajat"}

AMOUNT, CATEGORY, NOTE = range(3)
PRIVATE = filters.ChatType.PRIVATE
FLOW_TEXT = filters.TEXT & ~filters.COMMAND & ~filters.Text([BTN_CANCEL])

BOT_TOKEN = ""  # faqat xotirada, hech qayerga chop etilmaydi
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
logger = logging.getLogger("oilaviy_hisobchi")


# ---------- 2. LOG (token yashiriladi) ----------
class RedactingFormatter(logging.Formatter):
    """Log matnidagi tokenni *** bilan almashtiradi."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact(text)


def redact(text: str) -> str:
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "***TOKEN***")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***TOKEN***", text)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # httpx so‘rov manzillarini (ichida token bor) chiqarmasligi uchun
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------- 3. MA’LUMOTLAR BAZASI ----------
def now_local() -> datetime:
    return datetime.now(TZ)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with closing(get_conn()) as conn:
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    user_id    INTEGER PRIMARY KEY,
                    full_name  TEXT NOT NULL DEFAULT '',
                    username   TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS transactions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_id        TEXT NOT NULL UNIQUE,
                    user_id      INTEGER NOT NULL,
                    type         TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                    category     TEXT NOT NULL,
                    note         TEXT NOT NULL DEFAULT '',
                    date         TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions (user_id, date)"
            )


def ensure_user(user) -> None:
    full_name = (user.full_name or "").strip()[:100]
    username = user.username
    with closing(get_conn()) as conn:
        with conn:
            conn.execute(
                """INSERT INTO users (user_id, full_name, username, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       full_name = excluded.full_name,
                       username  = excluded.username""",
                (user.id, full_name, username, now_local().strftime("%Y-%m-%d %H:%M:%S")),
            )


def add_transaction(op_id: str, user_id: int, kind: str, amount_minor: int,
                    category: str, note: str) -> bool:
    """Yozuv qo‘shadi. Xuddi shu op_id bilan ikkinchi marta qo‘shilmaydi (False qaytadi)."""
    now = now_local()
    try:
        with closing(get_conn()) as conn:
            with conn:
                conn.execute(
                    """INSERT INTO transactions
                       (op_id, user_id, type, amount_minor, category, note, date, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (op_id, user_id, kind, amount_minor, category, note,
                     now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")),
                )
        return True
    except sqlite3.IntegrityError as exc:
        if "op_id" in str(exc):
            return False
        raise


def get_totals(user_id: int, date_from: Optional[str] = None,
               date_to: Optional[str] = None) -> Tuple[int, int]:
    """(daromad, xarajat) — faqat shu foydalanuvchiniki. date_to kiritilmaydi (qat’iy)."""
    sql = "SELECT type, COALESCE(SUM(amount_minor), 0) FROM transactions WHERE user_id = ?"
    params = [user_id]
    if date_from:
        sql += " AND date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND date < ?"
        params.append(date_to)
    sql += " GROUP BY type"
    income = expense = 0
    with closing(get_conn()) as conn:
        for kind, total in conn.execute(sql, params):
            if kind == "income":
                income = total
            else:
                expense = total
    return income, expense


def get_top_expense_categories(user_id: int, date_from: str, date_to: str, limit: int = 3):
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT category, SUM(amount_minor) AS s FROM transactions
               WHERE user_id = ? AND type = 'expense' AND date >= ? AND date < ?
               GROUP BY category ORDER BY s DESC LIMIT ?""",
            (user_id, date_from, date_to, limit),
        ).fetchall()


def get_recent(user_id: int, limit: int = 5):
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT type, amount_minor, category, note, date FROM transactions
               WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()


def get_family_totals():
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT u.user_id, u.full_name, u.username,
                      COALESCE(SUM(CASE WHEN t.type = 'income'  THEN t.amount_minor END), 0),
                      COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount_minor END), 0)
               FROM users u
               LEFT JOIN transactions t ON t.user_id = u.user_id
               GROUP BY u.user_id
               ORDER BY u.created_at, u.user_id"""
        ).fetchall()


# ---------- 4. YORDAMCHI FUNKSIYALAR ----------
def fmt_money(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 100)
    text = f"{whole:,}".replace(",", " ")
    if frac:
        text += f".{frac:02d}"
    return f"{sign}{text} {CURRENCY}"


def parse_amount(text: str) -> Tuple[Optional[int], Optional[str]]:
    """(tiyin_hisobida_summa, xato_matni). Xato bo‘lsa summa None."""
    cleaned = text.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if cleaned.startswith("-"):
        return None, "❌ Summa manfiy bo‘lishi mumkin emas. Musbat son kiriting."
    if not re.fullmatch(r"\d{1,15}(\.\d{1,2})?", cleaned):
        return None, ("❌ Summa noto‘g‘ri. Faqat raqam kiriting, masalan: 150000 yoki 25000.50")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None, "❌ Summani o‘qib bo‘lmadi. Masalan: 150000"
    if value <= 0:
        return None, "❌ Summa noldan katta bo‘lishi kerak."
    if value > MAX_AMOUNT:
        return None, "❌ Summa juda katta. Iltimos, tekshirib qayta kiriting."
    return int(value * 100), None


def is_admin(user) -> bool:
    return user is not None and user.id == ADMIN_ID


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [[BTN_INCOME, BTN_EXPENSE], [BTN_BALANCE, BTN_REPORT]]
    if user_id == ADMIN_ID:
        rows.append([BTN_FAMILY])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


def category_keyboard(kind: str) -> ReplyKeyboardMarkup:
    cats = INCOME_CATS if kind == "income" else EXPENSE_CATS
    rows = [cats[i:i + 2] for i in range(0, len(cats), 2)]
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_SKIP], [BTN_CANCEL]], resize_keyboard=True)


def block(title: str, income: int, expense: int) -> str:
    return (f"{title}\n"
            f"💰 Daromad: {fmt_money(income)}\n"
            f"💸 Xarajat: {fmt_money(expense)}\n"
            f"📌 Balans: {fmt_money(income - expense)}")


async def send_long(update: Update, text: str, limit: int = 3800) -> None:
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk)


# ---------- 5. BUYRUQLAR: /start, /help, /cancel ----------
HELP_TEXT = (
    "📖 Oilaviy Hisobchi — yordam\n\n"
    "💰 Daromad qo‘shish — pul kirimini yozish\n"
    "💸 Xarajat qo‘shish — sarflangan pulni yozish\n"
    "🏦 Mening balansim — jami daromad, xarajat va balans\n"
    "📊 Mening hisobotim — bugun, shu oy va umumiy hisobot\n"
    "👨‍👩‍👦 Oilaviy hisobot — faqat administrator uchun\n\n"
    "Buyruqlar:\n"
    "/start — botni boshlash\n"
    "/help — yordam\n"
    "/cancel — joriy amalni bekor qilish\n\n"
    "🔒 Sizning yozuvlaringizni boshqa oila a’zolari ko‘ra olmaydi."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END
    ensure_user(user)
    context.user_data.clear()
    await update.message.reply_text(
        f"Assalomu alaykum, {user.first_name or 'do‘st'}! 👋\n"
        "Men Oilaviy Hisobchi botman. Daromad va xarajatlaringizni yozib borishga yordam beraman.\n"
        "Quyidagi menyudan tanlang 👇",
        reply_markup=main_keyboard(user.id),
    )
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return
    await update.message.reply_text(HELP_TEXT, reply_markup=main_keyboard(user.id))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END
    had_flow = bool(context.user_data.get("flow"))
    context.user_data.clear()
    text = "❌ Amal bekor qilindi." if had_flow else "Bekor qilinadigan amal yo‘q."
    await update.message.reply_text(text, reply_markup=main_keyboard(user.id))
    return ConversationHandler.END


# ---------- 6. DAROMAD / XARAJAT KIRITISH ----------
async def begin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str):
    user = update.effective_user
    if update.message is None or user is None:
        return ConversationHandler.END
    ensure_user(user)
    context.user_data.clear()
    # op_id — har bir amal uchun noyob raqam: ikki marta yozilib ketishdan himoya
    context.user_data["flow"] = {"kind": kind, "op_id": uuid.uuid4().hex}
    word = "daromad" if kind == "income" else "xarajat"
    await update.message.reply_text(
        f"{KIND_LABEL[kind]} qo‘shamiz.\n"
        f"Iltimos, {word} summasini yozing ({CURRENCY}), masalan: 150000",
        reply_markup=cancel_keyboard(),
    )
    return AMOUNT


async def begin_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await begin_flow(update, context, "income")


async def begin_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await begin_flow(update, context, "expense")


async def lost_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Amal ma’lumotlari yo‘qolgan bo‘lsa (masalan bot qayta ishga tushgan)."""
    context.user_data.clear()
    user = update.effective_user
    await update.message.reply_text(
        "⚠️ Bu amal eskirgan. Iltimos, menyudan qaytadan tanlang.",
        reply_markup=main_keyboard(user.id),
    )
    return ConversationHandler.END


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    if not flow:
        return await lost_flow(update, context)
    text = (update.message.text or "").strip()
    if text in MENU_TEXTS:
        await update.message.reply_text(
            "Avval joriy amalni tugating: summani yozing yoki ❌ Bekor qilish tugmasini bosing."
        )
        return AMOUNT
    amount_minor, error = parse_amount(text)
    if error:
        await update.message.reply_text(error)
        return AMOUNT
    flow["amount_minor"] = amount_minor
    await update.message.reply_text(
        f"✅ Summa: {fmt_money(amount_minor)}\n\n"
        "Endi kategoriyani tanlang yoki o‘zingiz yozing:",
        reply_markup=category_keyboard(flow["kind"]),
    )
    return CATEGORY


async def receive_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    if not flow or "amount_minor" not in flow:
        return await lost_flow(update, context)
    text = " ".join((update.message.text or "").split())
    if text in MENU_TEXTS or text == BTN_SKIP:
        await update.message.reply_text(
            "Avval kategoriyani tanlang yoki ❌ Bekor qilish tugmasini bosing."
        )
        return CATEGORY
    if not text:
        await update.message.reply_text("Kategoriya bo‘sh bo‘lmasligi kerak.")
        return CATEGORY
    if len(text) > MAX_CATEGORY_LEN:
        await update.message.reply_text(
            f"Kategoriya juda uzun (eng ko‘pi {MAX_CATEGORY_LEN} belgi). Qisqaroq yozing."
        )
        return CATEGORY
    flow["category"] = text
    await update.message.reply_text(
        "📝 Izoh yozing (ixtiyoriy) yoki «⏭ O‘tkazib yuborish» tugmasini bosing.",
        reply_markup=skip_keyboard(),
    )
    return NOTE


async def receive_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    user = update.effective_user
    if not flow or "category" not in flow:
        return await lost_flow(update, context)
    text = " ".join((update.message.text or "").split())
    if text in MENU_TEXTS:
        await update.message.reply_text(
            "Avval izoh yozing, o‘tkazib yuboring yoki ❌ Bekor qilish tugmasini bosing."
        )
        return NOTE
    note = "" if text == BTN_SKIP else text
    if len(note) > MAX_NOTE_LEN:
        await update.message.reply_text(
            f"Izoh juda uzun (eng ko‘pi {MAX_NOTE_LEN} belgi). Qisqaroq yozing."
        )
        return NOTE

    try:
        ensure_user(user)
        saved = add_transaction(
            op_id=flow["op_id"], user_id=user.id, kind=flow["kind"],
            amount_minor=flow["amount_minor"], category=flow["category"], note=note,
        )
        income, expense = get_totals(user.id)
    except sqlite3.Error:
        logger.exception("Bazaga yozishda xatolik (user_id=%s)", user.id)
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ Saqlashda xatolik yuz berdi. Iltimos, birozdan so‘ng qayta urinib ko‘ring.",
            reply_markup=main_keyboard(user.id),
        )
        return ConversationHandler.END

    kind = flow["kind"]
    if saved:
        logger.info("Yozuv saqlandi (user_id=%s, turi=%s)", user.id, kind)
        text_out = (
            f"✅ Saqlandi!\n"
            f"Turi: {KIND_LABEL[kind]}\n"
            f"Summa: {fmt_money(flow['amount_minor'])}\n"
            f"Kategoriya: {flow['category']}\n"
            f"Izoh: {note or '—'}\n"
            f"Sana: {now_local().strftime('%d.%m.%Y')}\n\n"
            f"🏦 Joriy balansingiz: {fmt_money(income - expense)}"
        )
    else:
        text_out = "ℹ️ Bu yozuv allaqachon saqlangan, ikkinchi marta yozilmadi."
    context.user_data.clear()
    await update.message.reply_text(text_out, reply_markup=main_keyboard(user.id))
    return ConversationHandler.END


def wrong_type_handler(state: int) -> MessageHandler:
    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Iltimos, matn ko‘rinishida yozing yoki ❌ Bekor qilish tugmasini bosing."
        )
        return state

    return MessageHandler(PRIVATE & ~filters.TEXT & ~filters.COMMAND, _handler)


# ---------- 7. HISOBOTLAR ----------
async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return
    ensure_user(user)
    income, expense = get_totals(user.id)  # faqat shu foydalanuvchi ID si bo‘yicha
    await update.message.reply_text(
        block("🏦 Mening balansim (hamma vaqt)", income, expense),
        reply_markup=main_keyboard(user.id),
    )


async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return
    ensure_user(user)
    uid = user.id
    today = now_local().date()
    tomorrow = today + timedelta(days=1)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)

    d_inc, d_exp = get_totals(uid, today.isoformat(), tomorrow.isoformat())
    m_inc, m_exp = get_totals(uid, month_start.isoformat(), next_month.isoformat())
    a_inc, a_exp = get_totals(uid)

    parts = [
        "📊 Mening hisobotim",
        block(f"📅 Bugun ({today.strftime('%d.%m.%Y')})", d_inc, d_exp),
        block(f"🗓 Joriy oy ({month_start.strftime('%m.%Y')})", m_inc, m_exp),
        block("🧮 Jami (hamma vaqt)", a_inc, a_exp),
    ]

    top = get_top_expense_categories(uid, month_start.isoformat(), next_month.isoformat())
    if top:
        lines = ["🔝 Shu oy eng ko‘p xarajat kategoriyalari:"]
        for i, (cat, total) in enumerate(top, 1):
            lines.append(f"{i}. {cat} — {fmt_money(total)}")
        parts.append("\n".join(lines))

    recent = get_recent(uid)
    if recent:
        lines = ["🕘 Oxirgi yozuvlar:"]
        for kind, amount, cat, note, date in recent:
            icon = "💰" if kind == "income" else "💸"
            line = f"{icon} {date} — {fmt_money(amount)} — {cat}"
            if note:
                line += f" ({note})"
            lines.append(line)
        parts.append("\n".join(lines))

    await send_long(update, "\n\n".join(parts))


async def show_family_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return
    # Huquq tekshiruvi serverda: Telegram bergan haqiqiy ID bilan solishtiriladi
    if not is_admin(user):
        logger.warning("Ruxsatsiz oilaviy hisobot urinishi (user_id=%s)", user.id)
        await update.message.reply_text(
            "⛔ Oilaviy hisobot faqat administrator uchun.",
            reply_markup=main_keyboard(user.id),
        )
        return
    rows = get_family_totals()
    if not rows:
        await update.message.reply_text("Hozircha oilada foydalanuvchilar yo‘q.")
        return
    lines = ["👨‍👩‍👦 Oilaviy hisobot (hamma vaqt)", ""]
    total_inc = total_exp = 0
    for i, (uid, full_name, username, inc, exp) in enumerate(rows, 1):
        name = full_name or (f"@{username}" if username else f"ID {uid}")
        if username and full_name:
            name += f" (@{username})"
        lines.append(f"{i}. {name} [ID {uid}]")
        lines.append(f"   💰 {fmt_money(inc)}")
        lines.append(f"   💸 {fmt_money(exp)}")
        lines.append(f"   📌 {fmt_money(inc - exp)}")
        lines.append("")
        total_inc += inc
        total_exp += exp
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"👥 Foydalanuvchilar soni: {len(rows)}")
    lines.append(f"💰 Oila jami daromadi: {fmt_money(total_inc)}")
    lines.append(f"💸 Oila jami xarajati: {fmt_money(total_exp)}")
    lines.append(f"📌 Oila balansi: {fmt_money(total_inc - total_exp)}")
    await send_long(update, "\n".join(lines))


# ---------- 8. OVOZLI XABAR ----------
async def speech_to_text(audio_bytes: bytes) -> Optional[str]:
    """
    OVOZNI MATNGA AYLANTIRISH UCHUN TASHQI XIZMAT KERAK (masalan OpenAI Whisper,
    Google Speech-to-Text). Ular API kalit talab qiladi va pullik bo‘lishi mumkin.
    Hozir hech narsa ulanmagan, shuning uchun None qaytadi.
    Keyinchalik shu funksiyani to‘ldirasiz va STT_ENABLED = True qilasiz.
    """
    return None


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return
    if not STT_ENABLED:
        await update.message.reply_text(
            "🎙 Ovozli xabaringiz qabul qilindi, lekin ovozni matnga aylantirish xizmati "
            "hozircha sozlanmagan, shuning uchun uni tushuna olmayman va hech qanday yozuv "
            "yaratmadim.\n\nIltimos, menyudagi tugmalar orqali matn bilan kiriting.",
            reply_markup=main_keyboard(user.id),
        )
        return
    voice = update.message.voice
    if voice is None or (voice.file_size or 0) > 10 * 1024 * 1024:
        await update.message.reply_text("Ovozli xabar juda katta yoki o‘qilmadi.")
        return
    tg_file = await voice.get_file()
    audio = bytes(await tg_file.download_as_bytearray())
    recognized = await speech_to_text(audio)
    if not recognized:
        await update.message.reply_text("Ovozni tushuna olmadim. Matn bilan yozing.")
        return
    # Xavfsizlik uchun: ovozdan avtomatik yozuv yaratilmaydi, foydalanuvchi tasdiqlaydi
    await update.message.reply_text(
        f"🎙 Tanilgan matn: {recognized[:300]}\n\n"
        "Xavfsizlik uchun ovozdan avtomatik yozuv yaratmayman. "
        "Iltimos, ma’lumotni menyu orqali kiriting.",
        reply_markup=main_keyboard(user.id),
    )


# ---------- 9. BOSHQA XABARLAR VA XATOLAR ----------
async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message is None or user is None:
        return
    await update.message.reply_text(
        "Tushunmadim 🤔 Iltimos, menyudagi tugmalardan birini tanlang yoki /help ni bosing.",
        reply_markup=main_keyboard(user.id),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logger.error("Conflict: shu bot boshqa joyda ham ishlayapti. Bu nusxa to‘xtatiladi.")
        try:
            context.application.stop_running()
        except Exception:
            pass
        return
    if isinstance(err, NetworkError):
        logger.warning("Tarmoq xatosi (avtomatik qayta uriniladi): %s", type(err).__name__)
        return
    logger.error("Kutilmagan xato: %s", type(err).__name__,
                 exc_info=(type(err), err, err.__traceback__) if err else None)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Kutilmagan xatolik yuz berdi. Iltimos, /cancel ni bosib, qaytadan urinib ko‘ring."
            )
        except Exception:
            pass


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands([
            ("start", "Botni boshlash"),
            ("help", "Yordam"),
            ("cancel", "Amalni bekor qilish"),
        ])
    except Exception as exc:
        logger.warning("Buyruqlar ro‘yxatini o‘rnatib bo‘lmadi: %s", type(exc).__name__)


# ---------- 10. BOTNI YIG‘ISH ----------
def build_app(token: str) -> Application:
    app = Application.builder().token(token).post_init(post_init).build()

    conversation = ConversationHandler(
        entry_points=[
            MessageHandler(PRIVATE & filters.Text([BTN_INCOME]), begin_income),
            MessageHandler(PRIVATE & filters.Text([BTN_EXPENSE]), begin_expense),
        ],
        states={
            AMOUNT: [MessageHandler(PRIVATE & FLOW_TEXT, receive_amount), wrong_type_handler(AMOUNT)],
            CATEGORY: [MessageHandler(PRIVATE & FLOW_TEXT, receive_category), wrong_type_handler(CATEGORY)],
            NOTE: [MessageHandler(PRIVATE & FLOW_TEXT, receive_note), wrong_type_handler(NOTE)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel, filters=PRIVATE),
            MessageHandler(PRIVATE & filters.Text([BTN_CANCEL]), cancel),
            CommandHandler("start", start, filters=PRIVATE),
        ],
        allow_reentry=False,
    )
    app.add_handler(conversation)

    app.add_handler(CommandHandler("start", start, filters=PRIVATE))
    app.add_handler(CommandHandler("help", help_cmd, filters=PRIVATE))
    app.add_handler(CommandHandler("cancel", cancel, filters=PRIVATE))
    app.add_handler(MessageHandler(PRIVATE & filters.Text([BTN_CANCEL]), cancel))
    app.add_handler(MessageHandler(PRIVATE & filters.Text([BTN_BALANCE]), show_balance))
    app.add_handler(MessageHandler(PRIVATE & filters.Text([BTN_REPORT]), show_report))
    app.add_handler(MessageHandler(PRIVATE & filters.Text([BTN_FAMILY]), show_family_report))
    app.add_handler(MessageHandler(PRIVATE & filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(PRIVATE & filters.TEXT & ~filters.COMMAND, unknown_text))
    app.add_error_handler(on_error)
    return app


# ---------- 11. ISHGA TUSHIRISH ----------
def ask_token() -> Optional[str]:
    for _ in range(3):
        token = os.getenv("BOT_TOKEN","").strip()
        if TOKEN_RE.fullmatch(token):
            return token
        print("❌ Token formati noto‘g‘ri (masalan: 123456789:AAxxxxxxxx...). Qayta urinib ko‘ring.")
    return None


def acquire_lock():
    """Bir vaqtda faqat bitta nusxa ishlashi uchun."""
    if fcntl is None:
        return True
    try:
        handle = open(LOCK_PATH, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        return None


def release_lock(handle) -> None:
    if handle is None or handle is True:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
    except Exception:
        pass


def run_bot() -> None:
    global BOT_TOKEN
    token = ask_token()
    if not token:
        print("❌ To‘g‘ri token kiritilmadi. Katakni qayta ishga tushiring.")
        return
    BOT_TOKEN = token
    setup_logging()
    init_db()

    lock = acquire_lock()
    if lock is None:
        print("❌ Bot allaqachon ishlayapti (boshqa nusxa). Runtime → Restart session qiling.")
        return
    try:
        nest_asyncio.apply()  # Colab ichida asyncio ishlashi uchun
        app = build_app(token)
        print("✅ Bot ishga tushdi. To‘xtatish uchun katakdagi ■ (Stop) tugmasini bosing.")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            stop_signals=None,   # Colab uchun
            close_loop=False,    # Colab'ning event loopini yopmaslik uchun
        )
    except InvalidToken:
        print("❌ Token Telegram tomonidan qabul qilinmadi. @BotFather'dan tekshiring.")
    except KeyboardInterrupt:
        print("🛑 Bot to‘xtatildi.")
    except Exception as exc:
        print("❌ Bot ishga tushmadi:", type(exc).__name__, redact(str(exc)))
    finally:
        release_lock(lock)
        print("Bot to‘xtadi.")


if __name__ == "__main__":
    run_bot()
