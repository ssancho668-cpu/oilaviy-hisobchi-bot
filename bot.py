    if stage == "note":
        note = "" if text == B_SKIP else text[:200]
        try:
            with db() as c:
                if flow["kind"] in ("income","expense"):
                    c.execute("""INSERT INTO transactions(op_id,user_id,type,amount,category,note,date)
                                 VALUES(?,?,?,?,?,?,?)""",
                              (flow["op_id"],user.id,flow["kind"],flow["amount"],
                               flow["category"],note,datetime.now(TZ).date().isoformat()))
                else:
                    c.execute("""INSERT INTO obligations(user_id,kind,amount,person,note,status,created_at)
                                 VALUES(?,?,?,?,?,'open',?)""",
                              (user.id,flow["kind"],flow["amount"],flow["person"],note,now_str()))
        except sqlite3.Error:
            logger.exception("Database save error")
            await update.message.reply_text("Saqlashda xato yuz berdi. Qayta urinib ko'ring.")
            return
        kind = flow["kind"]
        amount_text = money(flow["amount"])
        context.user_data.clear()
        if kind in ("income","expense"):
            await update.message.reply_text(
                f"✅ Saqlandi!\nTuri: {'Daromad' if kind=='income' else 'Xarajat'}\n"
                f"Summa: {amount_text}\nKategoriya: {flow['category']}\nIzoh: {note or '—'}",
                reply_markup=keyboard(user))
        else:
            await update.message.reply_text(
                f"✅ {'Qarz' if kind=='debt' else 'Haq'} saqlandi!\nSumma: {amount_text}\n"
                f"Shaxs: {flow['person']}\nIzoh: {note or '—'}",
                reply_markup=keyboard(user))
        return

async def obligations_list(update, user, kind):
    with db() as c:
        rows = c.execute("SELECT amount,person,note,status,created_at FROM obligations WHERE user_id=? AND kind=? ORDER BY id DESC LIMIT 30",
                         (user.id,kind)).fetchall()
    title = "📉 Qarzlarim" if kind == "debt" else "📈 Haqlarim"
    if not rows:
        await update.message.reply_text(f"{title}: hozircha yozuv yo'q.", reply_markup=keyboard(user))
        return
    lines=[title]
    total=0
    for r in rows:
        total += r["amount"]
        lines.append(f"\n• {money(r['amount'])} — {r['person']} ({r['status']})")
        if r["note"]: lines.append(f"  Izoh: {r['note']}")
    lines.append(f"\nJami: {money(total)}")
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard(user))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    user = q.from_user
    if not is_admin(user):
        await q.edit_message_text("⛔ Bu amal faqat administrator uchun.")
        return
    action, uid_text = q.data.split(":",1)
    uid = int(uid_text)
    with db() as c:
        row = c.execute("SELECT status FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await q.edit_message_text("Foydalanuvchi topilmadi."); return
        status = "approved" if action == "approve" else "rejected"
        c.execute("UPDATE users SET status=? WHERE user_id=?", (status,uid))
    await q.edit_message_text("✅ Foydalanuvchi tasdiqlandi." if action=="approve" else "⛔ So'rov rad etildi.")
    try:
        await context.bot.send_message(uid,
            "✅ Administrator sizni tasdiqladi. /start ni bosing." if action=="approve"
            else "⛔ Botdan foydalanish so'rovingiz rad etildi.")
    except Exception:
        logger.warning("User notification failed for %s", uid)

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎙 Ovozli xabar qabul qilindi, lekin ovozni matnga aylantirish xizmati ulanmagan. "
        "Hozircha daromad/xarajatni matn bilan kiriting."
    )

async def error_handler(update, context):
    err = context.error
    if isinstance(err, Conflict):
        logger.error("Telegram Conflict: boshqa bot nusxasi ham ishlayapti. Boshqa nusxani to'xtating.")
    elif isinstance(err, NetworkError):
        logger.warning("Tarmoq xatosi: %s", type(err).__name__)
    else:
        logger.exception("Kutilmagan bot xatosi", exc_info=err)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable o'rnatilmagan.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_text))
    app.add_error_handler(error_handler)
    logger.info("Oilaviy Hisobchi bot ishga tushmoqda. DB: %s", DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

if __name__ == "__main__":
    try:
        main()
    except InvalidToken:
        logger.error("BOT_TOKEN noto'g'ri. @BotFather tokenini tekshiring.")
        raise
