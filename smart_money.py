import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

if not HELIUS_API_KEY:
    raise ValueError("Нет HELIUS_API_KEY в .env файле!")

BASE_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
SOL_MINT = "So11111111111111111111111111111111111111112"

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

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "getAsset",
        "params": {"id": mint},
    }
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
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}",
            timeout=10,
        )
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
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=10,
        )
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
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address],
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        lamports = data.get("result", {}).get("value", 0)
        return lamports / 1_000_000_000
    except Exception as e:
        print(f"⚠️  Не удалось получить SOL-баланс: {e}")
        return 0


def get_token_balances(address: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
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
                    "mint": mint,
                    "amount": ui_amount,
                    "decimals": token_amount.get("decimals", 0),
                })
        return balances
    except Exception as e:
        print(f"⚠️  Не удалось получить токен-балансы: {e}")
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
            "mint": mint,
            "name": name,
            "amount": amount,
            "price_usd": price_usd,
            "value_usd": value_usd,
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
# Транзакции
# ─────────────────────────────────────────────

def fetch_transactions(address, limit=100, before=None):
    url = BASE_URL.format(address=address)
    params = {
        "api-key": HELIUS_API_KEY,
        "limit": limit,
        "type": "SWAP",
    }
    if before:
        params["before"] = before
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"⚠️  Ошибка запроса: {e}")
        return []


def fetch_all_swaps(address, max_pages=5):
    all_txs = []
    before = None
    for page in range(max_pages):
        txs = fetch_transactions(address, limit=100, before=before)
        if not txs:
            break
        all_txs.extend(txs)
        print(f"📥 Страница {page + 1}: +{len(txs)} транзакций (всего {len(all_txs)})")
        if len(txs) < 100:
            break
        before = txs[-1]["signature"]
        time.sleep(0.5)
    return all_txs


def parse_swap(tx, wallet_address):
    if tx.get("type") != "SWAP":
        return None
    transfers = tx.get("tokenTransfers", [])
    if not transfers:
        return None

    sent = []
    received = []
    sol_sent = 0.0
    sol_received = 0.0

    for t in transfers:
        mint = t.get("mint", "")
        amount = float(t.get("tokenAmount", 0) or 0)
        from_acc = t.get("fromUserAccount", "")
        to_acc = t.get("toUserAccount", "")

        if from_acc == wallet_address:
            if mint == SOL_MINT:
                sol_sent += amount
            else:
                sent.append({"mint": mint, "amount": amount})
        elif to_acc == wallet_address:
            if mint == SOL_MINT:
                sol_received += amount
            else:
                received.append({"mint": mint, "amount": amount})

    if received and not sent:
        token = received[0]
        return {
            "action": "BUY",
            "token": token["mint"],
            "token_amount": token["amount"],
            "sol_amount": sol_sent,
            "timestamp": tx.get("timestamp"),
        }
    if sent and not received:
        token = sent[0]
        return {
            "action": "SELL",
            "token": token["mint"],
            "token_amount": token["amount"],
            "sol_amount": sol_received,
            "timestamp": tx.get("timestamp"),
        }
    return None


def analyze_pnl(swaps, wallet_address):
    sorted_swaps = sorted(swaps, key=lambda t: t.get("timestamp", 0))
    positions = {}
    closed = []
    buys_count = 0
    sells_count = 0

    for tx in sorted_swaps:
        parsed = parse_swap(tx, wallet_address)
        if not parsed:
            continue
        token = parsed["token"]
        amount = parsed["token_amount"]
        sol_amount = parsed["sol_amount"]

        if parsed["action"] == "BUY":
            buys_count += 1
            positions.setdefault(token, []).append({
                "amount": amount,
                "sol_spent": sol_amount,
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
                    "token": token,
                    "tokens_matched": tokens_matched,
                    "sol_spent": sol_spent_matched,
                    "sol_received": sol_received_matched,
                    "pnl_sol": pnl_sol,
                    "pnl_percent": pnl_pct,
                    "timestamp": parsed["timestamp"],
                })
    return closed, positions, buys_count, sells_count


