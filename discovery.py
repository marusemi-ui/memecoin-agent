"""
Discovery — автопоиск умных кошельков.
Источник: DexScreener Boosts + GMGN CLI (для готовых метрик).
Fallback: Helius RPC.
"""

import os
import re
import json
import time
import logging
import subprocess
import requests
from dotenv import load_dotenv

from smart_money import analyze_wallet, ANALYSIS_DAYS

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
MIN_TOKEN_APPEARANCE = 1
MIN_WIN_RATE = 45.0
MIN_PNL_SOL = 5.0
MAX_TRADES_PER_DAY = 200
MAX_CANDIDATES = 15
MAX_VALIDATE = 5
AUTO_WATCH_MIN_TRADES = 5

# Кеш DexScreener
_tokens_cache = {"data": None, "expires_at": 0}

# История прогонов
HISTORY_KEY = "memecoin_agent:discovery_history"
HISTORY_MAX = 10


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
# GMGN CLI
# ─────────────────────────────────────────────

def gmgn_portfolio_stats(wallet: str):
    """
    Вызывает npx gmgn-cli portfolio stats для кошелька.
    Возвращает dict или None.
    """
    try:
        result = subprocess.run(
            ["npx", "gmgn-cli", "portfolio", "stats", "--chain", "sol", "--wallet", wallet],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug(f"GMGN CLI failed для {wallet[:8]}: {result.stderr[:100]}")
            return None

        data = json.loads(result.stdout)
        return data
    except subprocess.TimeoutExpired:
        logger.warning(f"GMGN CLI timeout для {wallet[:8]}")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"GMGN CLI JSON error для {wallet[:8]}: {e}")
        return None
    except Exception as e:
        logger.error(f"GMGN CLI ошибка: {e}")
        return None


def parse_gmgn_stats(data: dict):
    """
    Извлекает метрики из ответа GMGN.
    Возвращает (winrate, realized_profit_sol, tags).
    """
    if not data:
        return None, None, []

    try:
        realized_profit = float(data.get("realized_profit", 0) or 0)
    except (ValueError, TypeError):
        realized_profit = 0

    winrate = None
    pnl_stat = data.get("pnl_stat", {})
    if isinstance(pnl_stat, dict):
        wr = pnl_stat.get("winrate")
        if wr is not None:
            try:
                winrate = float(wr) * 100  # GMGN отдаёт 0.3 = 30%
            except (ValueError, TypeError):
                winrate = None

    tags = data.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    return winrate, realized_profit, tags


# ─────────────────────────────────────────────
# История и авто-тюнинг
# ─────────────────────────────────────────────

def load_discovery_history():
    from watcher import upstash_command
    raw = upstash_command("GET", HISTORY_KEY)
    if not raw:
        return []
    try:
        if isinstance(raw, str):
            return json.loads(raw)
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def save_discovery_history(valid_count: int, candidates_found: int):
    from watcher import upstash_command
    history = load_discovery_history()
    history.append({
        "timestamp": int(time.time()),
        "valid": valid_count,
        "candidates": candidates_found,
    })
    history = history[-HISTORY_MAX:]
    payload = json.dumps(history, ensure_ascii=False)
    upstash_command("SET", HISTORY_KEY, payload)


def auto_tune_min_appearance():
    history = load_discovery_history()
    if len(history) < 3:
        return 1
    recent = history[-5:]
    avg_valid = sum(h.get("valid", 0) for h in recent) / len(recent)
    if avg_valid > 8:
        logger.info(f"🎛️ Авто-тюнинг: MIN_APP=2 (avg_valid={avg_valid:.1f})")
        return 2
    return 1


# ─────────────────────────────────────────────
# DexScreener
# ─────────────────────────────────────────────

def get_trending_tokens(limit: int = TOP_TOKENS_LIMIT):
    global _tokens_cache
    if _tokens_cache["data"] and time.time() < _tokens_cache["expires_at"]:
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
            logger.info(f"  DexScreener: {len(tokens)} токенов")
            return tokens
    except Exception as e:
        logger.error(f"  Ошибка DexScreener: {e}")

    if _tokens_cache["data"]:
        return _tokens_cache["data"][:limit]
    return []


def get_tokens():
    tokens = get_trending_tokens(limit=TOP_TOKENS_LIMIT)
    if tokens:
        for t in tokens:
            logger.info(f"    - BOOSTED: {t['mint'][:12]}...")
        return tokens
    logger.info(f"  Fallback: {len(FALLBACK_TOKENS)} токенов")
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
    if addr in EXCLUDED_ADDRESSES or addr == SOL_MINT:
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
            except Exception:
                continue
            time.sleep(0.05)

        return list(buyers), list(sellers)
    except Exception as e:
        logger.error(f"Ошибка get_token_traders: {e}")
        return [], []


