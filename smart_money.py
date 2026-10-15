import os
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

if not HELIUS_API_KEY:
    raise ValueError("Нет HELIUS_API_KEY в .env файле!")

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Уменьшили для экономии памяти
TX_LIMIT = 15

_token_names = {}
_token_prices = {}
_sol_price_usd = None


# ─────────────────────────────────────────────
# Имена и цены
# ─────────────────────────────────────────────

def get_token_name(mint: str):
    if mint in _token_names:
        return _token_names[mint]
    if mint == SOL_MINT:
        _token_names[mint] = "SOL"
        return "SOL"

    payload = {"jsonrpc": "2.0", "id": "1", "method": "getAsset", "params": {"id": mint}}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = data.get("result")
        if not result:
            _token_names[mint] = mint[:6]
            return _token_names[mint]
        content = result.get("content", {})
        metadata = content.get("metadata", {})
        name = metadata.get("name") or metadata.get("symbol") or mint[:6]
        symbol = metadata.get("symbol", "")
        display = f"{name} ({symbol})" if symbol and symbol != name else name
        _token_names[mint] = display[:30]
        return _token_names[mint]
    except Exception:
        _token_names[mint] = mint[:6]
        return _token_names[mint]


def get_sol_price_usd():
    global _sol_price_usd
    if _sol_price_usd is not None:
        return _sol_price_usd
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}", timeout=10)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs", [])
        if pairs:
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            _sol_price_usd = float(best.get("priceUsd", 0) or 0)
            return _sol_price_usd
    except Exception:
        pass
    _sol_price_usd = 0
    return 0


def get_token_price_usd(mint: str):
    if mint in _token_prices:
        return _token_prices[mint]
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=10)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            _token_prices[mint] = None
            return None
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        price = float(best.get("priceUsd", 0) or 0)
        _token_prices[mint] = price if price > 0 else None
        return _token_prices[mint]
    except Exception:
        _token_prices[mint] = None
        return None


# ─────────────────────────────────────────────
# Балансы
# ─────────────────────────────────────────────

def get_sol_balance(address: str):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        lamports = data.get("result", {}).get("value", 0)
        return lamports / 1_000_000_000
    except Exception as e:
        logger.warning(f"Не удалось получить SOL-баланс: {e}")
        return 0


def get_token_balances(address: str):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            address,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ],
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        accounts = data.get("result", {}).get("value", [])
        balances = []
        for acc in accounts:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = info.get("mint")
            token_amount = info.get("tokenAmount", {})
            ui_amount = float(token_amount.get("uiAmount", 0) or 0)
            if ui_amount > 0:
                balances.append({
                    "mint": mint, "amount": ui_amount,
                    "decimals": token_amount.get("decimals", 0),
                })
        return balances
    except Exception as e:
        logger.warning(f"Не удалось получить токен-балансы: {e}")
        return []


def summarize_portfolio(address: str):
    sol_price = get_sol_price_usd()
    sol_balance = get_sol_balance(address)
    sol_value_usd = sol_balance * sol_price if sol_price else 0

    token_list = get_token_balances(address)
    tokens_with_value = []
    for t in token_list:
        mint = t["mint"]
        amount = t["amount"]
        price_usd = get_token_price_usd(mint)
        value_usd = amount * price_usd if price_usd else 0
        name = get_token_name(mint)
        tokens_with_value.append({
            "mint": mint, "name": name, "amount": amount,
            "price_usd": price_usd, "value_usd": value_usd,
        })

    tokens_with_value.sort(key=lambda x: x["value_usd"], reverse=True)
    total_tokens_value = sum(t["value_usd"] for t in tokens_with_value)

    return {
        "sol_balance": sol_balance,
        "sol_value_usd": sol_value_usd,
        "sol_price_usd": sol_price,
        "tokens": tokens_with_value,
        "tokens_count": len(tokens_with_value),
        "total_tokens_value_usd": total_tokens_value,
        "total_portfolio_usd": sol_value_usd + total_tokens_value,
    }


# ─────────────────────────────────────────────
# Транзакции через ОБЫЧНЫЙ RPC
# ─────────────────────────────────────────────

def fetch_signatures(address: str, limit: int = TX_LIMIT):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        logger.warning(f"Ошибка getSignaturesForAddress: {e}")
        return []


def fetch_transaction_detail(sig: str):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data.get("result")
    except Exception as e:
        logger.debug(f"Ошибка getTransaction {sig[:8]}: {e}")
        return None


