import asyncio
import logging
import os
import json
import calendar
import io
import random
import tempfile
from datetime import datetime, date, timedelta

import pytz
from supabase import create_client, Client

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
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Не заданы SUPABASE_URL или SUPABASE_KEY")

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

_sb: Client = None


def get_sb() -> Client:
    global _sb
    if _sb is None:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


async def _db(func):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func)


async def register_user(chat_id: int):
    sb = get_sb()
    try:
        await _db(lambda: sb.table('users').insert({
            'chat_id': chat_id,
            'created_at': datetime.now().isoformat()
        }).execute())
    except Exception:
        pass


async def get_all_users() -> list[int]:
    sb = get_sb()
    result = await _db(lambda: sb.table('users').select('chat_id').execute())
    return [r['chat_id'] for r in result.data]


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
• update — ЛЮБОЕ изменение существующего пункта: перенести дату, изменить время, переформулировать, исправить. Триггеры: «перенеси», «сдвинь», «измени», «поменяй», «скорректируй», «замени», «исправь», «вместо X», «не X а Y», «X теперь Y»
• delete — УДАЛЕНИЕ существующего пункта. Триггеры: «удали», «убери», «не нужен», «отмени», «убрать», «удалить»
• show_day — ПОКАЗАТЬ план на конкретный день. Это НЕ question! Триггеры: «что на завтра», «план на пятницу», «покажи среду», «что у меня в среду», «картинка на сегодня», «что запланировано на 5 июля», «покажи мне день», «что на этой неделе в четверг». Поле date = дата этого дня в формате YYYY-MM-DD
• show_month — просьба показать план/календарь на конкретный месяц («план на июль», «следующий месяц», «покажи август», «картинка на июнь»). Поле date = первый день этого месяца в формате YYYY-MM-01

Для типа update — заполняй поля так:
• old_text = ключевые слова из СТАРОГО пункта (по ним ищем в базе, 1-3 слова)
• text = НОВЫЙ текст пункта. Если меняется только дата/время — оставь text таким же как old_text (не переформулируй). Если меняется формулировка — напиши новую грамотно.
• date = НОВАЯ дата если упоминается, иначе пусто
• time = НОВОЕ время если упоминается, иначе пусто

Примеры update:
• «перенеси психолога на 10 июля» → old_text="психолог", text="Записаться на сеанс у психолога", date="2026-07-10"
• «сдвинь УЗИ на среду» → old_text="УЗИ", text="Записаться на УЗИ голеностопа", date="2026-06-24"
• «встреча с мужем не в 15 а в 17» → old_text="встреча муж", text="Встреча с мужем", time="17:00"
• «замени мастер-класс по глине на урок рисования» → old_text="мастер-класс глина", text="Урок рисования"

