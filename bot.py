"""
Хаб-бот: единая точка входа в личный рабочий стол Telegram-ботов Зураба.

Сам по себе не дублирует функциональность других ботов — только:
- показывает меню с кнопками-переходами в «Главное с почты за день» / Визит КМВ / Авито / Авиабилеты
  (у ботов нет доступа к чужим чатам, поэтому реальное встраивание одного
  бота в другого невозможно — кнопка просто открывает нужный чат);
- собирает "живой" статус по тем ботам, чьи данные доступны для чтения:
  дайджест (last_sent.json из репозитория gmail-telegram-agent на GitHub)
  и Визит КМВ (та же Google Таблица, что заполняет сам бот, доступ на чтение
  через тот же сервисный аккаунт).
"""

import asyncio
import datetime
import html
import json
import logging
import os

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import KeyboardButtonStyle, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hub")
# httpx logs every request URL, and Telegram's URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

DIGEST_BOT_USERNAME = "workmtsmeg_bot"
VISIT_BOT_USERNAME = "megafonkmvbot"
AVITO_BOT_USERNAME = "Avitozurab_bot"
FLIGHTS_BOT_USERNAME = "TRIPZUbot"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = "israilovzurab-sys/gmail-telegram-agent"

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_FILE = os.path.join(SCRIPT_DIR, "service_account.json")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
SHEET_NAME = os.environ.get("SHEET_NAME", "Лист1")


WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
DIGEST_STALE_AFTER = datetime.timedelta(hours=36)


def _menu_keyboard(now: datetime.datetime):
    # Short labels in a 2x2 grid: four full-width rows with long captions
    # looked like a form, not a dashboard.
    return InlineKeyboardMarkup([
        # Telegram only offers three button colours; each is the closest to
        # the service's real logo: Gmail red, MegaFon green, Avito blue.
        [
            InlineKeyboardButton("📨 Почта", url=f"https://t.me/{DIGEST_BOT_USERNAME}", style=KeyboardButtonStyle.RED),
            InlineKeyboardButton("📋 Визиты", url=f"https://t.me/{VISIT_BOT_USERNAME}", style=KeyboardButtonStyle.GREEN),
        ],
        [
            InlineKeyboardButton("🏪 Авито", url=f"https://t.me/{AVITO_BOT_USERNAME}", style=KeyboardButtonStyle.BLUE),
            InlineKeyboardButton("🎫 Билеты", url=f"https://t.me/{FLIGHTS_BOT_USERNAME}", style=KeyboardButtonStyle.BLUE),
        ],
        [InlineKeyboardButton(f"↻ Обновить · {now:%H:%M}", callback_data="status")],
    ])


def _greeting(now: datetime.datetime) -> str:
    h = now.hour
    if 5 <= h < 12:
        return "Доброе утро"
    if 12 <= h < 18:
        return "Добрый день"
    if 18 <= h < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def _date_ru(now: datetime.datetime) -> str:
    return f"{WEEKDAYS_RU[now.weekday()]}, {now.day} {MONTHS_RU[now.month - 1]}"


def _when(dt: datetime.datetime, now: datetime.datetime) -> str:
    days = (now.date() - dt.date()).days
    day = "сегодня" if days == 0 else "вчера" if days == 1 else dt.strftime("%d.%m")
    return f"{day} в {dt:%H:%M}"


def _short(text: str, limit: int = 64) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _fetch_digest_status():
    """Читает last_sent.json из репозитория дайджест-бота на GitHub — сам
    бот пишет туда file_id и метаданные после каждой реальной отправки
    (см. daily_summary.py в gmail-telegram-agent)."""
    if not GITHUB_TOKEN:
        return "доступ к GitHub не настроен"
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/last_sent.json",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.raw+json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        info = json.loads(resp.content)
        sent_at = info["sent_at"].replace("Z", "+00:00")
        local_time = datetime.datetime.fromisoformat(sent_at).astimezone(MOSCOW_TZ)
        now = datetime.datetime.now(MOSCOW_TZ)
        stale = "⚠️ " if now - local_time > DIGEST_STALE_AFTER else ""
        return (
            f"{stale}Последняя сводка — {_when(local_time, now)}\n"
            f"<i>«{html.escape(_short(info['caption']))}»</i>"
        )
    except Exception:
        log.exception("Не удалось получить статус дайджеста")
        return "не удалось получить данные"


def _fetch_visit_status():
    """Считает визиты, внесённые сегодня (по московской дате), читая ту же
    таблицу, куда пишет бот Визит КМВ — только колонку timestamp, без
    выгрузки всей таблицы."""
    if not GOOGLE_SHEET_ID or not (GOOGLE_SERVICE_ACCOUNT_JSON or os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE)):
        return "доступ к таблице не настроен"
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            creds = Credentials.from_service_account_info(
                json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=scopes,
            )
        else:
            creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet(SHEET_NAME)
        timestamps = sheet.col_values(1)[1:]  # без заголовка
        now = datetime.datetime.now(MOSCOW_TZ)
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        today_count = sum(1 for ts in timestamps if ts.startswith(today))
        yesterday_count = sum(1 for ts in timestamps if ts.startswith(yesterday))
        return f"Сегодня <b>{today_count}</b> · вчера {yesterday_count} · всего {len(timestamps)}"
    except Exception:
        log.exception("Не удалось получить статус визитов")
        return "не удалось получить данные"


def _render_status(now: datetime.datetime) -> str:
    return (
        "<b>ZURAB WORKSPACE</b>\n"
        f"<i>{_greeting(now)} · {_date_ru(now)}</i>\n\n"
        f"<blockquote>📨 <b>Главное с почты</b>\n{_fetch_digest_status()}</blockquote>\n"
        f"<blockquote>📋 <b>Визиты КМВ</b>\n{_fetch_visit_status()}</blockquote>\n"
        "<blockquote>🏪 <b>Авито</b>  ·  🎫 <b>Билеты</b>\n"
        "Сами пишут, когда есть что показать</blockquote>"
    )


async def _send_status(message_target, edit=False):
    now = datetime.datetime.now(MOSCOW_TZ)
    text = _render_status(now)
    kwargs = {"parse_mode": ParseMode.HTML, "reply_markup": _menu_keyboard(now)}
    if edit:
        try:
            await message_target.edit_text(text, **kwargs)
        except BadRequest as e:
            # Pressing "refresh" twice within a minute produces identical
            # content; Telegram rejects that, and it is not an error for us.
            if "not modified" not in str(e).lower():
                raise
    else:
        await message_target.reply_text(text, **kwargs)


def _is_allowed(update: Update) -> bool:
    return not ALLOWED_USER_IDS or update.effective_chat.id in ALLOWED_USER_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    await _send_status(update.message, edit=False)


async def on_status_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer("Обновляю…")
    await _send_status(query.message, edit=True)


async def _poll_for(app, seconds):
    # GitHub Actions jobs can't run forever: poll for a fixed window, then
    # stop cleanly (stop() confirms handled updates so the next run doesn't
    # answer them twice).
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.sleep(seconds)
        await app.updater.stop()
        await app.stop()


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("status", cmd_start))
    app.add_handler(CallbackQueryHandler(on_status_button, pattern="^status$"))
    log.info("Хаб-бот запущен, жду сообщений...")
    window = int(os.environ.get("HUB_RUN_SECONDS", "0"))
    if window:
        asyncio.run(_poll_for(app, window))
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
