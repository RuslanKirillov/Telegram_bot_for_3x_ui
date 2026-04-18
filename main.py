import os
from dotenv import load_dotenv
load_dotenv()

import logging
import sqlite3
import requests
import urllib3
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── CONFIG ──────────────────────────────────────────────────────────────────

PANEL_URL        = os.getenv("PANEL_URL")
PANEL_USER       = os.getenv("PANEL_USER")
PANEL_PASS       = os.getenv("PANEL_PASS")
BOT_TOKEN        = os.getenv("BOT_TOKEN")
ADMIN_ID         = int(os.getenv("ADMIN_ID"))

TZ               = ZoneInfo("Europe/Moscow")   # МСК UTC+3
CHECK_INTERVAL_H = 6
NOTIFY_DAYS      = [7, 3, 1, 0]
DB_PATH          = os.getenv("DB_PATH", "vpn_bot.db")

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("vpn_bot")

# ─── DATABASE ────────────────────────────────────────────────────────────────

def db_init():
    with sqlite3.connect(DB_PATH) as conn:
        # Пользователи — просто регистрация факта что писал боту
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id  INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        # Подписки — много на одного пользователя
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER NOT NULL,
                sub_id   TEXT NOT NULL,
                label    TEXT,              -- человекочитаемое название, напр. "Домашний"
                UNIQUE(chat_id, sub_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notif_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER,
                sub_id    TEXT,
                days_left INTEGER,
                sent_at   TEXT
            )
        """)
        conn.commit()

# --- users ---

def db_register_user(chat_id: int, username: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO users (chat_id, username)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username
        """, (chat_id, username))
        conn.commit()

def db_all_known_users():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT chat_id, username FROM users ORDER BY chat_id"
        ).fetchall()

def db_get_username(chat_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row[0] if row else str(chat_id)

# --- subscriptions ---

def db_get_subs(chat_id: int) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, sub_id, label FROM subscriptions WHERE chat_id=? ORDER BY id",
            (chat_id,)
        ).fetchall()

def db_add_sub(chat_id: int, sub_id: str, label: str = "") -> bool:
    """Добавляет подписку. Возвращает False если уже существует."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO subscriptions (chat_id, sub_id, label) VALUES (?, ?, ?)",
                (chat_id, sub_id, label)
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def db_remove_sub(chat_id: int, sub_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM subscriptions WHERE chat_id=? AND sub_id=?",
            (chat_id, sub_id)
        )
        conn.commit()

def db_all_subs() -> list:
    """Все подписки всех пользователей."""
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT chat_id, sub_id, label FROM subscriptions"
        ).fetchall()

def db_already_notified(chat_id: int, sub_id: str, days_left: int) -> bool:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT 1 FROM notif_log
            WHERE chat_id=? AND sub_id=? AND days_left=? AND DATE(sent_at)=?
        """, (chat_id, sub_id, days_left, today)).fetchone()
    return row is not None

def db_log_notif(chat_id: int, sub_id: str, days_left: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO notif_log (chat_id, sub_id, days_left, sent_at) VALUES (?, ?, ?, ?)",
            (chat_id, sub_id, days_left, datetime.now(TZ).isoformat())
        )
        conn.commit()

# ─── 3x-ui API ───────────────────────────────────────────────────────────────

SESSION = requests.Session()

def panel_login() -> bool:
    try:
        r = SESSION.post(
            f"{PANEL_URL}/login",
            json={"username": PANEL_USER, "password": PANEL_PASS},
            timeout=10, verify=False
        )
        if r.json().get("success"):
            log.info("Залогинился в панель")
            return True
        log.warning("Логин провалился: %s", r.text)
        return False
    except Exception as e:
        log.error("Ошибка логина: %s", e)
        return False

def panel_get_clients() -> list[dict]:                                                              
      for attempt in range(2):
          try:                                                                                        
              r = SESSION.get(                                                                      
                  f"{PANEL_URL}/xui/API/inbounds/list",
                  timeout=15, verify=False
              )                                                                                       
              # Сессия истекла: 3x-ui может вернуть 401, 302 или пустой body
              if attempt == 0 and (r.status_code in (401, 302) or not r.content.strip()):             
                  log.info("Сессия истекла, перелогиниваюсь...")                                      
                  panel_login()
                  continue                                                                            
              if not r.content.strip():                                                             
                  log.error("Пустой ответ от панели после повторного логина")
                  return []                                                                           
              data = r.json()
              if not data.get("success"):                                                             
                  log.warning("API: %s", data.get("msg"))                                           
                  return []                                                                           
              clients = []
              for inbound in data.get("obj", []):                                                     
                  try:                                                                                
                      settings = json.loads(inbound.get("settings", "{}"))
                  except json.JSONDecodeError:                                                        
                      continue                                                                      
                  for c in settings.get("clients", []):                                               
                      clients.append({
                          **c,                                                                        
                          "inbound_tag": inbound.get("remark", ""),                                 
                          "inbound_id":  inbound.get("id"),                                           
                      })
              return clients                                                                          
          except Exception as e:                                                                    
              log.error("Ошибка получения клиентов: %s", e)
              return []                                                                               
      return []

