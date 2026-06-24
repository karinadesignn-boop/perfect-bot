import asyncio
import logging
import os
import sqlite3
import json
import calendar
import io
import random
import tempfile
from datetime import datetime, date, timedelta

import pytz

from groq import AsyncGroq
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    logging.warning("matplotlib not installed — image generation disabled")

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "data.db")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')

REFLECTION_QUESTIONS = [
    "✨ Остановись на мгновение. Что сейчас происходит внутри твоего тела — какое послание оно несёт?",
    "🌙 Какая мысль сегодня возвращается к тебе снова и снова, словно пытается достучаться?",
    "🔮 Оцени свою энергию от 1 до 10. Что сегодня питает тебя, а что забирает силу?",
    "🌿 Есть ли что-то, что тяготит твою душу прямо сейчас? Назови это — уже легче.",
    "⭐ Какой маленький момент сегодня был твоим даром — пусть совсем крошечным?",
    "💜 Как ты сейчас обращаешься с собой — как с другом или как с судьёй?",
    "🌸 Твоё тело мудрее ума. Что оно хочет тебе сказать прямо сейчас?",
    "🕊️ Какое чувство сейчас громче всех? Позволь ему просто быть.",
    "🌌 Что ты откладываешь? Какая часть тебя боится сделать этот шаг?",
    "🌊 Если бы твоё состояние было стихией — огнём, водой, ветром или землёй — что это было бы?",
    "🪬 Что сегодня требует твоего принятия, а не борьбы?",
    "✨ Представь себя через год. Что она хочет тебе сказать прямо сейчас?",
]

POLYVAGAL_PRACTICES = [
    "🌬 *Дыхание освобождения*\nДва вдоха через нос подряд — короткий и ещё один, потом медленный длинный выдох через рот. Повтори 3 раза. Нервная система получает сигнал — ты в безопасности.",
    "🤲 *Прикосновение к сердцу*\nПоложи обе руки на грудь. Почувствуй тепло. Три медленных вдоха и скажи себе: «Я здесь. Я в безопасности. Этот момент — мой.»",
    "🔯 *Дыхание по квадрату*\nВдох 4 сек → задержка 4 → выдох 4 → задержка 4. Повтори 4 раза. Это древняя практика возврата в центр.",
    "🌍 *Якорение в настоящем*\n5 вещей которые видишь → 4 которые можешь потрогать → 3 звука → 2 запаха → 1 вкус. Ты здесь. Ты реальна.",
    "🦋 *Объятие себя*\nСкрести руки на груди. Обними себя. Поочерёдно мягко постукивай по плечам — левое, правое, медленно. Это твоя забота о себе.",
    "👁 *Мягкий взгляд*\nСмотри перед собой и медленно расширяй взгляд по бокам, не двигая глазами. Удержи 30-60 секунд. Периферийное зрение — сигнал безопасности для древней части мозга.",
    "🌊 *Волна выдоха*\nВдох 4 сек, выдох 8 сек. Длинный выдох — как волна, уносящая напряжение. Повтори 5 раз. Парасимпатика просыпается.",
]


# ---------- БАЗА ДАННЫХ ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'inbox',
            date TEXT,
            time TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routine_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            UNIQUE(item_id, date)
        )
    """)
    conn.commit()
    conn.close()


def register_user(chat_id: int):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)",
        (chat_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_all_users() -> list[int]:
    conn = db()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r['chat_id'] for r in rows]


# ---------- КЛАВИАТУРЫ ----------

def routine_keyboard(item_id: int, today: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выполнено", callback_data=f"done_routine:{item_id}:{today}")
    b.button(text="🗑 Удалить", callback_data=f"delete:{item_id}")
    b.adjust(2)
    return b.as_markup()


def plan_keyboard(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Готово", callback_data=f"done_plan:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"delete:{item_id}")
    b.adjust(2)
    return b.as_markup()


def simple_keyboard(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Удалить", callback_data=f"delete:{item_id}")
    return b.as_markup()


# ---------- ИИ-РОУТЕР ----------

async def classify_message(text: str) -> dict:
    now_vn = datetime.now(VN_TZ)
    today_str = now_vn.strftime('%Y-%m-%d')
    weekdays = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
    weekday_ru = weekdays[now_vn.weekday()]

    tomorrow = (now_vn.date() + timedelta(days=1)).strftime('%Y-%m-%d')
    system_prompt = f"""Ты личный ассистент-органайзер. Сегодня {today_str} ({weekday_ru}). Пользователь в Вьетнаме UTC+7.