def summarize_open_positions(positions):
    sol_price = get_sol_price_usd()
    summary = []
    for token, buys in positions.items():
        if not buys:
            continue
        total_tokens = sum(b["amount"] for b in buys)
        total_sol = sum(b["sol_spent"] for b in buys)
        if total_tokens <= 0:
            continue

        avg_price_sol = total_sol / total_tokens if total_tokens else 0
        first_ts = min(b["timestamp"] for b in buys)

        token_price_usd = get_token_price_usd(token)
        current_value_usd = None
        unrealized_pnl_sol = None
        unrealized_pnl_pct = None

        if token_price_usd and sol_price:
            current_value_usd = total_tokens * token_price_usd
            current_value_sol = current_value_usd / sol_price
            unrealized_pnl_sol = current_value_sol - total_sol
            if total_sol > 0:
                unrealized_pnl_pct = (unrealized_pnl_sol / total_sol) * 100

        summary.append({
            "token": token,
            "tokens": total_tokens,
            "sol_invested": total_sol,
            "avg_price_sol": avg_price_sol,
            "first_buy_ts": first_ts,
            "unrealized_pnl_sol": unrealized_pnl_sol,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "current_value_usd": current_value_usd,
        })
    summary.sort(key=lambda x: x["sol_invested"], reverse=True)
    return summary


