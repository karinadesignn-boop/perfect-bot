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

# Conversation history per chat: last 8 turns so AI understands "эту", "ту", "её" etc.
_chat_history: dict[int, list[dict]] = {}
MAX_HISTORY_TURNS = 8



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

async def classify_message(text: str, history: list[dict] | None = None, inbox_cats: list[str] | None = None) -> dict:
    now_vn = datetime.now(VN_TZ)
    today_date = now_vn.date()
    today_str = today_date.strftime('%Y-%m-%d')
    weekdays = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']
    weekday_ru = weekdays[now_vn.weekday()]

    tomorrow = (today_date + timedelta(days=1)).strftime('%Y-%m-%d')

    system_prompt = f"""Органайзер-бот Карины. Сегодня {today_str} ({weekday_ru}), UTC+7.

JSON: {{"items":[{{"type":"...","text":"...","date":"...","time":"...","old_text":"...","day_of_week":null,"category":null}}],"response":"..."}}

ТИПЫ:
plan — новое дело на дату. date=YYYY-MM-DD, time=HH:MM. text дословно.
update — изменить существующее. old_text=ключевые слова, новые date/time.
delete — удалить одно дело. old_text=ключевые слова.
delete_day — очистить весь день. date=YYYY-MM-DD.
show_day — показать план дня. date=YYYY-MM-DD.
show_week — план недели картинкой.
show_week_text — план недели текстом.
show_month — план месяца. date=YYYY-MM-01.
routine — ежедневная привычка. text=текст.
weekly — привычка по дням. day_of_week: 0=пн..6=вс.
inbox — задача без даты. category из: {', '.join(f'"{c}"' for c in (inbox_cats or DEFAULT_INBOX_CATEGORIES))}.
someday — идея/мечта без срока. text=текст.
reminder — духовная фраза. practice — практика. lifehack — лайфхак.
move_to_plan — из инбокса в план. old_text=ключевые слова, date=дата.
add_category — новая категория инбокса. remove_category — удалить категорию (ярлык).
clear_category — удалить ВСЕ элементы внутри категории. text=название категории.
question — только если это вопрос без действия.

ПРИМЕРЫ:
"удали поехать на Бали" → {{"type":"delete","old_text":"поехать на Бали"}}
"убери встречу с врачом из someday" → {{"type":"delete","old_text":"встреча врач"}}
"вычеркни йогу из рутин" → {{"type":"delete","old_text":"йога"}}
"удали все книги из инбокса" → {{"type":"clear_category","text":"книги"}}
"очисти категорию книги" → {{"type":"clear_category","text":"книги"}}
"хочу поехать на Бали" → {{"type":"someday","text":"хочу поехать на Бали"}}
"добавь в список когда-нибудь: купить машину" → {{"type":"someday","text":"купить машину"}}

ПРАВИЛА:
— delete работает для ЛЮБОГО списка: план, someday, inbox, рутины.
— clear_category удаляет содержимое, remove_category удаляет ярлык.
— question ТОЛЬКО если нет никакого действия с данными.
— ТЕКСТ сохранять дословно, ссылки не удалять.
response = короткое подтверждение по-русски."""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history[-6:])
    messages.append({"role": "user", "content": text})

    response = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find('{')
        end = content.rfind('}') + 1
        return json.loads(content[start:end])


def _add_to_history(chat_id: int, user_text: str, bot_reply: str):
    history = _chat_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": bot_reply})
    if len(history) > MAX_HISTORY_TURNS * 2:
        _chat_history[chat_id] = history[-(MAX_HISTORY_TURNS * 2):]


async def _find_item(chat_id: int, keywords: str, hint_date: str | None = None) -> dict | None:
    """Find active item by keywords. hint_date narrows search to that date first."""
    sb = get_sb()
    terms = [w for w in keywords.lower().split() if len(w) > 1]
    if not terms:
        return None

    async def _search(extra_filter=None):
        for pattern in [
            "%" + "%".join(terms[:3]) + "%",
            *[f"%{t}%" for t in terms[:4]]
        ]:
            q = (sb.table('items').select('id, text')
                 .eq('chat_id', chat_id).eq('status', 'active')
                 .ilike('text', pattern).order('created_at', desc=True).limit(1))
            if extra_filter:
                q = extra_filter(q)
            row = await _db(lambda _q=q: _q.execute())
            if row.data:
                return row.data[0]
        return None

    # 1) With date hint (most specific)
    if hint_date:
        found = await _search(lambda q: q.eq('date', hint_date))
        if found:
            return found

    # 2) Without date filter
    return await _search()




