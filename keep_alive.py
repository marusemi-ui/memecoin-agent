"""
keep_alive — основной Flask-сервер для Render.
Принимает webhook-запросы от Telegram и передаёт их в bot_app.
"""

import os
import logging
from threading import Thread

from flask import Flask, request, jsonify
from telegram import Update

import telegram_bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/", methods=["GET"])
def home():
    return "Memecoin Agent bot is running"


@app.route("/health", methods=["GET"])
def health():
    """Health check для UptimeRobot."""
    return "OK", 200


@app.route(f"/webhook/{os.getenv('TELEGRAM_BOT_TOKEN', '')}", methods=["POST"])
def webhook():
    """Принимает обновления от Telegram."""
    try:
        data = request.get_json(force=True)
        if not data:
            return "no data", 400

        # Обрабатываем через Application из telegram_bot
        update = Update.de_json(data, telegram_bot.app.bot)

        # Запускаем обработку асинхронно
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(telegram_bot.app.process_update(update))
        loop.close()

        return "OK", 200
    except Exception as e:
        logger.exception(f"Ошибка обработки webhook: {e}")
        return "error", 500


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    """Устанавливает webhook (запускается один раз)."""
    try:
        from telegram import Bot
        bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
        render_url = os.getenv("RENDER_EXTERNAL_URL", "")
        if not render_url:
            return "RENDER_EXTERNAL_URL не задан", 500

        webhook_url = f"{render_url}/webhook/{os.getenv('TELEGRAM_BOT_TOKEN', '')}"
        result = bot.set_webhook(url=webhook_url)

        if result:
            return f"Webhook установлен: {webhook_url}", 200
        return "Не удалось установить webhook", 500
    except Exception as e:
        logger.exception(f"Ошибка установки webhook: {e}")
        return f"Ошибка: {e}", 500


def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    """Совместимость со старым кодом."""
    t = Thread(target=run)
    t.start()