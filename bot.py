# -*- coding: utf-8 -*-
"""
Oilaviy Hisobchi — Render Background Worker uchun.
Environment variable: BOT_TOKEN (majburiy)
Ixtiyoriy: ADMIN_ID (default: 1935206106), DATA_DIR (doimiy disk uchun /var/data)
"""
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.error import Conflict, InvalidToken, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ---------- SETTINGS ----------
ADMIN_ID = int(os.getenv("ADMIN_ID", "1935206106"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CURRENCY = "so'm"
TZ = timezone(timedelta(hours=5))
DATA_DIR = os.getenv("DATA_DIR", "").strip()
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(DATA_DIR, "oilaviy_hisobchi.db")
else:
    # Render doimiy disk ulanmagan bo'lsa, fayl qayta deployda yo'qolishi mumkin.
    DB_PATH = os.getenv("DB_PATH", "oilaviy_hisobchi.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO")
)
logger = logging.getLogger("oilaviy_hisobchi")

# ---------- BUTTONS ----------
B_INCOME = "💰 Daromadlar"
B_EXPENSE = "💸 Xarajatlar"
B_EXTRA = "⚠️ Ortiqcha xarajatlar"
B_DEBT = "📉 Qarzlarim"
B_ADD_DEBT = "➕ Qarz qo‘shish"
B_CLAIM = "📈 Haqlarim"
B_ADD_CLAIM = "➕ Haq qo‘shish"
B_BALANCE = "🏦 Balans"
B_REPORT = "📊 Hisobot"
B_FAMILY = "👨‍👩‍👦 Oilaviy hisobot"
B_USERS = "👥 Foydalanuvchilar"
B_CANCEL = "❌ Bekor qilish"
B_SKIP = "⏭ O‘tkazib yuborish"
B_APPROVE = "✅ Tasdiqlash so‘rovlari"
MAIN_BUTTONS = {
    B_INCOME, B_EXPENSE, B_EXTRA, B_DEBT, B_ADD_DEBT, B_CLAIM, B_ADD_CLAIM,
    B_BALANCE, B_REPORT, B_FAMILY, B_USERS, B_APPROVE
}
INCOME_CATS = ["Maosh", "Biznes", "Sovg'a", "Boshqa daromad"]
EXPENSE_CATS = ["Oziq-ovqat", "Mashina", "Kreditlar", "Kommunal",
                "Kiyim-kechak", "Sog'liq", "Ta'lim", "Ko'ngil ochish",
                "Ortiqcha xarajatlar", "Boshqa xarajat"]

def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL DEFAULT '',
            username TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            op_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('income','expense')),
            amount INTEGER NOT NULL CHECK(amount > 0),
            category TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            date TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS obligations(
