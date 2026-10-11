"""
Watcher — фоновый мониторинг кошельков.
Каждые N секунд проверяет новые транзакции в отслеживаемых кошельках
и присылает сигналы в Telegram при новых покупках.
"""

import os
import json
import time
import asyncio
import logging
import requests
from dotenv import load_dotenv

from aggregator import evaluate_signal

load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not HELIUS_API_KEY:
    raise ValueError("Нет HELIUS_API_KEY в .env!")

logger = logging.getLogger(__name__)

WATCH_FILE = "watched_wallets.json"
BASE_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Интервал проверки (в секундах)
CHECK_INTERVAL = 300  # 5 минут

# ─────────────────────────────────────────────
# НАСТРОЙКИ ФИЛЬТРАЦИИ
# ─────────────────────────────────────────────

# Присылать ли WEAK-сигналы (0.45 ≤ confidence < 0.65)
NOTIFY_WEAK = True

# Присылать ли IGNORE-сигналы (confidence < 0.45)
NOTIFY_IGNORE = False

# Присылать ли REJECT-сигналы (Safety Gate отклонил)
NOTIFY_REJECT = False


# ─────────────────────────────────────────────
# Хранилище
# ─────────────────────────────────────────────

def load_watch_data():
    if not os.path.exists(WATCH_FILE):
        return {}
    try:
        with open(WATCH_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка чтения {WATCH_FILE}: {e}")
        return {}


def save_watch_data(data):
    try:
        with open(WATCH_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка записи {WATCH_FILE}: {e}")


def add_wallet(wallet: str):
    data = load_watch_data()
    if wallet in data:
        return False
    data[wallet] = {
        "added_at": int(time.time()),
        "last_signature": None,
    }
    save_watch_data(data)
    return True


def remove_wallet(wallet: str):
    data = load_watch_data()
    if wallet not in data:
        return False
    del data[wallet]
    save_watch_data(data)
    return True


def list_wallets():
    return list(load_watch_data().keys())


# ─────────────────────────────────────────────
# Проверка транзакций
# ─────────────────────────────────────────────

def fetch_recent_swaps(address: str, limit: int = 20):
    url = BASE_URL.format(address=address)
    params = {
        "api-key": HELIUS_API_KEY,
        "limit": limit,
        "type": "SWAP",
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Ошибка загрузки свопов для {address}: {e}")
        return []


def parse_buy(tx, wallet_address):
    if tx.get("type") != "SWAP":
        return None

    transfers = tx.get("tokenTransfers", [])
    if not transfers:
        return None

    sent_tokens = []
    received_tokens = []
    sol_sent = 0.0

    for t in transfers:
        mint = t.get("mint", "")
        amount = float(t.get("tokenAmount", 0) or 0)
        from_acc = t.get("fromUserAccount", "")
        to_acc = t.get("toUserAccount", "")

        if from_acc == wallet_address:
            if mint == SOL_MINT:
                sol_sent += amount
            else:
                sent_tokens.append(mint)
        elif to_acc == wallet_address:
            if mint != SOL_MINT:
                received_tokens.append(mint)

    if received_tokens and not sent_tokens:
        return {
            "token": received_tokens[0],
            "sol_amount": sol_sent,
            "signature": tx.get("signature"),
            "timestamp": tx.get("timestamp"),
        }
    return None


# ─────────────────────────────────────────────
# Отправка сигналов в Telegram
# ─────────────────────────────────────────────

def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")


def notify_new_buy(wallet: str, buy_info: dict):
    """Формирует и отправляет сигнал о новой покупке (с фильтрацией)."""
    token = buy_info["token"]
    sol_amount = buy_info["sol_amount"]

    logger.info(f"🔔 Новая покупка: {wallet[:8]}... купил {token[:8]}...")

    # Прогоняем через агента
    try:
        result = evaluate_signal(token, wallet)

        verdict = result.get("verdict", "UNKNOWN")
        confidence = result.get("confidence", 0.0) or 0.0
        breakdown = result.get("breakdown", {}) or {}
        sm_stats = result.get("smart_money_stats", {}) or {}

        # ─── ФИЛЬТРАЦИЯ ───
        if verdict == "REJECT" and not NOTIFY_REJECT:
            logger.info(f"⏭️  REJECT отфильтрован: {token[:8]}...")
            return

        if verdict == "IGNORE" and not NOTIFY_IGNORE:
            logger.info(f"⏭️  IGNORE отфильтрован: {token[:8]}...")
            return

        if verdict == "WEAK" and not NOTIFY_WEAK:
            logger.info(f"⏭️  WEAK отфильтрован: {token[:8]}...")
            return

        # ─── Формируем сообщение ───
        emoji = {
            "SIGNAL": "🚀",
            "WEAK": "⚠️",
            "IGNORE": "❌",
            "REJECT": "🛑",
        }.get(verdict, "❓")

        def fmt(v):
            if v is None:
                return "—"
            try:
                return f"{float(v):.2f}"
            except (TypeError, ValueError):
                return "—"

        if verdict == "SIGNAL":
            # Полный отчёт для сильного сигнала
            text = (
                f"{emoji} СИЛЬНЫЙ СИГНАЛ!\n"
                f"Confidence: {confidence:.3f}\n\n"
                f"🪙 Токен: {token}\n"
                f"👤 Кошелёк: {wallet}\n"
                f"💰 Потрачено: {sol_amount:.4f} SOL\n\n"
                f"📊 Разбивка:\n"
                f"├ Safety:      {fmt(breakdown.get('safety'))}\n"
                f"├ Smart Money: {fmt(breakdown.get('smart_money'))}\n"
                f"└ Liquidity:   {fmt(breakdown.get('liquidity'))}\n"
            )

            win_rate = sm_stats.get("win_rate")
            pnl = sm_stats.get("total_pnl_sol")
            if win_rate is not None or pnl is not None:
                text += (
                    f"\n🧠 Smart Money:\n"
                    f"├ Win rate: {win_rate if win_rate is not None else '—'}%\n"
                    f"└ PnL:      {pnl if pnl is not None else '—'} SOL\n"
                )
        else:
            # Краткий отчёт для WEAK/IGNORE
            text = (
                f"{emoji} Слабый сигнал ({verdict})\n"
                f"Confidence: {confidence:.3f}\n\n"
                f"🪙 Токен: {token[:12]}...\n"
                f"💰 Потрачено: {sol_amount:.4f} SOL\n"
            )

        send_telegram_message(text)

    except Exception as e:
        logger.exception("Ошибка анализа покупки")


# ─────────────────────────────────────────────
# Фоновый цикл
# ─────────────────────────────────────────────

def check_wallet(wallet: str, watch_data: dict):
    txs = fetch_recent_swaps(wallet, limit=20)
    if not txs:
        return

    last_signature = watch_data.get(wallet, {}).get("last_signature")

    new_txs = []
    for tx in txs:
        sig = tx.get("signature")
        if sig == last_signature:
            break
        new_txs.append(tx)

    if not new_txs:
        return

    watch_data[wallet]["last_signature"] = txs[0].get("signature")

    for tx in reversed(new_txs):
        buy = parse_buy(tx, wallet)
        if buy:
            notify_new_buy(wallet, buy)


def run_watcher_loop():
    logger.info("👁️  Watcher запущен")
    time.sleep(60)

    while True:
        try:
            watch_data = load_watch_data()
            if watch_data:
                logger.info(f"👁️  Проверяю {len(watch_data)} кошельков...")
                for wallet in list(watch_data.keys()):
                    try:
                        check_wallet(wallet, watch_data)
                    except Exception as e:
                        logger.error(f"Ошибка проверки {wallet}: {e}")
                save_watch_data(watch_data)
            else:
                logger.info("👁️  Список кошельков пуст")

        except Exception as e:
            logger.exception("Ошибка в цикле watcher")

        time.sleep(CHECK_INTERVAL)