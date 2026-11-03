"""Lions Arena Padel — Telegram-бот (MVP). aiogram 3.x + SQLite."""
import asyncio, csv, io, logging, os, re, sqlite3
from datetime import datetime, timedelta
from html import escape as esc

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
GROUP_ID = int(os.getenv("GROUP_ID") or 0)
BOOKING_URL = os.getenv("BOOKING_URL", "")
OFFER_MIN = int(os.getenv("WAITLIST_CONFIRM_MIN", "45"))
FMT = "%Y-%m-%d %H:%M"
BOT_USERNAME = ""

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lions")
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
r = Router()

# ───────────────────────── DB ─────────────────────────
db = sqlite3.connect(os.getenv("DB_PATH", "lions.db"))
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, phone TEXT, level REAL, gender TEXT, side TEXT, rating INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, role TEXT);
CREATE TABLE IF NOT EXISTS tournaments(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, start TEXT, format TEXT, level TEXT, price TEXT, max_players INTEGER, deadline TEXT, status TEXT DEFAULT 'open', group_msg INTEGER, reminded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS regs(id INTEGER PRIMARY KEY AUTOINCREMENT, tid INTEGER, uid INTEGER, partner INTEGER, status TEXT, offer_until TEXT);
CREATE TABLE IF NOT EXISTS seekers(id INTEGER PRIMARY KEY AUTOINCREMENT, tid INTEGER, uid INTEGER, comment TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY AUTOINCREMENT, creator INTEGER, start TEXT, lo REAL, hi REAL, extra INTEGER, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS game_players(gid INTEGER, uid INTEGER, PRIMARY KEY(gid, uid));
CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, options TEXT);
CREATE TABLE IF NOT EXISTS votes(pid INTEGER, uid INTEGER, name TEXT, opt INTEGER, PRIMARY KEY(pid, uid));
""")


def q(sql, *a): return db.execute(sql, a).fetchall()
def q1(sql, *a): return db.execute(sql, a).fetchone()
def ex(sql, *a):
    c = db.execute(sql, a); db.commit(); return c.lastrowid


def user(uid): return q1("SELECT * FROM users WHERE id=?", uid)
def tour(tid): return q1("SELECT * FROM tournaments WHERE id=?", tid)
def game(gid): return q1("SELECT * FROM games WHERE id=?", gid)
def myreg(tid, uid): return q1("SELECT * FROM regs WHERE tid=? AND uid=?", tid, uid)
def taken(tid): return q1("SELECT COUNT(*) c FROM regs WHERE tid=? AND status IN('reg','offer')", tid)["c"]
def free(t): return t["max_players"] - taken(t["id"])
def fmt(s): return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M")
def now(): return datetime.now().strftime(FMT)


def role(uid):
    if uid in OWNERS: return "owner"
    x = q1("SELECT role FROM admins WHERE id=?", uid)
    return x["role"] if x else None


def full(uid): return role(uid) in ("owner", "admin")
def staff_ids(): return OWNERS | {x["id"] for x in q("SELECT id FROM admins")}


def nm(uid, adm=False):
    u = user(uid)
    if not u: return str(uid)
    s = esc(u["name"])
    if adm:
        s += f" (@{esc(u['username'])}" if u["username"] else " ("
        s += f", ур. {u['level']}" + (f", {esc(u['phone'])}" if u["phone"] else "") + ")"
    else:
        s += f" · ур. {u['level']}"
    return s


def parse_dt(t):
    t = t.strip()
    for f in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
        try: return datetime.strptime(t, f).strftime(FMT)
        except ValueError: pass
    try:
        d = datetime.strptime(t, "%d.%m %H:%M").replace(year=datetime.now().year)
        if d < datetime.now() - timedelta(days=1): d = d.replace(year=d.year + 1)
        return d.strftime(FMT)
    except ValueError:
        return None


def parse_lvl(t):
    n = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t.replace(",", "."))]
    if not n: return None
    return (n[0], n[0]) if len(n) == 1 else tuple(sorted(n[:2]))


def kb(*rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, url=d) if d.startswith("http")
                else InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


async def show(c: CallbackQuery, text, markup=None):
    try: await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest: await c.message.answer(text, reply_markup=markup)


async def notify(uid, text, markup=None):
    try: await bot.send_message(uid, text, reply_markup=markup)
    except Exception as e: log.warning("notify %s failed: %s", uid, e)


async def notify_tour(tid, text, statuses=("reg", "offer")):
    n = 0
    for x in q(f"SELECT DISTINCT uid FROM regs WHERE tid=? AND status IN({','.join('?' * len(statuses))})", tid, *statuses):
        await notify(x["uid"], text); n += 1
    return n


def menu(uid):
    rows = [["🏆 Турниры", "🎾 Найти игру"], ["➕ Создать игру", "🤝 Найти партнера"],
            ["👤 Мой профиль", "📊 Рейтинг игроков"], ["📅 Забронировать корт"]]
    if role(uid): rows.append(["⚙️ Админ-панель"])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True)


# ───────────────────────── Турниры: карточки ─────────────────────────
def is_open(t):
    return t["status"] == "open" and (not t["deadline"] or now() < t["deadline"])


def tcard(t):
    st = {"open": "🟢 Регистрация открыта", "closed": "🔒 Регистрация закрыта", "cancelled": "🚫 Турнир отменён"}[t["status"]]
    if t["status"] == "open" and not is_open(t): st = "🔒 Дедлайн регистрации прошёл"
    w = q1("SELECT COUNT(*) c FROM regs WHERE tid=? AND status='wait'", t["id"])["c"]
    s = (f"🏆 <b>{esc(t['title'])}</b>\n📅 {fmt(t['start'])}\n🎯 Формат: {esc(t['format'])}\n"
         f"📶 Уровень: {esc(t['level'])}\n💰 Стоимость: {esc(t['price'])}\n"
         f"👥 Зарегистрировано {taken(t['id'])} из {t['max_players']} игроков\n")
    if w: s += f"⏳ В листе ожидания: {w}\n"
    if t["deadline"]: s += f"⏰ Регистрация до {fmt(t['deadline'])}\n"
    return s + st


def tkb(t, uid):
    tid, reg, rows = t["id"], myreg(t["id"], uid), []
    if reg and reg["status"] == "reg":
        rows.append([("❌ Отменить участие", f"cx:{tid}")])
        if not reg["partner"]: rows.append([("🔎 Ищу партнера", f"seekadd:{tid}")])
    elif reg and reg["status"] == "offer":
        rows.append([("✅ Подтвердить участие", f"conf:{tid}")]); rows.append([("❌ Отказаться", f"decl:{tid}")])
    elif reg:
        rows.append([("🚪 Выйти из листа ожидания", f"cx:{tid}")])
    elif is_open(t):
        rows.append([("✅ Участвовать", f"join:{tid}")] if free(t) > 0 else [("⏳ Встать в лист ожидания", f"wait:{tid}")])
    rows += [[("👥 Участники", f"parts:{tid}"), ("🔎 Кто ищет партнера", f"seek:{tid}")]]
    if role(uid): rows.append([("⚙️ Управление", f"adm:{tid}")])
    rows.append([("⬅️ К турнирам", "tl")])
    return kb(*rows)


def upcoming():
    return q("SELECT * FROM tournaments WHERE status!='cancelled' AND start>? ORDER BY start", now())


def tlist_view():
    ts = upcoming()
    if not ts: return "Пока нет запланированных турниров. Мы сообщим, когда появятся 🙌", None
    return "🏆 <b>Ближайшие турниры</b>", kb(*[[(f"{fmt(t['start'])[:11]}{fmt(t['start'])[11:]} · {t['title']}", f"tv:{t['id']}")] for t in ts])


async def changed(tid):
    await process_waitlist(tid)
    await refresh_post(tid)


async def refresh_post(tid):
    t = tour(tid)
    if not (GROUP_ID and t and t["group_msg"]): return
    try: await bot.edit_message_text(tcard(t), chat_id=GROUP_ID, message_id=t["group_msg"], reply_markup=join_kb(tid))
    except Exception as e: log.info("refresh_post: %s", e)


def join_kb(tid):
    return kb([("🎾 Участвовать / участники", f"https://t.me/{BOT_USERNAME}?start=t_{tid}")])


async def post_to_group(tid):
    if not GROUP_ID: return False
    m = await bot.send_message(GROUP_ID, tcard(tour(tid)), reply_markup=join_kb(tid))
    ex("UPDATE tournaments SET group_msg=? WHERE id=?", m.message_id, tid)
    return True


async def process_waitlist(tid):
    t = tour(tid)
    if not t or t["status"] == "cancelled": return
    while True:
        w = q1("SELECT * FROM regs WHERE tid=? AND status='wait' ORDER BY id LIMIT 1", tid)
        if not w: return
        pair = [w["uid"]]
        if w["partner"] and q1("SELECT 1 FROM regs WHERE tid=? AND uid=? AND status='wait'", tid, w["partner"]):
            pair.append(w["partner"])
        if free(t) < len(pair): return
        until = (datetime.now() + timedelta(minutes=OFFER_MIN)).strftime(FMT)
        for u in pair: ex("UPDATE regs SET status='offer', offer_until=? WHERE tid=? AND uid=?", until, tid, u)
        for u in pair:
            await notify(u, f"🎉 Освободилось место на турнир «{esc(t['title'])}»!\nПодтвердите участие в течение {OFFER_MIN} мин., иначе место получит следующий игрок.",
                         kb([("✅ Подтвердить", f"conf:{tid}")], [("❌ Отказаться", f"decl:{tid}")]))


async def remove_player(tid, uid, by_admin=False):
    reg = myreg(tid, uid); t = tour(tid)
    if not reg: return
    ex("DELETE FROM regs WHERE id=?", reg["id"])
    ex("UPDATE seekers SET status='closed' WHERE tid=? AND uid=?", tid, uid)
    p = reg["partner"]
    if p:
        ex("UPDATE regs SET partner=NULL WHERE tid=? AND uid=?", tid, p)
        pr = myreg(tid, p)
        if pr and pr["status"] == "reg":
            ex("INSERT INTO seekers(tid,uid,comment) VALUES(?,?,?)", tid, p, "Партнёр отменил участие")
            await notify(p, f"⚠️ {nm(uid)} больше не в паре с вами на «{esc(t['title'])}». Мы добавили вас в поиск партнёра.")
    await changed(tid)
    if not by_admin:
        for a in staff_ids():
            await notify(a, f"ℹ️ {nm(uid, True)} отменил(а) участие в «{esc(t['title'])}». Сейчас {taken(tid)}/{t['max_players']}.")


# ───────────────────────── Профиль ─────────────────────────
class P(StatesGroup):
    name = State(); phone = State(); level = State(); gender = State(); side = State()


LEVELS = ["1.0", "1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "4.5", "5.0"]


async def start_profile(m: Message, state: FSMContext, after=""):
    await state.clear()
    await state.update_data(after=after)
    await state.set_state(P.name)
    await m.answer("👋 Давайте создадим профиль — это займёт минуту.\n\nНапишите <b>имя и фамилию</b>:", reply_markup=ReplyKeyboardRemove())


async def guard(m: Message, state: FSMContext):
    await state.clear()
    if user(m.from_user.id): return True
    await start_profile(m, state); return False


@r.message(P.name)
async def p_name(m: Message, state: FSMContext):
    if not m.text or len(m.text) < 2: return await m.answer("Напишите имя и фамилию текстом 🙂")
    await state.update_data(name=m.text.strip()[:60]); await state.set_state(P.phone)
    await m.answer("📱 Телефон (по желанию). Нажмите кнопку или введите номер:",
                   reply_markup=ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
                       [KeyboardButton(text="📱 Поделиться номером", request_contact=True)], [KeyboardButton(text="Пропустить")]]))


@r.message(P.phone)
async def p_phone(m: Message, state: FSMContext):
    ph = m.contact.phone_number if m.contact else (None if (m.text or "").strip() == "Пропустить" else (m.text or "").strip()[:20])
    await state.update_data(phone=ph); await state.set_state(P.level)
    await m.answer("📶 Ваш уровень игры:", reply_markup=ReplyKeyboardRemove())
    await m.answer("Выберите:", reply_markup=kb(*[[(l if l != "5.0" else "5.0+", f"lv:{l}") for l in LEVELS[i:i + 3]] for i in range(0, 9, 3)]))


@r.callback_query(P.level, F.data.startswith("lv:"))
async def p_level(c: CallbackQuery, state: FSMContext):
    await state.update_data(level=float(c.data[3:])); await state.set_state(P.gender)
    await show(c, "Пол:", kb([("Мужской", "gd:М"), ("Женский", "gd:Ж")])); await c.answer()


@r.callback_query(P.gender, F.data.startswith("gd:"))
async def p_gender(c: CallbackQuery, state: FSMContext):
    await state.update_data(gender=c.data[3:]); await state.set_state(P.side)
    await show(c, "Предпочтительная сторона игры:", kb([("Справа", "sd:Справа"), ("Слева", "sd:Слева")], [("Без разницы", "sd:Без разницы")]))
    await c.answer()


@r.callback_query(P.side, F.data.startswith("sd:"))
async def p_side(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); uid = c.from_user.id
    ex("INSERT OR REPLACE INTO users(id,name,username,phone,level,gender,side,rating) VALUES(?,?,?,?,?,?,?,COALESCE((SELECT rating FROM users WHERE id=?),0))",
       uid, d["name"], c.from_user.username, d.get("phone"), d["level"], d["gender"], c.data[3:], uid)
    await state.clear(); await c.answer()
    await show(c, "✅ Профиль сохранён!")
    await c.message.answer("Готово! Выбирайте раздел в меню 👇", reply_markup=menu(uid))
    if d.get("after", "").startswith("t_") and tour(int(d["after"][2:])):
        t = tour(int(d["after"][2:])); await c.message.answer(tcard(t), reply_markup=tkb(t, uid))


def profile_text(uid):
    u = user(uid)
    played = q1("SELECT COUNT(*) c FROM regs g JOIN tournaments t ON t.id=g.tid WHERE g.uid=? AND g.status='reg' AND t.start<? AND t.status!='cancelled'", uid, now())["c"]
    hist = q("SELECT t.title,t.start FROM regs g JOIN tournaments t ON t.id=g.tid WHERE g.uid=? AND g.status='reg' ORDER BY t.start DESC LIMIT 5", uid)
    gh = q("SELECT g.start FROM game_players p JOIN games g ON g.id=p.gid WHERE p.uid=? ORDER BY g.start DESC LIMIT 5", uid)
    s = (f"👤 <b>{esc(u['name'])}</b>" + (f" (@{esc(u['username'])})" if u["username"] else "") +
         f"\n📱 {esc(u['phone'] or '—')}\n📶 Уровень: {u['level']}\n⚧ Пол: {u['gender']}\n🎯 Сторона: {u['side']}\n"
         f"\n🏆 Сыграно турниров: {played}\n🥇 Побед: 0 (скоро)\n📊 Рейтинг: {u['rating']}")
    if hist: s += "\n\n<b>Турниры:</b>\n" + "\n".join(f"• {fmt(h['start'])[:10]} — {esc(h['title'])}" for h in hist)
    if gh: s += "\n\n<b>Игры:</b>\n" + "\n".join(f"• {fmt(g['start'])}" for g in gh)
    return s


# ───────────────────────── Главное меню ─────────────────────────
@r.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear(); uid = m.from_user.id; arg = command.args or ""
    if user(uid) and m.from_user.username:
        ex("UPDATE users SET username=? WHERE id=?", m.from_user.username, uid)
    if not user(uid):
        await m.answer("🦁 Добро пожаловать в <b>Lions Arena Padel</b>!\nЗапись на турниры, поиск игр и партнёров — всё здесь, кнопками.")
        return await start_profile(m, state, arg)
    await m.answer("🦁 Lions Arena Padel", reply_markup=menu(uid))
    if arg.startswith("t_") and arg[2:].isdigit() and tour(int(arg[2:])):
        t = tour(int(arg[2:])); await m.answer(tcard(t), reply_markup=tkb(t, uid))


@r.message(Command("id"))
async def cmd_id(m: Message): await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@r.message(F.text == "🏆 Турниры")
async def m_tours(m: Message, state: FSMContext):
    if await guard(m, state):
        t, k = tlist_view(); await m.answer(t, reply_markup=k)


@r.callback_query(F.data == "tl")
async def tl(c: CallbackQuery):
    t, k = tlist_view(); await show(c, t, k); await c.answer()


@r.message(F.text == "👤 Мой профиль")
async def m_profile(m: Message, state: FSMContext):
    if await guard(m, state):
        await m.answer(profile_text(m.from_user.id), reply_markup=kb([("✏️ Изменить профиль", "pedit")]))


@r.callback_query(F.data == "pedit")
async def pedit(c: CallbackQuery, state: FSMContext):
    await c.answer(); await start_profile(c.message, state)


@r.message(F.text == "📊 Рейтинг игроков")
async def m_rating(m: Message, state: FSMContext):
    await state.clear()
    top = q("SELECT name,rating FROM users WHERE rating>0 ORDER BY rating DESC LIMIT 10")
    if not top: return await m.answer("📊 Рейтинг Lions Arena Padel скоро заработает! Играйте в турнирах — очки пойдут в зачёт 🦁")
    await m.answer("📊 <b>Рейтинг Lions Arena Padel</b>\n\n" + "\n".join(f"{i}. {esc(x['name'])} — {x['rating']}" for i, x in enumerate(top, 1)))


@r.message(F.text == "📅 Забронировать корт")
async def m_book(m: Message, state: FSMContext):
    await state.clear()
    if BOOKING_URL: await m.answer("Бронирование кортов Lions Arena:", reply_markup=kb([("📅 Открыть бронирование", BOOKING_URL)]))
    else: await m.answer("Ссылка на бронирование скоро появится.")


# ───────────────────────── Турнир: запись ─────────────────────────
class J(StatesGroup): partner = State()
class S(StatesGroup): comment = State()


@r.callback_query(F.data.startswith("tv:"))
async def tv(c: CallbackQuery):
    t = tour(int(c.data[3:])); await show(c, tcard(t), tkb(t, c.from_user.id)); await c.answer()


async def need_profile(c: CallbackQuery):
    if user(c.from_user.id): return False
    await c.answer("Сначала создайте профиль: нажмите /start", show_alert=True); return True


@r.callback_query(F.data.startswith("join:"))
async def join(c: CallbackQuery):
    if await need_profile(c): return
    tid = int(c.data[5:]); t = tour(tid)
    if not is_open(t): return await c.answer("Регистрация закрыта", show_alert=True)
    if myreg(tid, c.from_user.id): return await c.answer("Вы уже записаны", show_alert=True)
    await show(c, f"{tcard(t)}\n\nВы играете с партнёром?",
               kb([("👫 У меня есть партнер", f"hp:{tid}")], [("🔎 Мне нужен партнер", f"np:{tid}")], [("⬅️ Назад", f"tv:{tid}")]))
    await c.answer()


async def register_solo(tid, uid, comment=""):
    t = tour(tid); reg = myreg(tid, uid)
    if reg:
        if reg["status"] == "reg" and not reg["partner"]:
            if not q1("SELECT 1 FROM seekers WHERE tid=? AND uid=? AND status='open'", tid, uid):
                ex("INSERT INTO seekers(tid,uid,comment) VALUES(?,?,?)", tid, uid, comment)
            return "🔎 Заявка на поиск партнёра размещена. Мы сообщим, когда кто-то предложит сыграть вместе."
        return "Вы уже в списке этого турнира."
    if free(t) < 1: return None
    ex("INSERT INTO regs(tid,uid,status) VALUES(?,?,'reg')", tid, uid)
    ex("INSERT INTO seekers(tid,uid,comment) VALUES(?,?,?)", tid, uid, comment)
    await changed(tid)
    return (f"✅ Вы зарегистрированы на турнир.\nЗарегистрировано {taken(tid)} из {t['max_players']} игроков.\n"
            "🔎 Вы в списке поиска партнёра — мы сообщим, когда кто-то предложит сыграть вместе.")


@r.callback_query(F.data.startswith("np:"))
async def np(c: CallbackQuery):
    tid = int(c.data[3:]); txt = await register_solo(tid, c.from_user.id)
    if txt is None: return await c.answer("Мест нет — встаньте в лист ожидания", show_alert=True)
    t = tour(tid); await show(c, f"{txt}\n\n{tcard(t)}", tkb(t, c.from_user.id)); await c.answer()


@r.callback_query(F.data.startswith("hp:"))
async def hp(c: CallbackQuery, state: FSMContext):
    await state.set_state(J.partner); await state.update_data(tid=int(c.data[3:]))
    await c.message.answer("Отправьте @ник партнёра в Telegram.\n(Партнёр должен хотя бы раз запустить этого бота и создать профиль.)")
    await c.answer()


@r.message(J.partner)
async def got_partner(m: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]; t = tour(tid); uid = m.from_user.id
    p = q1("SELECT * FROM users WHERE lower(username)=?", (m.text or "").strip().lstrip("@").lower())
    if not p: return await m.answer("Не нашёл такого игрока 🤔 Пусть партнёр откроет бота, создаст профиль, и отправьте ник ещё раз.")
    if p["id"] == uid: return await m.answer("Это вы 🙂 Отправьте ник партнёра.")
    if myreg(tid, p["id"]): return await m.answer("Этот игрок уже записан на турнир.")
    await state.clear()
    st = "reg" if free(t) >= 2 else "wait"
    for a, b in ((uid, p["id"]), (p["id"], uid)):
        ex("INSERT INTO regs(tid,uid,partner,status) VALUES(?,?,?,?)", tid, a, b, st)
    ex("UPDATE seekers SET status='closed' WHERE tid=? AND uid IN(?,?)", tid, uid, p["id"])
    await changed(tid)
    me = user(uid)
    await notify(p["id"], f"🎾 {esc(me['name'])} записал(а) вас в паре на «{esc(t['title'])}». Если не планируете играть — откройте турнир и отмените участие.",
                 kb([("🏆 Открыть турнир", f"tv:{tid}")]))
    txt = "✅ Вы зарегистрированы на турнир в паре." if st == "reg" else "⏳ Мест для пары не хватает — вы оба в листе ожидания. Сообщим, как только освободятся места."
    await m.answer(f"{txt}\n\n{tcard(tour(tid))}", reply_markup=tkb(tour(tid), uid))


@r.callback_query(F.data.startswith("wait:"))
async def wait(c: CallbackQuery):
    if await need_profile(c): return
    tid = int(c.data[5:]); uid = c.from_user.id
    if not myreg(tid, uid): ex("INSERT INTO regs(tid,uid,status) VALUES(?,?,'wait')", tid, uid)
    await changed(tid); t = tour(tid)
    await show(c, f"⏳ Вы в листе ожидания. Если место освободится, мы сразу напишем (на подтверждение — {OFFER_MIN} мин).\n\n{tcard(t)}", tkb(t, uid))
    await c.answer()


@r.callback_query(F.data.startswith("conf:"))
async def conf(c: CallbackQuery):
    tid = int(c.data[5:]); uid = c.from_user.id; reg = myreg(tid, uid)
    if not reg or reg["status"] != "offer": return await c.answer("Предложение уже неактуально", show_alert=True)
    for u in {uid, reg["partner"]} - {None}:
        ex("UPDATE regs SET status='reg', offer_until=NULL WHERE tid=? AND uid=? AND status='offer'", tid, u)
    await refresh_post(tid); t = tour(tid)
    await show(c, f"✅ Участие подтверждено!\n\n{tcard(t)}", tkb(t, uid)); await c.answer()
    if reg["partner"]: await notify(reg["partner"], f"✅ Ваше участие на «{esc(t['title'])}» подтверждено.")


@r.callback_query(F.data.startswith("decl:"))
async def decl(c: CallbackQuery):
    tid = int(c.data[5:]); reg = myreg(tid, c.from_user.id)
    if reg and reg["status"] == "offer":
        for u in {c.from_user.id, reg["partner"]} - {None}:
            ex("DELETE FROM regs WHERE tid=? AND uid=? AND status='offer'", tid, u)
        await changed(tid)
    await show(c, "Хорошо, место передано следующему игроку."); await c.answer()


@r.callback_query(F.data.startswith("cx:"))
async def cx(c: CallbackQuery):
    tid = int(c.data[3:])
    await show(c, "Точно отменить участие?", kb([("Да, отменить", f"cxy:{tid}")], [("Нет, остаться", f"tv:{tid}")])); await c.answer()


@r.callback_query(F.data.startswith("cxy:"))
async def cxy(c: CallbackQuery):
    tid = int(c.data[4:]); await remove_player(tid, c.from_user.id)
    t = tour(tid); await show(c, f"Участие отменено.\n\n{tcard(t)}", tkb(t, c.from_user.id)); await c.answer()


@r.callback_query(F.data.startswith("parts:"))
async def parts(c: CallbackQuery):
    tid = int(c.data[6:]); t = tour(tid); adm = bool(role(c.from_user.id)); seen, lines = set(), []
    for x in q("SELECT * FROM regs WHERE tid=? AND status IN('reg','offer') ORDER BY id", tid):
        if x["uid"] in seen: continue
        seen.add(x["uid"])
        if x["partner"]:
            seen.add(x["partner"]); lines.append(f"{len(lines) + 1}. {nm(x['uid'], adm)} + {nm(x['partner'], adm)}")
        else:
            lines.append(f"{len(lines) + 1}. {nm(x['uid'], adm)} 🔎 ищет партнёра")
    wl = q("SELECT uid FROM regs WHERE tid=? AND status='wait' ORDER BY id", tid)
    s = f"👥 <b>{esc(t['title'])}</b> — {taken(tid)}/{t['max_players']}\n\n" + ("\n".join(lines) or "Пока никого")
    if wl: s += "\n\n⏳ <b>Лист ожидания:</b>\n" + "\n".join(f"{i}. {nm(x['uid'], adm)}" for i, x in enumerate(wl, 1))
    await show(c, s, kb([("⬅️ Назад", f"tv:{tid}")])); await c.answer()


# ───────────────────────── Поиск партнёра ─────────────────────────
@r.message(F.text == "🤝 Найти партнера")
async def m_partner(m: Message, state: FSMContext):
    if not await guard(m, state): return
    ts = upcoming()
    if not ts: return await m.answer("Пока нет турниров.")
    await m.answer("Выберите турнир:", reply_markup=kb(*[[(f"{fmt(t['start'])[:10]} · {t['title']}", f"seek:{t['id']}")] for t in ts]))


@r.callback_query(F.data.startswith("seek:"))
async def seek(c: CallbackQuery):
    if await need_profile(c): return
    tid = int(c.data[5:]); t = tour(tid); rows = []; lines = []
    for s in q("SELECT * FROM seekers WHERE tid=? AND status='open' AND uid!=? ORDER BY id", tid, c.from_user.id):
        u = user(s["uid"])
        lines.append(f"• {esc(u['name'])} · ур. {u['level']} · {esc(u['side'])}" + (f"\n   💬 {esc(s['comment'])}" if s["comment"] else ""))
        rows.append([(f"🤝 {u['name']} · {u['level']}", f"prop:{s['id']}")])
    rows += [[("➕ Разместить заявку", f"seekadd:{tid}")], [("⬅️ Назад", f"tv:{tid}")]]
    await show(c, f"🔎 <b>Ищут партнёра — {esc(t['title'])}</b>\n\n" + ("\n".join(lines) or "Пока никто не ищет.") + "\n\nНажмите на игрока, чтобы предложить сыграть вместе.", kb(*rows))
    await c.answer()


@r.callback_query(F.data.startswith("seekadd:"))
async def seekadd(c: CallbackQuery, state: FSMContext):
    if await need_profile(c): return
    await state.set_state(S.comment); await state.update_data(tid=int(c.data[8:]))
    await c.message.answer("Добавьте комментарий к заявке (например, «играю вечером, люблю атаковать») или нажмите «Без комментария».",
                           reply_markup=kb([("Без комментария", "nocomment")])); await c.answer()


async def finish_seek(m: Message, state: FSMContext, uid, comment):
    tid = (await state.get_data())["tid"]; await state.clear()
    txt = await register_solo(tid, uid, comment)
    if txt is None: txt = "Мест нет. Встаньте в лист ожидания на странице турнира."
    t = tour(tid); await m.answer(f"{txt}\n\n{tcard(t)}", reply_markup=tkb(t, uid))


@r.message(S.comment)
async def s_comment(m: Message, state: FSMContext): await finish_seek(m, state, m.from_user.id, (m.text or "")[:200])


@r.callback_query(S.comment, F.data == "nocomment")
async def s_nocomment(c: CallbackQuery, state: FSMContext):
    await c.answer(); await finish_seek(c.message, state, c.from_user.id, "")


@r.callback_query(F.data.startswith("prop:"))
async def prop(c: CallbackQuery):
    s = q1("SELECT * FROM seekers WHERE id=?", int(c.data[5:])); me = c.from_user.id
    if not s or s["status"] != "open": return await c.answer("Заявка уже закрыта", show_alert=True)
    u = user(me); t = tour(s["tid"])
    await notify(s["uid"], f"🤝 {esc(u['name'])} (ур. {u['level']}, {esc(u['side'])}) предлагает сыграть вместе на «{esc(t['title'])}».",
                 kb([("✅ Принять", f"pacc:{s['id']}:{me}"), ("❌ Отклонить", f"pdec:{s['id']}:{me}")]))
    await c.answer("Предложение отправлено! Ждите ответ 🙌", show_alert=True)


@r.callback_query(F.data.startswith("pdec:"))
async def pdec(c: CallbackQuery):
    _, sid, p = c.data.split(":")
    await show(c, "Предложение отклонено."); await c.answer()
    await notify(int(p), "😔 Игрок отклонил ваше предложение сыграть вместе. Попробуйте другого партнёра.")


@r.callback_query(F.data.startswith("pacc:"))
async def pacc(c: CallbackQuery):
    _, sid, p = c.data.split(":"); p = int(p); me = c.from_user.id
    s = q1("SELECT * FROM seekers WHERE id=?", int(sid))
    if not s or s["status"] != "open" or s["uid"] != me: return await c.answer("Заявка уже закрыта", show_alert=True)
    tid = s["tid"]; t = tour(tid); mine = myreg(tid, me); pr = myreg(tid, p)
    if (mine and mine["partner"]) or (pr and pr["partner"]): return await c.answer("Кто-то из вас уже в паре", show_alert=True)
    if not pr:
        if free(t) < 1: return await c.answer("Мест на турнире больше нет", show_alert=True)
        ex("INSERT INTO regs(tid,uid,status) VALUES(?,?,'reg')", tid, p)
    ex("UPDATE regs SET partner=? WHERE tid=? AND uid=?", p, tid, me)
    ex("UPDATE regs SET partner=? WHERE tid=? AND uid=?", me, tid, p)
    ex("UPDATE seekers SET status='closed' WHERE tid=? AND uid IN(?,?)", tid, me, p)
    await refresh_post(tid)
    await show(c, f"✅ Пара сформирована: вы + {nm(p)}"); await c.answer()
    await notify(p, f"✅ {nm(me)} принял(а) ваше предложение! Вы в паре на «{esc(t['title'])}».")


# ───────────────────────── Обычные игры ─────────────────────────
class G(StatesGroup): when = State(); level = State(); have = State()


def gcount(g): return g["extra"] + q1("SELECT COUNT(*) c FROM game_players WHERE gid=?", g["id"])["c"]


def gcard(g):
    n = gcount(g); pl = q("SELECT uid FROM game_players WHERE gid=?", g["id"])
    s = f"🎾 <b>Игра {fmt(g['start'])}</b>\nУровень {g['lo']}–{g['hi']}\nСейчас {n}/4 игроков\n"
    s += "✅ <b>Игра собрана</b>" if n >= 4 else f"Нужно ещё: {4 - n}"
    return s + "\n\n" + "\n".join(f"• {nm(x['uid'])}" for x in pl)


def gkb(g, uid):
    inn = q1("SELECT 1 FROM game_players WHERE gid=? AND uid=?", g["id"], uid)
    rows = []
    if inn and uid != g["creator"]: rows.append([("🚪 Выйти из игры", f"gl:{g['id']}")])
    elif not inn and g["status"] == "open": rows.append([("✅ Присоединиться", f"gj:{g['id']}")])
    rows.append([("⬅️ К играм", "gl_list")]); return kb(*rows)


@r.message(F.text == "➕ Создать игру")
async def m_gcreate(m: Message, state: FSMContext):
    if not await guard(m, state): return
    await state.set_state(G.when)
    await m.answer("Когда играем? Напишите дату и время, например: <code>27.10 19:00</code>")


@r.message(G.when)
async def g_when(m: Message, state: FSMContext):
    d = parse_dt(m.text or "")
    if not d or d < now(): return await m.answer("Не понял дату 🤔 Формат: <code>27.10 19:00</code> (в будущем).")
    await state.update_data(start=d); await state.set_state(G.level)
    await m.answer("Какой уровень игроков? Например: <code>2.5-3.0</code>")


@r.message(G.level)
async def g_level(m: Message, state: FSMContext):
    lv = parse_lvl(m.text or "")
    if not lv: return await m.answer("Напишите уровень, например <code>2.5-3.0</code> или <code>3</code>")
    await state.update_data(lo=lv[0], hi=lv[1]); await state.set_state(G.have)
    await m.answer("Сколько игроков уже есть (включая вас)?", reply_markup=kb([("1", "gh:1"), ("2", "gh:2"), ("3", "gh:3")]))


@r.callback_query(G.have, F.data.startswith("gh:"))
async def g_have(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); uid = c.from_user.id; have = int(c.data[3:]); await state.clear()
    gid = ex("INSERT INTO games(creator,start,lo,hi,extra) VALUES(?,?,?,?,?)", uid, d["start"], d["lo"], d["hi"], have - 1)
    ex("INSERT INTO game_players VALUES(?,?)", gid, uid)
    g = game(gid); await show(c, "Игра создана! Игроки увидят её в разделе «Найти игру».\n\n" + gcard(g), gkb(g, uid)); await c.answer()
    for u in q("SELECT id FROM users WHERE id!=? AND level BETWEEN ? AND ?", uid, g["lo"] - 0.5, g["hi"] + 0.5):
        await notify(u["id"], "🎾 Новая игра по вашему уровню!\n\n" + gcard(g), kb([("👀 Открыть", f"gv:{gid}")]))


@r.message(F.text == "🎾 Найти игру")
async def m_gfind(m: Message, state: FSMContext):
    if await guard(m, state):
        t, k = glist(m.from_user.id); await m.answer(t, reply_markup=k)


def glist(uid):
    L = user(uid)["level"]
    gs = [g for g in q("SELECT * FROM games WHERE status='open' AND start>? ORDER BY start LIMIT 30", now()) if g["lo"] - 0.5 <= L <= g["hi"] + 0.5]
    if not gs: return "Подходящих игр пока нет. Создайте свою — кнопка «➕ Создать игру» 🙌", None
    return "🎾 <b>Открытые игры по вашему уровню</b>", kb(*[[(f"{fmt(g['start'])[:11]}{fmt(g['start'])[11:]} — {gcount(g)}/4 · ур. {g['lo']}–{g['hi']}", f"gv:{g['id']}")] for g in gs])


@r.callback_query(F.data == "gl_list")
async def gl_list(c: CallbackQuery):
    if await need_profile(c): return
    t, k = glist(c.from_user.id); await show(c, t, k); await c.answer()


@r.callback_query(F.data.startswith("gv:"))
async def gv(c: CallbackQuery):
    if await need_profile(c): return
    g = game(int(c.data[3:])); await show(c, gcard(g), gkb(g, c.from_user.id)); await c.answer()


@r.callback_query(F.data.startswith("gj:"))
async def gj(c: CallbackQuery):
    if await need_profile(c): return
    uid = c.from_user.id; g = game(int(c.data[3:]))
    if not g or g["status"] != "open" or gcount(g) >= 4: return await c.answer("Игра уже собрана", show_alert=True)
    ex("INSERT OR IGNORE INTO game_players VALUES(?,?)", g["id"], uid); g = game(g["id"])
    if gcount(g) >= 4:
        ex("UPDATE games SET status='full' WHERE id=?", g["id"]); g = game(g["id"])
        for x in q("SELECT uid FROM game_players WHERE gid=?", g["id"]):
            await notify(x["uid"], f"✅ Игра собрана! {fmt(g['start'])} — 4/4 игроков.")
    else:
        await notify(g["creator"], f"➕ К игре {fmt(g['start'])} присоединился(ась) {nm(uid)}. Сейчас {gcount(g)}/4.")
    await show(c, gcard(g), gkb(g, uid)); await c.answer("Вы в игре ✅")


@r.callback_query(F.data.startswith("gl:"))
async def gl(c: CallbackQuery):
    g = game(int(c.data[3:])); ex("DELETE FROM game_players WHERE gid=? AND uid=?", g["id"], c.from_user.id)
    ex("UPDATE games SET status='open' WHERE id=?", g["id"]); g = game(g["id"])
    await notify(g["creator"], f"➖ {nm(c.from_user.id)} вышел(а) из игры {fmt(g['start'])}. Сейчас {gcount(g)}/4.")
    await show(c, gcard(g), gkb(g, c.from_user.id)); await c.answer()


# ───────────────────────── Админ-панель ─────────────────────────
class T(StatesGroup): title = State(); when = State(); fmt = State(); level = State(); price = State(); maxp = State(); deadline = State()
class A(StatesGroup): inp = State()
class Q(StatesGroup): question = State(); options = State()


async def deny(x, fullonly=False):
    uid = x.from_user.id
    if role(uid) and (full(uid) or not fullonly): return False
    if isinstance(x, CallbackQuery): await x.answer("Недостаточно прав", show_alert=True)
    return True


def panel_kb(uid):
    rows = [[("📋 Управление турнирами", "al")]]
    if full(uid): rows = [[("➕ Создать турнир", "ac")]] + rows + [[("📊 Новый опрос", "apoll"), ("📈 Результаты опросов", "apr")]]
    return kb(*rows)


@r.message(F.text == "⚙️ Админ-панель")
async def m_admin(m: Message, state: FSMContext):
    await state.clear()
    if role(m.from_user.id): await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=panel_kb(m.from_user.id))


@r.callback_query(F.data == "ap")
async def ap(c: CallbackQuery):
    if await deny(c): return
    await show(c, "⚙️ <b>Админ-панель</b>", panel_kb(c.from_user.id)); await c.answer()


@r.callback_query(F.data == "al")
async def al(c: CallbackQuery):
    if await deny(c): return
    ts = q("SELECT * FROM tournaments WHERE status!='cancelled' ORDER BY start DESC LIMIT 20")
    await show(c, "📋 Турниры:" if ts else "Турниров пока нет.",
               kb(*[[(f"{fmt(t['start'])[:10]} · {t['title']}", f"adm:{t['id']}")] for t in ts], [("⬅️ Назад", "ap")])); await c.answer()


def adm_kb(t, uid):
    tid = t["id"]
    rows = [[("👥 Участники", f"parts:{tid}")],
            [("➕ Добавить игрока", f"aadd:{tid}"), ("➖ Удалить игрока", f"adel:{tid}")],
            [("🔗 Изменить пару", f"apair:{tid}"), ("⏳ В лист ожидания", f"awl:{tid}")],
            [("📣 Рассылка участникам", f"abc:{tid}"), ("📤 Выгрузить список", f"aexp:{tid}")]]
    if full(uid):
        rows += [[("🕒 Перенести", f"ares:{tid}"), ("👥 Лимит", f"alim:{tid}")],
                 [("🔒 Закрыть/открыть регистрацию", f"atog:{tid}")],
                 [("📢 Анонс в группу", f"apost:{tid}"), ("🚫 Отменить турнир", f"acan:{tid}")]]
    rows.append([("⬅️ Назад", "al")]); return kb(*rows)


@r.callback_query(F.data.startswith("adm:"))
async def adm(c: CallbackQuery, state: FSMContext):
    if await deny(c): return
    await state.clear(); t = tour(int(c.data[4:])); await show(c, tcard(t), adm_kb(t, c.from_user.id)); await c.answer()


# — создание турнира —
@r.callback_query(F.data == "ac")
async def ac(c: CallbackQuery, state: FSMContext):
    if await deny(c, True): return
    await state.set_state(T.title); await c.message.answer("Название турнира:"); await c.answer()


@r.message(T.title)
async def t_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text); await state.set_state(T.when); await m.answer("Дата и время: <code>27.10.2026 19:00</code>")


@r.message(T.when)
async def t_when(m: Message, state: FSMContext):
    d = parse_dt(m.text or "")
    if not d: return await m.answer("Формат: <code>27.10.2026 19:00</code>")
    await state.update_data(start=d); await state.set_state(T.fmt)
    await m.answer("Формат (кнопкой или текстом):", reply_markup=kb([("Mixt", "tf:Mixt"), ("Americano", "tf:Americano")], [("Мужской", "tf:Мужской"), ("Женский", "tf:Женский")]))


async def t_fmt_done(m: Message, state: FSMContext, f):
    await state.update_data(format=f); await state.set_state(T.level); await m.answer("Уровень игроков (например, 2.5–3.5):")


@r.callback_query(T.fmt, F.data.startswith("tf:"))
async def t_fmt_cb(c: CallbackQuery, state: FSMContext): await c.answer(); await t_fmt_done(c.message, state, c.data[3:])


@r.message(T.fmt)
async def t_fmt_msg(m: Message, state: FSMContext): await t_fmt_done(m, state, m.text)


@r.message(T.level)
async def t_level(m: Message, state: FSMContext):
    await state.update_data(level=m.text); await state.set_state(T.price); await m.answer("Стоимость участия (например, 3000 ₽):")


@r.message(T.price)
async def t_price(m: Message, state: FSMContext):
    await state.update_data(price=m.text); await state.set_state(T.maxp); await m.answer("Максимум игроков (число, пары = 2 игрока):")


@r.message(T.maxp)
async def t_maxp(m: Message, state: FSMContext):
    if not (m.text or "").isdigit() or int(m.text) < 2: return await m.answer("Нужно число, например 16")
    await state.update_data(maxp=int(m.text)); await state.set_state(T.deadline)
    await m.answer("Дедлайн регистрации: <code>25.10.2026 20:00</code> или «-» если без дедлайна")


@r.message(T.deadline)
async def t_deadline(m: Message, state: FSMContext):
    dl = None
    if (m.text or "").strip() != "-":
        dl = parse_dt(m.text or "")
        if not dl: return await m.answer("Формат: <code>25.10.2026 20:00</code> или «-»")
    d = await state.get_data(); await state.clear()
    tid = ex("INSERT INTO tournaments(title,start,format,level,price,max_players,deadline) VALUES(?,?,?,?,?,?,?)",
             d["title"], d["start"], d["format"], d["level"], d["price"], d["maxp"], dl)
    posted = False
    try: posted = await post_to_group(tid)
    except Exception as e: log.warning("group post failed: %s", e)
    t = tour(tid)
    await m.answer(f"✅ Турнир создан{' и опубликован в группе' if posted else ''}.\n\n{tcard(t)}", reply_markup=adm_kb(t, m.from_user.id))


# — действия над турниром —
@r.callback_query(F.data.startswith(("aadd:", "apair:", "abc:", "ares:", "alim:")))
async def a_ask(c: CallbackQuery, state: FSMContext):
    kind, tid = c.data.split(":"); kind = kind[1:]
    if await deny(c, kind in ("res", "lim")): return
    await state.set_state(A.inp); await state.update_data(kind=kind, tid=int(tid))
    await c.message.answer({"add": "Отправьте @ник игрока, которого нужно добавить:",
                            "pair": "Отправьте два @ника через пробел — они станут парой:",
                            "bc": "Напишите сообщение — оно уйдёт всем участникам турнира:",
                            "res": "Новые дата и время: <code>27.10.2026 19:00</code>",
                            "lim": "Новый лимит игроков (число):"}[kind]); await c.answer()


def by_username(s): return q1("SELECT * FROM users WHERE lower(username)=?", s.strip().lstrip("@").lower())


@r.message(A.inp)
async def a_input(m: Message, state: FSMContext):
    d = await state.get_data(); kind, tid, txt = d["kind"], d["tid"], (m.text or "").strip(); t = tour(tid)
    if kind == "bc":
        n = await notify_tour(tid, f"📣 <b>{esc(t['title'])}</b>\n\n{esc(txt)}", ("reg", "offer", "wait"))
        await state.clear(); return await m.answer(f"Отправлено: {n} чел.")
    if kind == "add":
        u = by_username(txt)
        if not u: return await m.answer("Игрок не найден (он должен создать профиль в боте). Попробуйте ещё раз.")
        if myreg(tid, u["id"]): return await m.answer("Уже в списке.")
        ex("INSERT INTO regs(tid,uid,status) VALUES(?,?,'reg')", tid, u["id"]); await state.clear(); await changed(tid)
        await notify(u["id"], f"✅ Администратор записал вас на «{esc(t['title'])}».")
        return await m.answer("Добавлен ✅")
    if kind == "pair":
        names = txt.split()
        us = [by_username(x) for x in names[:2]] if len(names) == 2 else []
        if len(us) != 2 or not all(us) or not all(myreg(tid, u["id"]) for u in us):
            return await m.answer("Нужны два @ника игроков, уже записанных на этот турнир.")
        a, b = us[0]["id"], us[1]["id"]
        for u in (a, b):
            old = myreg(tid, u)["partner"]
            if old: ex("UPDATE regs SET partner=NULL WHERE tid=? AND uid=?", tid, old)
        ex("UPDATE regs SET partner=? WHERE tid=? AND uid=?", b, tid, a); ex("UPDATE regs SET partner=? WHERE tid=? AND uid=?", a, tid, b)
        ex("UPDATE seekers SET status='closed' WHERE tid=? AND uid IN(?,?)", tid, a, b); await state.clear()
        await notify(a, f"🔄 Состав пары изменён: ваш партнёр — {nm(b)} («{esc(t['title'])}»)")
        await notify(b, f"🔄 Состав пары изменён: ваш партнёр — {nm(a)} («{esc(t['title'])}»)")
        return await m.answer("Пара обновлена ✅")
    if kind == "res":
        nd = parse_dt(txt)
        if not nd: return await m.answer("Формат: <code>27.10.2026 19:00</code>")
        ex("UPDATE tournaments SET start=?, reminded=0 WHERE id=?", nd, tid); await state.clear()
        await notify_tour(tid, f"🕒 Изменилось время турнира «{esc(t['title'])}»!\nБыло: {fmt(t['start'])}\nСтало: {fmt(nd)}", ("reg", "offer", "wait"))
        await refresh_post(tid); return await m.answer("Перенесено, участники уведомлены ✅")
    if kind == "lim":
        if not txt.isdigit(): return await m.answer("Нужно число")
        ex("UPDATE tournaments SET max_players=? WHERE id=?", int(txt), tid); await state.clear(); await changed(tid)
        return await m.answer("Лимит обновлён ✅")


@r.callback_query(F.data.startswith(("adel:", "awl:")))
async def a_pick(c: CallbackQuery):
    if await deny(c): return
    kind, tid = c.data.split(":"); tid = int(tid); act = "adr" if kind == "adel" else "awr"
    rows = q("SELECT uid FROM regs WHERE tid=? AND status IN('reg','offer') ORDER BY id", tid)
    await show(c, "Выберите игрока:", kb(*[[(user(x["uid"])["name"], f"{act}:{tid}:{x['uid']}")] for x in rows], [("⬅️ Назад", f"adm:{tid}")])); await c.answer()


@r.callback_query(F.data.startswith("adr:"))
async def adr(c: CallbackQuery):
    if await deny(c): return
    _, tid, uid = c.data.split(":"); tid, uid = int(tid), int(uid); t = tour(tid)
    await remove_player(tid, uid, by_admin=True)
    await notify(uid, f"ℹ️ Администратор снял вас с турнира «{esc(t['title'])}».")
    await show(c, "Игрок удалён ✅\n\n" + tcard(tour(tid)), adm_kb(tour(tid), c.from_user.id)); await c.answer()


@r.callback_query(F.data.startswith("awr:"))
async def awr(c: CallbackQuery):
    if await deny(c): return
    _, tid, uid = c.data.split(":"); tid, uid = int(tid), int(uid); t = tour(tid); reg = myreg(tid, uid)
    if reg:
        if reg["partner"]: ex("UPDATE regs SET partner=NULL WHERE tid=? AND uid=?", tid, reg["partner"])
        ex("DELETE FROM regs WHERE id=?", reg["id"]); ex("INSERT INTO regs(tid,uid,status) VALUES(?,?,'wait')", tid, uid)
        await changed(tid); await notify(uid, f"⏳ Администратор перенёс вас в лист ожидания турнира «{esc(t['title'])}».")
    await show(c, "Перемещён в лист ожидания ✅\n\n" + tcard(tour(tid)), adm_kb(tour(tid), c.from_user.id)); await c.answer()


@r.callback_query(F.data.startswith("atog:"))
async def atog(c: CallbackQuery):
    if await deny(c, True): return
    tid = int(c.data[5:]); t = tour(tid)
    ex("UPDATE tournaments SET status=? WHERE id=?", "closed" if t["status"] == "open" else "open", tid); await refresh_post(tid)
    t = tour(tid); await show(c, tcard(t), adm_kb(t, c.from_user.id)); await c.answer("Готово")


@r.callback_query(F.data.startswith("apost:"))
async def apost(c: CallbackQuery):
    if await deny(c, True): return
    try: ok = await post_to_group(int(c.data[6:]))
    except Exception as e: ok = False; log.warning(e)
    await c.answer("Опубликовано в группе ✅" if ok else "Не удалось: проверьте GROUP_ID и права бота в группе", show_alert=True)


@r.callback_query(F.data.startswith("acan:"))
async def acan(c: CallbackQuery):
    if await deny(c, True): return
    tid = int(c.data[5:]); await show(c, "Отменить турнир и уведомить всех участников?", kb([("Да, отменить", f"acany:{tid}")], [("Нет", f"adm:{tid}")])); await c.answer()


@r.callback_query(F.data.startswith("acany:"))
async def acany(c: CallbackQuery):
    if await deny(c, True): return
    tid = int(c.data[6:]); t = tour(tid)
    await notify_tour(tid, f"🚫 Турнир «{esc(t['title'])}» ({fmt(t['start'])}) отменён.", ("reg", "offer", "wait"))
    ex("UPDATE tournaments SET status='cancelled' WHERE id=?", tid); await refresh_post(tid)
    await show(c, "Турнир отменён, участники уведомлены ✅", kb([("⬅️ Назад", "al")])); await c.answer()


@r.callback_query(F.data.startswith("aexp:"))
async def aexp(c: CallbackQuery):
    if await deny(c): return
    tid = int(c.data[5:]); buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Статус", "Имя", "Ник", "Телефон", "Уровень", "Пол", "Сторона", "Партнёр"])
    for x in q("SELECT * FROM regs WHERE tid=? ORDER BY status, id", tid):
        u = user(x["uid"]); p = user(x["partner"]) if x["partner"] else None
        w.writerow([{"reg": "участник", "wait": "лист ожидания", "offer": "ждёт подтверждения"}[x["status"]], u["name"], u["username"] or "",
                    u["phone"] or "", u["level"], u["gender"], u["side"], p["name"] if p else ""])
    await c.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), f"tournament_{tid}.csv")); await c.answer()


# — опросы —
@r.callback_query(F.data == "apoll")
async def apoll(c: CallbackQuery, state: FSMContext):
    if await deny(c, True): return
    await state.set_state(Q.question); await c.message.answer("Вопрос опроса:"); await c.answer()


@r.message(Q.question)
async def q_question(m: Message, state: FSMContext):
    await state.update_data(question=m.text); await state.set_state(Q.options)
    await m.answer("Варианты ответа — каждый с новой строки (минимум 2):")


@r.message(Q.options)
async def q_options(m: Message, state: FSMContext):
    opts = [x.strip() for x in (m.text or "").split("\n") if x.strip()][:8]
    if len(opts) < 2: return await m.answer("Нужно минимум 2 варианта, каждый с новой строки.")
    d = await state.get_data(); await state.clear()
    pid = ex("INSERT INTO polls(question,options) VALUES(?,?)", d["question"], "\n".join(opts))
    markup = kb(*[[(o, f"pv:{pid}:{i}")] for i, o in enumerate(opts)]); text = f"📊 <b>{esc(d['question'])}</b>"
    if GROUP_ID: await bot.send_message(GROUP_ID, text, reply_markup=markup)
    else:
        for u in q("SELECT id FROM users"): await notify(u["id"], text, markup)
    await m.answer("Опрос опубликован ✅ Результаты — в «Результаты опросов».")


@r.callback_query(F.data.startswith("pv:"))
async def pv(c: CallbackQuery):
    _, pid, i = c.data.split(":")
    ex("INSERT OR REPLACE INTO votes VALUES(?,?,?,?)", int(pid), c.from_user.id, c.from_user.full_name, int(i)); await c.answer("Голос учтён ✅")


@r.callback_query(F.data == "apr")
async def apr(c: CallbackQuery):
    if await deny(c, True): return
    ps = q("SELECT * FROM polls ORDER BY id DESC LIMIT 10")
    await show(c, "📈 Опросы:" if ps else "Опросов пока нет.", kb(*[[(p["question"][:50], f"pres:{p['id']}")] for p in ps], [("⬅️ Назад", "ap")])); await c.answer()


@r.callback_query(F.data.startswith("pres:"))
async def pres(c: CallbackQuery):
    if await deny(c, True): return
    p = q1("SELECT * FROM polls WHERE id=?", int(c.data[5:])); s = f"📊 <b>{esc(p['question'])}</b>\n"
    for i, o in enumerate(p["options"].split("\n")):
        v = q("SELECT name FROM votes WHERE pid=? AND opt=?", p["id"], i)
        s += f"\n<b>{esc(o)}</b> — {len(v)}" + (f"\n   {esc(', '.join(x['name'] for x in v))}" if v else "")
    await show(c, s, kb([("⬅️ Назад", "apr")])); await c.answer()


# — команды владельца —
@r.message(Command("addadmin"))
async def addadmin(m: Message, command: CommandObject):
    if role(m.from_user.id) != "owner": return
    a = (command.args or "").split()
    if not a or not a[0].isdigit(): return await m.answer("Формат: /addadmin ID [admin|manager]\nadmin — полный доступ, manager — только участники и рассылки.")
    ex("INSERT OR REPLACE INTO admins VALUES(?,?)", int(a[0]), a[1] if len(a) > 1 and a[1] in ("admin", "manager") else "manager")
    await m.answer("Админ добавлен ✅")


@r.message(Command("rmadmin"))
async def rmadmin(m: Message, command: CommandObject):
    if role(m.from_user.id) == "owner" and (command.args or "").isdigit():
        ex("DELETE FROM admins WHERE id=?", int(command.args)); await m.answer("Удалён ✅")


@r.message(Command("setrating"))
async def setrating(m: Message, command: CommandObject):
    if not full(m.from_user.id): return
    a = (command.args or "").split(); u = by_username(a[0]) if len(a) == 2 else None
    if not u or not a[1].lstrip("-").isdigit(): return await m.answer("Формат: /setrating @ник 1250")
    ex("UPDATE users SET rating=? WHERE id=?", int(a[1]), u["id"]); await m.answer("Рейтинг обновлён ✅")


# ───────────────────────── Фоновые задачи ─────────────────────────
async def expire_offers():
    tids = set()
    for x in q("SELECT * FROM regs WHERE status='offer' AND offer_until<?", now()):
        ex("DELETE FROM regs WHERE id=?", x["id"]); tids.add(x["tid"])
        await notify(x["uid"], "⌛ Время на подтверждение истекло — место передано следующему игроку.")
    for tid in tids: await changed(tid)


async def background():
    while True:
        try:
            await expire_offers()
            lim = (datetime.now() + timedelta(hours=24)).strftime(FMT)
            for t in q("SELECT * FROM tournaments WHERE status!='cancelled' AND reminded=0 AND start<=? AND start>?", lim, now()):
                ex("UPDATE tournaments SET reminded=1 WHERE id=?", t["id"])
                await notify_tour(t["id"], f"⏰ Напоминание: турнир «{esc(t['title'])}» — {fmt(t['start'])}. Ждём вас! 🦁", ("reg",))
        except Exception:
            log.exception("background")
        await asyncio.sleep(60)


async def main():
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username
    dp.include_router(r)
    asyncio.create_task(background())
    log.info("Bot @%s started", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
