"""
Discovery — автопоиск «умных кошельков» через свежие pump.fun токены.
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv

from smart_money import analyze_wallet

load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

if not HELIUS_API_KEY:
    raise ValueError("Нет HELIUS_API_KEY в .env!")

logger = logging.getLogger(__name__)

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SOL_MINT = "So11111111111111111111111111111111111111112"

TOP_TOKENS_LIMIT = 10
MIN_TOKEN_APPEARANCE = 2
MIN_WIN_RATE = 50.0
MAX_TRADES_PER_DAY = 1000
MAX_CANDIDATES = 30
MAX_VALIDATE = 20


FALLBACK_TOKENS = [
    {"mint": "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump", "symbol": "PNUT"},
    {"mint": "CzLSujWBLFsSjncfkh59rUFqvafWcYt5N2cqAy8Apump", "symbol": "WEN"},
    {"mint": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump", "symbol": "FARTCOIN"},
    {"mint": "A8C3xuqscfmyLrte3VmTqrAq8kgMASius9AFNANwpump", "symbol": "FWOG"},
    {"mint": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5", "symbol": "MEW"},
    {"mint": "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82", "symbol": "BOME"},
    {"mint": "GJAFwWjJ3vnTsrQVabjBVK2TYB1YtRCQXRDfDgUnpump", "symbol": "MICHI"},
    {"mint": "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY", "symbol": "MOODENG"},
    {"mint": "7atgF8KQo4wJrD5ATGX7t1V2zVvykPJbFfNeVf1icFv1", "symbol": "MOTHER"},
    {"mint": "HgcxVs6kJhPAaGqnPNGaa7zYgNT49hJrLufiqcNMuYZT", "symbol": "SPCX"},
]


def get_fresh_pumpfun_tokens(limit: int = TOP_TOKENS_LIMIT):
    url = "https://frontend-api-v3.pump.fun/coins"
    params = {"offset": 0, "limit": 100, "sort": "created_timestamp", "order": "DESC"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            logger.warning(f"pump.fun вернул не список: {type(data)}")
            return []
        logger.info(f"  pump.fun вернул {len(data)} токенов")
        tokens = []
        for coin in data:
            mint = coin.get("mint")
            symbol = coin.get("symbol", "?")
            market_cap = float(coin.get("usd_market_cap", 0) or 0)
            if not mint or market_cap < 20_000:
                continue
            tokens.append({"mint": mint, "symbol": symbol, "volume_24h": market_cap, "market_cap": market_cap})
            if len(tokens) >= limit:
                break
        if tokens:
            logger.info(f"  Отобрано {len(tokens)} свежих токенов (MC > $20k)")
            for t in tokens:
                logger.info(f"    - {t['symbol']}: MC ${t['market_cap']:,.0f}")
            return tokens
        logger.warning("  Все токены pump.fun < $20k, использую fallback")
    except Exception as e:
        logger.error(f"  Ошибка pump.fun API: {e}, использую fallback")

    logger.info(f"  Fallback: использую {len(FALLBACK_TOKENS[:limit])} токенов")
    return [dict(t, volume_24h=0, market_cap=0) for t in FALLBACK_TOKENS[:limit]]


def get_token_traders(token_mint: str, limit: int = 30):
    """Возвращает покупателей и продавцов через OWNER-адреса."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [token_mint, {"limit": limit}],
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        signatures = data.get("result", [])
        if not signatures:
            return [], []

        buyers = set()
        sellers = set()

        for sig_info in signatures[:15]:
            sig = sig_info.get("signature")
            if not sig:
                continue

            tx_payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            }
            try:
                tx_r = requests.post(RPC_URL, json=tx_payload, timeout=15)
                tx_data = tx_r.json()
                result = tx_data.get("result")
                if not result:
                    continue

                meta = result.get("meta", {})
                post_balances = meta.get("postTokenBalances", [])
                pre_balances = meta.get("preTokenBalances", [])

                pre_map = {}
                for b in pre_balances:
                    owner = b.get("owner")
                    if owner:
                        pre_map[owner] = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)

                post_map = {}
                for b in post_balances:
                    owner = b.get("owner")
                    if owner:
                        post_map[owner] = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)

                for owner in post_map:
                    pre_amt = pre_map.get(owner, 0)
                    post_amt = post_map[owner]
                    if post_amt > pre_amt:
                        buyers.add(owner)
                    elif post_amt < pre_amt and pre_amt > 0:
                        sellers.add(owner)

            except Exception as e:
                logger.debug(f"Ошибка парсинга tx: {e}")
                continue

            time.sleep(0.1)

        return list(buyers), list(sellers)

    except Exception as e:
        logger.error(f"Ошибка get_token_traders: {e}")
        return [], []


