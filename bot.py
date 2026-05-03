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

# Глобальные хранилища
games: Dict[int, "Game"] = {}
user_stats: Dict[int, dict] = {}
user_to_game: Dict[int, int] = {}  # Связка: user_id -> chat_id игры

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
        self.last_private_messages: Dict[int, int] = {} # Для удаления кнопок в ЛС

MIN_PLAYERS = 4
MAX_PLAYERS = 6
NIGHT_TIMEOUT = 30
VOTING_TIMEOUT = 30

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
            try: await bot.delete_message(chat_id=chat_id, message_id=game.lobby_msg_id)
            except: pass
            del games[chat_id]
            return

        players_txt = "\n".join([f"- {p['username']}" for p in game.players.values()])
        status = "Ожидание..." if len(game.players) < MIN_PLAYERS else "Можно начинать!"
        
        text = (f"🎮 ИГРА МАФИЯ\n"
                f"👥 Игроки ({len(game.players)}/{MAX_PLAYERS}):\n{players_txt}\n\n"
                f"📊 Статус: {status}")

        kb_btns = [("✅ Войти", "join_game"), ("🚪 Выйти", "leave_game")]
        if MIN_PLAYERS <= len(game.players) <= MAX_PLAYERS:
            kb_btns.append(("🚀 Старт", "start_game"))

        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=game.lobby_msg_id, text=text, reply_markup=build_kb(kb_btns))
        except TelegramBadRequest: pass

def assign_roles(chat_id: int):
    game = games[chat_id]
    count = len(game.players)
    
    # Логика ролей согласно ТЗ
    if count == 4:
        roles = ["Мафия", "Шериф", "Доктор", "Мирный"]
    elif count == 5:
        roles = ["Мафия", "Шериф", "Доктор", "Мирный", "Мирный"]
    elif count == 6:
        roles = ["Мафия", "Мафия", "Шериф", "Доктор", "Мирный", "Мирный"]
    else: # На случай если будет > 6
        roles = ["Мафия", "Мафия", "Шериф", "Доктор"] + ["Мирный"] * (count - 4)

    random.shuffle(roles)
    for i, uid in enumerate(game.players):
        game.players[uid]["role"] = roles[i]
        user_to_game[uid] = chat_id

def check_win(chat_id: int) -> Optional[str]:
    game = games[chat_id]
    mafia = sum(1 for p in game.players.values() if p.get("alive") and p["role"] == "Мафия")
    civ = sum(1 for p in game.players.values() if p.get("alive") and p["role"] != "Мафия")
    
    if mafia == 0: return "🕊️ Мирные победили! Мафия устранена."
    if mafia >= civ: return "🩸 Мафия победила! Мирные не в силах сопротивляться."
    return None

async def safe_send(uid: int, text: str, kb: Optional[InlineKeyboardMarkup] = None, game: Optional[Game] = None):
    try:
        msg = await bot.send_message(uid, text, reply_markup=kb)
        if game and kb:
            game.last_private_messages[uid] = msg.message_id
        return True
    except Exception as e:
        logging.warning(f"Ошибка отправки в ЛС {uid}: {e}")
        return False

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if message.chat.type == "private":
        init_stats(uid)
        await message.answer(f"👋 Привет! Я бот для Мафии.", 
            reply_markup=build_kb([("➕ Добавить в группу", f"https://t.me/{BOT_USERNAME}?startgroup=true")], 1))
    else:
        chat_id = message.chat.id
        games[chat_id] = Game(chat_id, uid)
        game = games[chat_id]
        game.players[uid] = {"username": message.from_user.username or message.from_user.first_name, "alive": True, "role": ""}
        msg = await message.answer("Инициализация лобби...", reply_markup=build_kb([("✅ Войти", "join_game")]))
        game.lobby_msg_id = msg.message_id
        await update_lobby(chat_id)

