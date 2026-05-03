import asyncio
import random
import logging
import os
from typing import Dict, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot_username")

if not BOT_TOKEN:
    raise ValueError("Укажите BOT_TOKEN в файле .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

games: Dict[int, "Game"] = {}
user_stats: Dict[int, dict] = {}

class Game:
    def __init__(self, chat_id: int, creator_id: int):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.players: Dict[int, dict] = {}
        self.lobby_msg_id: Optional[int] = None
        self.phase = "lobby"
        self.night_actions: Dict[str, int] = {}
        self.votes: Dict[int, int] = {}
        self.timer_cancel = asyncio.Event()
        self.voting_cancel = asyncio.Event()
        self.round = 0
        self.lock = asyncio.Lock()

MIN_PLAYERS = 4
MAX_PLAYERS = 6
NIGHT_TIMEOUT = 20
VOTING_TIMEOUT = 20

def init_stats(uid: int):
    if uid not in user_stats:
        user_stats[uid] = {"games": 0, "wins": 0, "losses": 0}

def build_kb(buttons: list[tuple], row_width: int = 2) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in [buttons[i:i+row_width] for i in range(0, len(buttons), row_width)]
    ])

async def update_lobby(chat_id: int):
    if chat_id not in games: return
    game = games[chat_id]
    if game.phase != "lobby" or game.lobby_msg_id is None: return

    async with game.lock:
        if len(game.players) == 0:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=game.lobby_msg_id)
            except TelegramBadRequest:
                pass
            del games[chat_id]
            return

        creator_name = game.players[game.creator_id]["username"] if game.creator_id in game.players else "—"
        players_txt = "\n".join([f"- {p['username']}" for p in game.players.values()]) or "Пока никого нет"
        status = "Ожидание игроков" if len(game.players) < MIN_PLAYERS else "Готово к старту"
        
        text = (f"🎮 ИГРА МАФИЯ\n"
                f"👤 Создатель: {creator_name}\n\n"
                f"👥 Игроки ({len(game.players)}/{MAX_PLAYERS}):\n{players_txt}\n\n"
                f"📊 Статус: {status}")

        kb_btns = [("✅ Войти", "join_game"), ("🚪 Выйти", "leave_game")]
        if MIN_PLAYERS <= len(game.players) <= MAX_PLAYERS:
            kb_btns.append(("🚀 Старт", "start_game"))

        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=game.lobby_msg_id, text=text, reply_markup=build_kb(kb_btns))
        except TelegramBadRequest:
            pass

def assign_roles(chat_id: int):
    game = games[chat_id]
    count = len(game.players)
    roles = []
    
    if count == 4:
        roles = ["Мафия", "Шериф", "Доктор", "Мирный"]
    elif count == 5:
        roles = ["Мафия", "Шериф", "Доктор", "Мирный", "Мирный"]
    elif count == 6:
        roles = ["Мафия", "Мафия", "Шериф", "Доктор", "Мирный", "Мирный"]
    else:
        # Запасной вариант для безопасности
        roles = ["Мафия", "Шериф", "Доктор", "Мирный", "Мирный", "Мирный"][:count]
        
    random.shuffle(roles)
    for i, uid in enumerate(game.players):
        game.players[uid]["role"] = roles[i]

def check_win(chat_id: int) -> Optional[str]:
    game = games[chat_id]
    alive = [p for p in game.players.values() if p.get("alive")]
    mafia_alive = sum(1 for p in alive if p["role"] == "Мафия")
    
    if mafia_alive == 0:
        return "🕊️ Мирные победили! Все мафиози раскрыты."
    # Мафия побеждает, если осталось 2 игрока и хотя бы один из них мафия
    if len(alive) <= 2 and mafia_alive > 0:
        return "🩸 Мафия победила! Осталось 2 игрока, мафия выжила."
    return None