def find_by_sub_id(clients: list[dict], sub_id: str) -> dict | None:
    sub_id = sub_id.strip().lower()
    for c in clients:
        if c.get("subId", "").lower() == sub_id:
            return c
    return None

def panel_extend_client(sub_id: str, extra_days: int) -> bool:
    """Продлевает подписку на extra_days дней от текущего срока действия."""
    clients = panel_get_clients()
    client = find_by_sub_id(clients, sub_id)
    if not client:
        return False

    inbound_id  = client.get("inbound_id")
    client_uuid = client.get("id")

    now_ms          = int(datetime.now(timezone.utc).timestamp() * 1000)
    current_expiry  = client.get("expiryTime", 0)
    base_ms         = max(current_expiry, now_ms) if current_expiry > 0 else now_ms
    new_expiry      = base_ms + extra_days * 24 * 3600 * 1000

    updated = {k: v for k, v in client.items() if k not in ("inbound_tag", "inbound_id")}
    updated["expiryTime"] = new_expiry

    for attempt in range(2):
        try:
            r = SESSION.post(
                f"{PANEL_URL}/xui/API/inbounds/{inbound_id}/updateClient/{client_uuid}",
                json={"id": inbound_id, "settings": json.dumps({"clients": [updated]})},
                timeout=10, verify=False
            )
            if attempt == 0 and (r.status_code in (401, 302) or not r.content.strip()):
                log.info("Сессия истекла при продлении, перелогиниваюсь...")
                panel_login()
                continue
            return r.json().get("success", False)
        except Exception as e:
            log.error("Ошибка продления subId=%s: %s", sub_id, e)
            return False
    return False

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def days_until_expiry(expiry_ms: int) -> int | None:
    if not expiry_ms:
        return None
    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
    return (expiry_dt - datetime.now(timezone.utc)).days

def format_expiry(expiry_ms: int, days: int | None) -> str:
    if days is None:
        return "∞ бессрочно"
    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=TZ)
    date_str = expiry_dt.strftime("%d.%m.%Y %H:%M")
    if days < 0:
        return f"⛔ истёк {abs(days)} дн. назад ({date_str} МСК)"
    if days == 0:
        return f"🔴 истекает сегодня ({date_str} МСК)"
    return f"{date_str} МСК (через {days} дн.)"

def build_notify_text(days: int, name: str, label: str, expiry_ms: int) -> str:
    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=TZ)
    date_str = expiry_dt.strftime("%d.%m.%Y %H:%M")
    title = f"`{label}`" if label else f"`{name}`"
    if days <= 0:
        return (
            f"🔴 *Подписка истекла!*\n\n"
            f"Аккаунт: {title}\n"
            f"Истёк: {date_str} МСК\n\n"
            f"Продли подписку — соединение уже не работает."
        )
    elif days == 1:
        return (
            f"🟠 *Последний день подписки!*\n\n"
            f"Аккаунт: {title}\n"
            f"Истекает: {date_str} МСК\n\n"
            f"Не тяни — продли сегодня."
        )
    elif days <= 3:
        return (
            f"🟡 *Подписка истекает через {days} дн.*\n\n"
            f"Аккаунт: {title}\n"
            f"Дата: {date_str} МСК\n\n"
            f"Стоит продлить заранее."
        )
    else:
        return (
            f"🔔 *Напоминание: подписка истекает через {days} дн.*\n\n"
            f"Аккаунт: {title}\n"
            f"Дата: {date_str} МСК\n\n"
            f"Ещё есть время — продли когда будет удобно."
        )

# ─── KEYBOARDS ───────────────────────────────────────────────────────────────