def fetch_all_txs(address: str, max_pages: int = 1):
    sigs = fetch_signatures(address, limit=TX_LIMIT)
    if not sigs:
        return []

    all_txs = []
    for sig_info in sigs[:TX_LIMIT]:
        sig = sig_info.get("signature")
        if not sig:
            continue

        tx = fetch_transaction_detail(sig)
        if not tx:
            continue

        tx["timestamp"] = sig_info.get("blockTime") or 0

        # Определяем тип
        if tx.get("meta"):
            pre = len(tx["meta"].get("preTokenBalances", []))
            post = len(tx["meta"].get("postTokenBalances", []))
            tx["type"] = "SWAP" if (pre > 0 or post > 0) else "TRANSFER"

        all_txs.append(tx)
        time.sleep(0.05)

    return all_txs


# ─────────────────────────────────────────────
# Парсер через балансы
# ─────────────────────────────────────────────

def parse_swap_balances(tx, wallet_address):
    meta = tx.get("meta", {})
    if not meta:
        return None

    pre_balances = meta.get("preTokenBalances", [])
    post_balances = meta.get("postTokenBalances", [])

    changes = {}

    for b in pre_balances:
        owner = b.get("owner")
        if owner != wallet_address:
            continue
        mint = b.get("mint")
        if not mint:
            continue
        amount = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
        if mint not in changes:
            changes[mint] = {"pre": 0, "post": 0}
        changes[mint]["pre"] = amount

    for b in post_balances:
        owner = b.get("owner")
        if owner != wallet_address:
            continue
        mint = b.get("mint")
        if not mint:
            continue
        amount = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
        if mint not in changes:
            changes[mint] = {"pre": 0, "post": 0}
        changes[mint]["post"] = amount

    if not changes:
        return None

    sol_change = 0.0
    try:
        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        pre_sol = meta.get("preBalances", [])
        post_sol = meta.get("postBalances", [])
        for i, key in enumerate(account_keys):
            if isinstance(key, dict):
                pubkey = key.get("pubkey")
            else:
                pubkey = key
            if pubkey == wallet_address:
                if i < len(pre_sol) and i < len(post_sol):
                    sol_change = (post_sol[i] - pre_sol[i]) / 1_000_000_000
                break
    except Exception:
        sol_change = 0.0

    significant_changes = []
    for mint, amt in changes.items():
        if mint == SOL_MINT:
            continue
        delta = amt["post"] - amt["pre"]
        if abs(delta) > 0:
            significant_changes.append({
                "mint": mint, "delta": delta,
                "pre": amt["pre"], "post": amt["post"],
            })

    if not significant_changes:
        return None

    significant_changes.sort(key=lambda x: abs(x["delta"]), reverse=True)
    main = significant_changes[0]
    timestamp = tx.get("timestamp") or tx.get("blockTime") or 0

    if main["delta"] > 0:
        sol_spent = abs(sol_change) if sol_change < 0 else 0
        return {
            "action": "BUY", "token": main["mint"],
            "token_amount": main["delta"], "sol_amount": sol_spent,
            "timestamp": timestamp,
        }

    if main["delta"] < 0:
        sol_received = sol_change if sol_change > 0 else 0
        return {
            "action": "SELL", "token": main["mint"],
            "token_amount": abs(main["delta"]), "sol_amount": sol_received,
            "timestamp": timestamp,
        }

    return None


def analyze_pnl(txs, wallet_address):
    sorted_txs = sorted(txs, key=lambda t: t.get("timestamp", 0) or t.get("blockTime", 0) or 0)
    positions = {}
    closed = []
    buys_count = 0
    sells_count = 0

    for tx in sorted_txs:
        parsed = parse_swap_balances(tx, wallet_address)
        if not parsed:
            continue

        token = parsed["token"]
        amount = parsed["token_amount"]
        sol_amount = parsed["sol_amount"]

        if parsed["action"] == "BUY":
            buys_count += 1
            positions.setdefault(token, []).append({
                "amount": amount, "sol_spent": sol_amount,
                "timestamp": parsed["timestamp"],
            })

        elif parsed["action"] == "SELL":
            sells_count += 1
            if token not in positions or not positions[token]:
                continue

            remaining = amount
            tokens_matched = 0
            sol_spent_matched = 0.0

            while remaining > 0 and positions[token]:
                buy = positions[token][0]
                if buy["amount"] <= remaining:
                    tokens_matched += buy["amount"]
                    sol_spent_matched += buy["sol_spent"]
                    remaining -= buy["amount"]
                    positions[token].pop(0)
                else:
                    portion = remaining / buy["amount"]
                    tokens_matched += remaining
                    sol_spent_matched += buy["sol_spent"] * portion
                    buy["sol_spent"] *= (1 - portion)
                    buy["amount"] -= remaining
                    remaining = 0

            if tokens_matched > 0:
                sol_received_matched = sol_amount * (tokens_matched / amount) if amount else 0
                pnl_sol = sol_received_matched - sol_spent_matched
                pnl_pct = (pnl_sol / sol_spent_matched * 100) if sol_spent_matched > 0 else 0
                closed.append({
                    "token": token, "tokens_matched": tokens_matched,
                    "sol_spent": sol_spent_matched, "sol_received": sol_received_matched,
                    "pnl_sol": pnl_sol, "pnl_percent": pnl_pct,
                    "timestamp": parsed["timestamp"],
                })

    return closed, positions, buys_count, sells_count


