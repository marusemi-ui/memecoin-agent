"""
Discovery — автопоиск «умных кошельков» из двух источников:
  А) Покупатели топ-токенов
  Б) Продавцы топ-токенов (у них точно есть закрытые позиции)

Все кандидаты объединяются и прогоняются через Smart Money.
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

# Настройки
TOP_TOKENS_LIMIT = 10
MIN_TOKEN_APPEARANCE = 2
MIN_WIN_RATE = 30.0
MAX_TRADES_PER_DAY = 300
MAX_CANDIDATES = 30
MAX_VALIDATE = 15


# ─────────────────────────────────────────────
# 1. Топ токенов (DexScreener)
# ─────────────────────────────────────────────

def get_top_tokens(limit: int = TOP_TOKENS_LIMIT):
    """
    Возвращает список топовых токенов по объёму за 24ч.
    Фильтрует SOL и его wrapped-версии.
    """
    url = "https://api.dexscreener.com/latest/dex/search?q=SOL"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs", [])
        logger.info(f"  DexScreener вернул {len(pairs)} пар")

        sol_pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and float(p.get("liquidity", {}).get("usd", 0) or 0) > 50_000
        ]
        logger.info(f"  Из них Solana с ликвидностью >50k: {len(sol_pairs)}")

        sol_pairs.sort(
            key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0),
            reverse=True,
        )

        seen = set()
        tokens = []
        for p in sol_pairs:
            mint = p.get("baseToken", {}).get("address")
            symbol = p.get("baseToken", {}).get("symbol", "?")
            if not mint:
                continue
            if mint in seen:
                continue
            # Исключаем SOL и его обёртки
            if mint == SOL_MINT or symbol == "SOL" or symbol == "WSOL":
                continue
            seen.add(mint)
            tokens.append({
                "mint": mint,
                "symbol": symbol,
                "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
            })
            if len(tokens) >= limit:
                break

        logger.info(f"  Отобрано {len(tokens)} токенов:")
        for t in tokens:
            logger.info(f"    - {t['symbol']} ({t['mint'][:8]}...) vol=${t['volume_24h']:,.0f}")

        return tokens
    except Exception as e:
        logger.error(f"Ошибка топ токенов: {e}")
        return []


# ─────────────────────────────────────────────
# 2. Покупатели и продавцы токена
# ─────────────────────────────────────────────

def get_token_traders(token_mint: str, limit: int = 30):
    """
    Возвращает два списка: тех, кто купил токен (buyers) и тех, кто продал (sellers).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
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
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    sig,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                ],
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

                pre_map = {b.get("accountIndex"): float(b.get("uiTokenAmount", {}).get("uiAmount") or 0) for b in pre_balances}
                post_map = {b.get("accountIndex"): float(b.get("uiTokenAmount", {}).get("uiAmount") or 0) for b in post_balances}

                account_keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])

                all_indices = set(list(pre_map.keys()) + list(post_map.keys()))
                for idx_str in all_indices:
                    try:
                        idx = int(idx_str)
                    except (ValueError, TypeError):
                        continue

                    pre_amt = pre_map.get(idx_str, 0)
                    post_amt = post_map.get(idx_str, 0)

                    if idx >= len(account_keys):
                        continue

                    account = account_keys[idx]
                    pubkey = account.get("pubkey") if isinstance(account, dict) else account
                    if not pubkey:
                        continue

                    if post_amt > pre_amt:
                        buyers.add(pubkey)
                    elif post_amt < pre_amt and pre_amt > 0:
                        sellers.add(pubkey)

            except Exception as e:
                logger.debug(f"Ошибка парсинга tx: {e}")
                continue

            time.sleep(0.1)

        return list(buyers), list(sellers)

    except Exception as e:
        logger.error(f"Ошибка get_token_traders: {e}")
        return [], []


# ─────────────────────────────────────────────
# 3. Поиск активных кандидатов
# ─────────────────────────────────────────────

def find_candidates(tokens: list):
    """
    Собирает кандидатов из покупателей и продавцов.
    """
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

    # Приоритет: both > seller > buyer
    role_priority = {"both": 0, "seller": 1, "buyer": 2}

    active = [
        {
            "wallet": w,
            "count": data["count"],
            "role": data["role"],
            "tokens": data["tokens"],
        }
        for w, data in wallet_data.items()
        if data["count"] >= MIN_TOKEN_APPEARANCE
    ]

    active.sort(key=lambda x: (role_priority.get(x["role"], 99), -x["count"]))
    return active[:MAX_CANDIDATES]


