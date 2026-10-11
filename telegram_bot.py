"""
Telegram-бот для агента.
Команды:
  /start     — приветствие
  /help      — справка
  /check     — проверить токен и кошелёк
  /watch     — добавить в отслеживание
  /unwatch   — удалить из отслеживания
  /list      — показать отслеживаемые
  /discover  — найти умные кошельки автоматически
"""

import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from aggregator import evaluate_signal
from watcher import add_wallet, remove_wallet, list_wallets

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
# Форматирование
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
        "/watch <wallet> — добавить в отслеживание\n"
        "/unwatch <wallet> — удалить из отслеживания\n"
        "/list — список отслеживаемых\n"
        "/discover — найти умные кошельки\n"
        "/help — справка"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Как использовать:\n\n"
        "🔍 Разовый анализ:\n"
        "/check <token> <wallet>\n\n"
        "👁️  Мониторинг:\n"
        "/watch <wallet> — добавить\n"
        "/unwatch <wallet> — удалить\n"
        "/list — показать список\n\n"
        "🎯 Автопоиск:\n"
        "/discover — найти 5 умных кошельков\n"
        "(может занять 1-2 минуты)"
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("❌ /check <token> <wallet>")
        return

    token_mint, wallet_address = args
    msg = await update.message.reply_text(f"⏳ Проверяю {token_mint[:8]}...")

    try:
        result = evaluate_signal(token_mint, wallet_address)
        verdict = result.get("verdict", "UNKNOWN")
        confidence = result.get("confidence", 0.0) or 0.0
        breakdown = result.get("breakdown", {}) or {}
        sm_stats = result.get("smart_money_stats", {}) or {}
        reason = result.get("reason", "")

        emoji = {"SIGNAL": "🚀", "WEAK": "⚠️", "IGNORE": "❌", "REJECT": "🛑"}.get(verdict, "❓")

        text = f"{emoji} Вердикт: {verdict}\nConfidence: {confidence:.3f}\n"
        if reason:
            text += f"\n💬 {reason}\n"
        text += (
            f"\n📊 Разбивка:\n"
            f"├ Safety:      {fmt_score(breakdown.get('safety'))}\n"
            f"├ Smart Money: {fmt_score(breakdown.get('smart_money'))}\n"
            f"└ Liquidity:   {fmt_score(breakdown.get('liquidity'))}\n"
        )

        if sm_stats:
            text += (
                f"\n🧠 Smart Money:\n"
                f"├ Покупок:  {sm_stats.get('buys', '—')}\n"
                f"├ Продаж:   {sm_stats.get('sells', '—')}\n"
                f"├ Win rate: {fmt_percent(sm_stats.get('win_rate'))}\n"
                f"└ PnL:      {fmt_sol(sm_stats.get('total_pnl_sol'))}\n"
            )

        portfolio = result.get("portfolio")
        if portfolio:
            tokens = portfolio.get("tokens", [])
            text += (
                f"\n💰 Портфель:\n"
                f"├ SOL:     {fmt_amount(portfolio.get('sol_balance', 0))} SOL "
                f"({fmt_usd(portfolio.get('sol_value_usd', 0))})\n"
                f"├ Токенов: {portfolio.get('tokens_count', 0)}\n"
                f"└ Итого:   {fmt_usd(portfolio.get('total_portfolio_usd', 0))}\n"
            )
            if tokens:
                text += "\n🪙 Топ-5:\n"
                for t in tokens[:5]:
                    name = t.get("name") or t.get("mint", "")[:8]
                    text += f"├ {name} — {fmt_amount(t.get('amount', 0))} tok ({fmt_usd(t.get('value_usd', 0))})\n"

        await msg.edit_text(text)

    except Exception as e:
        logger.exception("Ошибка /check")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("❌ /watch <wallet>")
        return

    wallet = args[0]
    if add_wallet(wallet):
        await update.message.reply_text(f"✅ Кошелёк добавлен:\n{wallet}")
    else:
        await update.message.reply_text(f"⚠️ Уже отслеживается:\n{wallet}")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("❌ /unwatch <wallet>")
        return

    wallet = args[0]
    if remove_wallet(wallet):
        await update.message.reply_text(f"✅ Удалён:\n{wallet}")
    else:
        await update.message.reply_text(f"⚠️ Не найден:\n{wallet}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = list_wallets()
    if not wallets:
        await update.message.reply_text("📭 Список пуст.\nДобавь: /watch <wallet>")
        return

    text = f"👁️  Отслеживаемые ({len(wallets)}):\n\n"
    for i, w in enumerate(wallets, 1):
        text += f"{i}. {w}\n"
    await update.message.reply_text(text)


async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автопоиск умных кошельков."""
    msg = await update.message.reply_text(
        "🔍 Начинаю автопоиск умных кошельков...\n\n"
        "Это займёт 1-2 минуты.\n"
        "Я проверяю топ-токены, ищу активных покупателей "
        "и прогоняю каждого через Smart Money."
    )

    try:
        import asyncio
        from discovery import discover_smart_wallets

        # Запускаем в отдельном потоке, чтобы не блокировать бота
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, discover_smart_wallets)

        if "error" in result:
            await msg.edit_text(f"❌ Ошибка: {result['error']}")
            return

        valid = result.get("valid_wallets", [])
        tokens_checked = result.get("tokens_checked", 0)
        candidates_found = result.get("candidates_found", 0)

        if not valid:
            await msg.edit_text(
                f"😔 Не найдено подходящих кошельков.\n\n"
                f"Проверено токенов: {tokens_checked}\n"
                f"Кандидатов: {candidates_found}\n\n"
                f"Попробуй позже или ослабь фильтры."
            )
            return

        text = (
            f"🎯 Найдено {len(valid)} умных кошельков:\n"
            f"(проверено {tokens_checked} токенов, {candidates_found} кандидатов)\n\n"
        )

        for i, w in enumerate(valid, 1):
            text += (
                f"{i}. `{w['wallet']}`\n"
                f"   Win rate: {w['win_rate']:.0f}%\n"
                f"   PnL: {w['pnl_sol']:+.4f} SOL\n"
                f"   Закрытых: {w['closed']}, сделок/день: {w['trades_per_day']}\n"
                f"   Токены: {', '.join(w['tokens_bought'][:3])}\n\n"
            )

        text += (
            "💡 Добавить в мониторинг:\n"
            "`/watch <wallet>`"
        )

        await msg.edit_text(text, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Ошибка /discover")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("discover", cmd_discover))

    print("🤖 Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()

    import threading
    from watcher import run_watcher_loop

    watcher_thread = threading.Thread(target=run_watcher_loop, daemon=True)
    watcher_thread.start()

    main()