def find_candidates(tokens: list):
    wallet_data = {}
    for i, token in enumerate(tokens):
        mint = token["mint"]
        symbol = token["symbol"]
        logger.info(f"  [{i+1}/{len(tokens)}] Ищу трейдеров {symbol}...")
        buyers, sellers = get_token_traders(mint)
        logger.info(f"    Покупателей: {len(buyers)}, продавцов: {len(sellers)}")

        for w in buyers:
            if w not in wallet_data:
                wallet_data[w] = {"count": 0, "role": "buyer", "tokens": []}
            wallet_data[w]["count"] += 1
            wallet_data[w]["tokens"].append(symbol)

        for w in sellers:
            if w not in wallet_data:
                wallet_data[w] = {"count": 0, "role": "seller", "tokens": []}
            elif wallet_data[w]["role"] == "buyer":
                wallet_data[w]["role"] = "both"
            wallet_data[w]["count"] += 1
            wallet_data[w]["tokens"].append(symbol)

        time.sleep(0.3)

    role_priority = {"both": 0, "seller": 1, "buyer": 2}
    active = [
        {"wallet": w, "count": data["count"], "role": data["role"], "tokens": data["tokens"]}
        for w, data in wallet_data.items()
        if data["count"] >= MIN_TOKEN_APPEARANCE
    ]
    active.sort(key=lambda x: (role_priority.get(x["role"], 99), -x["count"]))
    return active[:MAX_CANDIDATES]


def validate_candidate(wallet: str):
    try:
        result = analyze_wallet(wallet)
        stats = result.get("stats", {}) or {}
        win_rate = stats.get("win_rate")
        pnl = stats.get("total_pnl_sol")
        closed = stats.get("closed_with_sol", 0)
        open_pos = stats.get("open_positions", 0)
        buys = stats.get("buys", 0)
        sells = stats.get("sells", 0)
        swaps_recent = stats.get("swaps_recent", 0)

        total_trades = buys + sells
        trades_per_day = total_trades / 3 if total_trades else 0

        # Логируем полную статистику кандидата
        logger.info(
            f"    📊 {wallet}: swaps={swaps_recent}, buys={buys}, sells={sells}, "
            f"closed={closed}, open={open_pos}, win={win_rate}, pnl={pnl}"
        )

        if trades_per_day > MAX_TRADES_PER_DAY:
            logger.info(f"    ⏭️  {wallet} — бот ({trades_per_day:.0f}/день)")
            return None

        if not closed and not open_pos and buys == 0 and sells == 0:
            logger.info(f"    ⏭️  {wallet} — нет активности")
            return None

        if closed and win_rate is not None and win_rate < MIN_WIN_RATE:
            logger.info(f"    ⏭️  {wallet} — win {win_rate:.0f}% < {MIN_WIN_RATE}%")
            return None

        if closed and pnl is not None and pnl < 0:
            logger.info(f"    ⏭️  {wallet} — PnL {pnl:.4f} < 0")
            return None

        return {
            "wallet": wallet, "win_rate": win_rate, "pnl_sol": pnl,
            "closed": closed, "open_positions": open_pos,
            "buys": buys, "sells": sells,
            "trades_per_day": round(trades_per_day, 1),
        }
    except Exception as e:
        logger.error(f"Ошибка валидации {wallet}: {e}")
        return None


def discover_smart_wallets():
    try:
        return _discover_impl()
    except Exception as e:
        logger.exception("КРИТИЧЕСКАЯ ОШИБКА в discover_smart_wallets")
        return {"error": f"Внутренняя ошибка: {str(e)[:200]}"}


def _discover_impl():
    logger.info("=" * 60)
    logger.info("🔍 НАЧИНАЮ АВТОПОИСК УМНЫХ КОШЕЛЬКОВ")
    logger.info("=" * 60)

    logger.info("📊 Шаг 1: Получаю свежие pump.fun токены...")
    tokens = get_fresh_pumpfun_tokens(limit=TOP_TOKENS_LIMIT)
    if not tokens:
        return {"error": "Не удалось получить токены"}

    logger.info("")
    logger.info("👥 Шаг 2: Ищу трейдеров...")
    candidates = find_candidates(tokens)

    if not candidates:
        return {"error": "Не найдено активных кандидатов"}

    logger.info("")
    logger.info(f"  Найдено {len(candidates)} кандидатов:")
    for c in candidates[:30]:
        logger.info(f"    {c['wallet']} — role={c['role']}, токенов={c['count']}")

    logger.info("")
    logger.info("🧠 Шаг 3: Проверяю через Smart Money (win rate ≥ 50%)...")
    valid = []
    for i, cand in enumerate(candidates[:MAX_VALIDATE]):
        wallet = cand["wallet"]
        logger.info(f"  [{i+1}/{min(len(candidates), MAX_VALIDATE)}] {wallet}")

        info = validate_candidate(wallet)
        if info:
            info["role"] = cand["role"]
            info["tokens_bought"] = cand["tokens"]
            valid.append(info)
            logger.info(f"    ✅ ПОДХОДИТ! win={info['win_rate']}, pnl={info['pnl_sol']}")
        time.sleep(0.5)

    logger.info("")
    logger.info(f"✅ Найдено {len(valid)} валидных кошельков")

    def sort_key(x):
        has_closed = 1 if x["closed"] else 0
        has_pnl = 1 if (x["pnl_sol"] is not None and x["pnl_sol"] > 0) else 0
        win = x["win_rate"] if x["win_rate"] is not None else 0
        return (has_closed, has_pnl, win)

    try:
        valid.sort(key=sort_key, reverse=True)
    except Exception as e:
        logger.error(f"Ошибка сортировки: {e}")

    logger.info("🏁 ФИНАЛ: возвращаю результат")

    return {
        "tokens_checked": len(tokens),
        "candidates_found": len(candidates),
        "valid_wallets": valid[:5],
    }