async def ai_chat(text: str, history: list[dict] | None = None) -> str:
    messages = [{"role": "system", "content": (
        "Ты личный ассистент Карины. Отвечай по-русски, тепло и кратко. "
        "Отвечай на вопросы, поддерживай в разговоре. "
        "НЕЛЬЗЯ: упоминать несуществующие функции или команды бота, выдумывать термины. "
        "НЕЛЬЗЯ: предлагать составить план, придумывать задачи или рутины. "
        "Если пользователь просит что-то сделать с данными (удалить, добавить, показать) — "
        "скажи коротко что не смогла обработать этот запрос и попроси написать иначе."
    )}]
    if history:
        messages.extend(history[-8:])
    messages.append({"role": "user", "content": text})
    resp = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        max_tokens=600,
    )
    return resp.choices[0].message.content.strip()


import re as _re

# (stem_for_search, stem_for_digit_check, month_number)
_MONTHS = [
    ('январ', 'янв', 1), ('феврал', 'фев', 2), ('март', 'мар', 3), ('апрел', 'апр', 4),
    ('май|мая|маю', 'май', 5), ('июн', 'июн', 6), ('июл', 'июл', 7), ('август', 'авг', 8),
    ('сентябр', 'сен', 9), ('октябр', 'окт', 10), ('ноябр', 'ноя', 11), ('декабр', 'дек', 12),
]

_SAVE_VERBS = ('добав', 'запис', 'занес', 'внес', 'поставь', 'сохран', 'создай',
               'удал', 'убер', 'убри', 'скорректир', 'измени', 'перенес', 'исправ')


def _quick_intent(text: str, today_str: str, tomorrow_str: str) -> dict | None:
    """Fast path for show-only requests. Returns classify-format result or None.
    Only intercepts clear view requests — save/update/delete always go to AI."""
    try:
        t = text.lower().strip()

        if any(v in t for v in _SAVE_VERBS):
            return None

        # show_week
        if _re.search(r'недел[юяеи]|на этой неделе|план недел', t):
            if 'текст' in t or 'список' in t:
                return {"items": [{"type": "show_week_text"}], "response": "Вот план недели:"}
            return {"items": [{"type": "show_week"}], "response": "Вот план недели:"}

        # show_month — month name without preceding digit
        now_vn = datetime.now(VN_TZ)
        for search_pat, digit_check, num in _MONTHS:
            if _re.search(search_pat, t):
                if _re.search(r'\d\s*' + digit_check, t):
                    return None
                yr = now_vn.year
                return {"items": [{"type": "show_month", "date": f"{yr}-{num:02d}-01"}],
                        "response": "Вот план месяца:"}

        # clear_category — "удали/очисти/убери все/всё/содержимое [из] [категории] X"
        clear_triggers = ('содержимое', 'всё из', 'все из', 'всё в ', 'все в ',
                          'очист', 'убери все', 'удали все', 'удали всё')
        if any(tr in t for tr in clear_triggers):
            return {"items": [{"type": "clear_category", "text": "__parse__"}],
                    "response": ""}

        # show someday list
        if _re.search(r'someday|когда.?нибудь|список мечт|мои мечты|мои идеи', t):
            if not any(v in t for v in ('добав', 'запис', 'удал', 'убер')):
                return {"items": [{"type": "show_someday"}], "response": ""}

        # show_day
        show_words = ('покажи', 'что у меня', 'что запланировано', 'мой план', 'расписани', 'план на день')
        has_show = any(w in t for w in show_words) or t.startswith('план')
        has_today = 'сегодня' in t
        has_tomorrow = 'завтра' in t

        if has_show and has_today:
            return {"items": [{"type": "show_day", "date": today_str}], "response": "Вот план на сегодня:"}
        if has_show and has_tomorrow:
            return {"items": [{"type": "show_day", "date": tomorrow_str}], "response": "Вот план на завтра:"}
        if _re.fullmatch(r'(план\s*)?(на\s*)?(сегодня|today)\??', t):
            return {"items": [{"type": "show_day", "date": today_str}], "response": "Вот план на сегодня:"}
        if _re.fullmatch(r'(план\s*)?(на\s*)?(завтра|tomorrow)\??', t):
            return {"items": [{"type": "show_day", "date": tomorrow_str}], "response": "Вот план на завтра:"}

        return None
    except Exception as e:
        logging.warning(f"_quick_intent error (falling back to AI): {e}")
        return None


