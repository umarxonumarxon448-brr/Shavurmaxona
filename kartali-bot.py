import os, threading, logging, asyncio, math, base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton as IB, InlineKeyboardMarkup as IM
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ConversationHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
SB = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/"
KEY = os.environ["SUPABASE_SECRET_KEY"]
ADMIN = os.environ.get("ADMIN_CHAT_ID")  # ixtiyoriy: yangi buyurtma xabari keladigan chat
H = {"apikey": KEY, "Content-Type": "application/json"}
SCOOTER_KMH = 30  # skuter oʻrtacha tezligi
K_LAT, K_LNG = os.environ.get("KITCHEN_LAT"), os.environ.get("KITCHEN_LNG")  # oshxona koordinatasi (vaqt hisoblash uchun)

MAIN = ReplyKeyboardMarkup([["🍽 Buyurtma berish", "🛒 Savat"], ["📅 Joy bron qilish", "⭐ Sharh qoldirish"]], resize_keyboard=True)
CO_PHONE, CO_ADDR, B_PHONE, B_DATE, B_TIME, B_PEOPLE, B_FOOD, R_STARS, R_TEXT, CO_PAY = range(10)


def so(n):
    return f"{int(n):,}".replace(",", " ") + " soʻm"


async def sb(method, path, json=None):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, SB + path, headers={**H, **({"Prefer": "return=representation"} if method == "POST" else {})}, json=json)
        r.raise_for_status()
        return r.json() if r.text else None


async def items():
    return await sb("GET", "items?select=*&order=id")


def cart(ctx):
    return ctx.user_data.setdefault("cart", {})


async def start(u: Update, ctx):
    await u.message.reply_text("Xush kelibsiz! Quyidan tanlang 👇", reply_markup=MAIN)


# ---------- menyu va savat ----------
async def menu(u: Update, ctx):
    cats = sorted({i["category"] for i in await items() if i.get("category")})
    if not cats:
        await u.message.reply_text("Menyu hozircha boʻsh.")
        return
    await u.message.reply_text("Boʻlimni tanlang:", reply_markup=IM([[IB(c, callback_data="cat:" + c)] for c in cats]))


async def on_cat(u: Update, ctx):
    q = u.callback_query
    await q.answer()
    c = q.data[4:]
    rows = [[IB(f'{i["name"]} · {so(i["price"])} · {i["minutes"]} daq', callback_data=f'add:{i["id"]}')]
            for i in await items() if i["category"] == c]
    rows.append([IB("🛒 Savatni koʻrish", callback_data="cart")])
    await q.message.reply_text(f"{c}: bosib savatga qoʻshing", reply_markup=IM(rows))


async def on_add(u: Update, ctx):
    q = u.callback_query
    i = q.data[4:]
    cart(ctx)[i] = cart(ctx).get(i, 0) + 1
    await q.answer("Savatga qoʻshildi ✅")


async def show_cart(u: Update, ctx):
    msg = u.message or u.callback_query.message
    if u.callback_query:
        await u.callback_query.answer()
    cr = cart(ctx)
    if not cr:
        await msg.reply_text("Savat boʻsh.")
        return
    its = {str(i["id"]): i for i in await items()}
    lines, tot = [], 0
    for k, q in cr.items():
        if k in its:
            tot += its[k]["price"] * q
            lines.append(f'{its[k]["name"]} × {q} = {so(its[k]["price"] * q)}')
    await msg.reply_text("🛒 Savat:\n" + "\n".join(lines) + f"\n\nJami: {so(tot)}\nToʻlov: kuryerga naqd.",
                         reply_markup=IM([[IB("✅ Buyurtma berish", callback_data="checkout")], [IB("🗑 Tozalash", callback_data="clear")]]))


async def on_clear(u: Update, ctx):
    ctx.user_data["cart"] = {}
    await u.callback_query.answer("Tozalandi")


