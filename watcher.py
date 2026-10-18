"""
Watcher — фоновый мониторинг кошельков с хранением в Upstash Redis.
"""

import os
import json
import time
import logging
import requests
from dotenv import load_dotenv

from aggregator import evaluate_signal

load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not HELIUS_API_KEY:
    raise ValueError("Нет HELIUS_API_KEY в .env!")
if not UPSTASH_URL or not UPSTASH_TOKEN:
    raise ValueError("Нет UPSTASH переменных в .env!")

logger = logging.getLogger(__name__)

BASE_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SOL_MINT = "So11111111111111111111111111111111111111112"

CHECK_INTERVAL = 300
UPSTASH_KEY = "memecoin_agent:watched_wallets"

NOTIFY_WEAK = True
NOTIFY_IGNORE = False
NOTIFY_REJECT = False
NOTIFY_SELLS = True


# ─────────────────────────────────────────────
# UPSTASH
# ─────────────────────────────────────────────

def upstash_command(*args):
    url = UPSTASH_URL
    headers = {
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=list(args), timeout=15)
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        logger.error(f"Ошибка Upstash: {e}")
        return None


def load_watch_data():
    raw = upstash_command("GET", UPSTASH_KEY)
    if not raw:
        return {}
    try:
        if isinstance(raw, str):
            return json.loads(raw)
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.error(f"Ошибка парсинга JSON: {e}")
        return {}


def save_watch_data(data):
    try:
        payload = json.dumps(data, ensure_ascii=False)
        result = upstash_command("SET", UPSTASH_KEY, payload)
        return result == "OK"
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")
        return False


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
# Cleanup
# ─────────────────────────────────────────────

def fetch_signatures(address: str, limit: int = 5):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        logger.warning(f"Ошибка getSignaturesForAddress: {e}")
        return []


def cleanup_inactive_wallets(days_inactive: int = 7):
    data = load_watch_data()
    if not data:
        return [], []

    now = time.time()
    cutoff = now - days_inactive * 86400

    removed = []
    kept = []

    for wallet in list(data.keys()):
        sigs = fetch_signatures(wallet, limit=5)
        if not sigs:
            remove_wallet(wallet)
            removed.append(wallet)
            logger.info(f"🗑️ Удалён (нет транзакций): {wallet[:16]}...")
            continue

        latest = sigs[0].get("blockTime") or 0
        if latest < cutoff:
            remove_wallet(wallet)
            removed.append(wallet)
            logger.info(f"🗑️ Удалён (последняя > {days_inactive}д): {wallet[:16]}...")
        else:
            kept.append(wallet)

        time.sleep(0.2)

    return removed, kept


# ─────────────────────────────────────────────
# Мониторинг
# ─────────────────────────────────────────────

def fetch_recent_swaps(address: str, limit: int = 20):
    url = BASE_URL.format(address=address)
    params = {"api-key": HELIUS_API_KEY, "limit": limit, "type": "SWAP"}
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


def parse_sell(tx, wallet_address):
    """Определяет продажу токена."""
    if tx.get("type") != "SWAP":
        return None
    transfers = tx.get("tokenTransfers", [])
    if not transfers:
        return None

    sent_tokens = []
    received_tokens = []
    sol_received = 0.0

    for t in transfers:
        mint = t.get("mint", "")
        amount = float(t.get("tokenAmount", 0) or 0)
        from_acc = t.get("fromUserAccount", "")
        to_acc = t.get("toUserAccount", "")

        if from_acc == wallet_address:
            if mint == SOL_MINT:
                sol_received += amount
            else:
                sent_tokens.append(mint)
        elif to_acc == wallet_address:
            if mint != SOL_MINT:
                received_tokens.append(mint)

    if sent_tokens and not received_tokens and sol_received > 0:
        return {
            "token": sent_tokens[0],
            "sol_amount": sol_received,
            "signature": tx.get("signature"),
            "timestamp": tx.get("timestamp"),
        }
    return None


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
    token = buy_info["token"]
    sol_amount = buy_info["sol_amount"]
    logger.info(f"🔔 Покупка: {wallet[:8]}... купил {token[:8]}...")

    try:
        result = evaluate_signal(token, wallet)
        verdict = result.get("verdict", "UNKNOWN")
        confidence = result.get("confidence", 0.0) or 0.0
        breakdown = result.get("breakdown", {}) or {}
        sm_stats = result.get("smart_money_stats", {}) or {}

        if verdict == "REJECT" and not NOTIFY_REJECT:
            return
        if verdict == "IGNORE" and not NOTIFY_IGNORE:
            return
        if verdict == "WEAK" and not NOTIFY_WEAK:
            return

        emoji = {"SIGNAL": "🚀", "WEAK": "⚠️", "IGNORE": "❌", "REJECT": "🛑"}.get(verdict, "❓")

        def fmt(v):
            if v is None:
                return "—"
            try:
                return f"{float(v):.2f}"
            except (TypeError, ValueError):
                return "—"

        if verdict == "SIGNAL":
            text = (
                f"{emoji} СИЛЬНЫЙ СИГНАЛ (покупка)!\n"
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
            text = (
                f"{emoji} Слабый сигнал (покупка)\n"
                f"Confidence: {confidence:.3f}\n\n"
                f"🪙 Токен: {token}\n"
                f"💰 Потрачено: {sol_amount:.4f} SOL\n"
            )

        send_telegram_message(text)
    except Exception as e:
        logger.exception("Ошибка анализа покупки")


def notify_sell(wallet: str, sell_info: dict):
    """Уведомление о продаже."""
    token = sell_info["token"]
    sol_amount = sell_info["sol_amount"]
    logger.info(f"💸 Продажа: {wallet[:8]}... продал {token[:8]}...")

    if not NOTIFY_SELLS:
        return

    text = (
        f"💸 ПРОДАЖА\n\n"
        f"👤 Кошелёк: {wallet}\n"
        f"🪙 Токен: {token}\n"
        f"💰 Получено: {sol_amount:.4f} SOL\n"
    )
    send_telegram_message(text)


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
            continue
        sell = parse_sell(tx, wallet)
        if sell:
            notify_sell(wallet, sell)


def run_watcher_loop():
    logger.info("👁️  Watcher запущен (Upstash)")
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