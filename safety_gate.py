import os
import requests
from dotenv import load_dotenv

load_dotenv()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

if not HELIUS_API_KEY:
    raise ValueError("Нет HELIUS_API_KEY в .env файле!")

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

DANGEROUS_EXTENSIONS = {
    "permanentDelegate": "Permanent Delegate — разработчик может забрать твои токены",
    "transferHook": "Transfer Hook — продажа может быть заблокирована",
    "defaultAccountState": "Default Account State — аккаунты могут быть заморожены",
    "transferFeeConfig": "Transfer Fee — скрытый налог на продажу",
}


def rpc_call(method, params):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    response = requests.post(RPC_URL, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def get_mint_info(token_mint: str):
    result = rpc_call("getAccountInfo", [token_mint, {"encoding": "jsonParsed"}])
    if "result" not in result or result["result"] is None:
        return None

    account_data = result["result"]["value"]
    if account_data is None:
        return None

    owner = account_data.get("owner")
    parsed = account_data["data"]["parsed"]["info"]

    extensions = parsed.get("extensions", [])
    extension_names = [ext.get("extension") for ext in extensions]

    return {
        "owner": owner,
        "mint_authority": parsed.get("mintAuthority"),
        "freeze_authority": parsed.get("freezeAuthority"),
        "decimals": parsed.get("decimals"),
        "supply": parsed.get("supply"),
        "extensions": extension_names,
        "raw_extensions": extensions,
    }


def check_token_2022_extensions(mint_info: dict):
    if mint_info["owner"] != TOKEN_2022_PROGRAM_ID:
        return None

    extensions = mint_info["extensions"]
    print(f"⚠️  Token-2022 обнаружен. Расширения: {extensions}")

    for ext_name, reason in DANGEROUS_EXTENSIONS.items():
        if ext_name in extensions:
            if ext_name == "transferFeeConfig":
                for raw_ext in mint_info["raw_extensions"]:
                    if raw_ext.get("extension") == "transferFeeConfig":
                        state = raw_ext.get("state", {})
                        newer_fee = state.get("newerTransferFee", {})
                        bps = int(newer_fee.get("transferFeeBasisPoints", 0))
                        percent = bps / 100
                        if percent > 5:
                            return f"❌ Transfer Fee = {percent}% — слишком высокий налог"
                continue
            return f"❌ {reason}"

    return None


def get_token_market_cap(token_mint: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        pairs = data.get("pairs", [])
        if not pairs:
            return None

        best_pair = max(
            pairs,
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
        )

        return {
            "fdv": best_pair.get("fdv"),
            "market_cap": best_pair.get("marketCap"),
            "liquidity_usd": float(best_pair.get("liquidity", {}).get("usd", 0) or 0),
            "price_usd": best_pair.get("priceUsd"),
            "dex": best_pair.get("dexId"),
            "symbol": best_pair.get("baseToken", {}).get("symbol"),
        }
    except Exception as e:
        print(f"⚠️  Не удалось получить market cap: {e}")
        return None


def get_concentration_threshold(fdv_usd):
    if fdv_usd is None:
        return 30
    if fdv_usd < 500_000:
        return 25
    elif fdv_usd < 1_000_000:
        return 30
    elif fdv_usd < 5_000_000:
        return 40
    elif fdv_usd < 20_000_000:
        return 50
    else:
        return 60


def get_largest_holders(token_mint: str):
    result = rpc_call("getTokenLargestAccounts", [token_mint])
    if "result" not in result or result["result"] is None:
        return []
    return result["result"]["value"]


def safety_gate(token_mint: str):
    print(f"\n🔍 Проверяем токен: {token_mint}\n")

    mint_info = get_mint_info(token_mint)
    if mint_info is None:
        return {"verdict": "REJECT", "reason": "Токен не найден или не является Mint Account"}

    is_token_2022 = mint_info["owner"] == TOKEN_2022_PROGRAM_ID
    program_name = "Token-2022" if is_token_2022 else "Classic SPL"

    print(f"Token Program:    {program_name}")
    print(f"Mint Authority:   {mint_info['mint_authority']}")
    print(f"Freeze Authority: {mint_info['freeze_authority']}")
    print(f"Supply:           {mint_info['supply']}")
    print(f"Decimals:         {mint_info['decimals']}")

    if mint_info["freeze_authority"] is not None:
        return {"verdict": "REJECT", "reason": "❌ Freeze Authority активна — honeypot!"}

    if mint_info["mint_authority"] is not None:
        return {"verdict": "REJECT", "reason": "❌ Mint Authority активна — можно допечатать токены."}

    ext_verdict = check_token_2022_extensions(mint_info)
    if ext_verdict:
        return {"verdict": "REJECT", "reason": ext_verdict}

    market_data = get_token_market_cap(token_mint)
    fdv = None
    threshold = 30
    liquidity = 0

    if market_data and market_data.get("fdv"):
        try:
            fdv = float(market_data["fdv"])
        except (ValueError, TypeError):
            fdv = None
        threshold = get_concentration_threshold(fdv)
        liquidity = market_data.get("liquidity_usd", 0) or 0
        symbol = market_data.get("symbol", "?")
        print(f"Symbol:           {symbol}")
        if fdv is not None:
            print(f"FDV:              ${fdv:,.0f}")
        else:
            print(f"FDV:              неизвестно")
        print(f"Ликвидность:      ${liquidity:,.0f}")
        print(f"Порог топ-10:     {threshold}%")
    else:
        print(f"⚠️  Не удалось получить FDV, использую жёсткий порог {threshold}%")

    holders = get_largest_holders(token_mint)
    if holders:
        top_10_amount = sum(int(h["amount"]) for h in holders[:10])
        total_supply = int(mint_info["supply"])
        if total_supply > 0:
            concentration = (top_10_amount / total_supply) * 100
            print(f"Топ-10 холдеров:  {concentration:.2f}% supply")

            if concentration > threshold:
                fdv_str = f"${fdv:,.0f}" if fdv is not None else "неизвестно"
                return {
                    "verdict": "REJECT",
                    "reason": (
                        f"❌ Топ-10 держат {concentration:.1f}% "
                        f"при пороге {threshold}% (FDV {fdv_str})"
                    ),
                }

    return {
        "verdict": "PASS",
        "reason": "✅ Mint/Freeze отозваны, расширения безопасны, концентрация в норме.",
    }


if __name__ == "__main__":
    pass