# ---------- buyurtma ----------
async def co_start(u: Update, ctx):
    await u.callback_query.answer()
    if not cart(ctx):
        await u.callback_query.message.reply_text("Savat boʻsh.")
        return ConversationHandler.END
    kb = ReplyKeyboardMarkup([[KeyboardButton("📱 Raqamni yuborish", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await u.callback_query.message.reply_text("Telefon raqamingiz:", reply_markup=kb)
    return CO_PHONE


async def co_phone(u: Update, ctx):
    ctx.user_data["phone"] = u.message.contact.phone_number if u.message.contact else u.message.text
    kb = ReplyKeyboardMarkup([[KeyboardButton("📍 Lokatsiyani yuborish", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
    await u.message.reply_text("Manzilni yozing yoki lokatsiya yuboring:", reply_markup=kb)
    return CO_ADDR


async def get_cfg():
    return {x["key"]: x["value"] for x in (await sb("GET", "settings?select=*") or [])}


async def send_qr(bot, chat, text, cfg, markup=None):
    """QR rasm bor boʻlsa rasm + matn, boʻlmasa faqat matn yuboradi."""
    qr = cfg.get("qrImg") or ""
    if qr.startswith("data:") and "," in qr:
        await bot.send_photo(chat, photo=base64.b64decode(qr.split(",", 1)[1]), caption=text[:1024], reply_markup=markup)
    else:
        await bot.send_message(chat, text, reply_markup=markup)


async def co_addr(u: Update, ctx):
    loc = u.message.location
    ctx.user_data["addr"] = f"📍 https://maps.google.com/?q={loc.latitude},{loc.longitude}" if loc else u.message.text
    ctx.user_data["loc"] = (loc.latitude, loc.longitude) if loc else None
    cfg = await get_cfg()
    if cfg.get("pay_card") != "0" and cfg.get("card_number"):
        await u.message.reply_text("Toʻlov turini tanlang:", reply_markup=IM([[IB("💵 Naqd", callback_data="pm:naqd"), IB("💳 Karta", callback_data="pm:karta")]]))
        return CO_PAY
    await save_order(u, ctx, "naqd")
    return ConversationHandler.END


async def co_pay(u: Update, ctx):
    q = u.callback_query
    await q.answer()
    await save_order(u, ctx, q.data[3:])
    return ConversationHandler.END


async def save_order(u: Update, ctx, pm):
    chat = u.effective_chat.id
    addr, loc = ctx.user_data.get("addr"), ctx.user_data.get("loc")
    its = {str(i["id"]): i for i in await items()}
    lines = [{"n": its[k]["name"], "q": q, "p": its[k]["price"]} for k, q in cart(ctx).items() if k in its]
    total = sum(l["q"] * l["p"] for l in lines)
    row = {"name": u.effective_user.full_name, "phone": ctx.user_data.get("phone"), "address": addr,
           "lines": lines, "total": total, "status": 0, "source": "telegram", "chat_id": chat, "pay_method": pm}
    if loc:
        row["lat"], row["lng"] = loc
    res = await sb("POST", "orders", row)
    oid = (res or [{}])[0].get("id", "")
    ctx.user_data["cart"] = {}
    if pm == "karta":
        cfg = await get_cfg()
        txt = (f"✅ Buyurtma №{oid} qabul qilindi.\nJami: {so(total)}\n\n💳 Karta: {cfg.get('card_number')}\n👤 {cfg.get('card_owner', '')}"
               + (f"\n🔗 {cfg['pay_link']}" if cfg.get("pay_link") else "")
               + "\n\nPulni oʻtkazing. Oshxona tekshirib, chekni shu yerga yuboradi.")
        await send_qr(ctx.bot, chat, txt, cfg, MAIN)
    else:
        await ctx.bot.send_message(chat, f"✅ Buyurtma №{oid} qabul qilindi! Jami: {so(total)}. Oshxona tez orada tayyorlaydi.", reply_markup=MAIN)
    if ADMIN:
        txt = f"🆕 Yangi buyurtma №{oid} (Telegram, {'💳 karta' if pm == 'karta' else '💵 naqd'})\n" + "\n".join(f'{l["n"]} × {l["q"]}' for l in lines) + f"\n{so(total)}\n{row['phone']}\n{addr}"
        await ctx.bot.send_message(ADMIN, txt)


# ---------- joy / ovqat bron ----------
async def b_start(u: Update, ctx):
    kb = ReplyKeyboardMarkup([[KeyboardButton("📱 Raqamni yuborish", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await u.message.reply_text("Bron uchun telefon raqamingiz:", reply_markup=kb)
    return B_PHONE


async def b_phone(u: Update, ctx):
    ctx.user_data["b"] = {"phone": u.message.contact.phone_number if u.message.contact else u.message.text}
    await u.message.reply_text("Qaysi sana? (masalan 2026-10-25)", reply_markup=ReplyKeyboardRemove())
    return B_DATE


async def b_date(u: Update, ctx):
    ctx.user_data["b"]["date"] = u.message.text
    await u.message.reply_text("Soat nechida? (masalan 19:30)")
    return B_TIME


async def b_time(u: Update, ctx):
    ctx.user_data["b"]["time"] = u.message.text
    await u.message.reply_text("Necha kishi?")
    return B_PEOPLE


async def b_people(u: Update, ctx):
    t = u.message.text.strip()
    ctx.user_data["b"]["people"] = int(t) if t.isdigit() else 1
    await u.message.reply_text("Oldindan ovqat buyurtma qilasizmi? Yozing yoki «yoʻq» deb yuboring.")
    return B_FOOD


async def b_food(u: Update, ctx):
    b = ctx.user_data.pop("b")
    f = u.message.text.strip()
    b.update({"name": u.effective_user.full_name, "food": "" if f.lower() in ("yoʻq", "yoq", "yo'q") else f, "source": "telegram"})
    await sb("POST", "bookings", b)
    await u.message.reply_text("✅ Bron qabul qilindi! Siz bilan bogʻlanamiz.", reply_markup=MAIN)
    if ADMIN:
        await ctx.bot.send_message(ADMIN, f'📅 Yangi bron: {b["name"]}, {b["phone"]}, {b["date"]} {b["time"]}, {b["people"]} kishi. {b["food"]}')
    return ConversationHandler.END


# ---------- sharh ----------
async def r_start(u: Update, ctx):
    await u.message.reply_text("Bahoingiz:", reply_markup=IM([[IB("⭐" * n, callback_data=f"st:{n}") for n in (1, 2, 3, 4, 5)]]))
    return R_STARS


async def r_stars(u: Update, ctx):
    q = u.callback_query
    await q.answer()
    ctx.user_data["stars"] = int(q.data[3:])
    await q.message.reply_text("Fikringizni yozing:")
    return R_TEXT


async def r_text(u: Update, ctx):
    await sb("POST", "reviews", {"name": u.effective_user.full_name, "stars": ctx.user_data.get("stars", 5), "text": u.message.text, "source": "telegram"})
    await u.message.reply_text("Rahmat! Sharhingiz qabul qilindi 🙏", reply_markup=MAIN)
    return ConversationHandler.END


async def cancel(u: Update, ctx):
    await u.message.reply_text("Bekor qilindi.", reply_markup=MAIN)
    return ConversationHandler.END


# ---------- buyurtma holati xabarlari (sayt tugmasi bosilganda mijozga) ----------
def haversine_min(a_lat, a_lng, b_lat, b_lng):
    r = math.pi / 180
    h = math.sin((b_lat - a_lat) * r / 2) ** 2 + math.cos(a_lat * r) * math.cos(b_lat * r) * math.sin((b_lng - a_lng) * r / 2) ** 2
    km = 2 * 6371 * math.asin(math.sqrt(h))
    return max(3, round(km * 1.3 / SCOOTER_KMH * 60))  # toʻgʻri masofa x1.3 = yoʻl


async def eta_min(o):
    """Oshxonadan mijozgacha taxminiy daqiqa. Hisoblab boʻlmasa None."""
    if not (K_LAT and K_LNG):
        return None
    lat, lng = o.get("lat"), o.get("lng")
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "cloud-oshxona-bot"}) as c:
            if lat is None:  # lokatsiya yuborilmagan: matnli manzilni xaritadan qidiramiz
                addr = o.get("address") or ""
                if not addr or addr.startswith("📍"):
                    return None
                j = (await c.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": addr, "format": "json", "limit": 1, "countrycodes": "uz"})).json()
                if not j:
                    return None
                lat, lng = float(j[0]["lat"]), float(j[0]["lon"])
            try:
                r = await c.get(f"https://router.project-osrm.org/route/v1/driving/{K_LNG},{K_LAT};{lng},{lat}",
                                params={"overview": "false"})
                km = r.json()["routes"][0]["distance"] / 1000  # xarita boʻyicha yoʻl masofasi
                return max(3, round(km / SCOOTER_KMH * 60))
            except Exception:
                return haversine_min(float(K_LAT), float(K_LNG), float(lat), float(lng))
    except Exception:
        logging.exception("eta")
        return None


async def watcher(app):
    """Har 10 soniyada tekshiradi: sayt/oshxona buyurtma holatini oʻzgartirsa, mijozga xabar yuboradi."""
    while True:
        try:
            rows = await sb("GET", "orders?select=*&chat_id=not.is.null&notified=lt.3")
            for o in rows or []:
                st = o.get("status") or 0
                if st <= (o.get("notified") or 0):
                    continue
                kb = None
                if st == 1:
                    txt = f"👨‍🍳 Buyurtmangiz #{o['id']} tayyorlanmoqda."
                elif st == 2:
                    e = await eta_min(o)
                    mins = e or 30
                    txt = f"🍽 Buyurtmangiz #{o['id']} tayyor va yoʻlga chiqdi! 🛵\n{mins} daqiqa ichida yetkazib beriladi."
                elif o.get("pay_method") == "karta":
                    txt = f"✅ Buyurtma №{o['id']} yetkazildi. Yoqimli ishtaha! 🙏"
                else:
                    txt = f"✅ Buyurtma #{o['id']} yetkazildi. Kuryerga pulni bergan boʻlsangiz, tugmani bosing:"
                    kb = IM([[IB("✅ Pul berildi", callback_data=f"paid:{o['id']}")]])
                try:
                    await app.bot.send_message(o["chat_id"], txt, reply_markup=kb)
                except Exception:
                    logging.exception("xabar yuborilmadi")
                await sb("PATCH", f"orders?id=eq.{o['id']}", {"notified": st})
            await receipts(app)
        except Exception:
            logging.exception("watcher")
        await asyncio.sleep(10)


async def receipts(app):
    """Sayt/oshxona "💰 Pul keldi" ni bossa, mijozga rasmli chek yuboradi."""
    rows = await sb("GET", "orders?select=*&chat_id=not.is.null&pay_method=eq.karta&paid_at=not.is.null&paid_notified=eq.false")
    if not rows:
        return
    cfg = await get_cfg()
    for o in rows:
        ls = o.get("lines") or []
        txt = ("🧾 CHEK\n✅ Pul kelib tushdi!\n\n" + f"Buyurtma №{o['id']}\n"
               + "\n".join(f'{l["n"]} × {l["q"]}' for l in ls)
               + f"\n\nTaomlar soni: {sum(l['q'] for l in ls)} ta\nJami: {so(o['total'])}\nSana: {(o.get('paid_at') or '')[:10]}")
        try:
            await send_qr(app.bot, o["chat_id"], txt, cfg)
        except Exception:
            logging.exception("chek yuborilmadi")
        await sb("PATCH", f"orders?id=eq.{o['id']}", {"paid_notified": True})


async def on_paid(u: Update, ctx):
    q = u.callback_query
    oid = q.data[5:]
    await sb("PATCH", f"orders?id=eq.{oid}&chat_id=eq.{q.from_user.id}", {"paid_at": datetime.now(timezone.utc).isoformat()})
    await q.answer("Rahmat! 🙏")
    await q.edit_message_text(f"✅ Buyurtma #{oid}: toʻlov tasdiqlandi. Yoqimli ishtaha! 🙏")


async def post_init(app):
    asyncio.create_task(watcher(app))


TXT = filters.TEXT & ~filters.COMMAND


def health():  # bepul hostingda "port ochiq" boʻlishi uchun
    class H_(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), H_).serve_forever()


def main():
    threading.Thread(target=health, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(co_start, pattern="^checkout$"),
                      MessageHandler(filters.Regex("^📅"), b_start),
                      MessageHandler(filters.Regex("^⭐"), r_start)],
        states={
            CO_PHONE: [MessageHandler(filters.CONTACT | TXT, co_phone)],
            CO_ADDR: [MessageHandler(filters.LOCATION | TXT, co_addr)],
            CO_PAY: [CallbackQueryHandler(co_pay, pattern="^pm:")],
            B_PHONE: [MessageHandler(filters.CONTACT | TXT, b_phone)],
            B_DATE: [MessageHandler(TXT, b_date)], B_TIME: [MessageHandler(TXT, b_time)],
            B_PEOPLE: [MessageHandler(TXT, b_people)], B_FOOD: [MessageHandler(TXT, b_food)],
            R_STARS: [CallbackQueryHandler(r_stars, pattern="^st:")], R_TEXT: [MessageHandler(TXT, r_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.Regex("^🍽"), menu))
    app.add_handler(MessageHandler(filters.Regex("^🛒"), show_cart))
    app.add_handler(CallbackQueryHandler(on_cat, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(on_add, pattern="^add:"))
    app.add_handler(CallbackQueryHandler(show_cart, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(on_clear, pattern="^clear$"))
    app.add_handler(CallbackQueryHandler(on_paid, pattern="^paid:"))
    app.run_polling()


if __name__ == "__main__":
    main()