def kb_main(has_subs: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔌 Получить подписку", callback_data="buy_vpn")],
        [InlineKeyboardButton("📊 Мои подписки", callback_data="my_subs")],
        [InlineKeyboardButton("➕ Привязать subId", callback_data="add_sub")],
    ]
    if has_subs:
        buttons.append([InlineKeyboardButton("❌ Отвязать подписку", callback_data="unlink_menu")])
    return InlineKeyboardMarkup(buttons)

def kb_unlink_list(subs: list) -> InlineKeyboardMarkup:
    buttons = []
    for _, sub_id, label in subs:
        btn_label = label or sub_id
        buttons.append([InlineKeyboardButton(f"❌ {btn_label}", callback_data=f"unlink_ask:{sub_id}")])
    buttons.append([InlineKeyboardButton("← Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def kb_unlink_confirm(sub_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, отвязать", callback_data=f"unlink_confirm:{sub_id}"),
        InlineKeyboardButton("← Отмена", callback_data="unlink_menu"),
    ]])

def kb_subs_list(subs: list, clients: list[dict]) -> InlineKeyboardMarkup:
    """Кнопка на каждую подписку — нажимаешь, видишь детали."""
    buttons = []
    for row_id, sub_id, label in subs:
        client = find_by_sub_id(clients, sub_id)
        if client:
            days = days_until_expiry(client.get("expiryTime", 0))
            if days is None:
                icon = "♾️"
            elif days <= 0:
                icon = "🔴"
            elif days <= 3:
                icon = "🟡"
            elif days <= 7:
                icon = "🟠"
            else:
                icon = "🟢"
        else:
            icon = "❓"
        btn_label = label or client.get("email", sub_id) if client else sub_id
        buttons.append([InlineKeyboardButton(
            f"{icon} {btn_label}",
            callback_data=f"sub_detail:{sub_id}"
        )])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="my_subs")])
    return InlineKeyboardMarkup(buttons)

# ─── SCHEDULER ───────────────────────────────────────────────────────────────

async def run_check(app: Application):
    log.info("Плановая проверка...")
    all_clients = panel_get_clients()
    if not all_clients:
        log.warning("Не удалось получить клиентов из панели")
        if ADMIN_ID:
            try:
                await app.bot.send_message(ADMIN_ID, "⚠️ Не удалось получить данные из 3x-ui панели")
            except Exception:
                pass
        return

    for chat_id, sub_id, label in db_all_subs():
        client = find_by_sub_id(all_clients, sub_id)
        if not client:
            log.warning("subId %s не найден в панели", sub_id)
            continue

        days = days_until_expiry(client.get("expiryTime", 0))
        if days is None:
            continue

        notify_day = None
        for threshold in NOTIFY_DAYS:
            if days <= threshold:
                notify_day = threshold
                break
        if notify_day is None:
            continue

        if db_already_notified(chat_id, sub_id, notify_day):
            continue

        name = client.get("email") or sub_id
        text = build_notify_text(days, name, label or "", client["expiryTime"])
        try:
            await app.bot.send_message(chat_id, text, parse_mode="Markdown")
            db_log_notif(chat_id, sub_id, notify_day)
            log.info("Уведомление → chat_id=%d sub=%s days=%d", chat_id, sub_id, days)
        except Exception as e:
            log.error("Ошибка отправки chat_id=%d: %s", chat_id, e)

# ─── HANDLERS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or ""
    db_register_user(chat_id, username)

    subs = db_get_subs(chat_id)
    if subs:
        await update.message.reply_text(
            f"👋 Привет! У тебя {len(subs)} подписок привязано.\n\n"
            f"Выбери действие в меню ниже:",
            reply_markup=kb_main(has_subs=True)
        )
    else:
        await update.message.reply_text(
            "👋 Привет! Буду напоминать когда заканчивается подписка.\n\n"
            "Чтобы начать — нажми кнопку ниже и введи свой subId.\n"
            "SubId можно получить у администратора.",
            reply_markup=kb_main(has_subs=False)
        )

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or ""
    db_register_user(chat_id, username)

    if not context.args:
        await update.message.reply_text(
            "Укажи subId: `/link твой_subId`\n"
            "Чтобы добавить метку: `/link твой_subId Домашний`",
            parse_mode="Markdown"
        )
        return

    sub_id = context.args[0].strip().lower()
    label = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    await update.message.reply_text("🔍 Проверяю в панели...")

    clients = panel_get_clients()
    client = find_by_sub_id(clients, sub_id)
    if not client:
        await update.message.reply_text(
            f"❌ Аккаунт с subId `{sub_id}` не найден.\nПроверь правильность ID.",
            parse_mode="Markdown"
        )
        return

    added = db_add_sub(chat_id, sub_id, label)
    if not added:
        await update.message.reply_text(
            f"ℹ️ Подписка `{sub_id}` уже привязана к твоему аккаунту.",
            parse_mode="Markdown"
        )
        return

    name = client.get("email") or sub_id
    days = days_until_expiry(client.get("expiryTime", 0))
    expiry_str = format_expiry(client.get("expiryTime", 0), days)
    display = label if label else name

    await update.message.reply_text(
        f"✅ Подписка *{display}* привязана!\n\n"
        f"📅 Действует до: {expiry_str}\n\n"
        f"Буду напоминать за 7, 3, 1 день и в день истечения.",
        parse_mode="Markdown",
        reply_markup=kb_main()
    )

