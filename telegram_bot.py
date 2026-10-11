"""
Telegram-бот для агента.
Команды:
  /start  — приветствие
  /help   — справка
  /check <token> <wallet>  — проверить токен и кошелёк
"""

import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from aggregator import evaluate_signal

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Нет TELEGRAM_BOT_TOKEN в .env!")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Безопасное форматирование
# ─────────────────────────────────────────────

def fmt_score(value):
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def fmt_percent(value):
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_sol(value):
    if value is None:
        return "—"
    try:
        return f"{float(value):+.4f} SOL"
    except (TypeError, ValueError):
        return "—"


def fmt_usd(value):
    if value is None:
        return "—"
    try:
        v = float(value)
        if v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        if v >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:.2f}"
    except (TypeError, ValueError):
        return "—"


def fmt_amount(value):
    if value is None:
        return "—"
    try:
        v = float(value)
        if v >= 1_000_000_000:
            return f"{v/1_000_000_000:.2f}B"
        if v >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if v >= 1_000:
            return f"{v/1_000:.2f}K"
        return f"{v:.4f}"
    except (TypeError, ValueError):
        return "—"


# ─────────────────────────────────────────────
# Команды
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я агент для анализа мемкоинов.\n\n"
        "Команды:\n"
        "/check <token> <wallet> — проверить токен и кошелёк\n"
        "/help — справка\n"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Как использовать:\n\n"
        "1. Найди адрес токена и адрес кошелька-покупателя.\n"
        "2. Отправь команду:\n"
        "   /check <token> <wallet>\n\n"
        "Пример:\n"
        "/check DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 "
        "DhGTwErJvS6vVjnbxAqzaeGjPHY632LWEHwv5PiXGDoJ\n\n"
        "Я проверю токен на безопасность, проанализирую кошелёк и скажу вердикт."
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ Нужно два аргумента:\n"
            "/check <token> <wallet>"
        )
        return

    token_mint, wallet_address = args

    msg = await update.message.reply_text(
        f"⏳ Проверяю...\n\n"
        f"Токен: {token_mint[:8]}...\n"
        f"Кошелёк: {wallet_address[:8]}..."
    )

    try:
        result = evaluate_signal(token_mint, wallet_address)

        verdict = result.get("verdict", "UNKNOWN")
        confidence = result.get("confidence", 0.0) or 0.0
        breakdown = result.get("breakdown", {}) or {}
        sm_stats = result.get("smart_money_stats", {}) or {}
        reason = result.get("reason", "")

        emoji = {
            "SIGNAL": "🚀",
            "WEAK": "⚠️",
            "IGNORE": "❌",
            "REJECT": "🛑",
        }.get(verdict, "❓")

        # ─── Основной вердикт ───
        text = (
            f"{emoji} Вердикт: {verdict}\n"
            f"Confidence: {confidence:.3f}\n"
        )

        if reason:
            text += f"\n💬 {reason}\n"

        text += (
            f"\n📊 Разбивка:\n"
            f"├ Safety:      {fmt_score(breakdown.get('safety'))}\n"
            f"├ Smart Money: {fmt_score(breakdown.get('smart_money'))}\n"
            f"└ Liquidity:   {fmt_score(breakdown.get('liquidity'))}\n"
        )

        # ─── Smart Money статистика ───
        if sm_stats:
            text += (
                f"\n🧠 Smart Money:\n"
                f"├ Покупок:  {sm_stats.get('buys', '—')}\n"
                f"├ Продаж:   {sm_stats.get('sells', '—')}\n"
                f"├ Закрытых: {sm_stats.get('closed_with_sol', '—')}\n"
                f"├ Win rate: {fmt_percent(sm_stats.get('win_rate'))}\n"
                f"└ PnL:      {fmt_sol(sm_stats.get('total_pnl_sol'))}\n"
            )

        # ─── Портфель кошелька ───
        portfolio = result.get("portfolio")
        if portfolio:
            sol_bal = portfolio.get("sol_balance", 0)
            sol_usd = portfolio.get("sol_value_usd", 0)
            total_usd = portfolio.get("total_portfolio_usd", 0)
            tokens = portfolio.get("tokens", [])
            tokens_count = portfolio.get("tokens_count", 0)

            text += (
                f"\n💰 Портфель кошелька:\n"
                f"├ SOL:     {fmt_amount(sol_bal)} SOL ({fmt_usd(sol_usd)})\n"
                f"├ Токенов: {tokens_count} разных\n"
                f"└ Итого:   {fmt_usd(total_usd)}\n"
            )

            if tokens:
                text += f"\n🪙 Топ-5 по стоимости:\n"
                for t in tokens[:5]:
                    name = t.get("name") or t.get("mint", "")[:8]
                    amount = t.get("amount", 0)
                    value_usd = t.get("value_usd", 0)
                    text += f"├ {name} — {fmt_amount(amount)} tok ({fmt_usd(value_usd)})\n"

                if len(tokens) > 5:
                    text += f"└ ... и ещё {len(tokens) - 5}\n"

        await msg.edit_text(text)

    except Exception as e:
        logger.exception("Ошибка при обработке /check")
        await msg.edit_text(f"❌ Ошибка: {e}")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("check", cmd_check))

    print("🤖 Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    main()