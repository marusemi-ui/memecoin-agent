"""
Discovery — автопоиск «умных кошельков».
Алгоритм:
1. Берём топ-20 токенов по объёму за 24ч (DexScreener)
2. Для каждого токена вытаскиваем крупных покупателей (Helius)
3. Считаем, сколько разных токенов купил каждый кошелёк
4. Прогоняем кандидатов через Smart Money (PnL, win rate)
5. Отсеиваем ботов и убыточных
6. Возвращаем топ-5
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

BASE_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Настройки поиска
TOP_TOKENS_LIMIT = 10        # сколько топовых токенов проверять
MIN_TOKEN_APPEARANCE = 2     # минимум токенов, чтобы считать кошелёк активным
MIN_WIN_RATE = 40.0          # минимальный win rate
MIN_PNL_SOL = 0.0            # минимальный PnL в SOL
MAX_TRADES_PER_DAY = 200     # больше = бот
MAX_CANDIDATES = 30          # максимум кандидатов для анализа


# ─────────────────────────────────────────────
# 1. Топ токенов через DexScreener
# ─────────────────────────────────────────────

def get_top_tokens(limit: int = TOP_TOKENS_LIMIT):
    """
    Возвращает список топовых токенов по объёму за 24ч.
    Использует публичный DexScreener API (без ключа).
    """
    # У DexScreener нет публичного "top tokens" эндпоинта.
    # Но можно взять токены через поиск по популярным парам.
    # Используем неофициальный эндпоинт.

    url = "https://api.dexscreener.com/latest/dex/search?q=SOL"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        pairs = data.get("pairs", [])

        # Фильтруем: только Solana, только с ликвидностью > $50k
        sol_pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and float(p.get("liquidity", {}).get("usd", 0) or 0) > 50_000
        ]

        # Сортируем по объёму за 24ч
        sol_pairs.sort(
            key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0),
            reverse=True,
        )

        # Достаём уникальные mint-адреса
        seen = set()
        tokens = []
        for p in sol_pairs:
            mint = p.get("baseToken", {}).get("address")
            if mint and mint not in seen and mint != SOL_MINT:
                seen.add(mint)
                tokens.append({
                    "mint": mint,
                    "symbol": p.get("baseToken", {}).get("symbol", "?"),
                    "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                    "liquidity": float(p.get("liquidity", {}).get("usd", 0) or 0),
                })
            if len(tokens) >= limit:
                break

        return tokens

    except Exception as e:
        logger.error(f"Ошибка получения топ токенов: {e}")
        return []


# ─────────────────────────────────────────────
# 2. Покупатели токена через Helius
# ─────────────────────────────────────────────

def get_token_buyers(token_mint: str, limit: int = 50):
    """
    Возвращает список адресов, которые купили токен.
    Использует Helius Enhanced Transactions API.
    """
    # Для токена мы не можем напрямую запросить транзакции.
    # Helius Enhanced работает по адресу кошелька.
    # Поэтому используем RPC: getSignaturesForAddress для самого токена.

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
            return []

        # Для каждой подписи получаем транзакцию, ищем покупателей
        buyers = set()
        for sig_info in signatures[:20]:  # ограничиваем — экономим кредиты
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

                # Ищем pre/post token balances: у кого вырос баланс этого токена
                meta = result.get("meta", {})
                post_balances = meta.get("postTokenBalances", [])
                pre_balances = meta.get("preTokenBalances", [])

                pre_map = {b.get("accountIndex"): float(b.get("uiTokenAmount", {}).get("uiAmount") or 0) for b in pre_balances}
                post_map = {b.get("accountIndex"): float(b.get("uiTokenAmount", {}).get("uiAmount") or 0) for b in post_balances}

                # Ищем аккаунты, у которых баланс вырос
                account_keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])

                for idx_str, post_amount in post_map.items():
                    try:
                        idx = int(idx_str)
                    except (ValueError, TypeError):
                        continue

                    pre_amount = pre_map.get(idx_str, 0)
                    if post_amount > pre_amount and idx < len(account_keys):
                        account = account_keys[idx]
                        # account может быть dict (jsonParsed) или str
                        if isinstance(account, dict):
                            pubkey = account.get("pubkey")
                        else:
                            pubkey = account
                        if pubkey:
                            buyers.add(pubkey)
            except Exception as e:
                logger.debug(f"Ошибка парсинга tx {sig[:8]}: {e}")
                continue

            time.sleep(0.1)  # не долбим RPC

        return list(buyers)

    except Exception as e:
        logger.error(f"Ошибка получения покупателей {token_mint[:8]}: {e}")
        return []


# ─────────────────────────────────────────────
# 3. Считаем частоту покупок
# ─────────────────────────────────────────────

def find_active_buyers(tokens: list, min_appearance: int = MIN_TOKEN_APPEARANCE):
    """
    Находит кошельки, которые купили несколько разных токенов из топа.
    """
    wallet_counts = {}

    for i, token in enumerate(tokens):
        mint = token["mint"]
        symbol = token["symbol"]
        logger.info(f"  [{i+1}/{len(tokens)}] Ищу покупателей {symbol}...")

        buyers = get_token_buyers(mint)
        logger.info(f"    Найдено {len(buyers)} адресов")

        for wallet in buyers:
            if wallet not in wallet_counts:
                wallet_counts[wallet] = {
                    "count": 0,
                    "tokens": [],
                }
            wallet_counts[wallet]["count"] += 1
            wallet_counts[wallet]["tokens"].append(symbol)

        time.sleep(0.3)

    # Оставляем тех, кто купил >= min_appearance токенов
    active = [
        {
            "wallet": w,
            "count": data["count"],
            "tokens": data["tokens"],
        }
        for w, data in wallet_counts.items()
        if data["count"] >= min_appearance
    ]

    active.sort(key=lambda x: x["count"], reverse=True)
    return active[:MAX_CANDIDATES]


# ─────────────────────────────────────────────
# 4. Проверка кандидата через Smart Money
# ─────────────────────────────────────────────

def validate_candidate(wallet: str):
    """
    Проверяет кандидата через Smart Money.
    Возвращает dict с метриками или None, если не подходит.
    """
    try:
        result = analyze_wallet(wallet)
        stats = result.get("stats", {}) or {}

        win_rate = stats.get("win_rate")
        pnl = stats.get("total_pnl_sol")
        closed = stats.get("closed_with_sol", 0)
        buys = stats.get("buys", 0)
        sells = stats.get("sells", 0)

        total_trades = buys + sells
        trades_per_day = total_trades / 30 if total_trades else 0

        # Отсеиваем ботов
        if trades_per_day > MAX_TRADES_PER_DAY:
            logger.info(f"    ⏭️  {wallet[:8]}... — бот ({trades_per_day:.0f} сделок/день)")
            return None

        # Отсеиваем тех, у кого нет закрытых позиций
        if not closed or win_rate is None:
            logger.info(f"    ⏭️  {wallet[:8]}... — нет закрытых позиций")
            return None

        # Отсеиваем убыточных
        if win_rate < MIN_WIN_RATE:
            logger.info(f"    ⏭️  {wallet[:8]}... — win rate {win_rate:.0f}% < {MIN_WIN_RATE}%")
            return None

        if pnl is not None and pnl < MIN_PNL_SOL:
            logger.info(f"    ⏭️  {wallet[:8]}... — PnL {pnl:.4f} SOL < {MIN_PNL_SOL}")
            return None

        return {
            "wallet": wallet,
            "win_rate": win_rate,
            "pnl_sol": pnl,
            "closed": closed,
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

def discover_smart_wallets(progress_callback=None):
    """
    Главная функция автопоиска.
    Возвращает список кошельков, прошедших проверку.
    """
    logger.info("🔍 Начинаю автопоиск умных кошельков...")

    # 1. Топ токенов
    logger.info("📊 Получаю топ токенов...")
    tokens = get_top_tokens(limit=TOP_TOKENS_LIMIT)
    if not tokens:
        return {"error": "Не удалось получить топ токенов"}

    logger.info(f"  Найдено {len(tokens)} токенов")
    for t in tokens:
        logger.info(f"    - {t['symbol']}: ${t['volume_24h']:,.0f} объём")

    # 2. Активные покупатели
    logger.info("👥 Ищу активных покупателей...")
    candidates = find_active_buyers(tokens, min_appearance=MIN_TOKEN_APPEARANCE)

    if not candidates:
        return {"error": "Не найдено активных покупателей"}

    logger.info(f"  Найдено {len(candidates)} кандидатов")

    # 3. Валидация через Smart Money
    logger.info("🧠 Проверяю кандидатов через Smart Money...")
    valid = []
    for i, cand in enumerate(candidates[:15]):  # максимум 15 на анализ
        wallet = cand["wallet"]
        logger.info(f"  [{i+1}] Проверяю {wallet[:8]}... ({cand['count']} токенов)")

        info = validate_candidate(wallet)
        if info:
            info["tokens_bought"] = cand["tokens"]
            valid.append(info)
            logger.info(
                f"    ✅ Подходит! Win rate: {info['win_rate']:.0f}%, "
                f"PnL: {info['pnl_sol']:+.4f} SOL"
            )

        # Пауза между кандидатами
        time.sleep(1)

    # Сортируем по win rate, потом по PnL
    valid.sort(key=lambda x: (x["win_rate"], x["pnl_sol"]), reverse=True)

    return {
        "tokens_checked": len(tokens),
        "candidates_found": len(candidates),
        "valid_wallets": valid[:5],  # топ-5
    }