@dp.callback_query(F.data.in_(["join_game", "leave_game", "start_game"]))
async def cb_lobby(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in games: return
    game = games[chat_id]
    uid = callback.from_user.id

    if callback.data == "join_game":
        if len(game.players) >= MAX_PLAYERS:
            return await callback.answer("❌ Лобби заполнено!", show_alert=True)
        if uid not in game.players:
            game.players[uid] = {"username": callback.from_user.username or callback.from_user.first_name, "alive": True, "role": ""}
            await callback.answer("Вы вошли!")
    elif callback.data == "leave_game":
        if uid in game.players:
            game.players.pop(uid)
            user_to_game.pop(uid, None)
            await callback.answer("Вы вышли.")
    elif callback.data == "start_game":
        if uid != game.creator_id:
            return await callback.answer("❌ Только создатель может запустить игру.", show_alert=True)
        if len(game.players) < MIN_PLAYERS:
            return await callback.answer(f"Нужно минимум {MIN_PLAYERS} игрока!", show_alert=True)
        
        game.phase = "night"
        assign_roles(chat_id)
        await bot.edit_message_text("🌙 Игра началась! Рассылаю роли...", chat_id=chat_id, message_id=game.lobby_msg_id)
        await start_game_flow(chat_id)
        return
    await update_lobby(chat_id)

async def start_game_flow(chat_id: int):
    game = games[chat_id]
    for uid, p in game.players.items():
        await safe_send(uid, f"🎭 Ваша роль: **{p['role']}**\n\nИгра начинается!", game=game)
    await asyncio.sleep(2)
    await start_night(chat_id)

async def start_night(chat_id: int):
    game = games[chat_id]
    game.phase = "night"
    game.night_actions.clear()
    game.timer_cancel = asyncio.Event()

    # Мафия
    maf_ids = [u for u, p in game.players.items() if p["alive"] and p["role"] == "Мафия"]
    targets = [(f"🔪 {p['username']}", f"mafia_{u}") for u, p in game.players.items() if p["alive"] and p["role"] != "Мафия"]
    for m_id in maf_ids:
        await safe_send(m_id, "🕶 Мафия, выберите цель:", build_kb(targets), game)

    # Шериф
    sheriff_ids = [u for u, p in game.players.items() if p["alive"] and p["role"] == "Шериф"]
    s_targets = [(f"🔍 {p['username']}", f"sheriff_{u}") for u, p in game.players.items() if p["alive"] and u not in sheriff_ids]
    for s_id in sheriff_ids:
        await safe_send(s_id, "🕵️‍♂️ Шериф, кого проверить?", build_kb(s_targets), game)

    # Доктор
    doc_ids = [u for u, p in game.players.items() if p["alive"] and p["role"] == "Доктор"]
    d_targets = [(f"💊 {p['username']}", f"doctor_{u}") for u, p in game.players.items() if p["alive"]]
    for d_id in doc_ids:
        await safe_send(d_id, "⚕️ Доктор, кого лечить?", build_kb(d_targets), game)

    try:
        await asyncio.wait_for(game.timer_cancel.wait(), timeout=NIGHT_TIMEOUT)
    except asyncio.TimeoutError:
        pass
    await resolve_night(chat_id)

@dp.callback_query(F.data.startswith(("mafia_", "sheriff_", "doctor_")))
async def cb_night_action(callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_to_game:
        return await callback.answer("Вы не в игре.")
    
    chat_id = user_to_game[uid]
    game = games.get(chat_id)
    
    if not game or game.phase != "night":
        return await callback.answer("⚠️ Сейчас не время для ночных действий.", show_alert=True)

    action_type, target_id = callback.data.split("_")
    target_id = int(target_id)
    
    game.night_actions[action_type] = target_id
    await callback.answer("Выбор принят!")
    
    # Удаляем кнопки после выбора
    try:
        await bot.edit_message_text("✅ Действие выбрано. Ожидайте рассвета.", chat_id=uid, message_id=callback.message.message_id)
    except: pass

    # Если все активные роли походили — завершаем ночь раньше
    needed = set()
    if any(p["role"] == "Мафия" and p["alive"] for p in game.players.values()): needed.add("mafia")
    if any(p["role"] == "Шериф" and p["alive"] for p in game.players.values()): needed.add("sheriff")
    if any(p["role"] == "Доктор" and p["alive"] for p in game.players.values()): needed.add("doctor")
    
    if needed.issubset(set(game.night_actions.keys())):
        game.timer_cancel.set()

async def resolve_night(chat_id: int):
    game = games[chat_id]
    killed = game.night_actions.get("mafia")
    saved = game.night_actions.get("doctor")
    checked = game.night_actions.get("sheriff")

    res_msg = "☀️ **Наступило утро!**\n\n"
    
    if killed:
        if killed == saved:
            res_msg += "💊 Мафия пыталась совершить убийство, но Доктор спас беднягу!"
        else:
            game.players[killed]["alive"] = False
            res_msg += f"💀 Этой ночью была убита жертва: {game.players[killed]['username']} ({game.players[killed]['role']})"
    else:
        res_msg += "🌙 Ночь прошла на удивление спокойно, никто не погиб."

    await bot.send_message(chat_id, res_msg)

    if checked:
        is_maf = game.players[checked]["role"] == "Мафия"
        res_sh = "🔴 МАФИЯ" if is_maf else "🟢 МИРНЫЙ"
        for u, p in game.players.items():
            if p["role"] == "Шериф" and p["alive"]:
                await safe_send(u, f"🔍 Результат проверки: {game.players[checked]['username']} — {res_sh}")

    win = check_win(chat_id)
    if win: return await end_game(chat_id, win)

    await start_voting(chat_id)

async def start_voting(chat_id: int):
    game = games[chat_id]
    game.phase = "voting"
    game.votes.clear()
    game.voting_cancel = asyncio.Event()

    alive = [(f"🗳 {p['username']}", f"vote_{u}") for u, p in game.players.items() if p["alive"]]
    await bot.send_message(chat_id, f"📢 **Голосование!**\nУ вас есть {VOTING_TIMEOUT} сек., чтобы изгнать кого-то.", reply_markup=build_kb(alive))

    try:
        await asyncio.wait_for(game.voting_cancel.wait(), timeout=VOTING_TIMEOUT)
    except asyncio.TimeoutError:
        pass
    await resolve_votes(chat_id)

@dp.callback_query(F.data.startswith("vote_"))
async def cb_vote(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in games or games[chat_id].phase != "voting":
        return await callback.answer("Голосование закрыто.")
    
    game = games[chat_id]
    voter = callback.from_user.id
    if not game.players.get(voter, {}).get("alive"):
        return await callback.answer("Мертвые не голосуют!", show_alert=True)
    
    if voter in game.votes:
        return await callback.answer("Вы уже проголосовали.")

    target = int(callback.data.split("_")[1])
    game.votes[voter] = target
    await callback.answer("Голос учтен.")

    alive_count = sum(1 for p in game.players.values() if p["alive"])
    if len(game.votes) >= alive_count:
        game.voting_cancel.set()

async def resolve_votes(chat_id: int):
    game = games[chat_id]
    if not game.votes:
        await bot.send_message(chat_id, "⚖️ Никто не проголосовал. Город засыпает без жертв.")
    else:
        v_counts = {}
        for t in game.votes.values(): v_counts[t] = v_counts.get(t, 0) + 1
        max_v = max(v_counts.values())
        potential = [u for u, c in v_counts.items() if c == max_v]

        if len(potential) == 1:
            vic = potential[0]
            game.players[vic]["alive"] = False
            await bot.send_message(chat_id, f"🔨 Горожане решили изгнать {game.players[vic]['username']}.\nОн был: {game.players[vic]['role']}")
        else:
            await bot.send_message(chat_id, "⚖️ Голоса разделились. Никто не покинул город.")

    win = check_win(chat_id)
    if win: return await end_game(chat_id, win)
    
    await asyncio.sleep(3)
    await start_night(chat_id)

async def end_game(chat_id: int, result_text: str):
    game = games[chat_id]
    await bot.send_message(chat_id, f"🏁 **ИГРА ОКОНЧЕНА!**\n{result_text}")
    
    is_mafia_win = "Мафия победила" in result_text

    for uid, p in game.players.items():
        # Обновляем статистику
        init_stats(uid)
        user_stats[uid]["games"] += 1
        won = (p["role"] == "Мафия" and is_mafia_win) or (p["role"] != "Мафия" and not is_mafia_win)
        
        if won: user_stats[uid]["wins"] += 1
        else: user_stats[uid]["losses"] += 1
        
        # Убираем кнопки из последнего сообщения в ЛС
        if uid in game.last_private_messages:
            try:
                await bot.edit_message_reply_markup(chat_id=uid, message_id=game.last_private_messages[uid], reply_markup=None)
            except: pass

        # Отправляем личную инфу
        s = user_stats[uid]
        wr = f"{(s['wins']/s['games']*100):.1f}%" if s['games'] > 0 else "0%"
        stat_msg = (f"🏁 Игра завершена!\nРезультат: {'🏆 ПОБЕДА' if won else '💀 ПОРАЖЕНИЕ'}\n\n"
                    f"📊 Ваша обновленная статистика:\n"
                    f"🎮 Игр: {s['games']}\n"
                    f"🏆 Побед: {s['wins']}\n"
                    f"💀 Поражений: {s['losses']}\n"
                    f"📈 Винрейт: {wr}")
        await safe_send(uid, stat_msg)
        
        user_to_game.pop(uid, None)

    games.pop(chat_id, None)

# Render.com Health Check
async def handle_health(request):
    return web.Response(text='{"status": "alive"}', content_type='application/json')

async def main():
    # Запуск веб-сервера для Render
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