async def process_and_save(chat_id: int, text: str, message: Message):
    if not groq_client:
        await message.answer("⚠️ ИИ не настроен. Добавь GROQ_API_KEY.")
        return

    await bot.send_chat_action(chat_id, ChatAction.TYPING)

    history = _chat_history.get(chat_id, [])

    now_vn = datetime.now(VN_TZ)
    today_str = now_vn.date().strftime('%Y-%m-%d')
    tomorrow_str = (now_vn.date() + timedelta(days=1)).strftime('%Y-%m-%d')
    inbox_cats = await get_inbox_categories(chat_id)
    quick = _quick_intent(text, today_str, tomorrow_str)
    if quick:
        result = quick
    else:
        try:
            result = await classify_message(text, history, inbox_cats)
        except Exception as e:
            logging.error(f"AI classify error: {e}", exc_info=True)
            await message.answer(f"❌ Ошибка ИИ: {type(e).__name__}: {e}")
            return

    items = result.get("items", [])
    response_text = result.get("response", "Сохранила ✅")

    if not items or all(i.get("type") == "question" for i in items):
        reply = await ai_chat(text, _chat_history.get(chat_id, []))
        await message.answer(reply)
        _add_to_history(chat_id, text, reply)
        return

    for item in items:
        if item.get("type") == "show_someday":
            try:
                sb = get_sb()
                rows = await _db(lambda: sb.table('items')
                    .select('text').eq('chat_id', chat_id).eq('type', 'someday')
                    .eq('status', 'active').order('id', desc=True).limit(50).execute())
                if not rows.data:
                    await message.answer("Список «когда-нибудь» пуст 🌙")
                else:
                    lines = [f"<b>🌙 когда-нибудь ({len(rows.data)})</b>", ""]
                    for r in rows.data:
                        lines.append(f"🌙  {_h(r['text'])}")
                    await message.answer("\n".join(lines), parse_mode="HTML")
                _add_to_history(chat_id, text, "показала список someday")
            except Exception as e:
                await message.answer(f"❌ Ошибка: {e}")
            return

        if item.get("type") == "show_week_text":
            try:
                now_vn = datetime.now(VN_TZ)
                today_vn = now_vn.date()
                monday = today_vn - timedelta(days=today_vn.weekday())
                days_w = [monday + timedelta(days=i) for i in range(7)]
                tasks = await _fetch_tasks(chat_id, days_w[0], days_w[-1])
                sb = get_sb()
                routines_res = await _db(lambda: sb.table('items')
                    .select('text')
                    .eq('chat_id', chat_id)
                    .eq('type', 'routine')
                    .eq('status', 'active')
                    .order('id')
                    .execute())
                day_names = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
                header = f"🔮 *план на неделю · {days_w[0].strftime('%d.%m')} — {days_w[-1].strftime('%d.%m')}*"
                lines = [header]
                if routines_res.data:
                    lines += ["", "🐈‍⬛ *ритуалы каждого дня*"]
                    for r in routines_res.data:
                        lines.append(f"🧿  {r['text']}")
                for d in days_w:
                    ds = d.strftime('%Y-%m-%d')
                    day_tasks = tasks.get(ds, [])
                    is_today = (d == today_vn)
                    marker = " ◀ сегодня" if is_today else ""
                    lines.append(f"\n🌙 *{day_names[d.weekday()]} · {d.strftime('%d.%m')}*{marker}")
                    if day_tasks:
                        for t_item in day_tasks:
                            lines.append(f"❤️‍🔥  {t_item}")
                    else:
                        lines.append("✨ свободно")
                week_text = "\n".join(lines)
                await message.answer(week_text, parse_mode="Markdown")
                _add_to_history(chat_id, text, week_text)
            except Exception as e:
                logging.error(f"show_week_text error: {e}")
                await message.answer(f"❌ Ошибка: {e}")
            return

        if item.get("type") == "show_week":
            if not HAS_MPL:
                await message.answer("❌ matplotlib не установлен на сервере.")
                return
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
                img_bytes = await generate_plan_image(chat_id, 'week')
                caption = "📅 План на неделю"
                await message.answer_photo(BufferedInputFile(img_bytes, filename="week.png"), caption=caption)
                _add_to_history(chat_id, text, caption)
            except Exception as e:
                logging.error(f"show_week error: {e}")
                await message.answer(f"❌ Ошибка: {e}")
            return

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
                    .select('text, time')
                    .eq('chat_id', chat_id)
                    .eq('type', 'routine')
                    .eq('status', 'active')
                    .order('id')
                    .execute())
                day_names = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
                today_vn = datetime.now(VN_TZ).date()
                diff = (target - today_vn).days
                prefix = "сегодня — " if diff == 0 else ("завтра — " if diff == 1 else "")
                lines = [f"🔮 *{prefix}{day_names[target.weekday()]}, {target.strftime('%d.%m.%Y')}*", ""]
                # filter: daily (time is null/empty) OR weekly matching this weekday
                target_dow = str(target.weekday())
                routines = [r for r in routines_res.data
                            if not (r.get('time') or '').startswith('dow:')
                            or r.get('time') == f'dow:{target_dow}']
                if routines:
                    lines.append("🐈‍⬛ *ритуалы дня*")
                    for r in routines:
                        lines.append(f"🧿  {r['text']}")
                    lines.append("")
                if plans_res.data:
                    lines.append("🌙 *планы*")
                    for p in plans_res.data:
                        t = f"`{p['time']}`  " if p['time'] else ""
                        lines.append(f"❤️‍🔥  {t}{p['text']}")
                elif not routines:
                    lines.append("✨ день пока чистый — всё возможно")
                day_text = "\n".join(lines)
                await message.answer(day_text, parse_mode="Markdown")
                _add_to_history(chat_id, text, day_text)
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
            _add_to_history(chat_id, text, caption)
            return

    icons = {"plan": "📅", "routine": "🔄", "someday": "🌙", "inbox": "📥", "update": "✏️"}
    sb = get_sb()
    saved = []

    for item in items:
        msg_type = item.get("type", "inbox")
        if msg_type == "question":
            continue

        save_text = item.get("text", text)
        item_date = item.get("date")
        item_time = item.get("time")

        if msg_type == "add_category":
            cat_name = save_text.strip()
            await _db(lambda c=cat_name: sb.table('items').insert({
                'chat_id': chat_id, 'text': c, 'type': 'inbox_category',
                'status': 'active', 'created_at': datetime.now().isoformat()
            }).execute())
            saved.append(f"✅ Категория добавлена: «{cat_name}»")

        elif msg_type == "move_to_plan":
            old_text = item.get("old_text", save_text)
            found = await _find_item(chat_id, old_text)
            if not found:
                # search specifically in inbox
                terms = [w for w in old_text.lower().split() if len(w) > 2]
                for term in terms[:3]:
                    row = await _db(lambda t=term: sb.table('items').select('id, text')
                        .eq('chat_id', chat_id).eq('type', 'inbox').eq('status', 'active')
                        .ilike('text', f'%{t}%').limit(1).execute())
                    if row.data:
                        found = row.data[0]
                        break
            if found:
                move_text = found['text']
                move_date = item_date or (datetime.now(VN_TZ).date().strftime('%Y-%m-%d'))
                await _db(lambda st=move_text, d=move_date, t=item_time: sb.table('items').insert({
                    'chat_id': chat_id, 'text': st, 'type': 'plan',
                    'date': d, 'time': t, 'status': 'active',
                    'created_at': datetime.now().isoformat()
                }).execute())
                fid = found['id']
                await _db(lambda f=fid: sb.table('items').delete().eq('id', f).execute())
                saved.append(f"📅 Перенесла в план на {move_date}: «{move_text}»")
            else:
                saved.append(f"❓ Не нашла в инбоксе: «{old_text}»")

        elif msg_type == "clear_category":
            raw = (item.get("text") or item.get("old_text") or "")
            if raw == "__parse__" or len(raw) > 30:
                raw = text
            # Find best matching category from user's actual categories
            cat_name = None
            raw_low = raw.lower()
            for c in inbox_cats:
                if c.lower() in raw_low or raw_low in c.lower():
                    cat_name = c
                    break
            if not cat_name:
                # Use first word that's long enough as fallback keyword
                words = [w for w in raw_low.split() if len(w) > 3]
                cat_name = words[0] if words else raw.strip()

            # Search with wildcard so "книги" matches "Книги", "книги / чтение", etc.
            rows_clr = await _db(lambda c=cat_name: sb.table('items')
                .select('id, time')
                .eq('chat_id', chat_id)
                .eq('type', 'inbox')
                .eq('status', 'active')
                .ilike('time', f'%{c}%')
                .execute())

            # Fallback: if nothing found with category, check items with null/empty time
            if not rows_clr.data:
                rows_all = await _db(lambda: sb.table('items')
                    .select('id, time, text')
                    .eq('chat_id', chat_id)
                    .eq('type', 'inbox')
                    .eq('status', 'active')
                    .execute())
                unique_times = list({r.get('time') for r in rows_all.data if rows_all.data})
                saved.append(
                    f"❓ Не нашла элементы категории «{cat_name}». "
                    f"Категории в базе: {unique_times}. "
                    f"Скажи точно: «очисти категорию [название]»"
                )
            else:
                for r in rows_clr.data:
                    rid = r['id']
                    await _db(lambda f=rid: sb.table('items').delete().eq('id', f).execute())
                saved.append(f"🗑 Удалила {len(rows_clr.data)} эл. из «{cat_name}»")

        elif msg_type == "remove_category":
            old_text = item.get("old_text", save_text)
            found = await _find_item(chat_id, old_text)
            if found and found.get('type') == 'inbox_category':
                fid = found['id']
                await _db(lambda f=fid: sb.table('items').delete().eq('id', f).execute())
                saved.append(f"🗑 Категория удалена: «{found['text']}»")
            else:
                # try direct text match among inbox_category
                rows_cat = await _db(lambda ot=old_text: sb.table('items')
                    .select('id, text')
                    .eq('chat_id', chat_id)
                    .eq('type', 'inbox_category')
                    .ilike('text', f'%{ot}%')
                    .limit(1)
                    .execute())
                if rows_cat.data:
                    fid = rows_cat.data[0]['id']
                    await _db(lambda f=fid: sb.table('items').delete().eq('id', f).execute())
                    saved.append(f"🗑 Категория удалена: «{rows_cat.data[0]['text']}»")
                else:
                    saved.append(f"❓ Категория «{old_text}» не найдена")

        elif msg_type == "reminder":
            await _db(lambda st=save_text: sb.table('items').insert({
                'chat_id': chat_id, 'text': st, 'type': 'reminder',
                'date': None, 'time': None, 'status': 'active',
                'created_at': datetime.now().isoformat()
            }).execute())
            saved.append(f"🔮 В хранилище: «{save_text}»")

        elif msg_type == "practice":
            await _db(lambda st=save_text: sb.table('items').insert({
                'chat_id': chat_id, 'text': st, 'type': 'practice',
                'date': None, 'time': None, 'status': 'active',
                'created_at': datetime.now().isoformat()
            }).execute())
            saved.append(f"🌿 Практика сохранена: «{save_text}»")

        elif msg_type == "lifehack":
            await _db(lambda st=save_text: sb.table('items').insert({
                'chat_id': chat_id, 'text': st, 'type': 'lifehack',
                'date': None, 'time': None, 'status': 'active',
                'created_at': datetime.now().isoformat()
            }).execute())
            saved.append(f"💡 Лайфхак сохранён: «{save_text}»")

        elif msg_type == "weekly":
            dow = item.get("day_of_week", 6)
            await _db(lambda st=save_text, d=dow:
                sb.table('items').insert({
                    'chat_id': chat_id, 'text': st, 'type': 'routine',
                    'date': None, 'time': f'dow:{d}', 'status': 'active',
                    'created_at': datetime.now().isoformat()
                }).execute())
            day_names_short = ['пн','вт','ср','чт','пт','сб','вс']
            saved.append(f"🔄 Каждое {day_names_short[int(dow)]}: «{save_text}»")

        elif msg_type == "delete_day":
            target_date = item.get("date", item_date)
            if target_date:
                rows_del = await _db(lambda d=target_date: sb.table('items')
                    .select('id, text')
                    .eq('chat_id', chat_id)
                    .eq('type', 'plan')
                    .eq('status', 'active')
                    .eq('date', d)
                    .execute())
                if rows_del.data:
                    for r in rows_del.data:
                        rid = r['id']
                        await _db(lambda f=rid: sb.table('items').delete().eq('id', f).execute())
                    saved.append(f"🗑 Удалила {len(rows_del.data)} пл. на {target_date}")
                else:
                    saved.append(f"✨ На {target_date} и так ничего не было")
            else:
                saved.append("❓ Не поняла какой день очистить")

        elif msg_type == "delete":
            old_text = item.get("old_text", "")
            found = await _find_item(chat_id, old_text)
            if found:
                fid = found['id']
                await _db(lambda f=fid: sb.table('items').delete().eq('id', f).execute())
                saved.append(f"🗑 Удалила: «{found['text']}»")
            else:
                saved.append(f"❓ Не нашла «{old_text}» — возможно уже удалено")
        elif msg_type == "update":
            old_text = item.get("old_text", "")
            # Use current date as hint when AI says "завтра событие" — item is on that date
            hint = item.get("date") or item_date
            found = await _find_item(chat_id, old_text, hint_date=hint)
            if found:
                update_data = {}
                if item_date:
                    update_data['date'] = item_date
                if item_time:
                    update_data['time'] = item_time
                if save_text and save_text != old_text:
                    update_data['text'] = save_text
                fid = found['id']
                await _db(lambda f=fid, ud=update_data:
                    sb.table('items').update(ud).eq('id', f).execute())
                saved.append(f"✏️ Обновила: «{found['text']}»")
            else:
                saved.append(f"❓ Не нашла «{old_text}» для изменения")
        else:
            category = item.get("category") if msg_type == "inbox" else None
            # Normalize category to exact stored name (case-insensitive match)
            if category and inbox_cats:
                low = category.lower()
                for c in inbox_cats:
                    if c.lower() == low or low in c.lower() or c.lower() in low:
                        category = c
                        break
            store_time = category if category else item_time

            # Duplicate check: same type + text starts with same ~40 chars
            dup_prefix = save_text[:40].strip()
            dup_q = (sb.table('items').select('id').eq('chat_id', chat_id)
                     .eq('type', msg_type).eq('status', 'active')
                     .ilike('text', f'{dup_prefix}%'))
            if item_date:
                dup_q = dup_q.eq('date', item_date)
            if category:
                dup_q = dup_q.ilike('time', f'%{category}%')
            dup = await _db(lambda q=dup_q: q.limit(1).execute())
            if dup.data:
                saved.append(f"↩️ уже есть: «{save_text[:50]}»")
                continue

            await _db(lambda st=save_text, mt=msg_type, d=item_date, t=store_time:
                sb.table('items').insert({
                    'chat_id': chat_id, 'text': st, 'type': mt,
                    'date': d, 'time': t, 'status': 'active',
                    'created_at': datetime.now().isoformat()
                }).execute())
            cat_note = f" → «{category}»" if category else ""
            saved.append(f"{icons.get(msg_type, '📥')}{cat_note}")

    if not saved:
        await message.answer(response_text)
        _add_to_history(chat_id, text, response_text)
        return

    detail = "\n".join(saved)
    final_reply = f"{detail}\n\n{response_text}" if response_text else detail
    await message.answer(final_reply)
    _add_to_history(chat_id, text, final_reply)


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
        "📥 /inbox — необработанные записи\n"
        "🔮 /reminders — послания и фразы\n"
        "🌿 /practices — телесные практики\n"
        "💡 /lifehacks — лайфхаки\n"
        "/help — как пользоваться"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Пиши мне в свободной форме:\n\n"
        "📅 *Планы:* «встреча в пятницу в 15:00», «сдать отчёт 30-го»\n"
        "🔄 *Рутины:* «каждый день медитация», «пить воду утром»\n"
        "🌙 *Когда-нибудь:* «хочу поехать в Японию»\n"
        "💬 *Вопрос:* любой вопрос — просто отвечу\n\n"
        "Голосовые тоже принимаю 🎤\n\n"
        "Каждый вечер в 22:00 — план на завтра.",
        parse_mode="Markdown"
    )


