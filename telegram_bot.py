"""
Telegram-бот — обработчики команд.
Без HTML/Markdown — чистый текст.
"""

import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from aggregator import evaluate_signal
from watcher import add_wallet, remove_wallet, list_wallets, cleanup_inactive_wallets

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Нет TELEGRAM_BOT_TOKEN в .env!")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def fmt_score(v):
    if v is None: return "—"
    try: return f"{float(v):.2f}"
    except (TypeError, ValueError): return "—"

def fmt_percent(v):
    if v is None: return "—"
    try: return f"{float(v):.1f}%"
    except (TypeError, ValueError): return "—"

def fmt_sol(v):
    if v is None: return "—"
    try: return f"{float(v):+.4f} SOL"
    except (TypeError, ValueError): return "—"

def fmt_usd(v):
    if v is None: return "—"
    try:
        v = float(v)
        if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
        if v >= 1_000: return f"${v/1_000:.2f}K"
        return f"${v:.2f}"
    except (TypeError, ValueError): return "—"

def fmt_amount(v):
    if v is None: return "—"
    try:
        v = float(v)
        if v >= 1_000_000_000: return f"{v/1_000_000_000:.2f}B"
        if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
        if v >= 1_000: return f"{v/1_000:.2f}K"
        return f"{v:.4f}"
    except (TypeError, ValueError): return "—"


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
        logger.info(f"✅ Отправлено ({len(text)} символов)")
        return True
    except Exception as e:
        logger.error(f"❌ Отправка не удалась: {e}")
        return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я агент для анализа мемкоинов.\n\n"
        "Команды:\n"
        "/check <token> <wallet>\n"
        "/watch <wallet>\n"
        "/unwatch <wallet>\n"
        "/list\n"
        "/stats — статистика\n"
        "/cleanup — удалить неактивные\n"
        "/discover — найти умные кошельки\n"
        "/help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Команды:\n\n"
        "🔍 /check <token> <wallet> — проверить токен\n"
        "👁️ /watch <wallet> — добавить в мониторинг\n"
        "/unwatch <wallet> — удалить\n"
        "/list — показать список\n"
        "📊 /stats — статистика по кошелькам\n"
        "🧹 /cleanup — удалить неактивные (> 7 дней)\n"
        "🎯 /discover — автопоиск умных кошельков"
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
                f"├ SOL:     {fmt_amount(portfolio.get('sol_balance', 0))} SOL ({fmt_usd(portfolio.get('sol_value_usd', 0))})\n"
                f"├ Токенов: {portfolio.get('tokens_count', 0)}\n"
                f"└ Итого:   {fmt_usd(portfolio.get('total_portfolio_usd', 0))}\n"
            )
            if tokens:
                text += "\n🪙 Топ-5:\n"
                for t in tokens[:5]:
                    name = t.get("name") or t.get("mint", "")[:8]
                    text += f"├ {name} — {fmt_amount(t.get('amount', 0))} tok ({fmt_usd(t.get('value_usd', 0))})\n"
        await safe_edit(msg, text)
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
        await update.message.reply_text(f"✅ Добавлен:\n{wallet}")
    else:
        await update.message.reply_text(f"⚠️ Уже отслеживается")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("❌ /unwatch <wallet>")
        return
    wallet = args[0]
    if remove_wallet(wallet):
        await update.message.reply_text(f"✅ Удалён:\n{wallet}")
    else:
        await update.message.reply_text(f"⚠️ Не найден")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = list_wallets()
    if not wallets:
        await update.message.reply_text("📭 Список пуст.")
        return
    text = f"👁️  Отслеживаемые ({len(wallets)}):\n\n"
    for i, w in enumerate(wallets, 1):
        text += f"{i}. {w}\n"
    await update.message.reply_text(text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по кошелькам."""
    wallets = list_wallets()
    if not wallets:
        await update.message.reply_text("📭 Список пуст.")
        return

    msg = await update.message.reply_text(f"📊 Собираю статистику по {len(wallets)} кошелькам...")

    try:
        import asyncio
        import time
        from watcher import fetch_signatures

        now = time.time()
        cutoff_7d = now - 7 * 86400

        active_count = 0
        inactive_count = 0
        details = []

        for w in wallets:
            try:
                loop = asyncio.get_event_loop()
                sigs = await loop.run_in_executor(None, fetch_signatures, w, 5)
                if not sigs:
                    inactive_count += 1
                    details.append((w, 0, "нет транзакций"))
                    continue
                latest = sigs[0].get("blockTime") or 0
                if latest < cutoff_7d:
                    inactive_count += 1
                    details.append((w, 0, "> 7 дней"))
                else:
                    active_count += 1
                    days_ago = (now - latest) / 86400
                    details.append((w, latest, f"{days_ago:.1f} дн. назад"))
            except Exception as e:
                details.append((w, 0, f"ошибка"))

        text = f"📊 СТАТИСТИКА\n\n"
        text += f"👁️ Всего кошельков: {len(wallets)}\n"
        text += f"✅ Активных (7 дней): {active_count}\n"
        text += f"💤 Неактивных: {inactive_count}\n\n"

        details.sort(key=lambda x: x[1], reverse=True)
        text += "🕐 Последняя активность:\n"
        for i, (w, ts, info) in enumerate(details[:10], 1):
            text += f"{i}. {w[:20]}... — {info}\n"

        if len(details) > 10:
            text += f"\n... и ещё {len(details) - 10}"

        await msg.edit_text(text)

    except Exception as e:
        logger.exception("Ошибка /stats")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🧹 Проверяю кошельки на неактивность (> 7 дней)...")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        removed, kept = await loop.run_in_executor(None, cleanup_inactive_wallets)

        text = "🧹 Очистка завершена:\n\n"
        text += f"✅ Оставлено активных: {len(kept)}\n"
        text += f"🗑️ Удалено неактивных: {len(removed)}\n\n"

        if removed:
            text += "Удалённые кошельки:\n"
            for w in removed:
                text += f"• {w}\n"
        else:
            text += "Все кошельки активны — удалять нечего."

        await msg.edit_text(text)
    except Exception as e:
        logger.exception("Ошибка /cleanup")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "🔍 Автопоиск...\n\n"
        "Ищу трейдеров в свежих токенах.\n"
        "Фильтр: win rate ≥ 45%, PnL ≥ 5 SOL\n\n"
        "1-2 минуты."
    )
    try:
        import asyncio
        from discovery import discover_smart_wallets, MIN_WIN_RATE, MIN_PNL_SOL, AUTO_WATCH_MIN_TRADES
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, discover_smart_wallets)

        if "error" in result:
            await msg.edit_text(f"❌ Ошибка: {result['error']}")
            return

        valid = result.get("valid_wallets", [])
        tokens_checked = result.get("tokens_checked", 0)
        candidates_found = result.get("candidates_found", 0)

        logger.info(f"📤 Формирую сообщение: {len(valid)} кошельков")

        if not valid:
            await msg.edit_text(
                f"😔 Не найдено.\n\n"
                f"Фильтр: win ≥ {MIN_WIN_RATE}%, PnL ≥ {MIN_PNL_SOL} SOL\n"
                f"Проверено токенов: {tokens_checked}\n"
                f"Кандидатов: {candidates_found}\n\n"
                f"💡 Строгий фильтр — попробуй позже."
            )
            return

        auto_added = []
        for w in valid:
            if w.get("auto_watch"):
                if add_wallet(w["wallet"]):
                    auto_added.append(w["wallet"])
                    logger.info(f"✅ Авто-добавлен: {w['wallet'][:16]}...")

        text = (
            f"🎯 Найдено {len(valid)} кошельков:\n"
            f"(win ≥ {MIN_WIN_RATE}%, PnL ≥ {MIN_PNL_SOL} SOL)\n"
            f"Проверено {tokens_checked} токенов, {candidates_found} кандидатов\n\n"
        )
        for i, w in enumerate(valid, 1):
            win = f"{w['win_rate']:.0f}%" if w['win_rate'] is not None else "—"
            pnl = f"{w['pnl_sol']:+.4f}" if w['pnl_sol'] is not None else "—"
            auto_mark = " ✅ в мониторинге" if w.get("auto_watch") else ""
            text += (
                f"{i}. {w['wallet']}{auto_mark}\n"
                f"   Win: {win}, PnL: {pnl} SOL\n"
                f"   Роль: {w.get('role', '?')}\n"
                f"   Сделок/день: {w.get('trades_per_day', '—')}\n"
                f"   Закрытых: {w.get('closed', 0)}, Открытых: {w.get('open_positions', 0)}\n"
                f"   Покупок: {w.get('buys', 0)}, Продаж: {w.get('sells', 0)}\n\n"
            )

        if auto_added:
            text += f"\n✅ Автоматически добавлено в мониторинг: {len(auto_added)}\n"
            text += f"(> {AUTO_WATCH_MIN_TRADES} сделок)\n\n"

        text += "💡 /watch WALLET — добавить вручную"

        await safe_edit(msg, text)
    except Exception as e:
        logger.exception("Ошибка /discover")
        try:
            await msg.edit_text(f"❌ Ошибка: {e}")
        except Exception as e2:
            logger.error(f"Не удалось показать ошибку: {e2}")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
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