"""
Discovery — автопоиск умных кошельков.
Источник: DexScreener Boosts (с кешем на 1 час) + fallback.
"""

import os
import re
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

# ─────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────

TOP_TOKENS_LIMIT = 10
MIN_TOKEN_APPEARANCE = 2
MIN_WIN_RATE = 45.0
MIN_PNL_SOL = 5.0
MAX_TRADES_PER_DAY = 200
MAX_CANDIDATES = 15
MAX_VALIDATE = 5

# Кеш DexScreener (1 час)
_tokens_cache = {"data": None, "expires_at": 0}


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


# ─────────────────────────────────────────────
# DexScreener с кешем
# ─────────────────────────────────────────────

def get_trending_tokens(limit: int = TOP_TOKENS_LIMIT):
    global _tokens_cache

    if _tokens_cache["data"] and time.time() < _tokens_cache["expires_at"]:
        logger.info(f"  Кеш DexScreener ({len(_tokens_cache['data'])} токенов)")
        return _tokens_cache["data"][:limit]

    url = "https://api.dexscreener.com/token-boosts/top/v1"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        solana_tokens = [item for item in data if item.get("chainId") == "solana"]
        solana_tokens.sort(key=lambda x: int(x.get("amount", 0) or 0), reverse=True)

        tokens = []
        for item in solana_tokens[:limit]:
            mint = item.get("tokenAddress")
            if mint:
                tokens.append({"mint": mint, "symbol": "BOOSTED", "volume_24h": 0, "market_cap": 0})

        if tokens:
            _tokens_cache["data"] = tokens
            _tokens_cache["expires_at"] = time.time() + 3600
            logger.info(f"  DexScreener (обновлено): {len(tokens)} токенов")
            return tokens
    except Exception as e:
        logger.error(f"  Ошибка DexScreener: {e}")

    if _tokens_cache["data"]:
        logger.warning("  Старый кеш (API упал)")
        return _tokens_cache["data"][:limit]

    return []


def get_tokens():
    tokens = get_trending_tokens(limit=TOP_TOKENS_LIMIT)
    if tokens:
        for t in tokens:
            logger.info(f"    - BOOSTED: {t['mint'][:12]}...")
        return tokens

    logger.info(f"  Fallback: {len(FALLBACK_TOKENS)} токенов")
    for t in FALLBACK_TOKENS:
        logger.info(f"    - {t['symbol']}: {t['mint'][:12]}...")
    return list(FALLBACK_TOKENS)


# ─────────────────────────────────────────────
# Валидация адресов
# ─────────────────────────────────────────────

BASE58_REGEX = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
EXCLUDED_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
}

_validated_addresses = {}


def is_valid_format(addr: str) -> bool:
    if not addr or not isinstance(addr, str):
        return False
    if len(addr) < 32 or len(addr) > 44:
        return False
    if not BASE58_REGEX.match(addr):
        return False
    if addr in EXCLUDED_ADDRESSES:
        return False
    if addr == SOL_MINT:
        return False
    return True


def is_helius_valid_address(addr: str) -> bool:
    if addr in _validated_addresses:
        return _validated_addresses[addr]

    url = f"https://api.helius.xyz/v0/addresses/{addr}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": 5}
    try:
        r = requests.get(url, params=params, timeout=10)
        try:
            data = r.json()
        except Exception:
            _validated_addresses[addr] = False
            return False

        if isinstance(data, dict):
            _validated_addresses[addr] = False
            return False

        if isinstance(data, list):
            if len(data) == 0:
                _validated_addresses[addr] = False
                return False
            _validated_addresses[addr] = True
            return True

        _validated_addresses[addr] = False
        return False
    except Exception:
        _validated_addresses[addr] = False
        return False


# ─────────────────────────────────────────────
# Поиск трейдеров
# ─────────────────────────────────────────────

def get_token_traders(token_mint: str, limit: int = 20):
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

        for sig_info in signatures[:10]:
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
                    if owner and is_valid_format(owner):
                        pre_map[owner] = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)

                post_map = {}
                for b in post_balances:
                    owner = b.get("owner")
                    if owner and is_valid_format(owner):
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

            time.sleep(0.05)

        return list(buyers), list(sellers)

    except Exception as e:
        logger.error(f"Ошибка get_token_traders: {e}")
        return [], []