@dp.message(Command("routines"))
async def routines_cmd(message: Message):
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('*')
        .eq('chat_id', message.chat.id)
        .eq('type', 'routine')
        .eq('status', 'active')
        .order('id')
        .execute())

    if not rows.data:
        await message.answer("Рутин пока нет 🌙\n\nНапиши например: «каждый день медитация»")
        return

    day_names_full = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
    daily, weekly = [], {}
    for r in rows.data:
        t = r.get('time') or ''
        if t.startswith('dow:'):
            dow = int(t[4:])
            weekly.setdefault(dow, []).append(r['text'])
        else:
            daily.append(r['text'])

    lines = ["🐈‍⬛ *мои ритуалы*", ""]
    if daily:
        lines.append("🌙 *каждый день*")
        for item in daily:
            lines.append(f"🧿  {item}")
    for dow in sorted(weekly):
        lines.append(f"\n🌙 *каждое {day_names_full[dow].lower()}*")
        for item in weekly[dow]:
            lines.append(f"🧿  {item}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


def _h(s: str) -> str:
    """Escape HTML special chars."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_list(message: Message, header: str, items: list[str], empty_msg: str):
    """Send a list with HTML formatting, splitting if over 4000 chars."""
    if not items:
        await message.answer(empty_msg)
        return
    lines = [f"<b>{_h(header)}</b>", ""] + [_h(t) for t in items]
    text_out = "\n".join(lines)
    for i in range(0, len(text_out), 4000):
        await message.answer(text_out[i:i+4000], parse_mode="HTML")


@dp.message(Command("someday"))
async def someday_cmd(message: Message):
    try:
        sb = get_sb()
        rows = await _db(lambda: sb.table('items')
            .select('text')
            .eq('chat_id', message.chat.id)
            .eq('type', 'someday')
            .eq('status', 'active')
            .order('id', desc=True)
            .limit(50)
            .execute())
        await _send_list(message, "🌙 когда-нибудь",
                         [f"🌙  {r['text']}" for r in rows.data],
                         "Список «когда-нибудь» пуст 🌙\n\nНапиши например: «хочу поехать на Бали»")
    except Exception as e:
        logging.error(f"someday_cmd error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("reminders"))
async def reminders_cmd(message: Message):
    try:
        sb = get_sb()
        rows = await _db(lambda: sb.table('items')
            .select('text')
            .eq('chat_id', message.chat.id)
            .eq('type', 'reminder')
            .eq('status', 'active')
            .order('created_at', desc=True)
            .limit(50)
            .execute())
        await _send_list(message, "🔮 мои послания",
                         [f"✨  {r['text']}" for r in rows.data],
                         "Хранилище посланий пустое 🔮\n\nДобавь: «в хранилище: твоя фраза»")
    except Exception as e:
        logging.error(f"reminders_cmd error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("practices"))
async def practices_cmd(message: Message):
    try:
        sb = get_sb()
        rows = await _db(lambda: sb.table('items')
            .select('text')
            .eq('chat_id', message.chat.id)
            .eq('type', 'practice')
            .eq('status', 'active')
            .order('created_at', desc=True)
            .limit(50)
            .execute())
        await _send_list(message, "🌿 телесные практики",
                         [f"🧿  {r['text']}" for r in rows.data],
                         "Хранилище практик пустое 🌿\n\nДобавь: «добавь практику: описание»")
    except Exception as e:
        logging.error(f"practices_cmd error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("lifehacks"))
async def lifehacks_cmd(message: Message):
    try:
        sb = get_sb()
        rows = await _db(lambda: sb.table('items')
            .select('text')
            .eq('chat_id', message.chat.id)
            .eq('type', 'lifehack')
            .eq('status', 'active')
            .order('created_at', desc=True)
            .limit(100)
            .execute())
        await _send_list(message, "💡 лайфхаки",
                         [f"{i}. {r['text']}" for i, r in enumerate(rows.data, 1)],
                         "Лайфхаков пока нет 💡\n\nДобавь: «лайфхак: текст»")
    except Exception as e:
        logging.error(f"lifehacks_cmd error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


DEFAULT_INBOX_CATEGORIES = ['рефлексия/психология', 'мой ум/обучение', 'здоровье', 'отношения']


async def get_inbox_categories(chat_id: int) -> list[str]:
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('text')
        .eq('chat_id', chat_id)
        .eq('type', 'inbox_category')
        .eq('status', 'active')
        .order('created_at')
        .execute())
    if rows.data:
        return [r['text'] for r in rows.data]
    for cat in DEFAULT_INBOX_CATEGORIES:
        await _db(lambda c=cat: sb.table('items').insert({
            'chat_id': chat_id, 'text': c, 'type': 'inbox_category',
            'status': 'active', 'created_at': datetime.now().isoformat()
        }).execute())
    return DEFAULT_INBOX_CATEGORIES


@dp.message(Command("inbox"))
async def inbox_cmd(message: Message):
    cats = await get_inbox_categories(message.chat.id)
    kb = InlineKeyboardBuilder()
    for i, cat in enumerate(cats):
        kb.button(text=cat, callback_data=f"incat:{i}")  # index, not name — avoids 64-byte limit
    kb.button(text="📋 все", callback_data="incat:__all__")
    kb.adjust(3)
    cat_list = "\n".join(f"🧿 {c}" for c in cats)
    await message.answer(
        f"📥 *Инбокс*\n\n{cat_list}\n\n▼ выбери категорию:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("incat:"))
async def inbox_category_cb(callback: CallbackQuery):
    await callback.answer()  # dismiss spinner immediately
    ref = callback.data[len("incat:"):]
    chat_id = callback.from_user.id
    try:
        sb = get_sb()
        if ref == '__all__':
            cat = None
            label = "все"
            rows = await _db(lambda: sb.table('items')
                .select('text, time')
                .eq('chat_id', chat_id)
                .eq('type', 'inbox')
                .eq('status', 'active')
                .order('created_at', desc=True)
                .limit(100)
                .execute())
        else:
            cats = await get_inbox_categories(chat_id)
            try:
                cat = cats[int(ref)]
            except (ValueError, IndexError):
                await callback.message.answer("❌ Категория не найдена, открой /inbox заново")
                return
            label = cat
            rows = await _db(lambda c=cat: sb.table('items')
                .select('text, time')
                .eq('chat_id', chat_id)
                .eq('type', 'inbox')
                .eq('status', 'active')
                .ilike('time', c)
                .order('created_at', desc=True)
                .limit(100)
                .execute())
        def _he(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        if not rows.data:
            await callback.message.answer(f"В «{_he(label)}» пока пусто ✨", parse_mode="HTML")
        else:
            lines = [f"📥 <b>{_he(label)}</b> ({len(rows.data)})", ""]
            for r in rows.data:
                cat_tag = f"  <i>[{_he(r['time'])}]</i>" if cat is None and r.get('time') else ""
                lines.append(f"❤️‍🔥  {_he(r['text'])}{cat_tag}")
            text_out = "\n".join(lines)
            for i in range(0, len(text_out), 4000):
                await callback.message.answer(text_out[i:i+4000], parse_mode="HTML")
    except Exception as e:
        logging.error(f"inbox_category_cb error: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка: {e}")




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


# ---------- ХРАНИЛИЩЕ НАПОМИНАНИЙ ----------

REMINDER_EMOJIS = ['🔮', '🌙', '✨', '🧿', '❤️‍🔥', '🐈‍⬛']
REMINDER_HOURS = {8, 10, 12, 14, 16, 18, 20}


async def send_random_reminder(chat_id: int):
    sb = get_sb()
    rows = await _db(lambda: sb.table('items')
        .select('text, type')
        .eq('chat_id', chat_id)
        .in_('type', ['reminder', 'practice'])
        .eq('status', 'active')
        .execute())
    if not rows.data:
        return
    item = random.choice(rows.data)
    if item['type'] == 'practice':
        msg = f"🌿 *практика*\n\n{item['text']}"
    else:
        emoji = random.choice(REMINDER_EMOJIS)
        msg = f"{emoji}\n\n_{item['text']}_"
    await bot.send_message(chat_id, msg, parse_mode="Markdown")


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




# ---------- ПЛАНИРОВЩИК ----------

async def scheduler_loop():
    sent_evening: set[str] = set()
    sent_plans: set[str] = set()
    sent_reminders: set[str] = set()

    while True:
        try:
            now_vn = datetime.now(VN_TZ)
            vn_date = now_vn.strftime('%Y-%m-%d')
            vn_hour = now_vn.hour
            vn_hhmm = now_vn.strftime('%H:%M')

            # Random reminder/practice every 2 hours
            reminder_key = f"reminder:{vn_date}:{vn_hour}"
            if vn_hour in REMINDER_HOURS and reminder_key not in sent_reminders:
                sent_reminders.add(reminder_key)
                logging.info(f"Sending random reminder, VN time: {vn_hhmm}")
                for chat_id in await get_all_users():
                    try:
                        await send_random_reminder(chat_id)
                    except Exception as e:
                        logging.error(f"Reminder error {chat_id}: {e}")

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
            if len(sent_plans) > 2000:
                sent_plans.clear()
            if len(sent_reminders) > 500:
                sent_reminders.clear()

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
        {"command": "inbox",       "description": "📥 Необработанные записи"},
        {"command": "reminders",   "description": "🔮 Послания и фразы"},
        {"command": "practices",   "description": "🌿 Телесные практики"},
        {"command": "lifehacks",   "description": "💡 Лайфхаки"},
        {"command": "help",        "description": "❓ Как пользоваться"},
    ])
    asyncio.create_task(scheduler_wrapper())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