async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        subs = db_get_subs(chat_id)
        if not subs:
            await update.message.reply_text("У тебя нет привязанных подписок.")
            return
        lines = ["Твои подписки (укажи subId для отвязки):\n"]
        for _, sub_id, label in subs:
            lines.append(f"• `{sub_id}`" + (f" — {label}" if label else ""))
        await update.message.reply_text(
            "\n".join(lines) + "\n\nИспользование: `/unlink subId`",
            parse_mode="Markdown"
        )
        return

    sub_id = context.args[0].strip().lower()
    subs = db_get_subs(chat_id)
    if not any(s[1] == sub_id for s in subs):
        await update.message.reply_text(f"❌ Подписка `{sub_id}` не найдена.", parse_mode="Markdown")
        return

    db_remove_sub(chat_id, sub_id)
    await update.message.reply_text(f"✅ Подписка `{sub_id}` отвязана.", parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = db_get_subs(chat_id)

    if not subs:
        await update.message.reply_text(
            "У тебя нет привязанных подписок.\nДобавь через `/link твой_subId`",
            parse_mode="Markdown"
        )
        return

    clients = panel_get_clients()
    await update.message.reply_text(
        "Выбери подписку чтобы посмотреть детали:",
        reply_markup=kb_subs_list(subs, clients)
    )

# ─── TEXT MESSAGE HANDLER ────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текст когда бот ждёт ввода subId."""
    if not context.user_data.get("awaiting_link"):
        return

    context.user_data.pop("awaiting_link")
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or ""
    db_register_user(chat_id, username)

    parts = update.message.text.strip().split(maxsplit=1)
    sub_id = parts[0].lower()
    label = parts[1] if len(parts) > 1 else ""

    await update.message.reply_text("🔍 Проверяю в панели...")

    clients = panel_get_clients()
    client = find_by_sub_id(clients, sub_id)
    if not client:
        subs = db_get_subs(chat_id)
        await update.message.reply_text(
            f"❌ Аккаунт с subId `{sub_id}` не найден.\nПроверь правильность ID.",
            parse_mode="Markdown",
            reply_markup=kb_main(has_subs=bool(subs))
        )
        return

    added = db_add_sub(chat_id, sub_id, label)
    if not added:
        subs = db_get_subs(chat_id)
        await update.message.reply_text(
            f"ℹ️ Подписка `{sub_id}` уже привязана.",
            parse_mode="Markdown",
            reply_markup=kb_main(has_subs=bool(subs))
        )
        return

    name = client.get("email") or sub_id
    days = days_until_expiry(client.get("expiryTime", 0))
    expiry_str = format_expiry(client.get("expiryTime", 0), days)
    display = label if label else name

    await update.message.reply_text(
        f"✅ Подписка *{display}* привязана!\n\n"
        f"📅 Действует до: {expiry_str}\n\n"
        f"Буду напоминать за 7, 3, 1 день и в день истечения.",
        parse_mode="Markdown",
        reply_markup=kb_main(has_subs=True)
    )

# ─── CALLBACK HANDLERS ───────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "buy_vpn":
        subs = db_get_subs(chat_id)
        await query.edit_message_text(
            "🔌 *Получить подписку*\n\n"
            "⚙️ Эта функция сейчас в разработке.\n\n"
            "Скоро здесь можно будет купить подписку прямо в боте!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Назад", callback_data="back_main")
            ]])
        )

    elif data == "add_sub":
        context.user_data["awaiting_link"] = True
        subs = db_get_subs(chat_id)
        await query.edit_message_text(
            "➕ *Привязать подписку*\n\n"
            "Введи свой subId в чат.\n"
            "Чтобы добавить метку — введи через пробел: `subId Домашний`\n\n"
            "_SubId можно получить у администратора._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Отмена", callback_data="back_main")
            ]])
        )

    elif data == "unlink_menu":
        subs = db_get_subs(chat_id)
        if not subs:
            await query.edit_message_text(
                "У тебя нет привязанных подписок.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("← Назад", callback_data="back_main")
                ]])
            )
            return
        await query.edit_message_text(
            "Выбери подписку для отвязки:",
            reply_markup=kb_unlink_list(subs)
        )

    elif data.startswith("unlink_ask:"):
        sub_id = data.split(":", 1)[1]
        subs = db_get_subs(chat_id)
        label = next((s[2] for s in subs if s[1] == sub_id), "") or sub_id
        await query.edit_message_text(
            f"Отвязать подписку *{label}*?",
            parse_mode="Markdown",
            reply_markup=kb_unlink_confirm(sub_id)
        )

    elif data.startswith("unlink_confirm:"):
        sub_id = data.split(":", 1)[1]
        subs = db_get_subs(chat_id)
        label = next((s[2] for s in subs if s[1] == sub_id), "") or sub_id
        db_remove_sub(chat_id, sub_id)
        subs_after = db_get_subs(chat_id)
        await query.edit_message_text(
            f"✅ Подписка *{label}* отвязана.",
            parse_mode="Markdown",
            reply_markup=kb_main(has_subs=bool(subs_after))
        )

    elif data == "back_main":
        context.user_data.pop("awaiting_link", None)
        subs = db_get_subs(chat_id)
        await query.edit_message_text(
            "Выбери действие:",
            reply_markup=kb_main(has_subs=bool(subs))
        )

    elif data == "my_subs":
        subs = db_get_subs(chat_id)
        if not subs:
            await query.edit_message_text(
                "У тебя нет привязанных подписок.\nНажми «Привязать subId» чтобы добавить.",
                reply_markup=kb_main(has_subs=False)
            )
            return
        clients = panel_get_clients()
        await query.edit_message_text(
            "Выбери подписку чтобы посмотреть детали:",
            reply_markup=kb_subs_list(subs, clients)
        )

    elif data.startswith("sub_detail:"):
        sub_id = data.split(":", 1)[1]
        subs = db_get_subs(chat_id)
        label = next((s[2] for s in subs if s[1] == sub_id), "")

        clients = panel_get_clients()
        client = find_by_sub_id(clients, sub_id)

        if not client:
            await query.edit_message_text(
                f"❌ Аккаунт `{sub_id}` не найден в панели.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("← Назад", callback_data="my_subs")
                ]])
            )
            return

        name = client.get("email") or sub_id
        display = label if label else name
        days = days_until_expiry(client.get("expiryTime", 0))
        expiry_str = format_expiry(client.get("expiryTime", 0), days)
        status_icon = "🟢" if client.get("enable") else "🔴"
        status_text = "активен" if client.get("enable") else "отключён"
        total_gb = client.get("totalGB", 0)
        traffic_str = f"{total_gb // (1024**3)} GB лимит" if total_gb > 0 else "без ограничений"

        text = (
            f"📊 *{display}*\n\n"
            f"Статус: {status_icon} {status_text}\n"
            f"📅 Действует до: {expiry_str}\n"
            f"📦 Трафик: {traffic_str}\n"
            f"🏷️ Сервер: {client.get('inbound_tag', '—')}\n\n"
            f"_Время указано по МСК (UTC+3)_"
        )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Назад", callback_data="my_subs"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"sub_detail:{sub_id}"),
            ]])
        )

