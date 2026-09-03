"""
Telegram alert module — sends a message when a high-score token is found.

Reads config from environment variables (set these on Railway):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  TELEGRAM_CHAT_ID     - the chat/user/group ID to send alerts to

If either is missing, alerts are silently skipped (no crash) — safe default
for local/dev runs where you haven't set these up yet.
"""
import os
import httpx

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def send_telegram_message(text: str) -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
    except Exception:
        # Never let a Telegram hiccup break the scan itself.
        return False


def format_high_score_alert(token: dict) -> str:
    """Build a nicely formatted HTML message for a high-score token."""
    volume = token.get("volume_24h") or 0
    return (
        f"🔥 <b>HIGH SCORE TOKEN</b>\n\n"
        f"Symbol: <b>{token['symbol']}</b>\n"
        f"Score: <b>{token['score']}/100</b>\n"
        f"Monthly RSI: {token.get('monthly_rsi')}\n\n"
        f"Entry: {token.get('entry')}\n"
        f"Stop Loss: {token.get('stop_loss')}\n"
        f"TP1: {token.get('tp1')}\n"
        f"TP2: {token.get('tp2')}\n"
        f"TP3: {token.get('tp3')}\n"
        f"24h Volume: ${volume:,.0f}"
    )