Разбери сообщение на отдельные пункты и верни JSON:
{{"items":[{{"type":"...","text":"...","date":"...","time":"...","old_text":"..."}}],"response":"..."}}

ТИПЫ — выбирай строго по смыслу:
• plan — дело привязанное к конкретной дате или времени («встреча в пятницу», «7 июля», «завтра», «сегодня»)
• routine — повторяющаяся ежедневная привычка («каждый день», «по утрам», «ежедневно»)
• someday — мечта или идея БЕЗ конкретной даты («хочу когда-нибудь», «было бы здорово»)
• reflection — личные мысли, чувства, наблюдения о себе
• question — вопрос или просьба об информации (только ответь в response, не сохраняй)
• update — ИСПРАВЛЕНИЕ или ЗАМЕНА уже существующего пункта («скорректируй», «замени», «исправь», «поменяй», «вместо X сделай Y»)

Для типа update:
• old_text = ключевые слова из старого пункта (что искать в базе)
• text = новая версия (грамотно сформулированная)
• date = дата старого пункта если упоминается

ДАТЫ:
• «завтра» = {tomorrow}
• «сегодня» = {today_str}
• «в пятницу/субботу/...» = ближайший такой день после сегодня
• числа без года → текущий год (или следующий если дата уже прошла)

ТЕКСТ (поле text): перепиши грамотно и чисто, исправь все ошибки. Для plan — формулируй как действие с глаголом.
Примеры: «зап на узи голеностоп» → «Записаться на УЗИ голеностопа» | «психолог 16-18» → «Сеанс у психолога» | «посчитать финансы» → «Подвести финансовый итог месяца»