async def safe_send(uid: int, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await bot.send_message(uid, text, reply_markup=kb)
        return True
    except Exception as e:
        logging.warning(f"ЛС ошибка {uid}: {e}")
        return False

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if message.chat.type == "private":
        init_stats(uid)
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}!\nЯ бот для игры в Мафию.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Добавить в группу", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")
            ]])
        )
    elif message.chat.type in ["group", "supergroup"]:
        chat_id = message.chat.id
        if chat_id in games:
            old_game = games[chat_id]
            if old_game.lobby_msg_id:
                try:
                    await bot.delete_message(chat_id, old_game.lobby_msg_id)
                except: pass
        games[chat_id] = Game(chat_id, uid)
        game = games[chat_id]
        game.players[uid] = {"username": message.from_user.username or message.from_user.first_name, "alive": True, "role": ""}
        text = (f"🎮 ИГРА МАФИЯ\n"
                f"👤 Создатель: {game.players[uid]['username']}\n\n"
                f"👥 Игроки (1/{MAX_PLAYERS}):\n- {game.players[uid]['username']}\n\n"
                f"📊 Статус: Ожидание игроков")
        msg = await message.answer(text, reply_markup=build_kb([("✅ Войти", "join_game"), ("🚪 Выйти", "leave_game")]))
        game.lobby_msg_id = msg.message_id

@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    chat_id = message.chat.id
    if chat_id in games:
        game = games[chat_id]
        if game.lobby_msg_id:
            try:
                await bot.delete_message(chat_id, game.lobby_msg_id)
            except: pass
        del games[chat_id]
        await message.answer("🛑 Игра отменена. Лобби удалено.")
    else:
        await message.answer("🚫 Нет активной игры для отмены.")

@dp.message(Command("profile"), F.chat.type == "private")
async def cmd_profile(message: types.Message):
    uid = message.from_user.id
    init_stats(uid)
    s = user_stats[uid]
    wr = f"{(s['wins']/s['games']*100):.1f}%" if s['games'] > 0 else "0%"
    await message.answer(f"📊 Профиль игрока\n🎮 Игр сыграно: {s['games']}\n🏆 Побед: {s['wins']}\n💀 Поражений: {s['losses']}\n📈 Винрейт: {wr}")