Для типа delete:
• old_text = ключевые слова из пункта который нужно удалить (1-3 слова)

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

    if items and all(i.get("type") == "question" for i in items):
        await message.answer(response_text)
        return

    for item in items:
        if item.get("type") == "show_day":
            date_str = item.get("date", "")
            try:
                target = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                target = datetime.now(VN_TZ).date()
            try:
                sb = get_sb()
                _ds = target.strftime('%Y-%m-%d')
                plans_res = await _db(lambda: sb.table('items')
                    .select('text, time')
                    .eq('chat_id', chat_id)
                    .eq('type', 'plan')
                    .eq('status', 'active')
                    .eq('date', _ds)
                    .order('time')
                    .execute())
                routines_res = await _db(lambda: sb.table('items')
                    .select('text')
                    .eq('chat_id', chat_id)
                    .eq('type', 'routine')
                    .eq('status', 'active')
                    .order('id')
                    .execute())
                day_names = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
                today_vn = datetime.now(VN_TZ).date()
                diff = (target - today_vn).days
                prefix = "Сегодня — " if diff == 0 else ("Завтра — " if diff == 1 else "")
                lines = [f"📅 *{prefix}{day_names[target.weekday()]}, {target.strftime('%d.%m.%Y')}*", ""]
                if routines_res.data:
                    lines.append("🔄 *Рутины:*")
                    for r in routines_res.data:
                        lines.append(f"  ⬜️ {r['text']}")
                    lines.append("")
                if plans_res.data:
                    lines.append("📌 *Планы:*")
                    for p in plans_res.data:
                        t = f"`{p['time']}`  " if p['time'] else ""
                        lines.append(f"  • {t}{p['text']}")
                elif not routines_res.data:
                    lines.append("Ничего не запланировано ✨")
                await message.answer("\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                logging.error(f"show_day error: {e}")
                await message.answer(f"❌ Ошибка: {e}")
            return

        if item.get("type") == "show_month":
            if not HAS_MPL:
                await message.answer("❌ matplotlib не установлен на сервере.")
                return
            date_str = item.get("date", "")
            try:
                target = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                target = datetime.now(VN_TZ).date().replace(day=1)
            target = target.replace(day=1)
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
            last_day = calendar.monthrange(target.year, target.month)[1]
            days = [target + timedelta(days=i) for i in range(last_day)]
            today = datetime.now(VN_TZ).date()
            tasks = await _fetch_tasks(chat_id, days[0], days[-1])
            img_bytes = _draw_month(tasks, days, today)
            caption = f"🗓 {MONTH_NAMES[target.month - 1]} {target.year}"
            await message.answer_photo(BufferedInputFile(img_bytes, filename="month.png"), caption=caption)
            return

    icons = {"plan": "📅", "routine": "🔄", "someday": "🌙", "reflection": "💭", "inbox": "📥", "update": "✏️"}
    sb = get_sb()
    saved = []

    for item in items:
        msg_type = item.get("type", "inbox")
        if msg_type == "question":
            continue

        save_text = item.get("text", text)
        item_date = item.get("date")
        item_time = item.get("time")

        if msg_type == "delete":
            old_text = item.get("old_text", "")
            search_terms = [w for w in old_text.lower().split() if len(w) > 2]
            if search_terms:
                like_pattern = "%" + "%".join(search_terms[:3]) + "%"
                row = await _db(lambda p=like_pattern: sb.table('items')
                    .select('id')
                    .eq('chat_id', chat_id)
                    .eq('status', 'active')
                    .ilike('text', p)
                    .order('created_at', desc=True)
                    .limit(1)
                    .execute())
                if row.data:
                    fid = row.data[0]['id']
                    await _db(lambda f=fid: sb.table('items').delete().eq('id', f).execute())
                    saved.append("🗑")
        elif msg_type == "update":
            old_text = item.get("old_text", "")
            search_terms = [w for w in old_text.lower().split() if len(w) > 2]
            found_id = None
            if search_terms:
                like_pattern = "%" + "%".join(search_terms[:3]) + "%"
                row = await _db(lambda p=like_pattern: sb.table('items')
                    .select('id')
                    .eq('chat_id', chat_id)
                    .eq('status', 'active')
                    .ilike('text', p)
                    .order('created_at', desc=True)
                    .limit(1)
                    .execute())
                if row.data:
                    found_id = row.data[0]['id']
            if found_id:
                update_data = {}
                if save_text:
                    update_data['text'] = save_text
                if item_date is not None:
                    update_data['date'] = item_date
                if item_time is not None:
                    update_data['time'] = item_time
                await _db(lambda fid=found_id, ud=update_data:
                    sb.table('items').update(ud).eq('id', fid).execute())
                saved.append("✏️")
            else:
                await _db(lambda st=save_text, d=item_date, t=item_time:
                    sb.table('items').insert({
                        'chat_id': chat_id, 'text': st, 'type': 'plan',
                        'date': d, 'time': t, 'status': 'active',
                        'created_at': datetime.now().isoformat()
                    }).execute())
                saved.append("📅")
        else:
            await _db(lambda st=save_text, mt=msg_type, d=item_date, t=item_time:
                sb.table('items').insert({
                    'chat_id': chat_id, 'text': st, 'type': mt,
                    'date': d, 'time': t, 'status': 'active',
                    'created_at': datetime.now().isoformat()
                }).execute())
            saved.append(icons.get(msg_type, "📥"))

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
    await register_user(message.chat.id)
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
    await register_user(message.chat.id)
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
    sb = get_sb()

    rows = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', message.chat.id)
        .eq('type', 'routine')
        .eq('status', 'active')
        .order('id')
        .execute())

    if not rows.data:
        await message.answer("Рутин пока нет.\n\nНапиши например: «каждый день медитация 10 минут»")
        return

    done_rows = await _db(lambda: sb.table('routine_log')
        .select('item_id')
        .eq('chat_id', message.chat.id)
        .eq('date', today)
        .execute())
    done_ids = {r['item_id'] for r in done_rows.data}

    for r in rows.data:
        icon = "✅" if r['id'] in done_ids else "⬜️"
        kb = None if r['id'] in done_ids else routine_keyboard(r['id'], today)
        await message.answer(f"{icon} {r['text']}", reply_markup=kb)