ОТВЕТ (поле response): одно короткое предложение-подтверждение на русском."""

    response = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        max_tokens=800,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find('{')
        end = content.rfind('}') + 1
        return json.loads(content[start:end])


async def process_and_save(chat_id: int, text: str, message: Message):
    if not groq_client:
        await message.answer("⚠️ ИИ не настроен. Добавь GROQ_API_KEY.")
        return

    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        result = await classify_message(text)
    except Exception as e:
        logging.error(f"AI classify error: {e}")
        await message.answer("❌ Не смогла обработать. Попробуй ещё раз.")
        return

    items = result.get("items", [])
    response_text = result.get("response", "Сохранила ✅")

    # только вопрос — ничего не сохраняем
    if items and all(i.get("type") == "question" for i in items):
        await message.answer(response_text)
        return

    icons = {"plan": "📅", "routine": "🔄", "someday": "🌙", "reflection": "💭", "inbox": "📥", "update": "✏️"}
    conn = db()
    saved = []
    for item in items:
        msg_type = item.get("type", "inbox")
        if msg_type == "question":
            continue

        save_text = item.get("text", text)
        item_date = item.get("date")
        item_time = item.get("time")

        if msg_type == "update":
            old_text = item.get("old_text", "")
            # ищем похожий пункт в базе
            search_terms = [w for w in old_text.lower().split() if len(w) > 2]
            found_id = None
            if search_terms:
                like_pattern = "%" + "%".join(search_terms[:3]) + "%"
                row = conn.execute(
                    "SELECT id FROM items WHERE chat_id=? AND status='active' AND LOWER(text) LIKE ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (chat_id, like_pattern)
                ).fetchone()
                if row:
                    found_id = row['id']
            if found_id:
                conn.execute(
                    "UPDATE items SET text=?, date=?, time=? WHERE id=?",
                    (save_text, item_date, item_time, found_id)
                )
                saved.append("✏️")
            else:
                # не нашли — создаём новый
                conn.execute(
                    "INSERT INTO items (chat_id, text, type, date, time, status, created_at) VALUES (?,?,?,?,?,?,?)",
                    (chat_id, save_text, "plan", item_date, item_time, 'active', datetime.now().isoformat())
                )
                saved.append("📅")
        else:
            conn.execute(
                "INSERT INTO items (chat_id, text, type, date, time, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (chat_id, save_text, msg_type, item_date, item_time, 'active', datetime.now().isoformat())
            )
            saved.append(icons.get(msg_type, "📥"))

    conn.commit()
    conn.close()

    if not saved:
        await message.answer(response_text)
        return

    icons_line = " ".join(dict.fromkeys(saved))
    await message.answer(f"{icons_line} {response_text}")


# ---------- ГОЛОСОВЫЕ ----------

async def transcribe_voice(file_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        with open(tmp_path, 'rb') as f:
            transcription = await groq_client.audio.transcriptions.create(
                file=("voice.ogg", f),
                model="whisper-large-v3-turbo",
                response_format="text",
                language="ru",
            )
        return transcription
    finally:
        os.unlink(tmp_path)


@dp.message(F.voice)
async def handle_voice(message: Message):
    register_user(message.chat.id)
    if not groq_client:
        await message.answer("⚠️ ИИ не настроен.")
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    file = await bot.get_file(message.voice.file_id)
    downloaded = await bot.download_file(file.file_path)

    try:
        text = await transcribe_voice(downloaded.read())
        await message.answer(f"🎤 _{text}_", parse_mode="Markdown")
        await process_and_save(message.chat.id, text, message)
    except Exception as e:
        logging.error(f"Voice error: {e}")
        await message.answer("❌ Не смогла расшифровать голосовое.")


# ---------- КОМАНДЫ ----------

@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.chat.id)
    await message.answer(
        "Привет! 👋 Я твой личный ассистент.\n\n"
        "Пиши мне всё что угодно — разберусь сама куда положить.\n\n"
        "📅 /plans — план на неделю картинкой\n"
        "🗓 /month — план на месяц картинкой\n"
        "🔄 /routines — ежедневные рутины\n"
        "🌙 /someday — список «когда-нибудь»\n"
        "💭 /reflections — дневник рефлексий\n"
        "📥 /inbox — необработанные записи\n"
        "/help — как пользоваться"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Пиши мне в свободной форме:\n\n"
        "📅 *Планы:* «встреча в пятницу в 15:00», «сдать отчёт 30-го»\n"
        "🔄 *Рутины:* «каждый день медитация», «пить воду утром»\n"
        "🌙 *Когда-нибудь:* «хочу поехать в Японию»\n"
        "💭 *Рефлексия:* «сегодня поняла что...», «чувствую тревогу»\n"
        "💬 *Вопрос:* любой вопрос — просто отвечу\n\n"
        "Голосовые тоже принимаю 🎤\n\n"
        "Каждый вечер в 22:00 — план на завтра.\n"
        "Каждые 3 часа — вопрос для рефлексии или практика.",
        parse_mode="Markdown"
    )


@dp.message(Command("routines"))
async def routines_cmd(message: Message):
    today = datetime.now(VN_TZ).strftime('%Y-%m-%d')
    conn = db()
    rows = conn.execute(
        "SELECT i.*, EXISTS(SELECT 1 FROM routine_log r WHERE r.item_id=i.id AND r.date=?) as done "
        "FROM items i WHERE i.chat_id=? AND i.type='routine' AND i.status='active' ORDER BY i.id",
        (today, message.chat.id)
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("Рутин пока нет.\n\nНапиши например: «каждый день медитация 10 минут»")
        return

    for r in rows:
        icon = "✅" if r['done'] else "⬜️"
        kb = None if r['done'] else routine_keyboard(r['id'], today)
        await message.answer(f"{icon} {r['text']}", reply_markup=kb)



@dp.message(Command("someday"))
async def someday_cmd(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='someday' AND status='active' ORDER BY id DESC LIMIT 20",
        (message.chat.id,)
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("Список «когда-нибудь» пуст.\n\nНапиши например: «хочу поехать на Бали»")
        return

    for r in rows:
        await message.answer(f"🌙 {r['text']}", reply_markup=simple_keyboard(r['id']))


@dp.message(Command("inbox"))
async def inbox_cmd(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='inbox' AND status='active' ORDER BY id DESC LIMIT 10",
        (message.chat.id,)
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("Инбокс пуст 🎉")
        return

    for r in rows:
        await message.answer(f"📥 {r['text']}", reply_markup=simple_keyboard(r['id']))


@dp.message(Command("reflections"))
async def reflections_cmd(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='reflection' AND status='active' "
        "ORDER BY created_at DESC LIMIT 10",
        (message.chat.id,)
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("Дневник пуст.\n\nПоделись наблюдением о себе — напиши или запиши голосовое 🎤")
        return

    await message.answer("*Последние записи:*", parse_mode="Markdown")
    for r in rows:
        dt = datetime.fromisoformat(r['created_at'])
        dt_vn = pytz.utc.localize(dt).astimezone(VN_TZ) if dt.tzinfo is None else dt.astimezone(VN_TZ)
        label = dt_vn.strftime('%d.%m %H:%M')
        await message.answer(f"💭 _{label}_\n{r['text']}", parse_mode="Markdown")


# ---------- КАРТИНКИ-ПЛАННЕРЫ ----------

MONTH_NAMES = ['Январь','Февраль','Март','Апрель','Май','Июнь',
               'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']
DAY_NAMES_FULL = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']


async def generate_plan_image(chat_id: int, mode: str = 'week') -> bytes:
    now_vn = datetime.now(VN_TZ)
    today = now_vn.date()

    if mode == 'week':
        start = today - timedelta(days=today.weekday())
        days = [start + timedelta(days=i) for i in range(7)]
        return _draw_week(chat_id, days, today)
    else:
        start = date(today.year, today.month, 1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        days = [start + timedelta(days=i) for i in range(last_day)]
        return _draw_month(chat_id, days, today)


def _fetch_tasks(chat_id: int, start: date, end: date) -> dict[str, list[str]]:
    conn = db()
    rows = conn.execute(
        "SELECT text, date, time FROM items WHERE chat_id=? AND type='plan' AND status='active' "
        "AND date >= ? AND date <= ? ORDER BY date, time",
        (chat_id, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
    ).fetchall()
    conn.close()
    result: dict[str, list[str]] = {}
    for r in rows:
        label = f"{r['time']}  {r['text']}" if r['time'] else r['text']
        result.setdefault(r['date'], []).append(label)
    return result


# пастельная палитра
BG        = '#fdf8ff'   # фон страницы
CARD_BG   = '#ffffff'   # фон карточки дня
WKND_BG   = '#f8f4ff'   # фон выходного
HDR_TODAY = '#d8b4fe'   # шапка сегодня (лаванда)
HDR_WKND  = '#fce7f3'   # шапка выходного (пудра)
HDR_REG   = '#f0e9ff'   # шапка обычного дня
TXT_MAIN  = '#3d2c5e'   # основной текст
TXT_MUTED = '#b0a0c8'   # приглушённый текст
BORDER    = '#ede8f5'   # граница

PASTEL_PILLS = [
    ('#e9d5ff', '#6b21a8'),  # лаванда
    ('#fce7f3', '#9d174d'),  # розовая пудра
    ('#d1fae5', '#065f46'),  # мята
    ('#fef3c7', '#92400e'),  # персик
    ('#dbeafe', '#1e40af'),  # нежно-голубой
    ('#ffd7d7', '#991b1b'),  # пастельный коралл
    ('#d4f5e9', '#155e3c'),  # шалфей
    ('#ede9fe', '#4c1d95'),  # сирень
]


def _draw_week(chat_id: int, days: list, today: date) -> bytes:
    tasks = _fetch_tasks(chat_id, days[0], days[-1])

    WIDTH = 860
    PAD   = 28
    TITLE_H = 54
    DAY_H   = 52
    LINE_H  = 42
    GAP     = 8
    R       = 10  # радиус скругления шапки

    total_h = TITLE_H
    for d in days:
        n = len(tasks.get(d.strftime('%Y-%m-%d'), []))
        total_h += DAY_H + max(n, 1) * LINE_H + GAP * 2
    total_h += PAD + 10

    fig = plt.figure(figsize=(WIDTH / 100, total_h / 100), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(0, total_h)
    ax.invert_yaxis()
    ax.axis('off')

    title = f"{days[0].strftime('%d')} – {days[-1].strftime('%d %B %Y')}"
    ax.text(WIDTH / 2, 16, title, ha='center', va='top',
            fontsize=17, color=TXT_MAIN, fontweight='bold')

    y = TITLE_H
    day_names_full = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']

    for d in days:
        date_str = d.strftime('%Y-%m-%d')
        is_today = (d == today)
        is_weekend = d.weekday() >= 5

        hdr = HDR_TODAY if is_today else (HDR_WKND if is_weekend else HDR_REG)
        ax.add_patch(mpatches.FancyBboxPatch(
            [PAD, y], WIDTH - PAD * 2, DAY_H,
            boxstyle=f"round,pad=0", facecolor=hdr, edgecolor=BORDER, linewidth=0.8,
            zorder=1
        ))
        day_label = f"{day_names_full[d.weekday()].upper()}  ·  {d.strftime('%d %B')}"
        ax.text(PAD + 18, y + DAY_H / 2, day_label, ha='left', va='center',
                fontsize=13, color=TXT_MAIN, fontweight='bold', zorder=2)

        y += DAY_H + GAP
        day_tasks = tasks.get(date_str, [])

        if not day_tasks:
            ax.text(PAD + 18, y + LINE_H / 2, 'нет событий', ha='left', va='center',
                    fontsize=11, color=TXT_MUTED, style='italic')
            y += LINE_H
        else:
            for j, task in enumerate(day_tasks):
                pill_bg, pill_txt = PASTEL_PILLS[j % len(PASTEL_PILLS)]
                ax.add_patch(mpatches.FancyBboxPatch(
                    [PAD + 4, y + 5], WIDTH - (PAD + 4) * 2, LINE_H - 10,
                    boxstyle="round,pad=4", facecolor=pill_bg, edgecolor='none'
                ))
                ax.text(PAD + 22, y + LINE_H / 2, task, ha='left', va='center',
                        fontsize=12, color=pill_txt, fontweight='bold')
                y += LINE_H

        y += GAP
        ax.plot([PAD, WIDTH - PAD], [y, y], color=BORDER, linewidth=0.8)
        y += 4

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _draw_month(chat_id: int, days: list, today: date) -> bytes:
    tasks = _fetch_tasks(chat_id, days[0], days[-1])

    CELL_W  = 174
    CELL_H  = 124
    PAD_TOP = 72
    HDR_H   = 34
    COLS    = 7
    first_wd = days[0].weekday()
    n_rows   = ((first_wd + len(days)) + 6) // 7
    WIDTH    = CELL_W * COLS
    HEIGHT   = PAD_TOP + HDR_H + n_rows * CELL_H + 24

    fig = plt.figure(figsize=(WIDTH / 100, HEIGHT / 100), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(0, HEIGHT)
    ax.invert_yaxis()
    ax.axis('off')

    m = days[0]
    ax.text(WIDTH / 2, 22, f"{MONTH_NAMES[m.month - 1]}  {m.year}",
            ha='center', va='top', fontsize=22, color=TXT_MAIN, fontweight='bold')

    for i, dn in enumerate(['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС']):
        cx = i * CELL_W + CELL_W / 2
        color = '#c084fc' if i >= 5 else TXT_MUTED
        ax.text(cx, PAD_TOP + 8, dn, ha='center', va='top',
                fontsize=12, color=color, fontweight='bold')

    for idx, d in enumerate(days):
        cell_idx = first_wd + idx
        col = cell_idx % 7
        row = cell_idx // 7
        cx = col * CELL_W
        cy = PAD_TOP + HDR_H + row * CELL_H
        date_str = d.strftime('%Y-%m-%d')
        is_today = (d == today)
        is_weekend = d.weekday() >= 5

        bg = WKND_BG if is_weekend else CARD_BG
        ax.add_patch(plt.Rectangle([cx, cy], CELL_W, CELL_H,
                                    facecolor=bg, edgecolor=BORDER, linewidth=0.6))

        if is_today:
            ax.add_patch(plt.Circle((cx + 20, cy + 20), 15,
                                     facecolor='#c084fc', edgecolor='none', zorder=2))
            ax.text(cx + 20, cy + 20, str(d.day), ha='center', va='center',
                    fontsize=12, color='#ffffff', fontweight='bold', zorder=3)
        else:
            nc = '#c084fc' if is_weekend else TXT_MAIN
            ax.text(cx + 10, cy + 8, str(d.day), ha='left', va='top',
                    fontsize=12, color=nc, fontweight='bold')

        for j, task in enumerate(tasks.get(date_str, [])[:3]):
            py = cy + 34 + j * 27
            pill_bg, pill_txt = PASTEL_PILLS[j % len(PASTEL_PILLS)]
            ax.add_patch(mpatches.FancyBboxPatch(
                [cx + 4, py], CELL_W - 8, 22,
                boxstyle="round,pad=2", facecolor=pill_bg, edgecolor='none'
            ))
            short = task[:19] + '…' if len(task) > 19 else task
            ax.text(cx + CELL_W / 2, py + 11, short,
                    ha='center', va='center', fontsize=9, color=pill_txt, fontweight='bold')

        extra = len(tasks.get(date_str, [])) - 3
        if extra > 0:
            ax.text(cx + CELL_W - 6, cy + CELL_H - 6, f'+{extra}',
                    ha='right', va='bottom', fontsize=8, color=TXT_MUTED)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


@dp.message(Command("plans"))
async def plans_cmd(message: Message):
    if not HAS_MPL:
        await message.answer("❌ matplotlib не установлен на сервере. Напиши администратору.")
        return
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        img_bytes = await generate_plan_image(message.chat.id, 'week')
        await message.answer_photo(BufferedInputFile(img_bytes, filename="plans.png"), caption="📅 План на неделю")
    except Exception as e:
        logging.error(f"Plan image error: {e}")
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("month"))
async def month_cmd(message: Message):
    if not HAS_MPL:
        await message.answer("❌ matplotlib не установлен на сервере. Напиши администратору.")
        return
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        img_bytes = await generate_plan_image(message.chat.id, 'month')
        now_vn = datetime.now(VN_TZ)
        await message.answer_photo(
            BufferedInputFile(img_bytes, filename="month.png"),
            caption=f"🗓 {MONTH_NAMES[now_vn.month - 1]} {now_vn.year}"
        )
    except Exception as e:
        logging.error(f"Month image error: {e}")
        await message.answer(f"❌ Ошибка: {e}")


# ---------- КОЛБЭКИ ----------

@dp.callback_query(F.data.startswith("done_routine:"))
async def done_routine(callback: CallbackQuery):
    _, item_id, today = callback.data.split(":")
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO routine_log (chat_id, item_id, date) VALUES (?,?,?)",
        (callback.message.chat.id, int(item_id), today)
    )
    conn.commit()
    conn.close()
    await callback.message.edit_text("✅ " + callback.message.text)
    await callback.answer("Отмечено!")


@dp.callback_query(F.data.startswith("done_plan:"))
async def done_plan(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("UPDATE items SET status='done' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text("✅ " + callback.message.text)
    await callback.answer("Готово!")


@dp.callback_query(F.data.startswith("delete:"))
async def delete_item(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text("🗑 " + callback.message.text)
    await callback.answer("Удалено")


# ---------- ГЛАВНЫЙ ОБРАБОТЧИК ----------

@dp.message(F.text)
async def handle_message(message: Message):
    register_user(message.chat.id)
    await process_and_save(message.chat.id, message.text, message)


# ---------- ВЕЧЕРНИЙ ПЛАН ----------

async def send_evening_plan(chat_id: int):
    now_vn = datetime.now(VN_TZ)
    tomorrow = now_vn.date() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime('%Y-%m-%d')
    tomorrow_display = tomorrow.strftime('%d.%m.%Y')

    conn = db()
    plans = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='plan' AND date=? AND status='active' ORDER BY time",
        (chat_id, tomorrow_str)
    ).fetchall()
    routines = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='routine' AND status='active' ORDER BY id",
        (chat_id,)
    ).fetchall()
    conn.close()

    text = f"🌙 *План на завтра — {tomorrow_display}*\n\n"

    if routines:
        text += "🔄 *Рутины:*\n"
        for r in routines:
            text += f"⬜️ {r['text']}\n"
        text += "\n"

    if plans:
        text += "📅 *Запланировано:*\n"
        for p in plans:
            time_str = f"{p['time']}  " if p['time'] else ""
            text += f"• {time_str}{p['text']}\n"
    elif not routines:
        text += "Ничего не запланировано. Завтра — чистый лист ✨"

    await bot.send_message(chat_id, text, parse_mode="Markdown")


# ---------- ПРОМПТ РЕФЛЕКСИИ ----------

async def send_reflection_prompt(chat_id: int):
    if random.random() < 0.5:
        question = random.choice(REFLECTION_QUESTIONS)
        await bot.send_message(
            chat_id,
            f"💭 *Минута для себя:*\n\n{question}\n\n_Можешь ответить текстом или голосовым._",
            parse_mode="Markdown"
        )
    else:
        practice = random.choice(POLYVAGAL_PRACTICES)
        await bot.send_message(
            chat_id,
            f"🌿 *Практика для нервной системы:*\n\n{practice}",
            parse_mode="Markdown"
        )


# ---------- ПЛАНИРОВЩИК ----------

async def scheduler_loop():
    sent_evening: set[str] = set()
    sent_reflection: set[str] = set()
    sent_plans: set[str] = set()

    # Рефлексия в 9, 12, 15, 18 по Вьетнаму = 2, 5, 8, 11 UTC
    REFLECTION_UTC = {'02:00', '05:00', '08:00', '11:00'}

    while True:
        now_utc = datetime.utcnow()
        now_vn = datetime.now(VN_TZ)
        utc_hhmm = now_utc.strftime('%H:%M')
        vn_date = now_vn.strftime('%Y-%m-%d')
        vn_hhmm = now_vn.strftime('%H:%M')

        # Вечерний план в 22:00 VN = 15:00 UTC
        evening_key = f"evening:{vn_date}"
        if utc_hhmm == '15:00' and evening_key not in sent_evening:
            sent_evening.add(evening_key)
            for chat_id in get_all_users():
                try:
                    await send_evening_plan(chat_id)
                except Exception as e:
                    logging.error(f"Evening plan error {chat_id}: {e}")

        # Рефлексия каждые 3 часа
        reflection_key = f"reflection:{vn_date}:{utc_hhmm}"
        if utc_hhmm in REFLECTION_UTC and reflection_key not in sent_reflection:
            sent_reflection.add(reflection_key)
            for chat_id in get_all_users():
                try:
                    await send_reflection_prompt(chat_id)
                except Exception as e:
                    logging.error(f"Reflection error {chat_id}: {e}")

        # Напоминания о планах в точное время
        plan_key = f"plan:{vn_date}:{vn_hhmm}"
        if plan_key not in sent_plans:
            conn = db()
            due = conn.execute(
                "SELECT * FROM items WHERE type='plan' AND status='active' AND date=? AND time=?",
                (vn_date, vn_hhmm)
            ).fetchall()
            conn.close()
            if due:
                sent_plans.add(plan_key)
                for p in due:
                    try:
                        await bot.send_message(
                            p['chat_id'],
                            f"⏰ *{p['time']}* — {p['text']}",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logging.error(f"Plan reminder error: {e}")

        if len(sent_evening) > 500:
            sent_evening.clear()
        if len(sent_reflection) > 500:
            sent_reflection.clear()
        if len(sent_plans) > 2000:
            sent_plans.clear()

        await asyncio.sleep(60)


async def main():
    init_db()
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
