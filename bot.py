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
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

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
    "Как твоё тело прямо сейчас? Закрой глаза на секунду и просто почувствуй.",
    "Что сейчас занимает больше всего твоих мыслей?",
    "Оцени свою энергию от 1 до 10. Что влияет на неё сегодня?",
    "Есть ли что-то, что тебя сейчас беспокоит? Можно просто назвать это одним словом.",
    "Что сегодня было хорошего, даже самого маленького?",
    "Как ты сейчас относишься к себе — с добротой или с критикой?",
    "Что твоё тело пытается тебе сказать прямо сейчас?",
    "Какая эмоция сейчас самая громкая внутри?",
    "Что ты сейчас откладываешь? Почему, как тебе кажется?",
    "Если бы твоё состояние было погодой — какой она была бы прямо сейчас?",
]

POLYVAGAL_PRACTICES = [
    "🌬 *Физиологический вздох*\nДва вдоха через нос подряд — короткий и ещё один, потом медленный длинный выдох через рот. Повтори 3 раза. Это быстро успокаивает нервную систему.",
    "🤲 *Рука на сердце*\nПоложи руку на грудь. Почувствуй тепло ладони. Медленно дыши и скажи себе: «Я здесь. Я в безопасности прямо сейчас.»",
    "📦 *Дыхание по квадрату*\nВдох 4 сек → задержка 4 → выдох 4 → задержка 4. Повтори 4 раза. Можно смотреть на любой прямоугольник вокруг.",
    "🌍 *Заземление 5-4-3-2-1*\n5 вещей которые видишь → 4 которые можешь потрогать → 3 звука которые слышишь → 2 запаха → 1 вкус.",
    "🦋 *Объятие бабочки*\nСкрести руки на груди, обними себя. Поочерёдно мягко постукивай по плечам — левое, правое, медленно. 1-2 минуты.",
    "👁 *Панорамное зрение*\nСмотри прямо перед собой и медленно расширяй взгляд максимально широко по бокам, не двигая глазами. Удержи 30-60 секунд. Это сигнал безопасности для нервной системы.",
    "🫀 *Долгий выдох*\nВдох 4 сек, выдох 8 сек. Длинный выдох активирует парасимпатику. Повтори 5 раз.",
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

    system_prompt = f"""Ты личный ассистент. Анализируй сообщение и классифицируй каждый пункт отдельно.

Сегодня: {today_str}, {weekday_ru}. Пользователь во Вьетнаме (UTC+7).

Верни ТОЛЬКО валидный JSON:
{{
  "items": [
    {{
      "type": "plan|routine|someday|reflection|question",
      "text": "текст одного пункта",
      "date": "YYYY-MM-DD или null",
      "time": "HH:MM или null"
    }}
  ],
  "response": "короткий ответ на русском — подтверждение или ответ на вопрос"
}}

Типы:
- plan: конкретное дело с датой (сегодня/завтра/в пятницу/числа месяца)
- routine: ежедневная привычка которую надо делать каждый день
- someday: мечта, идея без даты, «хочу когда-нибудь»
- reflection: наблюдения о себе, чувства, мысли о жизни
- question: вопрос или разговор (не сохраняй, только ответь в response)

Если в сообщении несколько дел — каждое отдельным элементом в items.
Для plan: вычисли точную дату если сказано «завтра», «в пятницу» и т.д.

ВАЖНО: поле text — это улучшенная версия записи. Исправь орфографию, пунктуацию, сделай формулировку чёткой и красивой. Сохраняй смысл, но убирай черновой стиль."""

    response = await groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
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

    icons = {"plan": "📅", "routine": "🔄", "someday": "🌙", "reflection": "💭", "inbox": "📥"}
    conn = db()
    saved = []
    for item in items:
        msg_type = item.get("type", "inbox")
        if msg_type == "question":
            continue
        save_text = item.get("text", text)
        item_date = item.get("date")
        item_time = item.get("time")
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
        "Просто пиши или записывай голосовые — я сама разберусь куда положить.\n\n"
        "/routines — ежедневные рутины\n"
        "/plans — ближайшие планы\n"
        "/someday — список «когда-нибудь»\n"
        "/inbox — необработанные\n"
        "/reflections — дневник рефлексий\n"
        "/month — план месяца картинкой\n"
        "/help — как пользоваться"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Просто пиши в свободной форме:\n\n"
        "📅 *Планы:* «встреча в пятницу в 15:00», «сдать отчёт 30-го»\n"
        "🔄 *Рутины:* «каждый день пить воду», «медитация утром»\n"
        "🌙 *Когда-нибудь:* «хочу поехать в Японию»\n"
        "💭 *Рефлексия:* «сегодня поняла что...», «чувствую тревогу»\n"
        "💬 *Вопрос:* любой вопрос — просто отвечу\n\n"
        "Голосовые тоже принимаю 🎤\n\n"
        "Каждый вечер в 22:00 пришлю план на завтра.\n"
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


@dp.message(Command("plans"))
async def plans_cmd(message: Message):
    today = datetime.now(VN_TZ).strftime('%Y-%m-%d')
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='plan' AND status='active' "
        "AND (date >= ? OR date IS NULL) ORDER BY date, time LIMIT 20",
        (message.chat.id, today)
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("Планов пока нет.\n\nНапиши например: «встреча в пятницу в 15:00»")
        return

    for r in rows:
        parts = []
        if r['date']:
            parts.append(f"📆 {r['date']}")
        if r['time']:
            parts.append(f"🕐 {r['time']}")
        header = "  ".join(parts)
        text = f"{header}\n{r['text']}" if header else r['text']
        await message.answer(f"📅 {text}", reply_markup=plan_keyboard(r['id']))


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
        end = start + timedelta(days=6)
        title = f"Неделя  {start.strftime('%d')}–{end.strftime('%d %B %Y')}"
    else:
        start = date(today.year, today.month, 1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end = date(today.year, today.month, last_day)
        title = f"{MONTH_NAMES[today.month - 1]}  {today.year}"

    conn = db()
    rows = conn.execute(
        "SELECT text, date, time FROM items WHERE chat_id=? AND type='plan' AND status='active' "
        "AND date >= ? AND date <= ? ORDER BY date, time",
        (chat_id, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
    ).fetchall()
    conn.close()

    tasks_by_date: dict[str, list[str]] = {}
    for r in rows:
        d = r['date']
        label = f"{r['time']}  {r['text']}" if r['time'] else r['text']
        tasks_by_date.setdefault(d, []).append(label)

    all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    LINE_H = 30
    HEADER_H = 48
    TITLE_H = 70
    PAD = 30
    WIDTH = 900

    total_h = TITLE_H + PAD
    for d in all_days:
        tasks = tasks_by_date.get(d.strftime('%Y-%m-%d'), [])
        total_h += HEADER_H + max(len(tasks), 1) * LINE_H + 14
    total_h += PAD

    HEIGHT = max(500, total_h)

    fig = plt.figure(figsize=(WIDTH / 100, HEIGHT / 100), dpi=100)
    fig.patch.set_facecolor('#0d1117')
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(0, HEIGHT)
    ax.invert_yaxis()
    ax.axis('off')

    y = PAD
    ax.text(WIDTH / 2, y, title, ha='center', va='top',
            fontsize=22, color='#c9d1d9', fontweight='bold')
    y += TITLE_H - 10
    ax.plot([40, WIDTH - 40], [y, y], color='#30363d', linewidth=1)
    y += 18

    for d in all_days:
        date_str = d.strftime('%Y-%m-%d')
        wd = d.weekday()
        is_today = (d == today)
        is_weekend = wd >= 5

        if is_today:
            rect = mpatches.FancyBboxPatch(
                [30, y - 4], WIDTH - 60, HEADER_H,
                boxstyle="round,pad=3", facecolor='#1c2b4a', edgecolor='#2d4a7a', linewidth=1
            )
            ax.add_patch(rect)

        day_color = '#4f9eff' if is_today else ('#ff7b7b' if is_weekend else '#e6edf3')
        prefix = '▶  ' if is_today else ''
        ax.text(55, y + 8, f"{prefix}{DAY_NAMES_FULL[wd]},  {d.strftime('%d %B')}",
                ha='left', va='top', fontsize=15, color=day_color, fontweight='bold')
        y += HEADER_H

        tasks = tasks_by_date.get(date_str, [])
        if not tasks:
            ax.text(75, y, 'нет дел', ha='left', va='top',
                    fontsize=12, color='#3d444d', style='italic')
            y += LINE_H
        else:
            for task in tasks:
                ax.text(68, y, '•', ha='left', va='top', fontsize=14, color='#4f9eff')
                ax.text(85, y, task, ha='left', va='top', fontsize=12, color='#adbac7')
                y += LINE_H

        y += 6
        ax.plot([40, WIDTH - 40], [y, y], color='#21262d', linewidth=0.5)
        y += 8

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0d1117')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


@dp.message(Command("plans"))
async def plans_cmd(message: Message):
    args = message.text.replace('/plans', '').strip().lower()
    mode = 'month' if any(w in args for w in ('month', 'месяц', 'мес')) else 'week'
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        img_bytes = await generate_plan_image(message.chat.id, mode)
        caption = "📅 План на месяц" if mode == 'month' else "📅 План на неделю"
        await message.answer_photo(BufferedInputFile(img_bytes, filename="plan.png"), caption=caption)
    except Exception as e:
        logging.error(f"Plan image error: {e}")
        await message.answer("❌ Не смогла сгенерировать план.")


@dp.message(Command("month"))
async def month_cmd(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        img_bytes = await generate_plan_image(message.chat.id, 'month')
        now_vn = datetime.now(VN_TZ)
        await message.answer_photo(
            BufferedInputFile(img_bytes, filename="month.png"),
            caption=f"📅 {MONTH_NAMES[now_vn.month - 1]} {now_vn.year}"
        )
    except Exception as e:
        logging.error(f"Month image error: {e}")
        await message.answer("❌ Не смогла сгенерировать календарь.")


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
            f"🫀 *Практика для нервной системы:*\n\n{practice}",
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
