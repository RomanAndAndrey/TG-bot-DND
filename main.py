import asyncio
import logging
import sqlite3
import json
import os
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import random
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# =================================================================================================
# КОНФИГУРАЦИЯ
# =================================================================================================

# Токены (Замените на свои реальные ключи!)
TELEGRAM_TOKEN = "8229692641:AAFtw5RO0QLqiFIRDc220eiT8oUiIzDiMdg"
GEMINI_API_KEY = "AIzaSyDSTBjcTC8pdW3p4xJJi4P2QkmtGc9qehg"

# Имя файла базы данных
DB_NAME = "dnd_bot.db"

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация роутера aiogram
router = Router()

# =================================================================================================
# КЛАВИАТУРЫ
# =================================================================================================
def game_keyboard():
    """Клавиатура для игрового режима."""
    buttons = [
        [KeyboardButton(text="🎲 Бросить кубик (D20)")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# =================================================================================================
# РАБОТА С БАЗОЙ ДАННЫХ
# =================================================================================================

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Создание таблицы users."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                name TEXT,
                race TEXT,
                char_class TEXT,
                origin TEXT,
                backstory TEXT,
                history TEXT DEFAULT '[]'
            )
        """)
        # Миграция (если таблица была старая, добавим колонки, если их нет)
        # Для простоты можно игнорировать ошибки, если колонки уже есть
        for col in ['char_class', 'origin', 'backstory']:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    logger.info("База данных инициализирована.")

def get_user(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def create_or_update_user(user_id, **kwargs):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        
        if kwargs:
            set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [user_id]
            cursor.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", values)
        conn.commit()

def save_history(user_id, history_list):
    """Сохраняем историю в JSON."""
    json_history = json.dumps(history_list, ensure_ascii=False)
    create_or_update_user(user_id, history=json_history)

def load_history(user_id):
    """Загружаем историю."""
    user = get_user(user_id)
    if user and user["history"]:
        try:
            return json.loads(user["history"])
        except json.JSONDecodeError:
            return []
    return []

# =================================================================================================
# FSM (АНКЕТА)
# =================================================================================================

class Registration(StatesGroup):
    name = State()
    race = State()
    char_class = State()
    origin = State()
    backstory = State()

class GameState(StatesGroup):
    active = State()

# =================================================================================================
# ЛОГИКА GEMINI
# =================================================================================================

def configure_gemini():
    genai.configure(api_key=GEMINI_API_KEY)

async def generate_response(user_data, user_message, history):
    """
    Генерирует ответ от Gemini.
    """
    # 1. Формируем системный промпт (профиль персонажа)
    system_instruction = (
        f"Ты — строгий Мастер Подземелий (Dungeon Master) в D&D фэнтези игре. "
        f"Вот анкета игрока:\n"
        f"Имя: {user_data.get('name')}\n"
        f"Раса: {user_data.get('race')}\n"
        f"Класс: {user_data.get('char_class')}\n"
        f"Происхождение: {user_data.get('origin')}\n"
        f"Предыстория: {user_data.get('backstory')}\n\n"
        f"Твоя задача — вести игру, описывать мир и реагировать на действия игрока. "
        f"Будь атмосферным, но лаконичным (не пиши огромные простыни текста).\n\n"
        f"ВАЖНО: Ты НЕ помощник, НЕ калькулятор и НЕ поисковик. "
        f"Если пользователь задает вопросы, не касающиеся сюжета игры (математика, политика, код, погода в реальности), "
        f"ты должен ИГНОРИРОВАТЬ сам вопрос и грубо или иронично возвращать игрока в реальность игры "
        f"(например: 'Эти руны мне незнакомы, сосредоточься на гоблине перед тобой!'). "
        f"Никогда не давай прямых ответов на вопросы вне лора игры."
    )

    # 2. Создаем модель с системной инструкцией
    model = genai.GenerativeModel(
        model_name="gemini-flash-latest",
        system_instruction=system_instruction
    )

    # 3. Подготавливаем историю чата для Gemini
    # API ожидает список словарей: [{'role': 'user'|'model', 'parts': ['text']}]
    # Наша БД хранит: [{'role': 'user'|'model', 'parts': [...]}] (мы будем так сохранять)
    
    # Запускаем чат с историей
    chat = model.start_chat(history=history)

    # 4. Отправляем сообщение асинхронно
    try:
        response = await chat.send_message_async(user_message)
        return response.text
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return "Мастер задумался... (Ошибка магической связи, попробуй еще раз)."

# =================================================================================================
# ОБРАБОТЧИКИ
# =================================================================================================

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)

    # Если анкета полная, сразу в игру
    if user and user.get("name") and user.get("backstory"):
        await message.answer("С возвращением в игру! Что будешь делать?", reply_markup=game_keyboard())
        await state.set_state(GameState.active)
        create_or_update_user(user_id, state="GAME_ACTIVE")
        return

    await message.answer("Приветствую, путник! Я — Gemini Dungeon Master.\nДавай создадим твоего персонажа.\n\nКак тебя зовут?")
    await state.set_state(Registration.name)
    create_or_update_user(user_id, state="REGISTRATION")

@router.message(Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Отлично. Твоя Раса? (Человек, Эльф, Орк...)")
    await state.set_state(Registration.race)

@router.message(Registration.race)
async def process_race(message: types.Message, state: FSMContext):
    await state.update_data(race=message.text)
    await message.answer("Твой Класс? (Воин, Маг, Плут...)")
    await state.set_state(Registration.char_class)

@router.message(Registration.char_class)
async def process_class(message: types.Message, state: FSMContext):
    await state.update_data(char_class=message.text)
    await message.answer("Твое Происхождение? (Откуда ты родом, чем занимался?)")
    await state.set_state(Registration.origin)

@router.message(Registration.origin)
async def process_origin(message: types.Message, state: FSMContext):
    await state.update_data(origin=message.text)
    await message.answer("Краткая Предыстория (как ты стал приключенцем?)")
    await state.set_state(Registration.backstory)

@router.message(Registration.backstory)
async def process_backstory(message: types.Message, state: FSMContext):
    # Финал регистрации
    await state.update_data(backstory=message.text)
    data = await state.get_data()
    user_id = message.from_user.id
    
    create_or_update_user(
        user_id,
        name=data['name'],
        race=data['race'],
        char_class=data['char_class'],
        origin=data['origin'],
        backstory=data['backstory'],
        state="GAME_ACTIVE",
        history="[]"  # Сброс истории при новой игре
    )
    
    await message.answer("Персонаж создан! История начинается...\n\nТы стоишь на распутье. Куда направишься?", reply_markup=game_keyboard())
    await state.set_state(GameState.active)

@router.message(GameState.active)
async def game_loop(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text
    user_data = get_user(user_id)

    if not user_data:
        await message.answer("Ошибка пользователя. Нажмите /start")
        return

    # Логика броска кубика
    prompt_to_send = user_text
    
    if user_text.strip() == "🎲 Бросить кубик (D20)" or user_text.lower() == "бросить кубик":
        roll_result = random.randint(1, 20)
        # Сообщение пользователю
        await message.answer(f"🎲 Кубик брошен! Результат: {roll_result}")
        
        # Инструкция для нейросети
        prompt_to_send = (
            f"System Update: Игрок бросил кубик D20. Результат: {roll_result}. "
            f"Опиши результат действий игрока (или события), исходя из этого числа. "
            f"(1 - критическая неудача, 20 - триумф, остальные по ситуации)."
        )

    # Загружаем и валидируем историю для Gemini
    raw_history = load_history(user_id)
    # Gemini требует историю: list of content objects.
    # Простой формат для python-sdk: [{'role': 'user', 'parts': ['text']}, ...]
    
    # Генерируем ответ
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    response_text = await generate_response(user_data, prompt_to_send, raw_history)
    
    # Отправляем ответ с клавиатурой (чтобы она не пропадала)
    await message.answer(response_text, reply_markup=game_keyboard())

    # Обновляем историю
    new_turn_user = {"role": "user", "parts": [prompt_to_send]}
    new_turn_model = {"role": "model", "parts": [response_text]}
    
    raw_history.append(new_turn_user)
    raw_history.append(new_turn_model)
    
    # Храним последние 20 сообщений (10 пар)
    if len(raw_history) > 20:
        raw_history = raw_history[-20:]
        
    save_history(user_id, raw_history)

# =================================================================================================
# ЗАПУСК
# =================================================================================================

async def main():
    if GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        print("!!! ОШИБКА: Вставьте GEMINI_API_KEY в код !!!")
    
    configure_gemini()
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    print("Gemini D&D Bot запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        # Для Windows
        if os.name == 'nt':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
