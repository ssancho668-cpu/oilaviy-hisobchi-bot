    if s == FAMILY: await family_report(update, u); return
    if s == USERS: await users_list(update, u); return
    if s == DEBT or s == CLAIM: return

    f = context.user_data.get("flow")
    if not f:
        await msg.reply_text("Kerakli bo‘limni menyudan tanlang.", reply_markup=menu(u)); return
    stage = f["stage"]
    if stage == "amount":
        n = amount_from(s)
        if n is None:
            await msg.reply_text("Summani musbat raqamda kiriting. Masalan: 250000"); return
        f["amount"] = n
        if f["kind"] == "income":
            f["stage"] = "category"
            await msg.reply_text("Daromad turini tanlang:", reply_markup=choice_menu(INCOME_CATS))
        elif f["kind"] == "expense":
            if f.get("forced"):
                f["category"] = f["forced"]; f["stage"] = "note"
                await msg.reply_text("Izoh yozing yoki o‘tkazib yuboring:", reply_markup=choice_menu([SKIP]))
            else:
                f["stage"] = "category"
                await msg.reply_text("Xarajat kategoriyasini tanlang:", reply_markup=choice_menu(EXPENSE_CATS))
        else:
            f["stage"] = "person"
            await msg.reply_text("Shaxsning ismini kiriting:")
        return
    if stage == "category":
        cats = INCOME_CATS if f["kind"] == "income" else EXPENSE_CATS
        if s not in cats:
            await msg.reply_text("Ro‘yxatdan kategoriyani tanlang:", reply_markup=choice_menu(cats)); return
        f["category"] = s; f["stage"] = "note"
        await msg.reply_text("Izoh yozing yoki o‘tkazib yuboring:", reply_markup=choice_menu([SKIP])); return
    if stage == "person":
        if len(s) < 2:
            await msg.reply_text("Ismni kiriting (kamida 2 ta belgi)."); return
        f["person"] = s[:100]; f["stage"] = "note"
        await msg.reply_text("Izoh yozing yoki o‘tkazib yuboring:", reply_markup=choice_menu([SKIP])); return
    if stage == "note":
        note = "" if s == SKIP else s[:200]
        try:
            with db() as c:
                if f["kind"] in ("income", "expense"):
                    c.execute("INSERT INTO transactions(user_id,type,amount,category,note,created_at) VALUES(?,?,?,?,?,?)",
                              (u.id, f["kind"], f["amount"], f["category"], note, now()))
                else:
                    c.execute("INSERT INTO obligations(user_id,kind,amount,person,note,status,created_at) VALUES(?,?,?,?,?,'open',?)",
                              (u.id, f["kind"], f["amount"], f["person"], note, now()))
        except sqlite3.Error:
            log.exception("Database save error")
            await msg.reply_text("Saqlashda xato yuz berdi. Qayta urinib ko‘ring."); return
        kind, n, cat, person = f["kind"], f["amount"], f.get("category",""), f.get("person","")
        context.user_data.pop("flow", None)
        if kind in ("income", "expense"):
            answer = f"✅ Saqlandi!\nTuri: {'Daromad' if kind=='income' else 'Xarajat'}\nSumma: {fmt(n)}\nKategoriya: {cat}\nIzoh: {note or '—'}"
        else:
            answer = f"✅ {'Qarz' if kind=='debt' else 'Haq'} saqlandi!\nSumma: {fmt(n)}\nShaxs: {person}\nIzoh: {note or '—'}"
        await msg.reply_text(answer, reply_markup=menu(u)); return

    context.user_data.pop("flow", None)
    await msg.reply_text("Jarayon bekor qilindi. Menyudan qayta tanlang.", reply_markup=menu(u))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not admin(q.from_user):
        await q.edit_message_text("⛔ Faqat administrator uchun."); return
    try:
        action, raw_id = q.data.split(":", 1); uid = int(raw_id)
    except (ValueError, AttributeError):
        await q.edit_message_text("So‘rov noto‘g‘ri."); return
    if action not in ("approve", "reject"):
        await q.edit_message_text("Amal topilmadi."); return
    with db() as c:
        row = c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await q.edit_message_text("Foydalanuvchi topilmadi."); return
        status = "approved" if action == "approve" else "rejected"
        c.execute("UPDATE users SET status=? WHERE user_id=?", (status, uid))
    await q.edit_message_text("✅ Tasdiqlandi." if action == "approve" else "⛔ Rad etildi.")
    try:
        await context.bot.send_message(uid, "✅ Administrator tasdiqladi. /start bosing." if action=="approve" else "⛔ So‘rov rad etildi.")
    except Exception:
        log.warning("Foydalanuvchiga xabar yuborilmadi: %s", uid)

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await register_or_check(update, context):
        await update.effective_message.reply_text("🎙 Ovozli xabar qabul qilindi, ammo ovozni matnga aylantirish xizmati ulanmagan. Hozircha matn bilan kiriting.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.error("Botning boshqa nusxasi ham ishlayapti. Boshqa nusxani to‘xtating.")
    elif isinstance(err, NetworkError):
        log.warning("Tarmoq xatosi: %s", type(err).__name__)
    else:
        log.exception("Kutilmagan bot xatosi", exc_info=err)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Render Environment'da BOT_TOKEN o‘rnatilmagan.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_error_handler(error_handler)
    log.info("Oilaviy Hisobchi ishga tushmoqda. DB: %s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

if __name__ == "__main__":
    try:
        main()
    except InvalidToken:
        log.error("BOT_TOKEN noto‘g‘ri. @BotFather tokenini tekshiring.")
        raise