@dp.message(Command("someday"))
async def someday_cmd(message: Message):
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', message.chat.id)
        .eq('type', 'someday')
        .eq('status', 'active')
        .order('id', desc=True)
        .limit(20)
        .execute())

    if not rows.data:
        await message.answer("Список «когда-нибудь» пуст.\n\nНапиши например: «хочу поехать на Бали»")
        return

    for r in rows.data:
        await message.answer(f"🌙 {r['text']}", reply_markup=simple_keyboard(r['id']))


@dp.message(Command("inbox"))
async def inbox_cmd(message: Message):
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', message.chat.id)
        .eq('type', 'inbox')
        .eq('status', 'active')
        .order('id', desc=True)
        .limit(10)
        .execute())

    if not rows.data:
        await message.answer("Инбокс пуст 🎉")
        return

    for r in rows.data:
        await message.answer(f"📥 {r['text']}", reply_markup=simple_keyboard(r['id']))


@dp.message(Command("reflections"))
async def reflections_cmd(message: Message):
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', message.chat.id)
        .eq('type', 'reflection')
        .eq('status', 'active')
        .order('created_at', desc=True)
        .limit(10)
        .execute())

    if not rows.data:
        await message.answer("Дневник пуст.\n\nПоделись наблюдением о себе — напиши или запиши голосовое 🎤")
        return

    await message.answer("*Последние записи:*", parse_mode="Markdown")
    for r in rows.data:
        dt = datetime.fromisoformat(r['created_at'])
        dt_vn = pytz.utc.localize(dt).astimezone(VN_TZ) if dt.tzinfo is None else dt.astimezone(VN_TZ)
        label = dt_vn.strftime('%d.%m %H:%M')
        await message.answer(f"💭 _{label}_\n{r['text']}", parse_mode="Markdown")


# ---------- КАРТИНКИ-ПЛАННЕРЫ ----------

MONTH_NAMES = ['Январь','Февраль','Март','Апрель','Май','Июнь',
               'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь']


