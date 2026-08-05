import os
import re
import time
from datetime import datetime

import requests
import yaml

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
VICTORIA_METRICS_URL = os.environ["VICTORIA_METRICS_URL"]
VICTORIA_METRICS_QUERY_URL = os.environ["VICTORIA_METRICS_QUERY_URL"]

_COMPARATORS = {
    "lt": lambda value, threshold: value < threshold,
    "gt": lambda value, threshold: value > threshold,
    "eq": lambda value, threshold: value == threshold,
}


def load_rules():
    with open("/config/rules.yaml") as f:
        return yaml.safe_load(f)["rules"]


def vm_query(query, timeout=5):
    """Instant MetricsQL query. Returns the result vector (possibly empty).
    Raises on HTTP/network failure or a non-success response — callers must
    distinguish "no data" (empty list) from "couldn't ask" (exception)."""
    resp = requests.get(
        VICTORIA_METRICS_QUERY_URL, params={"query": query}, timeout=timeout
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"VM query status={data.get('status')}: {data}")
    return data["data"]["result"]


def format_since(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def format_duration(seconds):
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def evaluate_rule(rule, now):
    """Runs one rule's state machine for this cycle. Returns
    (new_state, new_since, notification_text_or_None, metrics_to_push).
    Returns (None, None, None, []) if the rule must be skipped this run
    (either its own query or the state read-back couldn't be answered)."""
    name = rule["name"]

    # Step 1: evaluate the rule's own condition. Absence of data is not
    # "false" — it means we can't say anything this run, so skip and warn
    # rather than guessing.
    try:
        result = vm_query(rule["query"])
    except Exception as e:
        print(f"[{name}] WARNING: no se pudo evaluar la query, salteo esta corrida: {e}")
        return None, None, None, []

    if not result:
        print(f"[{name}] WARNING: la query no devolvió datos, salteo esta corrida")
        return None, None, None, []

    value = float(result[0]["value"][1])
    condition = _COMPARATORS[rule["comparator"]](value, rule["threshold"])

    # Step 2: read back previous (state, since). Reading state does NOT
    # fail open — a broken read must not be treated as "was inactive",
    # since that would turn a VictoriaMetrics outage into a burst of
    # duplicate FIRING notifications the moment it comes back.
    try:
        state_result = vm_query(f'last_over_time(watchdog_alert_state{{alert="{name}"}}[1h])')
        since_result = vm_query(f'last_over_time(watchdog_alert_since_seconds{{alert="{name}"}}[1h])')
    except Exception as e:
        print(f"[{name}] WARNING: no se pudo leer el estado previo, salteo esta corrida: {e}")
        return None, None, None, []

    if state_result and since_result:
        prev_state = int(float(state_result[0]["value"][1]))
        prev_since = float(since_result[0]["value"][1])
    else:
        # First-ever run, or a gap wider than 1h (missed runs / retention).
        # Bootstrap as inactive rather than guessing pending/firing.
        prev_state = 0
        prev_since = now

    notification = None
    if condition:
        since = prev_since if prev_state != 0 else now
        firing = (now - since) >= rule["for_seconds"]
        new_state = 2 if firing else 1
        if new_state == 2 and prev_state != 2:
            notification = f'🔴 FIRING: {rule["message"]} (desde {format_since(since)})'
        new_since = since
    else:
        if prev_state == 2:
            notification = f'🟢 RESOLVED: {rule["message"]} (duró {format_duration(now - prev_since)})'
        new_state = 0
        new_since = now

    metrics = [
        f'watchdog_alert_state{{alert="{name}"}} {new_state}',
        f'watchdog_alert_since_seconds{{alert="{name}"}} {int(new_since)}',
    ]
    return new_state, new_since, notification, metrics


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
        for sub in _pack(block.split("\n"), max_chars, "\n"):
            if len(sub) <= max_chars:
                chunks.append(sub)
            else:
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
    # Same convention as morning-digest: pushing metrics/state fails open —
    # log and continue. Worst case on a push failure is one duplicate
    # notification next run, not a crashed agent.
    if not lines:
        return
    try:
        resp = requests.post(VICTORIA_METRICS_URL, data="\n".join(lines), timeout=3)
        print(f"VictoriaMetrics response: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"No se pudieron reportar métricas, sigo igual: {e}")


def main():
    start = time.time()
    success = False
    try:
        rules = load_rules()
        now = time.time()
        all_metric_lines = []
        for rule in rules:
            new_state, new_since, notification, metrics = evaluate_rule(rule, now)
            if new_state is None:
                continue
            all_metric_lines.extend(metrics)
            if notification:
                print(notification)
                send_telegram(notification)
        push_metrics(all_metric_lines)
        success = True
    finally:
        push_metrics([
            f'agent_run_success{{agent="watchdog"}} {int(success)}',
            f'agent_run_duration_seconds{{agent="watchdog"}} {time.time() - start:.2f}',
            f'agent_llm_tokens_total{{agent="watchdog"}} 0',
            f'agent_last_run_timestamp_seconds{{agent="watchdog"}} {int(time.time())}',
        ])


if __name__ == "__main__":
    main()