def analyze_wallet(address):
    print(f"\n🔍 Анализируем кошелёк: {address}\n")

    print("💰 Считаем баланс кошелька...")
    portfolio = summarize_portfolio(address)

    txs = fetch_all_swaps(address, max_pages=5)
    if not txs:
        return {
            "verdict": "UNKNOWN",
            "reason": "Не удалось загрузить транзакции",
            "stats": {},
            "portfolio": portfolio,
        }

    print(f"\n📊 Всего свопов: {len(txs)}")

    now = time.time()
    # ОКНО 3 ДНЯ (вместо 30)
    txs_recent = [t for t in txs if t.get("timestamp", 0) > now - 3 * 86400]
    print(f"📅 За 3 дня: {len(txs_recent)}")

    closed, open_positions, buys, sells = analyze_pnl(txs_recent, address)

    print(f"\n🛒 Покупок: {buys}")
    print(f"💸 Продаж: {sells}")
    print(f"📈 Закрытых позиций: {len(closed)}")
    print(f"📦 Открытых токенов: {len(open_positions)}")

    closed_with_sol = []
    total_pnl_sol = 0
    win_rate = 0
    avg_win = 0
    avg_loss = 0

    if closed:
        closed_with_sol = [p for p in closed if p["sol_spent"] > 0]
        winners = [p for p in closed_with_sol if p["pnl_sol"] > 0]
        losers = [p for p in closed_with_sol if p["pnl_sol"] <= 0]

        total_pnl_sol = sum(p["pnl_sol"] for p in closed_with_sol)
        win_rate = (len(winners) / len(closed_with_sol) * 100) if closed_with_sol else 0

        avg_win = sum(p["pnl_sol"] for p in winners) / len(winners) if winners else 0
        avg_loss = sum(p["pnl_sol"] for p in losers) / len(losers) if losers else 0

        print(f"\n{'=' * 60}")
        print(f"💎 Закрытых с точным SOL: {len(closed_with_sol)}")
        print(f"💰 Реализованный PnL: {total_pnl_sol:+.4f} SOL")
        print(f"🎯 Win rate: {win_rate:.1f}% ({len(winners)} / {len(closed_with_sol)})")
        if winners:
            print(f"🟢 Средний выигрыш: +{avg_win:.4f} SOL")
        if losers:
            print(f"🔴 Средний проигрыш: {avg_loss:.4f} SOL")

        sorted_by_pnl = sorted(closed_with_sol, key=lambda p: p["pnl_sol"], reverse=True)

        print(f"\n🏆 Топ-3 прибыльных:")
        shown = 0
        for p in sorted_by_pnl:
            if p["pnl_sol"] > 0 and shown < 3:
                name = get_token_name(p["token"])
                print(f"   {name:<28} → {p['pnl_sol']:+.4f} SOL ({p['pnl_percent']:+.1f}%)")
                shown += 1

        print(f"\n💀 Топ-3 убыточных:")
        shown = 0
        for p in reversed(sorted_by_pnl):
            if p["pnl_sol"] < 0 and shown < 3:
                name = get_token_name(p["token"])
                print(f"   {name:<28} → {p['pnl_sol']:+.4f} SOL ({p['pnl_percent']:+.1f}%)")
                shown += 1

        print(f"{'=' * 60}")

    open_summary = summarize_open_positions(open_positions)
    total_open_sol = 0
    total_unrealized_sol = 0

    if open_summary:
        total_open_sol = sum(p["sol_invested"] for p in open_summary)
        known_pnl = [p["unrealized_pnl_sol"] for p in open_summary if p["unrealized_pnl_sol"] is not None]
        total_unrealized_sol = sum(known_pnl)

        print(f"\n{'=' * 60}")
        print(f"📦 ОТКРЫТЫЕ ПОЗИЦИИ (топ-10, всего: {len(open_summary)})")
        print(f"💼 Суммарно вложено: {total_open_sol:.4f} SOL")
        print(f"📊 Нереализованный PnL: {total_unrealized_sol:+.4f} SOL "
              f"({len(known_pnl)}/{len(open_summary)} с ценой)")
        print(f"{'-' * 60}")

        for i, pos in enumerate(open_summary[:10]):
            name = get_token_name(pos["token"])
            dt = time.strftime("%Y-%m-%d", time.localtime(pos["first_buy_ts"]))
            try:
                if pos["unrealized_pnl_sol"] is not None and pos["unrealized_pnl_pct"] is not None:
                    pnl_str = f"{pos['unrealized_pnl_sol']:+.4f} SOL ({pos['unrealized_pnl_pct']:+.1f}%)"
                else:
                    pnl_str = "нет цены"
            except (TypeError, ValueError):
                pnl_str = "нет цены"
            print(
                f"#{i+1:2} {name:<26} "
                f"| {pos['tokens']:>15,.0f} tok "
                f"| {pos['sol_invested']:.4f} SOL "
                f"| PnL: {pnl_str} "
                f"| {dt}"
            )
        print(f"{'=' * 60}")

    if closed_with_sol:
        if win_rate >= 50 and total_pnl_sol > 0 and len(closed_with_sol) >= 3:
            verdict = "PASS"
            reason = f"✅ Прибыльный: win rate {win_rate:.0f}%, PnL {total_pnl_sol:+.4f} SOL."
        elif total_pnl_sol > 0:
            verdict = "WEAK"
            reason = f"⚠️  Слабо прибыльный: win rate {win_rate:.0f}%, PnL {total_pnl_sol:+.4f} SOL."
        else:
            verdict = "REJECT"
            reason = f"❌ Убыточный: win rate {win_rate:.0f}%, PnL {total_pnl_sol:+.4f} SOL."
    else:
        verdict = "UNKNOWN"
        reason = f"❓ Нет закрытых позиций. Покупок: {buys}, продаж: {sells}."

    return {
        "verdict": verdict,
        "reason": reason,
        "stats": {
            "swaps_recent": len(txs_recent),
            "buys": buys,
            "sells": sells,
            "closed_positions": len(closed),
            "closed_with_sol": len(closed_with_sol),
            "open_positions": len(open_summary),
            "win_rate": round(win_rate, 1) if closed_with_sol else None,
            "total_pnl_sol": round(total_pnl_sol, 4) if closed_with_sol else None,
            "avg_win_sol": round(avg_win, 4) if closed_with_sol else None,
            "avg_loss_sol": round(avg_loss, 4) if closed_with_sol else None,
            "total_open_sol": round(total_open_sol, 4) if open_summary else 0,
            "total_unrealized_sol": round(total_unrealized_sol, 4) if open_summary else 0,
        },
        "portfolio": portfolio,
    }


if __name__ == "__main__":
    pass