# ─── ADMIN HANDLERS ──────────────────────────────────────────────────────────

async def cmd_admin_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /admin_link <chat_id> <subId> [метка]
    Привязывает подписку пользователю от имени админа.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    if len(context.args) < 2:
        known = db_all_known_users()
        lines = ["*Использование:* `/admin_link <chat_id> <subId> [метка]`\n"]
        if known:
            lines.append("*Известные пользователи:*")
            for chat_id, username in known:
                subs = db_get_subs(chat_id)
                name = f"@{username}" if username and not username.isdigit() else f"id:{chat_id}"
                sub_info = f"{len(subs)} подп." if subs else "нет подписок"
                lines.append(f"• {name} `{chat_id}` — {sub_info}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ chat_id должен быть числом.")
        return

    sub_id = context.args[1].strip().lower()
    label = " ".join(context.args[2:]) if len(context.args) > 2 else ""

    await update.message.reply_text("🔍 Проверяю в панели...")
    clients = panel_get_clients()
    client = find_by_sub_id(clients, sub_id)

    if not client:
        await update.message.reply_text(
            f"❌ subId `{sub_id}` не найден в панели.",
            parse_mode="Markdown"
        )
        return

    # Регистрируем пользователя если ещё нет
    existing_username = db_get_username(target_id)
    db_register_user(target_id, existing_username)
    added = db_add_sub(target_id, sub_id, label)

    name = client.get("email") or sub_id
    days = days_until_expiry(client.get("expiryTime", 0))
    expiry_str = format_expiry(client.get("expiryTime", 0), days)
    display = label if label else name

    if not added:
        await update.message.reply_text(
            f"ℹ️ Подписка `{sub_id}` уже была привязана к `{target_id}`.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"✅ Готово!\n\n"
        f"Пользователь: `{target_id}`\n"
        f"Подписка: *{display}*\n"
        f"Действует до: {expiry_str}",
        parse_mode="Markdown"
    )
    log.info("Админ привязал chat_id=%d → subId=%s label=%s", target_id, sub_id, label)

    # Уведомляем пользователя
    try:
        await context.bot.send_message(
            target_id,
            f"✅ Администратор добавил тебе подписку *{display}*.\n\n"
            f"📅 Действует до: {expiry_str}\n\n"
            f"Нажми «Мои подписки» чтобы посмотреть детали.",
            parse_mode="Markdown",
            reply_markup=kb_main(has_subs=True)
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Привязка сохранена, но уведомить пользователя не удалось "
            "(возможно он ещё не писал боту).",
            parse_mode="Markdown"
        )

