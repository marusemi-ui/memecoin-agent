"""
keep_alive — простой Flask-сервер для UptimeRobot.
Запускается в отдельном потоке, пока бот работает через polling.
"""

import os
import logging
from threading import Thread

from flask import Flask

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
    return "OK", 200


def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run)
    t.start()