def find_candidates(tokens: list, min_appearance: int = 1):
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
    active.sort(key=lambda x: (-x["count"], role_priority.get(x["role"], 99)))

    valid_active = []
    for c in active:
        if is_helius_valid_address(c["wallet"]):
            valid_active.append(c)
        if len(valid_active) >= MAX_CANDIDATES:
            break
    return valid_active[:MAX_CANDIDATES]


# ─────────────────────────────────────────────
# Валидация через GMGN (с fallback на Helius)
# ─────────────────────────────────────────────

def validate_candidate(wallet: str):
    """
    Проверяет кандидата через GMGN CLI.
    Если GMGN не отвечает — fallback на Helius.
    """
    # Попытка 1: GMGN
    data = gmgn_portfolio_stats(wallet)
    if data:
        winrate, realized_profit, tags = parse_gmgn_stats(data)
        logger.info(
            f"    📊 {wallet[:16]} (GMGN): win={winrate}, pnl={realized_profit} SOL, tags={tags}"
        )

        if winrate is not None and winrate < MIN_WIN_RATE:
            logger.info(f"    ⏭️  win {winrate:.0f}% < {MIN_WIN_RATE}%")
            return None

        if realized_profit is not None and realized_profit < MIN_PNL_SOL:
            logger.info(f"    ⏭️  PnL {realized_profit:.2f} < {MIN_PNL_SOL}")
            return None

        return {
            "wallet": wallet,
            "win_rate": winrate,
            "pnl_sol": realized_profit,
            "tags": tags,
            "source": "gmgn",
        }

    # Fallback: Helius
    logger.info(f"    ⚠️ GMGN недоступен, fallback на Helius для {wallet[:16]}...")
    result = analyze_wallet(wallet, include_portfolio=False)
    stats = result.get("stats", {}) or {}
    win_rate = stats.get("win_rate")
    pnl = stats.get("total_pnl_sol")
    closed = stats.get("closed_with_sol", 0)
    open_pos = stats.get("open_positions", 0)
    buys = stats.get("buys", 0)
    sells = stats.get("sells", 0)

    if buys == 0 and sells == 0:
        return None

    if closed and win_rate is not None and win_rate < MIN_WIN_RATE:
        return None
    if closed and pnl is not None and pnl < MIN_PNL_SOL:
        return None

    auto_watch = (buys + sells) >= AUTO_WATCH_MIN_TRADES
    return {
        "wallet": wallet,
        "win_rate": win_rate,
        "pnl_sol": pnl,
        "closed": closed,
        "open_positions": open_pos,
        "buys": buys,
        "sells": sells,
        "trades_per_day": round((buys + sells) / ANALYSIS_DAYS, 1) if (buys + sells) else 0,
        "auto_watch": auto_watch,
        "source": "helius",
    }


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
    dynamic_min_app = auto_tune_min_appearance()
    candidates = find_candidates(tokens, min_appearance=dynamic_min_app)
    if not candidates:
        save_discovery_history(0, 0)
        return {"error": "Не найдено активных кандидатов"}

    logger.info("")
    logger.info(f"🧠 Шаг 3: Проверяю через GMGN (топ-{MAX_VALIDATE})...")
    valid = []
    for i, cand in enumerate(candidates[:MAX_VALIDATE]):
        wallet = cand["wallet"]
        logger.info(f"  [{i+1}/{min(len(candidates), MAX_VALIDATE)}] {wallet}")
        info = validate_candidate(wallet)
        if info:
            info["role"] = cand["role"]
            info["tokens_bought"] = cand["tokens"]
            valid.append(info)
            logger.info(f"    ✅ ПОДХОДИТ!")
        time.sleep(0.3)

    logger.info("")
    logger.info(f"✅ Найдено {len(valid)} валидных кошельков")
    save_discovery_history(len(valid), len(candidates))

    # Сортировка: сначала с GMGN, потом по PnL
    def sort_key(x):
        source = 0 if x.get("source") == "gmgn" else 1
        pnl = x.get("pnl_sol") or 0
        return (source, -pnl)

    valid.sort(key=sort_key)

    return {
        "tokens_checked": len(tokens),
        "candidates_found": len(candidates),
        "valid_wallets": valid[:5],
    }