def find_candidates(tokens: list, min_appearance: int = 2):
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

        time.sleep(0.2)

    role_priority = {"both": 0, "seller": 1, "buyer": 2}
    active = [
        {"wallet": w, "count": data["count"], "role": data["role"], "tokens": data["tokens"]}
        for w, data in wallet_data.items()
        if data["count"] >= min_appearance
    ]

    # Сортировка по count (активности в разных токенах) + role
    active.sort(key=lambda x: (-x["count"], role_priority.get(x["role"], 99)))

    valid_active = []
    for c in active:
        if is_helius_valid_address(c["wallet"]):
            valid_active.append(c)
        if len(valid_active) >= MAX_CANDIDATES:
            break

    return valid_active[:MAX_CANDIDATES]


# ─────────────────────────────────────────────
# Валидация через Smart Money
# ─────────────────────────────────────────────

def validate_candidate(wallet: str, min_win_rate: float, min_pnl_sol: float, max_trades_per_day: int):
    try:
        result = analyze_wallet(wallet, include_portfolio=False)
        stats = result.get("stats", {}) or {}
        win_rate = stats.get("win_rate")
        pnl = stats.get("total_pnl_sol")
        closed = stats.get("closed_with_sol", 0)
        open_pos = stats.get("open_positions", 0)
        buys = stats.get("buys", 0)
        sells = stats.get("sells", 0)

        total_trades = buys + sells
        trades_per_day = total_trades / 3 if total_trades else 0

        logger.info(
            f"    📊 {wallet[:16]}: buys={buys}, sells={sells}, "
            f"closed={closed}, open={open_pos}, win={win_rate}, pnl={pnl}"
        )

        if trades_per_day > max_trades_per_day:
            logger.info(f"    ⏭️  бот ({trades_per_day:.0f}/день)")
            return None

        # ФИЛЬТР: только активные трейдеры (buys > 0 И sells > 0)
        if buys == 0 or sells == 0:
            logger.info(f"    ⏭️  мало активности (buys={buys}, sells={sells})")
            return None

        if closed and win_rate is not None and win_rate < min_win_rate:
            logger.info(f"    ⏭️  win {win_rate:.0f}% < {min_win_rate}%")
            return None

        if closed and pnl is not None and pnl < min_pnl_sol:
            logger.info(f"    ⏭️  PnL {pnl:.4f} < {min_pnl_sol}")
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


# ─────────────────────────────────────────────
# Главная функция
# ─────────────────────────────────────────────

def discover_smart_wallets():
    try:
        return _discover_impl()
    except Exception as e:
        logger.exception("КРИТИЧЕСКАЯ ОШИБКА")
        return {"error": f"Внутренняя ошибка: {str(e)[:200]}"}


def _discover_impl():
    logger.info("=" * 60)
    logger.info("🔍 АВТОПОИСК УМНЫХ КОШЕЛЬКОВ")
    logger.info(f"   win ≥ {MIN_WIN_RATE}%, PnL ≥ {MIN_PNL_SOL} SOL")
    logger.info("=" * 60)

    logger.info("📊 Шаг 1: Получаю токены...")
    tokens = get_tokens()
    if not tokens:
        return {"error": "Не удалось получить токены"}

    logger.info("")
    logger.info("👥 Шаг 2: Ищу трейдеров...")
    candidates = find_candidates(tokens, min_appearance=MIN_TOKEN_APPEARANCE)

    if not candidates:
        return {"error": "Не найдено активных кандидатов"}

    logger.info("")
    logger.info(f"  Найдено {len(candidates)} валидных кандидатов")

    logger.info("")
    logger.info(f"🧠 Шаг 3: Проверяю через Smart Money (топ-{MAX_VALIDATE})...")
    valid = []
    for i, cand in enumerate(candidates[:MAX_VALIDATE]):
        wallet = cand["wallet"]
        logger.info(f"  [{i+1}/{min(len(candidates), MAX_VALIDATE)}] {wallet}")

        info = validate_candidate(wallet, MIN_WIN_RATE, MIN_PNL_SOL, MAX_TRADES_PER_DAY)
        if info:
            info["role"] = cand["role"]
            info["tokens_bought"] = cand["tokens"]
            valid.append(info)
            logger.info(f"    ✅ ПОДХОДИТ! buys={info['buys']}, sells={info['sells']}")
        time.sleep(0.3)

    logger.info("")
    logger.info(f"✅ Найдено {len(valid)} валидных кошельков")

    # Сортировка по общей активности (buys + sells)
    def sort_key(x):
        activity = x.get("buys", 0) + x.get("sells", 0)
        has_pnl = 1 if (x["pnl_sol"] is not None and x["pnl_sol"] > 0) else 0
        return (activity, has_pnl)

    valid.sort(key=sort_key, reverse=True)

    logger.info("🏁 ФИНАЛ")

    return {
        "tokens_checked": len(tokens),
        "candidates_found": len(candidates),
        "valid_wallets": valid[:5],
    }