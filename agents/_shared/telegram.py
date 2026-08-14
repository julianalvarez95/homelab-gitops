import os
import re

import requests

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
VICTORIA_METRICS_URL = os.environ.get("VICTORIA_METRICS_URL")

_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def sanitize_telegram_html(text):
    text = _BR_RE.sub("\n", text)

    def _replace(match):
        tag = match.group(1).lower()
        return match.group(0) if tag in _ALLOWED_TAGS else ""

    return _TAG_RE.sub(_replace, text)


def _pack(pieces, max_chars, separator):
    chunks = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{separator}{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def split_telegram_message(text, max_chars=3500):
    if len(text) <= max_chars:
        return [text]

    chunks = []
    for block in _pack(text.split("\n\n"), max_chars, "\n\n"):
        if len(block) <= max_chars:
            chunks.append(block)
            continue
        # a single topic-block still overflows: fall back to bullet lines
        for sub in _pack(block.split("\n"), max_chars, "\n"):
            if len(sub) <= max_chars:
                chunks.append(sub)
            else:
                # a single line has no more separators: hard-slice as a
                # last resort so this never recurses/loops indefinitely
                chunks.extend(
                    sub[i:i + max_chars] for i in range(0, len(sub), max_chars)
                )
    return chunks


def send_telegram(text):
    token = TELEGRAM_BOT_TOKEN.removeprefix("bot")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sanitized = sanitize_telegram_html(text)
    for chunk in split_telegram_message(sanitized):
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        })
        print(f"Telegram response: {resp.status_code} {resp.text}")
        resp.raise_for_status()


def push_metrics(lines):
    # Fails open: an observability outage must never take down the agent
    # run itself, and a lost push just means one gap in the metric series.
    if not lines or not VICTORIA_METRICS_URL:
        return
    try:
        resp = requests.post(VICTORIA_METRICS_URL, data="\n".join(lines), timeout=3)
        print(f"VictoriaMetrics response: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"No se pudieron reportar métricas, sigo igual: {e}")