# ─────────────────────────────────────────────
# Анализ кошелька
# ─────────────────────────────────────────────

def analyze_wallet(address, include_portfolio=True):
    """Анализ кошелька. include_portfolio=False экономит память."""
    logger.info(f"🔍 Анализ: {address[:16]}...")

    portfolio = None
    if include_portfolio:
        portfolio = summarize_portfolio(address)

    txs = fetch_all_txs(address, max_pages=1)
    logger.info(f"  📥 Загружено {len(txs)} транзакций через RPC")

    if not txs:
        return {
            "verdict": "UNKNOWN",
            "reason": "Не удалось загрузить транзакции",
            "stats": {"swaps_recent": 0, "buys": 0, "sells": 0, "closed_with_sol": 0,
                      "open_positions": 0, "win_rate": None, "total_pnl_sol": None},
            "portfolio": portfolio,
        }

    for i, tx in enumerate(txs[:3]):
        tx_type = tx.get("type", "?")
        meta = tx.get("meta", {})
        pre_count = len(meta.get("preTokenBalances", []))
        post_count = len(meta.get("postTokenBalances", []))
        logger.info(f"  TX#{i+1}: type={tx_type}, preBal={pre_count}, postBal={post_count}")

    now = time.time()
    txs_recent = [t for t in txs if (t.get("timestamp", 0) or t.get("blockTime", 0) or 0) > now - 3 * 86400]
    swaps_recent_count = len(txs_recent)
    logger.info(f"  📅 За 3 дня: {swaps_recent_count}")

    closed, open_positions, buys, sells = analyze_pnl(txs_recent, address)

    del txs
    del txs_recent

    logger.info(f"  🛒 Покупок: {buys}, Продаж: {sells}, Закрытых: {len(closed)}, Открытых: {len(open_positions)}")

    closed_with_sol = [p for p in closed if p["sol_spent"] > 0]
    total_pnl_sol = 0
    win_rate = 0
    avg_win = 0
    avg_loss = 0

    if closed_with_sol:
        winners = [p for p in closed_with_sol if p["pnl_sol"] > 0]
        losers = [p for p in closed_with_sol if p["pnl_sol"] <= 0]
        total_pnl_sol = sum(p["pnl_sol"] for p in closed_with_sol)
        win_rate = (len(winners) / len(closed_with_sol) * 100) if closed_with_sol else 0
        avg_win = sum(p["pnl_sol"] for p in winners) / len(winners) if winners else 0
        avg_loss = sum(p["pnl_sol"] for p in losers) / len(losers) if losers else 0

    if closed_with_sol:
        if win_rate >= 50 and total_pnl_sol > 0 and len(closed_with_sol) >= 3:
            verdict = "PASS"
            reason = f"✅ Прибыльный: win {win_rate:.0f}%, PnL {total_pnl_sol:+.4f} SOL."
        elif total_pnl_sol > 0:
            verdict = "WEAK"
            reason = f"⚠️  Слабо прибыльный: win {win_rate:.0f}%, PnL {total_pnl_sol:+.4f} SOL."
        else:
            verdict = "REJECT"
            reason = f"❌ Убыточный: win {win_rate:.0f}%, PnL {total_pnl_sol:+.4f} SOL."
    else:
        verdict = "UNKNOWN"
        reason = f"❓ Нет закрытых с SOL. Покупок: {buys}, продаж: {sells}."

    return {
        "verdict": verdict,
        "reason": reason,
        "stats": {
            "swaps_recent": swaps_recent_count,
            "buys": buys, "sells": sells,
            "closed_positions": len(closed),
            "closed_with_sol": len(closed_with_sol),
            "open_positions": len(open_positions),
            "win_rate": round(win_rate, 1) if closed_with_sol else None,
            "total_pnl_sol": round(total_pnl_sol, 4) if closed_with_sol else None,
            "avg_win_sol": round(avg_win, 4) if closed_with_sol else None,
            "avg_loss_sol": round(avg_loss, 4) if closed_with_sol else None,
        },
        "portfolio": portfolio,
    }


if __name__ == "__main__":
    pass