# ─────────────────────────────────────────────
# 4. Валидация кандидата через Smart Money
# ─────────────────────────────────────────────

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

        total_trades = buys + sells
        trades_per_day = total_trades / 30 if total_trades else 0

        # Фильтр ботов
        if trades_per_day > MAX_TRADES_PER_DAY:
            logger.info(f"    ⏭️  {wallet[:8]}... — бот ({trades_per_day:.0f}/день)")
            return None

        # Разрешаем без закрытых, но с открытыми
        if not closed and not open_pos:
            logger.info(f"    ⏭️  {wallet[:8]}... — нет ни закрытых, ни открытых")
            return None

        # Если есть закрытые — проверяем win rate
        if closed and win_rate is not None and win_rate < MIN_WIN_RATE:
            logger.info(f"    ⏭️  {wallet[:8]}... — win rate {win_rate:.0f}% < {MIN_WIN_RATE}%")
            return None

        # Проверяем PnL только если есть закрытые
        if closed and pnl is not None and pnl < 0:
            logger.info(f"    ⏭️  {wallet[:8]}... — PnL {pnl:.4f} SOL < 0")
            return None

        return {
            "wallet": wallet,
            "win_rate": win_rate,
            "pnl_sol": pnl,
            "closed": closed,
            "open_positions": open_pos,
            "buys": buys,
            "sells": sells,
            "trades_per_day": round(trades_per_day, 1),
        }

    except Exception as e:
        logger.error(f"Ошибка валидации {wallet[:8]}: {e}")
        return None


# ─────────────────────────────────────────────
# 5. Главная функция
# ─────────────────────────────────────────────

def discover_smart_wallets():
    logger.info("=" * 60)
    logger.info("🔍 НАЧИНАЮ АВТОПОИСК УМНЫХ КОШЕЛЬКОВ")
    logger.info("=" * 60)

    logger.info("📊 Шаг 1: Получаю топ токенов...")
    tokens = get_top_tokens(limit=TOP_TOKENS_LIMIT)
    if not tokens:
        return {"error": "Не удалось получить топ токенов"}

    logger.info("")
    logger.info("👥 Шаг 2: Ищу трейдеров...")
    candidates = find_candidates(tokens)

    if not candidates:
        return {"error": "Не найдено активных кандидатов"}

    logger.info("")
    logger.info(f"  Найдено {len(candidates)} кандидатов:")
    for c in candidates[:10]:
        logger.info(f"    {c['wallet'][:8]}... — role={c['role']}, токенов={c['count']}")

    logger.info("")
    logger.info("🧠 Шаг 3: Проверяю через Smart Money...")
    valid = []
    for i, cand in enumerate(candidates[:MAX_VALIDATE]):
        wallet = cand["wallet"]
        logger.info(f"  [{i+1}/{min(len(candidates), MAX_VALIDATE)}] {wallet[:8]}... (role={cand['role']}, {cand['count']} токенов)")

        info = validate_candidate(wallet)
        if info:
            info["role"] = cand["role"]
            info["tokens_bought"] = cand["tokens"]
            valid.append(info)
            win_str = f"{info['win_rate']:.0f}%" if info['win_rate'] is not None else "—"
            pnl_str = f"{info['pnl_sol']:+.4f}" if info['pnl_sol'] is not None else "—"
            logger.info(f"    ✅ ПОДХОДИТ! win={win_str}, pnl={pnl_str} SOL")
        time.sleep(0.5)

    logger.info("")
    logger.info(f"✅ Найдено {len(valid)} валидных кошельков")

    # Сортировка: сначала с закрытыми и положительным PnL
    def sort_key(x):
        has_closed = 1 if x["closed"] else 0
        has_pnl = 1 if (x["pnl_sol"] is not None and x["pnl_sol"] > 0) else 0
        win = x["win_rate"] if x["win_rate"] is not None else 0
        return (has_closed, has_pnl, win)

    valid.sort(key=sort_key, reverse=True)

    return {
        "tokens_checked": len(tokens),
        "candidates_found": len(candidates),
        "valid_wallets": valid[:5],
    }