async def generate_plan_image(chat_id: int, mode: str = 'week') -> bytes:
    now_vn = datetime.now(VN_TZ)
    today = now_vn.date()

    if mode == 'week':
        start = today - timedelta(days=today.weekday())
        days = [start + timedelta(days=i) for i in range(7)]
        tasks = await _fetch_tasks(chat_id, days[0], days[-1])
        return _draw_week(tasks, days, today)
    else:
        start = date(today.year, today.month, 1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        days = [start + timedelta(days=i) for i in range(last_day)]
        tasks = await _fetch_tasks(chat_id, days[0], days[-1])
        return _draw_month(tasks, days, today)


async def _fetch_tasks(chat_id: int, start: date, end: date) -> dict[str, list[str]]:
    sb = get_sb()
    result = await _db(lambda: sb.table('items')
        .select('text, date, time')
        .eq('chat_id', chat_id)
        .eq('type', 'plan')
        .eq('status', 'active')
        .gte('date', start.strftime('%Y-%m-%d'))
        .lte('date', end.strftime('%Y-%m-%d'))
        .order('date')
        .order('time')
        .execute())

    tasks: dict[str, list[str]] = {}
    for r in result.data:
        label = f"{r['time']}  {r['text']}" if r['time'] else r['text']
        tasks.setdefault(r['date'], []).append(label)
    return tasks


# пастельная палитра
BG        = '#fdf8ff'
CARD_BG   = '#ffffff'
WKND_BG   = '#f8f4ff'
HDR_TODAY = '#d8b4fe'
HDR_WKND  = '#fce7f3'
HDR_REG   = '#f0e9ff'
TXT_MAIN  = '#3d2c5e'
TXT_MUTED = '#b0a0c8'
BORDER    = '#ede8f5'

PASTEL_PILLS = [
    ('#e9d5ff', '#6b21a8'),
    ('#fce7f3', '#9d174d'),
    ('#d1fae5', '#065f46'),
    ('#fef3c7', '#92400e'),
    ('#dbeafe', '#1e40af'),
    ('#ffd7d7', '#991b1b'),
    ('#d4f5e9', '#155e3c'),
    ('#ede9fe', '#4c1d95'),
]


async def _draw_day(chat_id: int, target: date, today: date) -> bytes:
    sb = get_sb()
    date_str = target.strftime('%Y-%m-%d')

    plans = await _db(lambda: sb.table('items')
        .select('text, time')
        .eq('chat_id', chat_id)
        .eq('type', 'plan')
        .eq('status', 'active')
        .eq('date', date_str)
        .order('time')
        .execute())

    routines = await _db(lambda: sb.table('items')
        .select('text')
        .eq('chat_id', chat_id)
        .eq('type', 'routine')
        .eq('status', 'active')
        .order('id')
        .execute())

    WIDTH   = 860
    PAD     = 28
    TITLE_H = 64
    DAY_H   = 56
    LINE_H  = 42
    GAP     = 8

    items_to_draw = []
    if routines.data:
        items_to_draw.append(('routine', None, '🔄 Рутины'))
        for r in routines.data:
            items_to_draw.append(('routine_item', None, r['text']))
    if plans.data:
        items_to_draw.append(('section', None, '📅 Планы'))
        for p in plans.data:
            items_to_draw.append(('plan_item', p['time'], p['text']))
    if not items_to_draw:
        items_to_draw.append(('empty', None, 'Ничего не запланировано ✨'))

    total_h = TITLE_H + DAY_H + len([i for i in items_to_draw if i[0].endswith('_item') or i[0] == 'empty']) * LINE_H + PAD
    total_h += sum(GAP for i in items_to_draw if not i[0].endswith('_item') and i[0] != 'empty') * 2

    fig = plt.figure(figsize=(WIDTH / 100, total_h / 100), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(0, total_h)
    ax.invert_yaxis()
    ax.axis('off')

    day_names = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
    is_today = (target == today)
    title = f"{'Сегодня — ' if is_today else ''}{day_names[target.weekday()]}, {target.strftime('%d %B %Y')}"
    ax.text(WIDTH / 2, 16, title, ha='center', va='top',
            fontsize=17, color=TXT_MAIN, fontweight='bold')

    is_weekend = target.weekday() >= 5
    hdr = HDR_TODAY if is_today else (HDR_WKND if is_weekend else HDR_REG)
    ax.add_patch(mpatches.FancyBboxPatch(
        [PAD, 46], WIDTH - PAD * 2, DAY_H,
        boxstyle="round,pad=0", facecolor=hdr, edgecolor=BORDER, linewidth=0.8, zorder=1
    ))
    ax.text(PAD + 18, 46 + DAY_H / 2, title, ha='left', va='center',
            fontsize=14, color=TXT_MAIN, fontweight='bold', zorder=2)

    y = TITLE_H + DAY_H + GAP
    pill_idx = 0
    for kind, time_val, text in items_to_draw:
        if kind in ('routine', 'section'):
            ax.text(PAD + 8, y + 10, text, ha='left', va='top',
                    fontsize=11, color=TXT_MUTED, fontweight='bold')
            y += 28
        elif kind in ('routine_item', 'plan_item'):
            pill_bg, pill_txt = PASTEL_PILLS[pill_idx % len(PASTEL_PILLS)]
            ax.add_patch(mpatches.FancyBboxPatch(
                [PAD + 4, y + 5], WIDTH - (PAD + 4) * 2, LINE_H - 10,
                boxstyle="round,pad=4", facecolor=pill_bg, edgecolor='none'
            ))
            label = f"{time_val}  {text}" if time_val else text
            ax.text(PAD + 22, y + LINE_H / 2, label, ha='left', va='center',
                    fontsize=12, color=pill_txt, fontweight='bold')
            y += LINE_H
            pill_idx += 1
        elif kind == 'empty':
            ax.text(WIDTH / 2, y + LINE_H / 2, text, ha='center', va='center',
                    fontsize=13, color=TXT_MUTED, style='italic')
            y += LINE_H

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _draw_week(tasks: dict, days: list, today: date) -> bytes:
    WIDTH   = 860
    PAD     = 28
    TITLE_H = 54
    DAY_H   = 52
    LINE_H  = 42
    GAP     = 8

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
            boxstyle="round,pad=0", facecolor=hdr, edgecolor=BORDER, linewidth=0.8, zorder=1
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
                _, pill_color = PASTEL_PILLS[j % len(PASTEL_PILLS)]
                ax.add_patch(mpatches.FancyBboxPatch(
                    [PAD + 4, y + 5], WIDTH - (PAD + 4) * 2, LINE_H - 10,
                    boxstyle="round,pad=4", facecolor='none', edgecolor=pill_color, linewidth=1.2
                ))
                ax.text(PAD + 22, y + LINE_H / 2, task, ha='left', va='center',
                        fontsize=12, color=TXT_MAIN)
                y += LINE_H

        y += GAP
        ax.plot([PAD, WIDTH - PAD], [y, y], color=BORDER, linewidth=0.8)
        y += 4

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _draw_month(tasks: dict, days: list, today: date) -> bytes:
    import textwrap

    COLS     = 7
    CELL_W   = 182
    HDR_H    = 34
    LINE_H   = 17
    PILL_GAP = 2
    CELL_PAD = 3
    PAD_TOP  = 64
    COL_HDR  = 26
    WRAP_W   = 20
    WIDTH    = CELL_W * COLS

    first_wd = days[0].weekday()
    n_rows   = ((first_wd + len(days)) + 6) // 7

    # заранее считаем перенесённые строки для каждого дня
    day_lines: dict[str, list[list[str]]] = {}
    for d in days:
        date_str = d.strftime('%Y-%m-%d')
        day_lines[date_str] = [
            textwrap.wrap(t, width=WRAP_W) or [t]
            for t in tasks.get(date_str, [])
        ]

    # высота строки сетки — по самому загруженному дню в строке
    row_heights = []
    for row in range(n_rows):
        max_content = LINE_H
        for col in range(COLS):
            day_idx = row * COLS + col - first_wd
            if 0 <= day_idx < len(days):
                lpt = day_lines[days[day_idx].strftime('%Y-%m-%d')]
                content = sum(len(ls) for ls in lpt) * LINE_H + len(lpt) * PILL_GAP
                max_content = max(max_content, content)
        row_heights.append(HDR_H + CELL_PAD + max_content + CELL_PAD)

    HEIGHT = PAD_TOP + COL_HDR + sum(row_heights) + 20

    fig = plt.figure(figsize=(WIDTH / 100, HEIGHT / 100), dpi=100)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(0, HEIGHT)
    ax.invert_yaxis()
    ax.axis('off')

    m = days[0]
    ax.text(WIDTH / 2, 18, f"{MONTH_NAMES[m.month - 1]}  {m.year}",
            ha='center', va='top', fontsize=20, color=TXT_MAIN, fontweight='bold')

    for i, dn in enumerate(['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС']):
        cx = i * CELL_W + CELL_W / 2
        color = '#c084fc' if i >= 5 else TXT_MUTED
        ax.text(cx, PAD_TOP + 5, dn, ha='center', va='top',
                fontsize=11, color=color, fontweight='bold')

    row_y = PAD_TOP + COL_HDR
    for row in range(n_rows):
        rh = row_heights[row]
        for col in range(COLS):
            day_idx = row * COLS + col - first_wd
            cx = col * CELL_W
            cy = row_y

            in_month = 0 <= day_idx < len(days)
            bg = (WKND_BG if in_month and days[day_idx].weekday() >= 5 else CARD_BG) if in_month else '#f0eaf8'
            ax.add_patch(plt.Rectangle([cx, cy], CELL_W, rh,
                                        facecolor=bg, edgecolor=BORDER, linewidth=0.5))

            if in_month:
                d = days[day_idx]
                date_str = d.strftime('%Y-%m-%d')
                is_today  = (d == today)
                is_weekend = d.weekday() >= 5

                if is_today:
                    ax.add_patch(plt.Circle((cx + 16, cy + 16), 12,
                                             facecolor='#c084fc', edgecolor='none', zorder=2))
                    ax.text(cx + 16, cy + 16, str(d.day), ha='center', va='center',
                            fontsize=10, color='#ffffff', fontweight='bold', zorder=3)
                else:
                    nc = '#c084fc' if is_weekend else TXT_MAIN
                    ax.text(cx + 6, cy + 5, str(d.day), ha='left', va='top',
                            fontsize=10, color=nc, fontweight='bold')

                ty = cy + HDR_H
                for j, lines in enumerate(day_lines[date_str]):
                    pill_h = len(lines) * LINE_H
                    _, pill_color = PASTEL_PILLS[j % len(PASTEL_PILLS)]
                    ax.add_patch(mpatches.FancyBboxPatch(
                        [cx + 3, ty], CELL_W - 6, pill_h,
                        boxstyle="round,pad=1", facecolor='none', edgecolor=pill_color, linewidth=0.8
                    ))
                    for k, line in enumerate(lines):
                        ax.text(cx + CELL_W / 2, ty + k * LINE_H + LINE_H / 2, line,
                                ha='center', va='center', fontsize=7.5,
                                color=TXT_MAIN)
                    ty += pill_h + PILL_GAP

        row_y += rh

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


@dp.message(Command("plan_list"))
async def plan_list_cmd(message: Message):
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', message.chat.id)
        .eq('type', 'plan')
        .eq('status', 'active')
        .order('date')
        .order('time')
        .limit(30)
        .execute())

    if not rows.data:
        await message.answer("Планов нет 🎉")
        return

    await message.answer("📋 Все активные планы (нажми 🗑 чтобы удалить):")
    for r in rows.data:
        date_str = f"📅 {r['date']}" if r['date'] else "📅 без даты"
        time_str = f"  {r['time']}" if r['time'] else ""
        label = f"{date_str}{time_str}\n{r['text']}"
        await message.answer(label, reply_markup=plan_keyboard(r['id']))


@dp.message(Command("plans"))
async def plans_cmd(message: Message):
    if not HAS_MPL:
        await message.answer("❌ matplotlib не установлен на сервере.")
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
        await message.answer("❌ matplotlib не установлен на сервере.")
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
    sb = get_sb()
    try:
        await _db(lambda: sb.table('routine_log').insert({
            'chat_id': callback.message.chat.id,
            'item_id': int(item_id),
            'date': today
        }).execute())
    except Exception:
        pass
    await callback.message.edit_text("✅ " + callback.message.text)
    await callback.answer("Отмечено!")


@dp.callback_query(F.data.startswith("done_plan:"))
async def done_plan(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    sb = get_sb()
    await _db(lambda: sb.table('items').update({'status': 'done'}).eq('id', item_id).execute())
    await callback.message.edit_text("✅ " + callback.message.text)
    await callback.answer("Готово!")


@dp.callback_query(F.data.startswith("delete:"))
async def delete_item(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    sb = get_sb()
    await _db(lambda: sb.table('items').delete().eq('id', item_id).execute())
    await callback.message.edit_text("🗑 " + callback.message.text)
    await callback.answer("Удалено")


# ---------- ГЛАВНЫЙ ОБРАБОТЧИК ----------

@dp.message(F.text)
async def handle_message(message: Message):
    await register_user(message.chat.id)
    await process_and_save(message.chat.id, message.text, message)


# ---------- ВЕЧЕРНИЙ ПЛАН ----------

async def send_evening_plan(chat_id: int):
    now_vn = datetime.now(VN_TZ)
    tomorrow = now_vn.date() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime('%Y-%m-%d')
    tomorrow_display = tomorrow.strftime('%d.%m.%Y')

    sb = get_sb()
    plans_res = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', chat_id)
        .eq('type', 'plan')
        .eq('date', tomorrow_str)
        .eq('status', 'active')
        .order('time')
        .execute())
    routines_res = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', chat_id)
        .eq('type', 'routine')
        .eq('status', 'active')
        .order('id')
        .execute())

    plans = plans_res.data
    routines = routines_res.data

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

    # VN hours (UTC+7): 9, 12, 15, 18
    REFLECTION_HOURS_VN = {9, 12, 15, 18}

    while True:
        try:
            now_vn = datetime.now(VN_TZ)
            vn_date = now_vn.strftime('%Y-%m-%d')
            vn_hour = now_vn.hour
            vn_hhmm = now_vn.strftime('%H:%M')

            # Evening plan at 22:xx VN
            evening_key = f"evening:{vn_date}"
            if vn_hour == 22 and evening_key not in sent_evening:
                sent_evening.add(evening_key)
                logging.info(f"Sending evening plan, VN time: {vn_hhmm}")
                for chat_id in await get_all_users():
                    try:
                        await send_evening_plan(chat_id)
                    except Exception as e:
                        logging.error(f"Evening plan error {chat_id}: {e}")

            # Reflection every 3 hours at 9, 12, 15, 18 VN
            reflection_key = f"reflection:{vn_date}:{vn_hour}"
            if vn_hour in REFLECTION_HOURS_VN and reflection_key not in sent_reflection:
                sent_reflection.add(reflection_key)
                logging.info(f"Sending reflection, VN time: {vn_hhmm}")
                for chat_id in await get_all_users():
                    try:
                        await send_reflection_prompt(chat_id)
                    except Exception as e:
                        logging.error(f"Reflection error {chat_id}: {e}")

            # Per-plan reminders at their scheduled time
            plan_key = f"plan:{vn_date}:{vn_hhmm}"
            if plan_key not in sent_plans:
                sb = get_sb()
                _vn_date = vn_date
                _vn_hhmm = vn_hhmm
                due = await _db(lambda: sb.table('items')
                    .select('*')
                    .eq('type', 'plan')
                    .eq('status', 'active')
                    .eq('date', _vn_date)
                    .eq('time', _vn_hhmm)
                    .execute())
                if due.data:
                    sent_plans.add(plan_key)
                    for p in due.data:
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

        except Exception as e:
            logging.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(60)


async def scheduler_wrapper():
    while True:
        try:
            await scheduler_loop()
        except Exception as e:
            logging.error(f"Scheduler crashed, restarting in 60s: {e}")
            await asyncio.sleep(60)


async def main():
    await bot.set_my_commands([
        {"command": "plans",       "description": "📅 План на неделю картинкой"},
        {"command": "month",       "description": "🗓 План на месяц картинкой"},
        {"command": "plan_list",   "description": "📋 Список планов (удалить/отметить)"},
        {"command": "routines",    "description": "🔄 Ежедневные рутины"},
        {"command": "someday",     "description": "🌙 Список «когда-нибудь»"},
        {"command": "reflections", "description": "💭 Дневник рефлексий"},
        {"command": "inbox",       "description": "📥 Необработанные записи"},
        {"command": "help",        "description": "❓ Как пользоваться"},
    ])
    asyncio.create_task(scheduler_wrapper())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
