import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from groq import AsyncGroq

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "data.db")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавь переменную окружения BOT_TOKEN с токеном от @BotFather.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

conversation_histories: dict[int, list[dict]] = {}


# ---------- БАЗА ДАННЫХ ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'idea',     -- idea | reminder
            time TEXT,                              -- 'HH:MM'
            repeat TEXT,                             -- daily | once
            status TEXT NOT NULL DEFAULT 'inbox',    -- inbox | active | done
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


# ---------- СОСТОЯНИЯ (FSM) ----------

class AddReminderState(StatesGroup):
    waiting_time = State()


class QuickAddState(StatesGroup):
    waiting_time = State()


class AIConversationState(StatesGroup):
    chatting = State()


# ---------- КЛАВИАТУРЫ ----------

def idea_keyboard(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ В напоминание", callback_data=f"toreminder:{item_id}")
    b.button(text="🌙 Отложить", callback_data=f"later:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"delete:{item_id}")
    b.adjust(2)
    return b.as_markup()


def task_keyboard(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✔️ Готово", callback_data=f"done:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"delete:{item_id}")
    b.adjust(2)
    return b.as_markup()


def later_keyboard(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ В напоминание", callback_data=f"toreminder:{item_id}")
    b.button(text="📥 В инбокс", callback_data=f"toinbox:{item_id}")
    b.button(text="🗑 Удалить", callback_data=f"delete:{item_id}")
    b.adjust(2)
    return b.as_markup()


def stop_ai_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛑 Завершить чат с ИИ", callback_data="stop_ai")
    return b.as_markup()


def is_valid_time(s: str) -> bool:
    try:
        h, m = s.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except Exception:
        return False


HELP_TEXT = (
    "Я помогу не терять идеи, напоминать о делах и отвечать на вопросы.\n\n"
    "📝 Просто напиши мне любую мысль — сохраню её как идею в инбокс.\n"
    "/ideas — посмотреть идеи и решить, что с ними делать\n"
    "/add Текст — сразу создать напоминание (спрошу время)\n"
    "/later Текст — закинуть что-то в «когда-нибудь» (книга, практика, не срочное)\n"
    "/later — посмотреть список «когда-нибудь»\n"
    "/tasks — список активных напоминаний\n"
    "/ai Вопрос — задать вопрос ИИ\n"
    "/ai — войти в режим диалога с ИИ (выход: /stop)\n"
    "/help — это сообщение"
)


# ---------- КОМАНДЫ ----------

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет! 👋\n\n" + HELP_TEXT)


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(Command("ideas"))
async def list_ideas(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='idea' AND status='inbox' ORDER BY id",
        (message.chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("Пока нет идей в инбоксе. Просто напиши мне что-нибудь — сохраню.")
        return
    for r in rows:
        await message.answer(f"💡 {r['text']}", reply_markup=idea_keyboard(r["id"]))


@dp.message(Command("tasks"))
async def list_tasks(message: Message):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND type='reminder' AND status='active' ORDER BY time",
        (message.chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer("Активных напоминаний нет.")
        return
    for r in rows:
        repeat_label = "каждый день" if r["repeat"] == "daily" else "разово"
        await message.answer(
            f"⏰ {r['time']} ({repeat_label}) — {r['text']}",
            reply_markup=task_keyboard(r["id"]),
        )


@dp.message(Command("later"))
async def later_cmd(message: Message):
    text = message.text.replace("/later", "", 1).strip()
    if text:
        # добавляем сразу в "когда-нибудь", минуя инбокс
        conn = db()
        conn.execute(
            "INSERT INTO items (chat_id, text, type, status, created_at) VALUES (?,?,?,?,?)",
            (message.chat.id, text, "idea", "later", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        await message.answer(f"🌙 Записал в «когда-нибудь»: «{text}»")
        return

    # без текста — показываем список
    conn = db()
    rows = conn.execute(
        "SELECT * FROM items WHERE chat_id=? AND status='later' ORDER BY id",
        (message.chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await message.answer(
            "Список «когда-нибудь» пуст.\nДобавить можно так: /later Прочитать книгу про привычки"
        )
        return
    for r in rows:
        await message.answer(f"🌙 {r['text']}", reply_markup=later_keyboard(r["id"]))


@dp.message(Command("add"))
async def add_reminder_cmd(message: Message, state: FSMContext):
    text = message.text.replace("/add", "", 1).strip()
    if not text:
        await message.answer("Напиши текст после команды, например:\n/add Полить цветы")
        return
    await state.update_data(text=text)
    await state.set_state(QuickAddState.waiting_time)
    await message.answer("В какое время напоминать? Формат ЧЧ:ММ, например 09:00")


@dp.message(QuickAddState.waiting_time)
async def quick_add_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    if not is_valid_time(time_str):
        await message.answer("Не похоже на время. Формат ЧЧ:ММ, например 18:30")
        return
    data = await state.get_data()
    conn = db()
    conn.execute(
        "INSERT INTO items (chat_id, text, type, time, repeat, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (message.chat.id, data["text"], "reminder", time_str, "daily", "active", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"Готово! Буду напоминать каждый день в {time_str}: «{data['text']}»")


# ---------- ПРЕВРАЩЕНИЕ ИДЕИ В НАПОМИНАНИЕ ----------

@dp.callback_query(F.data.startswith("toreminder:"))
async def to_reminder(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.split(":")[1])
    await state.update_data(item_id=item_id)
    await state.set_state(AddReminderState.waiting_time)
    await callback.message.answer("В какое время напоминать об этом? Формат ЧЧ:ММ, например 08:30")
    await callback.answer()


@dp.message(AddReminderState.waiting_time)
async def set_reminder_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    if not is_valid_time(time_str):
        await message.answer("Формат ЧЧ:ММ, например 08:30. Попробуй ещё раз.")
        return
    data = await state.get_data()
    conn = db()
    conn.execute(
        "UPDATE items SET type='reminder', time=?, repeat='daily', status='active' WHERE id=?",
        (time_str, data["item_id"]),
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"Идея превращена в напоминание на {time_str} каждый день ✅")


@dp.callback_query(F.data.startswith("later:"))
async def move_to_later(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("UPDATE items SET status='later' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(callback.message.text + "\n\n🌙 Отложено («когда-нибудь»)")
    await callback.answer("Отложено")


@dp.callback_query(F.data.startswith("toinbox:"))
async def move_to_inbox(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("UPDATE items SET status='inbox' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(callback.message.text + "\n\n📥 Возвращено в инбокс")
    await callback.answer("Возвращено в инбокс")


@dp.callback_query(F.data.startswith("done:"))
async def mark_done(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("UPDATE items SET status='done' WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(callback.message.text + "\n\n✔️ Готово")
    await callback.answer("Отмечено как выполненное")


@dp.callback_query(F.data.startswith("delete:"))
async def delete_item(callback: CallbackQuery):
    item_id = int(callback.data.split(":")[1])
    conn = db()
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(callback.message.text + "\n\n🗑 Удалено")
    await callback.answer("Удалено")


# ---------- ИИ-ПОМОЩНИК ----------

async def get_ai_response(chat_id: int, user_message: str) -> str:
    history = conversation_histories.get(chat_id, [])

    messages = [
        {"role": "system", "content": "Ты полезный ИИ-ассистент в Telegram боте. Отвечай кратко, по делу и на русском языке."}
    ]
    for m in history[-20:]:
        messages.append({"role": m["role"], "content": m["text"]})
    messages.append({"role": "user", "content": user_message})

    response = await groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        max_tokens=1024,
    )

    assistant_text = response.choices[0].message.content

    if chat_id not in conversation_histories:
        conversation_histories[chat_id] = []

    conversation_histories[chat_id].append({"role": "user", "text": user_message})
    conversation_histories[chat_id].append({"role": "assistant", "text": assistant_text})

    return assistant_text


@dp.message(Command("ai"))
async def ai_command(message: Message, state: FSMContext):
    if not groq_client:
        await message.answer(
            "⚠️ ИИ не настроен.\n"
            "Добавь переменную окружения GROQ_API_KEY с ключом от console.groq.com"
        )
        return

    text = message.text.replace("/ai", "", 1).strip()

    if text:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            response = await get_ai_response(message.chat.id, text)
            await message.answer(f"🤖 {response}")
        except Exception as e:
            logging.error(f"AI error: {e}")
            await message.answer("❌ Произошла ошибка при обращении к ИИ. Попробуй позже.")
    else:
        conversation_histories.pop(message.chat.id, None)
        await state.set_state(AIConversationState.chatting)
        await message.answer(
            "🤖 Режим разговора с ИИ активирован!\n"
            "Задавай любые вопросы. Для выхода — /stop",
            reply_markup=stop_ai_keyboard(),
        )


@dp.callback_query(F.data == "stop_ai")
async def stop_ai_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("✅ Разговор с ИИ завершён. Снова сохраняю твои идеи.")
    await callback.answer()


@dp.message(Command("stop"))
async def stop_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Режим ИИ завершён. Снова сохраняю твои идеи.")


@dp.message(AIConversationState.chatting, F.text)
async def ai_chat_message(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        response = await get_ai_response(message.chat.id, message.text)
        await message.answer(f"🤖 {response}")
    except Exception as e:
        logging.error(f"AI chat error: {e}")
        await message.answer("❌ Произошла ошибка. Попробуй ещё раз.")


# ---------- ЛОВИМ ЛЮБОЙ ТЕКСТ КАК НОВУЮ ИДЕЮ ----------

@dp.message(F.text)
async def catch_idea(message: Message):
    conn = db()
    conn.execute(
        "INSERT INTO items (chat_id, text, type, status, created_at) VALUES (?,?,?,?,?)",
        (message.chat.id, message.text, "idea", "inbox", datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    await message.answer("💡 Сохранил как идею. /ideas — посмотреть все идеи.")


# ---------- ФОНОВЫЙ ЦИКЛ НАПОМИНАНИЙ ----------

async def reminder_loop():
    while True:
        now = datetime.now().strftime("%H:%M")
        conn = db()
        rows = conn.execute(
            "SELECT * FROM items WHERE type='reminder' AND status='active' AND time=?",
            (now,),
        ).fetchall()
        for r in rows:
            try:
                await bot.send_message(r["chat_id"], f"⏰ Напоминание: {r['text']}")
                if r["repeat"] == "once":
                    conn.execute("UPDATE items SET status='done' WHERE id=?", (r["id"],))
            except Exception as e:
                logging.error(f"Не удалось отправить напоминание {r['id']}: {e}")
        conn.commit()
        conn.close()
        await asyncio.sleep(60)


async def main():
    init_db()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
