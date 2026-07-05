import logging

import requests

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    try:
        response = requests.post(
            _TELEGRAM_API.format(token=bot_token),
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("Failed to send Telegram message")
        return False


def notify_cycle_summary(bot_token, chat_id, candidates, entries, exits, partials) -> None:
    lines = [f"Cycle summary — {len(candidates)} candidates scanned"]
    if candidates:
        lines.append("Candidates: " + ", ".join(c["symbol"] for c in candidates))
    if entries:
        lines.append("Entries: " + ", ".join(entries))
    if exits:
        lines.append("Exits: " + ", ".join(exits))
    if partials:
        lines.append("Partials: " + ", ".join(partials))
    send_telegram_message(bot_token, chat_id, "\n".join(lines))


def notify_entry(bot_token, chat_id, symbol, qty, price) -> None:
    send_telegram_message(bot_token, chat_id, f"ENTRY {symbol} qty={qty} @ {price:.2f}")


def notify_partial(bot_token, chat_id, symbol, price, r_multiple) -> None:
    send_telegram_message(
        bot_token, chat_id, f"PARTIAL {symbol} @ {price:.2f} ({r_multiple:.2f}R)"
    )


def notify_exit(bot_token, chat_id, symbol, price, r_multiple, reason) -> None:
    send_telegram_message(
        bot_token, chat_id, f"EXIT {symbol} @ {price:.2f} ({r_multiple:.2f}R) reason={reason}"
    )


def notify_eod_summary(bot_token, chat_id, closed_today: list) -> None:
    if not closed_today:
        send_telegram_message(bot_token, chat_id, "EOD summary: no trades closed today")
        return
    total_r = sum(t["r_multiple"] for t in closed_today)
    lines = [f"EOD summary: {len(closed_today)} trades, total {total_r:.2f}R"]
    for t in closed_today:
        lines.append(f"  {t['symbol']}: {t['r_multiple']:.2f}R")
    send_telegram_message(bot_token, chat_id, "\n".join(lines))


def notify_error(bot_token, chat_id, message: str) -> None:
    send_telegram_message(bot_token, chat_id, f"ERROR: {message}")