async def cmd_admin_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin_unlink <chat_id> <subId>"""
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: `/admin_unlink <chat_id> <subId>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ chat_id должен быть числом.")
        return
    sub_id = context.args[1].strip().lower()
    db_remove_sub(target_id, sub_id)
    await update.message.reply_text(f"✅ Подписка `{sub_id}` отвязана от `{target_id}`.", parse_mode="Markdown")

async def cmd_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    known = db_all_known_users()
    if not known:
        await update.message.reply_text("Пока никто не писал боту.")
        return

    clients = panel_get_clients()
    lines = [f"*Все пользователи ({len(known)}):*\n"]
    for chat_id, username in known:
        name = f"@{username}" if username and not username.isdigit() else f"id:{chat_id}"
        subs = db_get_subs(chat_id)
        if not subs:
            lines.append(f"⚪ {name} (`{chat_id}`) — нет подписок")
            continue
        lines.append(f"\n👤 {name} (`{chat_id}`):")
        for _, sub_id, label in subs:
            client = find_by_sub_id(clients, sub_id)
            if client:
                days = days_until_expiry(client.get("expiryTime", 0))
                expiry_str = format_expiry(client.get("expiryTime", 0), days)
                icon = "🟢" if (days is None or days > 7) else ("🟡" if days > 1 else "🔴")
                vpn_name = label or client.get("email") or sub_id
                lines.append(f"  {icon} {vpn_name} — {expiry_str}")
            else:
                lines.append(f"  ❓ {label or sub_id} — не найден в панели")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🔄 Запускаю проверку...")
    await run_check(context.application)
    await update.message.reply_text("✅ Готово")

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    import asyncio

    db_init()
    if not panel_login():
        log.warning("Не удалось залогиниться в панель при старте")

    scheduler = AsyncIOScheduler(timezone=str(TZ))

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("link",         cmd_link))
    app.add_handler(CommandHandler("unlink",       cmd_unlink))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("check",        cmd_admin_check))
    app.add_handler(CommandHandler("admin_link",   cmd_admin_link))
    app.add_handler(CommandHandler("admin_unlink", cmd_admin_unlink))
    app.add_handler(CommandHandler("admin_users",  cmd_admin_users))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async with app:
        await app.start()

        scheduler.add_job(
            run_check,
            trigger="interval",
            hours=CHECK_INTERVAL_H,
            args=[app],
            id="vpn_check",
            next_run_time=datetime.now()
        )
        scheduler.start()
        log.info("Планировщик запущен, интервал %dч", CHECK_INTERVAL_H)

        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Бот запущен")

        await asyncio.Event().wait()  # ждём Ctrl+C

        await app.updater.stop()
        if scheduler.running:
            scheduler.shutdown()
        await app.stop()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
