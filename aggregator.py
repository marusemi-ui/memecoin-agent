"""
Aggregator — главный узел Decision Engine.
Объединяет Safety Gate и Smart Money Tracker в один вердикт.
"""

import os
import time
from dotenv import load_dotenv

from safety_gate import safety_gate
from smart_money import analyze_wallet

load_dotenv()


WEIGHTS = {
    "safety": 0.40,
    "smart_money": 0.45,
    "liquidity": 0.15,
}

SIGNAL_THRESHOLD = 0.65


def score_safety(safety_result: dict) -> float:
    if safety_result["verdict"] == "REJECT":
        return 0.0
    if safety_result["verdict"] == "PASS":
        return 1.0
    return 0.5


def score_smart_money(sm_result: dict) -> float:
    stats = sm_result.get("stats", {}) or {}
    win_rate = stats.get("win_rate")
    total_pnl = stats.get("total_pnl_sol")
    closed = stats.get("closed_with_sol", 0) or 0

    if not closed or win_rate is None:
        return 0.3

    score = 0.0

    if win_rate >= 70:
        score += 0.5
    elif win_rate >= 55:
        score += 0.4
    elif win_rate >= 45:
        score += 0.3
    elif win_rate >= 30:
        score += 0.15

    if total_pnl is not None:
        if total_pnl > 1.0:
            score += 0.4
        elif total_pnl > 0.1:
            score += 0.3
        elif total_pnl > 0:
            score += 0.2

    if closed >= 20:
        score += 0.1
    elif closed >= 10:
        score += 0.05

    return min(score, 1.0)


def score_liquidity(safety_result: dict) -> float:
    return 0.7


def evaluate_signal(token_mint: str, wallet_address: str) -> dict:
    print(f"\n{'=' * 65}")
    print(f"🎯 ОЦЕНКА СИГНАЛА")
    print(f"   Токен:   {token_mint}")
    print(f"   Кошелёк: {wallet_address}")
    print(f"{'=' * 65}\n")

    print("🛡️  Шаг 1: Safety Gate...")
    safety_result = safety_gate(token_mint)
    safety_score = score_safety(safety_result)

    if safety_result["verdict"] == "REJECT":
        print(f"\n❌ СИГНАЛ ОТКЛОНЁН на Safety Gate")
        print(f"   Причина: {safety_result['reason']}")
        return {
            "verdict": "REJECT",
            "confidence": 0.0,
            "reason": f"Safety Gate: {safety_result['reason']}",
            "breakdown": {
                "safety": 0.0,
                "smart_money": None,
                "liquidity": None,
            },
            "smart_money_stats": {},
        }

    print(f"\n🧠 Шаг 2: Smart Money анализ кошелька...")
    sm_result = analyze_wallet(wallet_address)
    smart_score = score_smart_money(sm_result)

    liq_score = score_liquidity(safety_result)

    confidence = (
        safety_score * WEIGHTS["safety"]
        + smart_score * WEIGHTS["smart_money"]
        + liq_score * WEIGHTS["liquidity"]
    )

    if confidence >= SIGNAL_THRESHOLD:
        verdict = "SIGNAL"
        emoji = "🚀"
    elif confidence >= 0.45:
        verdict = "WEAK"
        emoji = "⚠️"
    else:
        verdict = "IGNORE"
        emoji = "❌"

    print(f"\n{'=' * 65}")
    print(f"📊 ИТОГОВАЯ ОЦЕНКА")
    print(f"{'-' * 65}")
    print(f"   Safety Gate:  {safety_score:.2f} × {WEIGHTS['safety']:.2f} = {safety_score * WEIGHTS['safety']:.3f}")
    print(f"   Smart Money:  {smart_score:.2f} × {WEIGHTS['smart_money']:.2f} = {smart_score * WEIGHTS['smart_money']:.3f}")
    print(f"   Liquidity:    {liq_score:.2f} × {WEIGHTS['liquidity']:.2f} = {liq_score * WEIGHTS['liquidity']:.3f}")
    print(f"{'-' * 65}")
    print(f"   Confidence:   {confidence:.3f}")
    print(f"{'=' * 65}")
    print(f"{emoji} Вердикт: {verdict}")
    print(f"{'=' * 65}\n")

    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "reason": f"Safety={safety_score:.2f}, Smart={smart_score:.2f}, Liq={liq_score:.2f}",
        "breakdown": {
            "safety": round(safety_score, 2),
            "smart_money": round(smart_score, 2),
            "liquidity": round(liq_score, 2),
        },
        "safety_reason": safety_result["reason"],
        "smart_money_stats": sm_result.get("stats", {}) or {},
    }


if __name__ == "__main__":
    pass