@dp.message(Command("help", "commands"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_help(message: types.Message):
    await message.answer("📜 Список команд:\n/start - Создать/Обновить лобби\n/stop - Отменить игру\n/profile - Статистика (в ЛС)\n\n🔘 Кнопки в лобби: Войти, Выйти, Старт")

@dp.callback_query(F.data.in_(["join_game", "leave_game", "start_game"]))
async def cb_lobby(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in games: return
    game = games[chat_id]
    uid = callback.from_user.id
    name = callback.from_user.username or callback.from_user.first_name

    async with game.lock:
        if callback.data == "join_game":
            if game.phase == "lobby" and len(game.players) < MAX_PLAYERS and uid not in game.players:
                game.players[uid] = {"username": name, "alive": True, "role": ""}
                await callback.answer("✅ Вы в игре!")
            else:
                await callback.answer("🚫 Лобби заполнено или игра уже идёт.", show_alert=True)
        elif callback.data == "leave_game":
            if game.phase == "lobby" and uid in game.players:
                del game.players[uid]
                await callback.answer("🚪 Вы вышли.")
        elif callback.data == "start_game":
            if game.phase == "lobby" and MIN_PLAYERS <= len(game.players) <= MAX_PLAYERS:
                game.phase = "night"
                assign_roles(chat_id)
                await callback.answer("🚀 Игра начинается!")
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=game.lobby_msg_id, text="🌙 Игра началась! Роли отправлены в личные сообщения.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
                except TelegramBadRequest: pass
                await start_game_flow(chat_id)
                return
    await update_lobby(chat_id)

async def start_game_flow(chat_id: int):
    for uid, p in games[chat_id].players.items():
        await safe_send(uid, f"🎭 Ваша роль: {p['role']}\n📌 Дождитесь ночи. Специальные роли получат запросы лично.")
    await asyncio.sleep(2)
    await start_night(chat_id)

async def start_night(chat_id: int):
    game = games[chat_id]
    game.night_actions.clear()
    
    # 1. Мафия
    game.timer_cancel = asyncio.Event()
    targets = [(u, p["username"]) for u, p in game.players.items() if p.get("alive") and p["role"] != "Мафия"]
    mafia_ids = [u for u, p in game.players.items() if p.get("alive") and p["role"] == "Мафия"]
    if mafia_ids and targets:
        kb = build_kb([(f" Убить {n}", f"mafia_{u}") for u, n in targets])
        for uid in mafia_ids: await safe_send(uid, "🕶️ Мафия просыпается. Выберите жертву:", kb)
    try: await asyncio.wait_for(game.timer_cancel.wait(), timeout=NIGHT_TIMEOUT)
    except: pass

    # 2. Шериф
    game.timer_cancel = asyncio.Event()
    sheriff_ids = [u for u, p in game.players.items() if p.get("alive") and p["role"] == "Шериф"]
    if sheriff_ids:
        kb = build_kb([(f" Проверить {p['username']}", f"sheriff_{u}") for u, p in game.players.items() if p.get("alive")])
        for uid in sheriff_ids: await safe_send(uid, "️‍️ Шериф просыпается. Кого проверить?", kb)
    try: await asyncio.wait_for(game.timer_cancel.wait(), timeout=NIGHT_TIMEOUT)
    except: pass

    # 3. Доктор
    game.timer_cancel = asyncio.Event()
    doc_ids = [u for u, p in game.players.items() if p.get("alive") and p["role"] == "Доктор"]
    if doc_ids:
        kb = build_kb([(f"💉 Спасти {p['username']}", f"doctor_{u}") for u, p in game.players.items() if p.get("alive")])
        for uid in doc_ids: await safe_send(uid, "️ Доктор просыпается. Кого спасти?", kb)
    try: await asyncio.wait_for(game.timer_cancel.wait(), timeout=NIGHT_TIMEOUT)
    except: pass

    await resolve_night(chat_id)

@dp.callback_query(F.data.startswith(("mafia_", "sheriff_", "doctor_")))
async def cb_night(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    try:
        if chat_id not in games: return
        game = games[chat_id]
        if game.phase != "night":
            return await callback.answer("🌙 Ночная фаза уже завершена.", show_alert=True)
        
        uid = callback.from_user.id
        role, target = callback.data.split("_")
        target = int(target)
        role_map = {"mafia": "Мафия", "sheriff": "Шериф", "doctor": "Доктор"}
        
        if game.players[uid].get("role") != role_map.get(role):
            return await callback.answer("❌ Это не ваша роль.", show_alert=True)

        game.night_actions[role] = target
        game.timer_cancel.set()
        await callback.answer("✅ Действие принято.")
        if callback.message:
            try: await callback.message.delete()
            except TelegramBadRequest: pass
    except Exception as e:
        logging.error(f"Ошибка ночного колбэка: {e}")
        await callback.answer("Произошла ошибка.", show_alert=True)

async def resolve_night(chat_id: int):
    game = games[chat_id]
    # Небольшая задержка для гарантии применения состояний
    await asyncio.sleep(0.5)
    
    killed = game.night_actions.get("mafia")
    saved = game.night_actions.get("doctor")
    checked = game.night_actions.get("sheriff")

    dead_msg = "☀️ Наступает день.\n"
    if killed and game.players.get(killed, {}).get("alive"):
        if killed == saved:
            dead_msg += "💊 Доктор спас жертву! Ночь прошла тихо."
        else:
            game.players[killed]["alive"] = False
            dead_msg += f"🩸 Погиб: {game.players[killed]['username']} (роль: {game.players[killed]['role']})"
    else:
        dead_msg += "🌙 Никто не умер."
    await bot.send_message(chat_id, dead_msg)

    if checked and game.players[checked].get("alive"):
        res = " МАФИЯ" if game.players[checked]["role"] == "Мафия" else "🟢 МИРНЫЙ"
        for u, p in game.players.items():
            if p["role"] == "Шериф" and p.get("alive"):
                await safe_send(u, f"🕵️️ Проверка: {game.players[checked]['username']} - {res}")

    win = check_win(chat_id)
    if win: await end_game(chat_id, win); return

    game.phase = "voting"
    game.votes.clear()
    game.voting_cancel.clear()
    await start_voting(chat_id)

async def start_voting(chat_id: int):
    game = games[chat_id]
    alive = [(u, p["username"]) for u, p in game.players.items() if p.get("alive")]
    kb = build_kb([(f"🗳️ Голос против {n}", f"vote_{u}") for u, n in alive])
    await bot.send_message(chat_id, f"📢 Голосование! Выберите подозреваемого. Таймер: {VOTING_TIMEOUT} сек.", reply_markup=kb)

    async def voting_timeout():
        try:
            await asyncio.wait_for(game.voting_cancel.wait(), timeout=VOTING_TIMEOUT)
        except asyncio.TimeoutError:
            if game.phase == "voting":
                await bot.send_message(chat_id, " Время голосования истекло. Подсчёт голосов...")
                await resolve_votes(chat_id)

    asyncio.create_task(voting_timeout())

@dp.callback_query(F.data.startswith("vote_"))
async def cb_vote(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in games: return
    game = games[chat_id]
    if game.phase != "voting":
        return await callback.answer("🗳️ Голосование не активно или завершено.", show_alert=True)
    
    voter = callback.from_user.id
    target = int(callback.data.split("_")[1])
    
    if not game.players[voter].get("alive"): 
        return await callback.answer("💀 Мертвые не голосуют.", show_alert=True)
    if game.votes.get(voter): 
        return await callback.answer("🚫 Вы уже голосовали.", show_alert=True)

    await callback.answer("✅ Голос принят.")
    game.votes[voter] = target
    alive_cnt = sum(1 for p in game.players.values() if p.get("alive"))
    if len(game.votes) >= alive_cnt:
        game.voting_cancel.set()
        await resolve_votes(chat_id)

async def resolve_votes(chat_id: int):
    game = games[chat_id]
    if game.phase != "voting": return
    game.phase = "day_resolve"
    
    if not game.votes:
        await bot.send_message(chat_id, "🚫 Никто не проголосовал. Ничья. Раунд завершен.")
    else:
        counts = {}
        for t in game.votes.values(): counts[t] = counts.get(t, 0) + 1
        max_v = max(counts.values())
        executed = [u for u, c in counts.items() if c == max_v]
        
        if len(executed) == 1:
            v = executed[0]
            game.players[v]["alive"] = False
            await bot.send_message(chat_id, f"🔨 Изгнан: {game.players[v]['username']}.\nРоль: {game.players[v]['role']}")
        else:
            await bot.send_message(chat_id, "⚖️ Ничья! Никто не изгнан.")
            
    game.votes.clear()
    win = check_win(chat_id)
    if win: await end_game(chat_id, win); return
    
    game.phase = "night"
    game.round += 1
    await asyncio.sleep(3)
    await bot.send_message(chat_id, " Наступает новая ночь...")
    await start_night(chat_id)

async def end_game(chat_id: int, msg: str):
    game = games[chat_id]
    # Исправлено: передаём пустой список в inline_keyboard
    await bot.send_message(chat_id, msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    
    for uid, p in game.players.items():
        init_stats(uid)
        user_stats[uid]["games"] += 1
        is_mafia_win = "Мафия" in msg
        win_role = (p["role"] == "Мафия") == is_mafia_win
        if win_role: user_stats[uid]["wins"] += 1
        else: user_stats[uid]["losses"] += 1
        
        s = user_stats[uid]
        wr = f"{(s['wins']/s['games']*100):.1f}%" if s['games'] > 0 else "0%"
        try:
            # Исправлено: передаём пустой список в inline_keyboard
            await bot.send_message(uid, f"Игра завершена!\nРезультат: {msg}\n\n📊 Ваша новая статистика:\nИгр: {s['games']}\nПобед: {s['wins']}\nПоражений: {s['losses']}\nВинрейт: {wr}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
        except: pass
        
    game.phase = "ended"
    games.pop(chat_id, None)

async def health_check(request):
    return web.Response(text='{"status": "ok"}', content_type='application/json')

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health-check сервер запущен на порту {port}")

async def main():
    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
