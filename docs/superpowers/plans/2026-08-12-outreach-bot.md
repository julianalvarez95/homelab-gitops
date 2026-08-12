# Outreach bot (WhatsApp + Kapso) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `agents/outreach-bot`, a WhatsApp outreach agent (via Kapso) that sends a daily template message to pending contacts from a Google Sheet, conversation-qualifies replies with an LLM, and either sends a scheduling link, hands off to the lawyer via Telegram, or marks the contact not-interested.

**Architecture:** One Docker image, two Kubernetes workloads (per the approved design, `docs/superpowers/specs/2026-08-10-outreach-bot-design.md`): a `CronJob` running `sender.py` (daily template send + follow-up sweep) and a `Deployment` running `webhook.py` (FastAPI app receiving Kapso webhooks, exposed publicly via a Cloudflare Tunnel `Deployment`). State lives entirely in a Google Sheet (`status` column) plus Kapso's own message history — no database, no agent framework (LangGraph/Hermes/Eve/Cloudflare Agents evaluated and rejected in the spec).

**Tech Stack:** Python 3.12, FastAPI + uvicorn (webhook), `requests` (Kapso HTTP calls), `gspread` + `google-auth` (Sheets), `openai` (LLM), Phoenix/OpenInference (LLM tracing), pytest (tests), GitHub Actions (CI, first in this repo).

## Global Constraints

- Base image: `python:3.12-slim` (matches `morning-digest`/`watchdog`).
- Pin every dependency version in `requirements.txt`, including transitive deps known to break in this repo: `httpx==0.27.2`, `wrapt==1.17.3` (see `CLAUDE.md` gotchas).
- Never let an outbound HTTP call fail silently: log the response and call `resp.raise_for_status()`. Exception: telemetry/metrics pushes (`push_metrics`), which fail open (log and continue) so an observability outage never breaks the agent.
- Secrets only via `kubectl create secret` + `envFrom.secretRef`, referenced by name — never committed to git.
- `TELEGRAM_BOT_TOKEN` must be stripped of a duplicated `bot` prefix via `.removeprefix("bot")`.
- `PHOENIX_COLLECTOR_ENDPOINT` must include the explicit `http://` scheme.
- Hard cap: 10 messages of conversation context before the webhook forces a handoff without calling the LLM.
- Terminal contact states: `qualified`, `handoff`, `no_response`, `not_interested`. `qualified` and `handoff` notify Telegram; `not_interested` does not (avoids notification noise for explicit rejections).
- No code sharing between `agents/*` directories (Telegram/metrics helpers are copied, not imported) — sharing *within* `agents/outreach-bot/src/` itself (e.g. both `sender.py` and `webhook.py` importing this agent's own `telegram.py`) is fine, that's not the convention being protected.
- Namespace: `default` for `outreach-bot-sender`/`outreach-bot-webhook`/`outreach-bot-cloudflared`, matching `morning-digest`/`watchdog`. Phoenix/VictoriaMetrics stay in `observability`.
- Google Sheet columns, exact names and order: `nombre`, `telefono`, `status`, `last_update` (A–D). `telefono` stored as digits only, no leading `+` (matches Kapso's `from`/`to` phone format). `last_update` stored as UTC ISO-8601 `%Y-%m-%dT%H:%M:%SZ`.

---

### Task 1: `telegram.py` — Telegram delivery + VictoriaMetrics push (copied helpers)

**Files:**
- Create: `agents/outreach-bot/src/telegram.py`
- Create: `agents/outreach-bot/src/requirements.txt`
- Create: `agents/outreach-bot/src/requirements-dev.txt`
- Create: `agents/outreach-bot/Dockerfile`
- Create: `agents/outreach-bot/pytest.ini`
- Create: `agents/outreach-bot/tests/conftest.py`
- Test: `agents/outreach-bot/tests/test_telegram.py`

**Interfaces:**
- Produces: `sanitize_telegram_html(text: str) -> str`, `split_telegram_message(text: str, max_chars: int = 3500) -> list[str]`, `send_telegram(text: str) -> None`, `push_metrics(lines: list[str]) -> None`. All later tasks (`sender.py`, `webhook.py`) call `telegram.send_telegram(...)` and `telegram.push_metrics([...])`.

- [ ] **Step 1: Create the directory scaffolding and pinned dependencies**

`agents/outreach-bot/src/requirements.txt`:

```
fastapi==0.115.6
uvicorn[standard]==0.32.1
requests==2.32.3
openai==1.99.9
httpx==0.27.2
gspread==6.1.4
google-auth==2.35.0
arize-phoenix-otel==0.16.1
openinference-instrumentation-openai==0.1.40
wrapt==1.17.3
```

`agents/outreach-bot/src/requirements-dev.txt`:

```
-r requirements.txt
pytest==8.3.3
```

`agents/outreach-bot/Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ .
CMD ["python", "sender.py"]
```

`agents/outreach-bot/pytest.ini`:

```ini
[pytest]
pythonpath = src
```

- [ ] **Step 2: Write `conftest.py` with the env vars every module needs at import time**

Every `src/*.py` module reads required config via `os.environ["X"]` at module import time (same convention as `morning-digest`/`watchdog`). Tests need these set before any `src` module is imported — `conftest.py` runs before test collection in its directory, so this is the right place:

```python
# agents/outreach-bot/tests/conftest.py
import os

os.environ.setdefault("KAPSO_API_KEY", "test-kapso-key")
os.environ.setdefault("KAPSO_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("KAPSO_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    '{"type": "service_account", "project_id": "test", '
    '"private_key": "x", "client_email": "test@test.iam.gserviceaccount.com", '
    '"token_uri": "https://oauth2.googleapis.com/token"}',
)
os.environ.setdefault("SPREADSHEET_ID", "test-spreadsheet-id")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("SCHEDULING_LINK", "https://cal.example.com/intro")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault(
    "VICTORIA_METRICS_URL",
    "http://victoria-metrics.observability.svc.cluster.local:8428/api/v1/import/prometheus",
)
```

- [ ] **Step 3: Write the failing tests for `telegram.py`**

```python
# agents/outreach-bot/tests/test_telegram.py
from unittest.mock import MagicMock, patch

import telegram


def test_sanitize_strips_disallowed_tags_and_converts_br():
    text = "<div><b>Hola</b><br>Mundo</div>"
    assert telegram.sanitize_telegram_html(text) == "<b>Hola</b>\nMundo"


def test_sanitize_keeps_allowed_tags():
    text = '<b>bold</b> <a href="https://x.com">link</a> <i>it</i>'
    assert telegram.sanitize_telegram_html(text) == text


def test_split_returns_single_chunk_under_limit():
    assert telegram.split_telegram_message("hola", max_chars=100) == ["hola"]


def test_split_breaks_on_paragraph_boundaries():
    text = "a" * 10 + "\n\n" + "b" * 10
    chunks = telegram.split_telegram_message(text, max_chars=15)
    assert chunks == ["a" * 10, "b" * 10]


@patch("telegram.requests.post")
def test_send_telegram_strips_bot_prefix_and_sends(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "{}"
    mock_post.return_value.raise_for_status = MagicMock()
    telegram.TELEGRAM_BOT_TOKEN = "bot123:ABC"

    telegram.send_telegram("hola")

    url = mock_post.call_args[0][0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"


@patch("telegram.requests.post")
def test_push_metrics_swallows_errors(mock_post):
    mock_post.side_effect = Exception("network down")

    telegram.push_metrics(['agent_run_success{agent="outreach-bot"} 1'])  # must not raise


@patch("telegram.requests.post")
def test_push_metrics_skips_when_no_lines(mock_post):
    telegram.push_metrics([])

    mock_post.assert_not_called()
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd agents/outreach-bot && pip install -r src/requirements-dev.txt && python -m pytest tests/test_telegram.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telegram'` (a well-known PyPI package named `telegram` may shadow this if installed; since `src` is first on `pythonpath` this resolves to our local file once it exists — for now it fails because `agents/outreach-bot/src/telegram.py` doesn't exist yet).

- [ ] **Step 5: Implement `telegram.py`**

```python
# agents/outreach-bot/src/telegram.py
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
    # Fails open: an observability outage can never break the agent run.
    if not lines or not VICTORIA_METRICS_URL:
        return
    try:
        resp = requests.post(VICTORIA_METRICS_URL, data="\n".join(lines), timeout=3)
        print(f"VictoriaMetrics response: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"No se pudieron reportar métricas, sigo igual: {e}")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd agents/outreach-bot && python -m pytest tests/test_telegram.py -v`
Expected: `6 passed`

- [ ] **Step 7: Commit**

```bash
git add agents/outreach-bot/src/telegram.py agents/outreach-bot/src/requirements.txt \
        agents/outreach-bot/src/requirements-dev.txt agents/outreach-bot/Dockerfile \
        agents/outreach-bot/pytest.ini agents/outreach-bot/tests/conftest.py \
        agents/outreach-bot/tests/test_telegram.py
git commit -m "feat(outreach-bot): scaffold project + Telegram/metrics helpers"
```

---

### Task 2: `sheets_client.py` — Google Sheet as contact store + state machine

**Files:**
- Create: `agents/outreach-bot/src/sheets_client.py`
- Test: `agents/outreach-bot/tests/test_sheets_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `list_contacts_by_status(status: str) -> list[dict]` (each dict is the row plus `row_number: int`), `get_contact_by_phone(phone: str) -> dict | None` (same shape), `update_status(row_number: int, status: str) -> None`, `count_by_status() -> dict[str, int]`. `sender.py` and `webhook.py` (Tasks 5, 6) call these by exact name.

- [ ] **Step 1: Write the failing tests**

```python
# agents/outreach-bot/tests/test_sheets_client.py
from unittest.mock import MagicMock, patch

import pytest

import sheets_client


@pytest.fixture(autouse=True)
def _reset_worksheet_cache():
    sheets_client._worksheet = None
    yield
    sheets_client._worksheet = None


def _fake_worksheet(rows):
    ws = MagicMock()
    ws.get_all_records.return_value = rows
    return ws


def _authorize_returns(mock_authorize, ws):
    mock_client = MagicMock()
    mock_client.open_by_key.return_value.worksheet.return_value = ws
    mock_authorize.return_value = mock_client


@patch("sheets_client.gspread.authorize")
@patch("sheets_client.Credentials.from_service_account_info")
def test_list_contacts_by_status_filters_and_adds_row_number(mock_creds, mock_authorize):
    rows = [
        {"nombre": "Ana", "telefono": "5491111", "status": "pending", "last_update": ""},
        {"nombre": "Beto", "telefono": "5492222", "status": "sent", "last_update": ""},
        {"nombre": "Caro", "telefono": "5493333", "status": "pending", "last_update": ""},
    ]
    _authorize_returns(mock_authorize, _fake_worksheet(rows))

    result = sheets_client.list_contacts_by_status("pending")

    assert [r["nombre"] for r in result] == ["Ana", "Caro"]
    assert result[0]["row_number"] == 2
    assert result[1]["row_number"] == 4


@patch("sheets_client.gspread.authorize")
@patch("sheets_client.Credentials.from_service_account_info")
def test_get_contact_by_phone_matches_and_returns_row_number(mock_creds, mock_authorize):
    rows = [
        {"nombre": "Ana", "telefono": "5491111", "status": "sent", "last_update": ""},
        {"nombre": "Beto", "telefono": "5492222", "status": "sent", "last_update": ""},
    ]
    _authorize_returns(mock_authorize, _fake_worksheet(rows))

    result = sheets_client.get_contact_by_phone("5492222")

    assert result["nombre"] == "Beto"
    assert result["row_number"] == 3


@patch("sheets_client.gspread.authorize")
@patch("sheets_client.Credentials.from_service_account_info")
def test_get_contact_by_phone_returns_none_when_not_found(mock_creds, mock_authorize):
    _authorize_returns(mock_authorize, _fake_worksheet([]))

    assert sheets_client.get_contact_by_phone("0000") is None


@patch("sheets_client.gspread.authorize")
@patch("sheets_client.Credentials.from_service_account_info")
def test_update_status_writes_status_and_timestamp(mock_creds, mock_authorize):
    ws = MagicMock()
    _authorize_returns(mock_authorize, ws)

    sheets_client.update_status(5, "qualified")

    _, kwargs = ws.update.call_args
    assert kwargs["range_name"] == "C5:D5"
    assert kwargs["values"][0][0] == "qualified"
    assert kwargs["values"][0][1].endswith("Z")


@patch("sheets_client.gspread.authorize")
@patch("sheets_client.Credentials.from_service_account_info")
def test_count_by_status_tallies_rows(mock_creds, mock_authorize):
    rows = [
        {"nombre": "Ana", "telefono": "1", "status": "pending", "last_update": ""},
        {"nombre": "Beto", "telefono": "2", "status": "sent", "last_update": ""},
        {"nombre": "Caro", "telefono": "3", "status": "pending", "last_update": ""},
    ]
    _authorize_returns(mock_authorize, _fake_worksheet(rows))

    assert sheets_client.count_by_status() == {"pending": 2, "sent": 1}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agents/outreach-bot && python -m pytest tests/test_sheets_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sheets_client'`

- [ ] **Step 3: Implement `sheets_client.py`**

```python
# agents/outreach-bot/src/sheets_client.py
import json
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "contactos")

_worksheet = None


def _get_worksheet():
    global _worksheet
    if _worksheet is None:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        _worksheet = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
    return _worksheet


def list_contacts_by_status(status):
    ws = _get_worksheet()
    rows = ws.get_all_records()
    return [
        {**row, "row_number": i + 2}
        for i, row in enumerate(rows)
        if row["status"] == status
    ]


def get_contact_by_phone(phone):
    ws = _get_worksheet()
    rows = ws.get_all_records()
    for i, row in enumerate(rows):
        if str(row["telefono"]) == phone:
            return {**row, "row_number": i + 2}
    return None


def update_status(row_number, status):
    ws = _get_worksheet()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ws.update(range_name=f"C{row_number}:D{row_number}", values=[[status, now]])


def count_by_status():
    ws = _get_worksheet()
    rows = ws.get_all_records()
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agents/outreach-bot && python -m pytest tests/test_sheets_client.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add agents/outreach-bot/src/sheets_client.py agents/outreach-bot/tests/test_sheets_client.py
git commit -m "feat(outreach-bot): add Google Sheets contact store client"
```

---

### Task 3: `kapso_client.py` — send templates/text, read conversation history

**Files:**
- Create: `agents/outreach-bot/src/kapso_client.py`
- Test: `agents/outreach-bot/tests/test_kapso_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `send_template(to: str, template_name: str, params: dict) -> dict`, `send_text(to: str, text: str) -> dict`, `get_history(phone: str, limit: int = 10) -> list[dict]` (each item `{"role": "user"|"assistant", "content": str}`, oldest-first). `sender.py` (Task 5) calls `send_template`; `webhook.py` (Task 6) calls `send_text` and `get_history`.

**API reference used** (from `docs.kapso.ai`, verify against the dashboard before go-live — Task 10):
- `POST https://api.kapso.ai/meta/whatsapp/v24.0/{phone_number_id}/marketing_messages` — template send, header `X-API-Key`.
- `POST https://api.kapso.ai/meta/whatsapp/v24.0/{phone_number_id}/messages` — free-text send, same header.
- `GET https://api.kapso.ai/platform/v1/whatsapp/messages?phone_number=...&limit=...` — history, newest-first, header `X-API-Key`.

- [ ] **Step 1: Write the failing tests**

```python
# agents/outreach-bot/tests/test_kapso_client.py
from unittest.mock import MagicMock, patch

import kapso_client


@patch("kapso_client.requests.post")
def test_send_template_builds_marketing_message_request(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "{}"
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {}

    kapso_client.send_template("15551234567", "outreach_intro", {"nombre": "Ana"})

    url = mock_post.call_args[0][0]
    kwargs = mock_post.call_args[1]
    assert url == f"{kapso_client.BASE_URL}/marketing_messages"
    body = kwargs["json"]
    assert body["to"] == "15551234567"
    assert body["template"]["name"] == "outreach_intro"
    assert body["template"]["components"][0]["parameters"][0] == {
        "type": "text", "parameter_name": "nombre", "text": "Ana",
    }
    assert kwargs["headers"]["X-API-Key"] == kapso_client.KAPSO_API_KEY


@patch("kapso_client.requests.post")
def test_send_text_builds_text_message_request(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "{}"
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {}

    kapso_client.send_text("15551234567", "hola")

    url = mock_post.call_args[0][0]
    body = mock_post.call_args[1]["json"]
    assert url == f"{kapso_client.BASE_URL}/messages"
    assert body["text"]["body"] == "hola"


@patch("kapso_client.requests.get")
def test_get_history_passes_limit_and_orders_oldest_first(mock_get):
    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = {
        "data": [
            {"text": {"body": "segundo"}, "kapso": {"direction": "outbound"}},
            {"text": {"body": "primero"}, "kapso": {"direction": "inbound"}},
        ]
    }

    history = kapso_client.get_history("15551234567", limit=11)

    assert mock_get.call_args[1]["params"] == {"phone_number": "15551234567", "limit": 11}
    assert history == [
        {"role": "user", "content": "primero"},
        {"role": "assistant", "content": "segundo"},
    ]


@patch("kapso_client.requests.get")
def test_get_history_defaults_missing_text_to_empty_string(mock_get):
    mock_get.return_value.raise_for_status = MagicMock()
    mock_get.return_value.json.return_value = {
        "data": [{"kapso": {"direction": "inbound"}}],  # e.g. a media message, no "text" key
    }

    history = kapso_client.get_history("15551234567")

    assert history == [{"role": "user", "content": ""}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agents/outreach-bot && python -m pytest tests/test_kapso_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kapso_client'`

- [ ] **Step 3: Implement `kapso_client.py`**

```python
# agents/outreach-bot/src/kapso_client.py
import os

import requests

KAPSO_API_KEY = os.environ["KAPSO_API_KEY"]
KAPSO_PHONE_NUMBER_ID = os.environ["KAPSO_PHONE_NUMBER_ID"]
KAPSO_TEMPLATE_LANG = os.environ.get("KAPSO_TEMPLATE_LANG", "es_AR")

BASE_URL = f"https://api.kapso.ai/meta/whatsapp/v24.0/{KAPSO_PHONE_NUMBER_ID}"
MESSAGES_HEADERS = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
HISTORY_URL = "https://api.kapso.ai/platform/v1/whatsapp/messages"
HISTORY_HEADERS = {"X-API-Key": KAPSO_API_KEY}


def send_template(to, template_name, params):
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": KAPSO_TEMPLATE_LANG},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "parameter_name": name, "text": value}
                    for name, value in params.items()
                ],
            }],
        },
    }
    resp = requests.post(f"{BASE_URL}/marketing_messages", json=body, headers=MESSAGES_HEADERS, timeout=10)
    print(f"Kapso send_template response: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()


def send_text(to, text):
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    resp = requests.post(f"{BASE_URL}/messages", json=body, headers=MESSAGES_HEADERS, timeout=10)
    print(f"Kapso send_text response: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()


def get_history(phone, limit=10):
    resp = requests.get(
        HISTORY_URL,
        params={"phone_number": phone, "limit": limit},
        headers=HISTORY_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    messages = resp.json()["data"]
    return [
        {
            "role": "assistant" if m["kapso"]["direction"] == "outbound" else "user",
            "content": m.get("text", {}).get("body", ""),
        }
        for m in reversed(messages)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agents/outreach-bot && python -m pytest tests/test_kapso_client.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add agents/outreach-bot/src/kapso_client.py agents/outreach-bot/tests/test_kapso_client.py
git commit -m "feat(outreach-bot): add Kapso WhatsApp API client"
```

---

### Task 4: `llm.py` — conversation qualification (no framework)

**Files:**
- Create: `agents/outreach-bot/src/llm.py`
- Test: `agents/outreach-bot/tests/test_llm.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Decision` dataclass (`action: str`, `reply_text: str | None`, `reasoning: str`) and `decide(contact: dict, history: list[dict]) -> Decision`. `webhook.py` (Task 6) imports both `Decision` and `decide` by these exact names.

- [ ] **Step 1: Write the failing tests**

```python
# agents/outreach-bot/tests/test_llm.py
import json
from unittest.mock import MagicMock, patch

import llm


def _openai_response(payload):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))])


@patch("llm.client.chat.completions.create")
def test_decide_parses_reply_action(mock_create):
    mock_create.return_value = _openai_response({
        "action": "reply",
        "reply_text": "Contame más sobre tu caso",
        "reasoning": "todavía no calificó",
    })

    decision = llm.decide({"nombre": "Ana"}, [{"role": "user", "content": "hola"}])

    assert decision.action == "reply"
    assert decision.reply_text == "Contame más sobre tu caso"
    assert decision.reasoning == "todavía no calificó"


@patch("llm.client.chat.completions.create")
def test_decide_defaults_reasoning_when_missing(mock_create):
    mock_create.return_value = _openai_response({
        "action": "close",
        "reply_text": "Te paso el link para agendar: https://cal.example.com/intro",
    })

    decision = llm.decide({"nombre": "Ana"}, [])

    assert decision.action == "close"
    assert decision.reasoning == ""


@patch("llm.client.chat.completions.create")
def test_decide_includes_contact_name_in_messages(mock_create):
    mock_create.return_value = _openai_response({"action": "reply", "reply_text": "ok", "reasoning": ""})

    llm.decide({"nombre": "Beto"}, [{"role": "user", "content": "hola"}])

    messages = mock_create.call_args[1]["messages"]
    assert any("Beto" in m["content"] for m in messages if m["role"] == "system")
    assert mock_create.call_args[1]["response_format"] == {"type": "json_object"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agents/outreach-bot && python -m pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'llm'`

- [ ] **Step 3: Implement `llm.py`**

```python
# agents/outreach-bot/src/llm.py
import json
import os
from dataclasses import dataclass

from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SCHEDULING_LINK = os.environ["SCHEDULING_LINK"]

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = f"""\
Sos el asistente de primer contacto de un estudio de abogados. Tu tarea es
conversar por WhatsApp con un contacto que ya recibió un mensaje de
presentación, calificar su interés real en una consulta legal, y si
corresponde, cerrar ofreciendo agendar una reunión introductoria por Google
Meet usando este link: {SCHEDULING_LINK}

Reglas:
- Tono profesional pero cercano, en español rioplatense, mensajes cortos
  (es WhatsApp, no email).
- Si el contacto muestra interés genuino y ya diste suficiente contexto
  como para que decida agendar, cerrá mandando el link de agenda: acción
  "close".
- Si no podés cerrar en un intercambio razonable (el contacto tiene dudas
  que requieren criterio legal, pide hablar con la abogada directamente, o
  la conversación se estanca), derivá: acción "handoff".
- Si el contacto rechaza explícitamente el contacto (pide no ser
  contactado, dice que no le interesa, etc.), marcá: acción "reject". No
  insistas.
- En cualquier otro caso, seguí conversando: acción "reply".
- Nunca dés asesoramiento legal concreto vos mismo — tu único objetivo es
  calificar interés y agendar, no resolver la consulta.

Respondé ÚNICAMENTE con un JSON con este formato exacto, sin texto
adicional:
{{"action": "reply|close|handoff|reject", "reply_text": "<mensaje a enviar al contacto, o null si action es handoff/reject>", "reasoning": "<una frase breve para el log/notificación>"}}
"""


@dataclass
class Decision:
    action: str
    reply_text: str | None
    reasoning: str


def decide(contact, history):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Nombre del contacto: {contact['nombre']}"},
    ]
    messages.extend(history)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.choices[0].message.content)
    return Decision(
        action=parsed["action"],
        reply_text=parsed.get("reply_text"),
        reasoning=parsed.get("reasoning", ""),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agents/outreach-bot && python -m pytest tests/test_llm.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add agents/outreach-bot/src/llm.py agents/outreach-bot/tests/test_llm.py
git commit -m "feat(outreach-bot): add LLM qualification decision function"
```

---

### Task 5: `sender.py` — CronJob entrypoint (daily send + follow-up sweep)

**Files:**
- Create: `agents/outreach-bot/src/sender.py`
- Test: `agents/outreach-bot/tests/test_sender.py`

**Interfaces:**
- Consumes: `sheets_client.list_contacts_by_status`, `sheets_client.update_status`, `sheets_client.count_by_status` (Task 2); `kapso_client.send_template` (Task 3); `telegram.push_metrics` (Task 1).
- Produces: `send_pending() -> (sent: int, failed: int)`, `send_followups() -> (followed_up: int, expired: int)`, `push_status_metrics() -> None`, `main() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# agents/outreach-bot/tests/test_sender.py
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import sender


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@patch("sender.sheets_client")
@patch("sender.kapso_client")
def test_send_pending_sends_template_and_updates_status(mock_kapso, mock_sheets):
    mock_sheets.list_contacts_by_status.return_value = [
        {"nombre": "Ana", "telefono": "5491111", "row_number": 2},
    ]

    sent, failed = sender.send_pending()

    mock_kapso.send_template.assert_called_once_with("5491111", sender.TEMPLATE_INTRO, {"nombre": "Ana"})
    mock_sheets.update_status.assert_called_once_with(2, "sent")
    assert (sent, failed) == (1, 0)


@patch("sender.sheets_client")
@patch("sender.kapso_client")
def test_send_pending_continues_after_one_failure(mock_kapso, mock_sheets):
    mock_sheets.list_contacts_by_status.return_value = [
        {"nombre": "Ana", "telefono": "5491111", "row_number": 2},
        {"nombre": "Beto", "telefono": "5492222", "row_number": 3},
    ]
    mock_kapso.send_template.side_effect = [Exception("Kapso caído"), None]

    sent, failed = sender.send_pending()

    assert (sent, failed) == (1, 1)
    mock_sheets.update_status.assert_called_once_with(3, "sent")


@patch("sender.sheets_client")
@patch("sender.kapso_client")
def test_send_followups_moves_sent_to_followed_up_after_threshold(mock_kapso, mock_sheets):
    old_date = _iso(datetime.now(timezone.utc) - timedelta(days=4))
    mock_sheets.list_contacts_by_status.side_effect = lambda status: (
        [{"nombre": "Ana", "telefono": "5491111", "row_number": 2, "last_update": old_date}]
        if status == "sent" else []
    )

    followed_up, expired = sender.send_followups()

    mock_kapso.send_template.assert_called_once_with("5491111", sender.TEMPLATE_FOLLOWUP, {"nombre": "Ana"})
    mock_sheets.update_status.assert_called_once_with(2, "followed_up")
    assert (followed_up, expired) == (1, 0)


@patch("sender.sheets_client")
@patch("sender.kapso_client")
def test_send_followups_skips_recent_contacts(mock_kapso, mock_sheets):
    recent_date = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    mock_sheets.list_contacts_by_status.side_effect = lambda status: (
        [{"nombre": "Ana", "telefono": "5491111", "row_number": 2, "last_update": recent_date}]
        if status == "sent" else []
    )

    followed_up, expired = sender.send_followups()

    mock_kapso.send_template.assert_not_called()
    assert (followed_up, expired) == (0, 0)


@patch("sender.sheets_client")
@patch("sender.kapso_client")
def test_send_followups_marks_no_response_after_second_threshold(mock_kapso, mock_sheets):
    old_date = _iso(datetime.now(timezone.utc) - timedelta(days=7))
    mock_sheets.list_contacts_by_status.side_effect = lambda status: (
        [{"nombre": "Ana", "telefono": "5491111", "row_number": 2, "last_update": old_date}]
        if status == "followed_up" else []
    )

    followed_up, expired = sender.send_followups()

    mock_sheets.update_status.assert_called_once_with(2, "no_response")
    assert (followed_up, expired) == (0, 1)


@patch("sender.telegram")
@patch("sender.sheets_client")
def test_push_status_metrics_formats_gauge_lines(mock_sheets, mock_telegram):
    mock_sheets.count_by_status.return_value = {"pending": 2, "qualified": 1}

    sender.push_status_metrics()

    lines = mock_telegram.push_metrics.call_args[0][0]
    assert 'outreach_bot_contacts_total{status="pending"} 2' in lines
    assert 'outreach_bot_contacts_total{status="qualified"} 1' in lines
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agents/outreach-bot && python -m pytest tests/test_sender.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sender'`

- [ ] **Step 3: Implement `sender.py`**

```python
# agents/outreach-bot/src/sender.py
import os
import time
from datetime import datetime, timezone

import kapso_client
import sheets_client
import telegram

FOLLOWUP_AFTER_DAYS = int(os.environ.get("FOLLOWUP_AFTER_DAYS", "3"))
NO_RESPONSE_AFTER_DAYS = int(os.environ.get("NO_RESPONSE_AFTER_DAYS", "6"))
TEMPLATE_INTRO = os.environ.get("KAPSO_TEMPLATE_INTRO", "outreach_intro")
TEMPLATE_FOLLOWUP = os.environ.get("KAPSO_TEMPLATE_FOLLOWUP", "outreach_followup")


def _days_since(last_update_iso):
    last = datetime.strptime(last_update_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 86400


def send_pending():
    sent, failed = 0, 0
    for contact in sheets_client.list_contacts_by_status("pending"):
        try:
            kapso_client.send_template(contact["telefono"], TEMPLATE_INTRO, {"nombre": contact["nombre"]})
            sheets_client.update_status(contact["row_number"], "sent")
            sent += 1
        except Exception as e:
            print(f"Fallo enviando a {contact['telefono']}, reintenta el próximo run: {e}")
            failed += 1
    return sent, failed


def send_followups():
    followed_up = 0
    for contact in sheets_client.list_contacts_by_status("sent"):
        if _days_since(contact["last_update"]) < FOLLOWUP_AFTER_DAYS:
            continue
        try:
            kapso_client.send_template(contact["telefono"], TEMPLATE_FOLLOWUP, {"nombre": contact["nombre"]})
            sheets_client.update_status(contact["row_number"], "followed_up")
            followed_up += 1
        except Exception as e:
            print(f"Fallo mandando seguimiento a {contact['telefono']}: {e}")

    expired = 0
    for contact in sheets_client.list_contacts_by_status("followed_up"):
        if _days_since(contact["last_update"]) >= NO_RESPONSE_AFTER_DAYS:
            sheets_client.update_status(contact["row_number"], "no_response")
            expired += 1
    return followed_up, expired


def push_status_metrics():
    counts = sheets_client.count_by_status()
    lines = [
        f'outreach_bot_contacts_total{{status="{status}"}} {count}'
        for status, count in counts.items()
    ]
    telegram.push_metrics(lines)


def main():
    start = time.time()
    success = False
    try:
        sent, failed = send_pending()
        followed_up, expired = send_followups()
        print(f"sent={sent} failed={failed} followed_up={followed_up} expired={expired}")
        try:
            push_status_metrics()
        except Exception as e:
            print(f"No se pudieron calcular métricas de estado, sigo igual: {e}")
        success = True
    finally:
        telegram.push_metrics([
            f'agent_run_success{{agent="outreach-bot"}} {int(success)}',
            f'agent_run_duration_seconds{{agent="outreach-bot"}} {time.time() - start:.2f}',
            f'agent_last_run_timestamp_seconds{{agent="outreach-bot"}} {int(time.time())}',
        ])


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agents/outreach-bot && python -m pytest tests/test_sender.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add agents/outreach-bot/src/sender.py agents/outreach-bot/tests/test_sender.py
git commit -m "feat(outreach-bot): add sender.py CronJob entrypoint"
```

---

### Task 6: `webhook.py` — Deployment entrypoint (FastAPI, receives Kapso webhooks)

**Files:**
- Create: `agents/outreach-bot/src/webhook.py`
- Test: `agents/outreach-bot/tests/test_webhook.py`

**Interfaces:**
- Consumes: `sheets_client.get_contact_by_phone`, `sheets_client.update_status` (Task 2); `kapso_client.send_text`, `kapso_client.get_history` (Task 3); `llm.decide`, `llm.Decision` (Task 4); `telegram.send_telegram` (Task 1).
- Produces: FastAPI `app` with `POST /webhook` and `GET /healthz`, used directly by `deployment.yaml` (Task 8) via `uvicorn webhook:app`.

- [ ] **Step 1: Write the failing tests**

```python
# agents/outreach-bot/tests/test_webhook.py
import hashlib
import hmac
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import llm as llm_module
import webhook

client = TestClient(webhook.app)


def _signed_post(payload):
    body = json.dumps(payload).encode()
    signature = hmac.new(webhook.KAPSO_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={"X-Webhook-Signature": signature, "Content-Type": "application/json"},
    )


def _inbound_payload(text="Hola, quiero consultar"):
    return {
        "message": {
            "id": "wamid.1",
            "timestamp": "1730000000",
            "type": "text",
            "from": "5491111",
            "text": {"body": text},
            "kapso": {"direction": "inbound"},
        },
        "conversation": {"id": "conv_1", "phone_number": "5491111", "phone_number_id": "123"},
        "is_new_conversation": False,
        "phone_number_id": "123",
    }


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_rejects_invalid_signature():
    body = json.dumps(_inbound_payload()).encode()
    resp = client.post(
        "/webhook", content=body,
        headers={"X-Webhook-Signature": "wrongsignature", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_unknown_contact_is_ignored(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = None

    resp = _signed_post(_inbound_payload())

    assert resp.json() == {"status": "ignored"}
    mock_llm.decide.assert_not_called()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_reply_action_sends_text_and_updates_status(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = [{"role": "user", "content": "hola"}]
    mock_llm.decide.return_value = llm_module.Decision(action="reply", reply_text="Contame más", reasoning="calificando")

    resp = _signed_post(_inbound_payload())

    assert resp.json() == {"status": "ok"}
    mock_kapso.send_text.assert_called_once_with("5491111", "Contame más")
    mock_sheets.update_status.assert_called_once_with(2, "in_conversation")
    mock_telegram.send_telegram.assert_not_called()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_close_action_notifies_telegram(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = []
    mock_llm.decide.return_value = llm_module.Decision(
        action="close", reply_text="Agendá acá: https://cal.example.com", reasoning="quiere avanzar",
    )

    _signed_post(_inbound_payload())

    mock_sheets.update_status.assert_called_once_with(2, "qualified")
    mock_telegram.send_telegram.assert_called_once()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_handoff_action_notifies_telegram(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = []
    mock_llm.decide.return_value = llm_module.Decision(action="handoff", reply_text=None, reasoning="pidió hablar con la abogada")

    _signed_post(_inbound_payload())

    mock_kapso.send_text.assert_not_called()
    mock_sheets.update_status.assert_called_once_with(2, "handoff")
    mock_telegram.send_telegram.assert_called_once()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_reject_action_does_not_notify_telegram(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = []
    mock_llm.decide.return_value = llm_module.Decision(action="reject", reply_text=None, reasoning="no le interesa")

    _signed_post(_inbound_payload())

    mock_sheets.update_status.assert_called_once_with(2, "not_interested")
    mock_telegram.send_telegram.assert_not_called()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_context_cap_forces_handoff_without_calling_llm(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = [{"role": "user", "content": f"m{i}"} for i in range(11)]

    _signed_post(_inbound_payload())

    mock_kapso.get_history.assert_called_once_with("5491111", limit=11)
    mock_llm.decide.assert_not_called()
    mock_sheets.update_status.assert_called_once_with(2, "handoff")
    mock_telegram.send_telegram.assert_called_once()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_sheets_failure_sends_generic_fallback(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.side_effect = Exception("Sheets caído")

    resp = _signed_post(_inbound_payload())

    assert resp.json() == {"status": "ok"}
    mock_kapso.send_text.assert_called_once()
    mock_llm.decide.assert_not_called()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_kapso_history_failure_sends_generic_fallback(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.side_effect = Exception("Kapso caído")

    resp = _signed_post(_inbound_payload())

    assert resp.json() == {"status": "ok"}
    mock_kapso.send_text.assert_called_once()
    mock_llm.decide.assert_not_called()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_llm_failure_sends_fallback_message(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = []
    mock_llm.decide.side_effect = Exception("OpenAI caído")

    resp = _signed_post(_inbound_payload())

    assert resp.json() == {"status": "ok"}
    mock_kapso.send_text.assert_called_once()
    mock_sheets.update_status.assert_not_called()


@patch("webhook.sheets_client")
@patch("webhook.kapso_client")
@patch("webhook.llm")
@patch("webhook.telegram")
def test_dispatch_failure_does_not_crash_the_request(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    mock_sheets.get_contact_by_phone.return_value = {"nombre": "Ana", "row_number": 2}
    mock_kapso.get_history.return_value = []
    mock_llm.decide.return_value = llm_module.Decision(action="reply", reply_text="hola", reasoning="")
    mock_kapso.send_text.side_effect = Exception("Kapso caído")

    resp = _signed_post(_inbound_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_sheets.update_status.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd agents/outreach-bot && python -m pytest tests/test_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webhook'`

- [ ] **Step 3: Implement `webhook.py`**

```python
# agents/outreach-bot/src/webhook.py
import hashlib
import hmac
import os

from fastapi import FastAPI, Header, Request

import kapso_client
import llm
import sheets_client
import telegram

KAPSO_WEBHOOK_SECRET = os.environ["KAPSO_WEBHOOK_SECRET"]
MAX_CONTEXT_MESSAGES = 10
FALLBACK_MESSAGE = "Gracias por tu mensaje, en breve te respondemos."
LLM_FALLBACK_MESSAGE = "Perdón, tuvimos un inconveniente técnico. Te responden a la brevedad."

# Tracing is best-effort, same convention as morning-digest: if Phoenix is
# unreachable or setup fails, the webhook still has to answer messages.
try:
    from phoenix.otel import register
    from openinference.instrumentation.openai import OpenAIInstrumentor

    _tracer_provider = register(batch=False, project_name="outreach-bot")
    OpenAIInstrumentor().instrument(tracer_provider=_tracer_provider)
except Exception as e:
    print(f"Tracing no disponible, sigo sin instrumentación: {e}")

app = FastAPI()


def _verify_signature(raw_body: bytes, signature: str) -> bool:
    expected = hmac.new(KAPSO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(request: Request, x_webhook_signature: str = Header(default="")):
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_webhook_signature):
        print("Firma de webhook inválida, descarto el evento")
        return {"status": "ignored"}

    payload = await request.json()
    message = payload.get("message", {})
    phone = message.get("from")
    if not phone or message.get("kapso", {}).get("direction") != "inbound":
        return {"status": "ignored"}

    try:
        contact = sheets_client.get_contact_by_phone(phone)
    except Exception as e:
        print(f"No se pudo leer la Sheet, respondo con mensaje genérico: {e}")
        kapso_client.send_text(phone, FALLBACK_MESSAGE)
        return {"status": "ok"}

    if contact is None:
        print(f"Número no reconocido ({phone}), descarto")
        return {"status": "ignored"}

    try:
        history = kapso_client.get_history(phone, limit=MAX_CONTEXT_MESSAGES + 1)
    except Exception as e:
        print(f"No se pudo leer el historial de Kapso: {e}")
        kapso_client.send_text(phone, FALLBACK_MESSAGE)
        return {"status": "ok"}

    if len(history) > MAX_CONTEXT_MESSAGES:
        decision = llm.Decision(
            action="handoff", reply_text=None,
            reasoning="tope de 10 mensajes de contexto alcanzado",
        )
    else:
        try:
            decision = llm.decide(contact, history)
        except Exception as e:
            print(f"LLM falló, respondo con fallback: {e}")
            kapso_client.send_text(phone, LLM_FALLBACK_MESSAGE)
            return {"status": "ok"}

    _dispatch(contact, phone, decision)
    return {"status": "ok"}


def _dispatch(contact, phone, decision):
    try:
        if decision.action == "reply":
            kapso_client.send_text(phone, decision.reply_text)
            sheets_client.update_status(contact["row_number"], "in_conversation")
        elif decision.action == "close":
            kapso_client.send_text(phone, decision.reply_text)
            sheets_client.update_status(contact["row_number"], "qualified")
            telegram.send_telegram(f"✅ Lead calificado: {contact['nombre']} ({phone})\n{decision.reasoning}")
        elif decision.action == "handoff":
            sheets_client.update_status(contact["row_number"], "handoff")
            telegram.send_telegram(f"🤝 Derivar a humano: {contact['nombre']} ({phone})\n{decision.reasoning}")
        elif decision.action == "reject":
            sheets_client.update_status(contact["row_number"], "not_interested")
    except Exception as e:
        print(f"Fallo despachando la decisión ({decision.action}) para {phone}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd agents/outreach-bot && python -m pytest tests/test_webhook.py -v`
Expected: `11 passed`

- [ ] **Step 5: Run the full test suite**

Run: `cd agents/outreach-bot && python -m pytest tests/ -v`
Expected: all tests across `test_telegram.py`, `test_sheets_client.py`, `test_kapso_client.py`, `test_llm.py`, `test_sender.py`, `test_webhook.py` pass — trust the pytest summary line for the exact count.

- [ ] **Step 6: Commit**

```bash
git add agents/outreach-bot/src/webhook.py agents/outreach-bot/tests/test_webhook.py
git commit -m "feat(outreach-bot): add webhook.py Deployment entrypoint"
```

---

### Task 7: Finalize the Dockerfile for two entrypoints and smoke-test the image

**Files:**
- Modify: `agents/outreach-bot/Dockerfile` (created in Task 1 — verify it still matches, no changes expected since `COPY src/ .` already picks up every module added in Tasks 2–6)

**Interfaces:**
- Consumes: all of `src/*.py` (Tasks 1–6).
- Produces: `ghcr.io/julianalvarez95/outreach-bot:latest` image with default `CMD ["python", "sender.py"]`; `deployment.yaml` (Task 8) overrides `command` to run `webhook.py` via uvicorn from the same image.

- [ ] **Step 1: Build the image locally**

Run: `cd agents/outreach-bot && docker build -t outreach-bot:local .`
Expected: build succeeds, ends with `Successfully tagged outreach-bot:local` (or buildkit's equivalent final `naming to docker.io/library/outreach-bot:local done`).

- [ ] **Step 2: Smoke-test the `sender.py` entrypoint (default CMD)**

Run:
```bash
docker run --rm \
  -e KAPSO_API_KEY=x -e KAPSO_PHONE_NUMBER_ID=x -e KAPSO_WEBHOOK_SECRET=x \
  -e GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account","project_id":"x","private_key":"x","client_email":"x@x.iam.gserviceaccount.com","token_uri":"https://oauth2.googleapis.com/token"}' \
  -e SPREADSHEET_ID=x -e OPENAI_API_KEY=x -e SCHEDULING_LINK=https://x \
  -e TELEGRAM_BOT_TOKEN=x -e TELEGRAM_CHAT_ID=x \
  outreach-bot:local
```
Expected: the process starts and fails on the first real network call (invalid `GOOGLE_SERVICE_ACCOUNT_JSON`/Sheets auth, since these are fake credentials) — that's fine, this step only proves the image builds, installs dependencies correctly, and `sender.py` is importable and runs as the entrypoint. A `ModuleNotFoundError` or `ImportError` here would mean a real problem (e.g. a missing dependency in `requirements.txt`); a Google auth error is expected and OK.

- [ ] **Step 3: Smoke-test the `webhook.py` entrypoint (Deployment override)**

Run:
```bash
docker run --rm -p 8000:8000 \
  -e KAPSO_API_KEY=x -e KAPSO_PHONE_NUMBER_ID=x -e KAPSO_WEBHOOK_SECRET=x \
  -e GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account","project_id":"x","private_key":"x","client_email":"x@x.iam.gserviceaccount.com","token_uri":"https://oauth2.googleapis.com/token"}' \
  -e SPREADSHEET_ID=x -e OPENAI_API_KEY=x -e SCHEDULING_LINK=https://x \
  -e TELEGRAM_BOT_TOKEN=x -e TELEGRAM_CHAT_ID=x \
  outreach-bot:local python -m uvicorn webhook:app --host 0.0.0.0 --port 8000
```
In another terminal: `curl -s http://localhost:8000/healthz`
Expected: `{"status":"ok"}`

- [ ] **Step 4: Commit (only if the Dockerfile needed changes; otherwise skip)**

If Steps 1–3 passed without editing the Dockerfile, there's nothing to commit — move to Task 8.

---

### Task 8: Kubernetes manifests — CronJob, Deployment, Service, Cloudflare Tunnel

**Files:**
- Create: `agents/outreach-bot/cronjob.yaml`
- Create: `agents/outreach-bot/deployment.yaml`
- Create: `agents/outreach-bot/service.yaml`
- Create: `agents/outreach-bot/cloudflared/deployment.yaml`
- Create: `agents/outreach-bot/kustomization.yaml`

**Interfaces:**
- Consumes: image `ghcr.io/julianalvarez95/outreach-bot:latest` (Task 7), secret `outreach-bot-secrets` (created manually in Task 10, referenced by name here).
- Produces: the deployable manifest set ArgoCD will sync from this repo.

- [ ] **Step 1: Write `cronjob.yaml`**

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: outreach-bot-sender
  namespace: default
spec:
  schedule: "0 9 * * 1-5"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 300
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: outreach-bot-sender
            image: ghcr.io/julianalvarez95/outreach-bot:latest
            imagePullPolicy: Always
            envFrom:
            - secretRef:
                name: outreach-bot-secrets
            env:
            - name: VICTORIA_METRICS_URL
              value: "http://victoria-metrics.observability.svc.cluster.local:8428/api/v1/import/prometheus"
```

- [ ] **Step 2: Write `deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: outreach-bot-webhook
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: outreach-bot-webhook
  template:
    metadata:
      labels:
        app: outreach-bot-webhook
    spec:
      containers:
      - name: outreach-bot-webhook
        image: ghcr.io/julianalvarez95/outreach-bot:latest
        imagePullPolicy: Always
        command: ["python", "-m", "uvicorn", "webhook:app", "--host", "0.0.0.0", "--port", "8000"]
        ports:
        - containerPort: 8000
        envFrom:
        - secretRef:
            name: outreach-bot-secrets
        env:
        - name: PHOENIX_COLLECTOR_ENDPOINT
          value: "http://phoenix.observability.svc.cluster.local:4317"
        - name: OTEL_EXPORTER_OTLP_TIMEOUT
          value: "2"
        readinessProbe:
          httpGet:
            path: /healthz
            port: 8000
          initialDelaySeconds: 3
        livenessProbe:
          httpGet:
            path: /healthz
            port: 8000
          initialDelaySeconds: 10
```

- [ ] **Step 3: Write `service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: outreach-bot-webhook
  namespace: default
spec:
  selector:
    app: outreach-bot-webhook
  ports:
  - port: 8000
    targetPort: 8000
```

- [ ] **Step 4: Write `cloudflared/deployment.yaml`**

`cloudflared` picks up a remotely-managed tunnel token from the `TUNNEL_TOKEN` env var automatically — no local `config.yml`/credentials file needed. The public hostname → internal service mapping is configured in the Cloudflare Zero Trust dashboard (Task 10), not in git, same reasoning as every other secret in this repo.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: outreach-bot-cloudflared
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: outreach-bot-cloudflared
  template:
    metadata:
      labels:
        app: outreach-bot-cloudflared
    spec:
      containers:
      - name: cloudflared
        image: cloudflare/cloudflared:2025.7.0
        args: ["tunnel", "--no-autoupdate", "run"]
        envFrom:
        - secretRef:
            name: outreach-bot-secrets
```

- [ ] **Step 5: Write `kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - cronjob.yaml
  - deployment.yaml
  - service.yaml
  - cloudflared/deployment.yaml
```

- [ ] **Step 6: Validate the manifests render correctly**

Run: `cd agents/outreach-bot && kubectl kustomize .`
Expected: prints the 4 rendered resources (CronJob, Deployment×2, Service) with no errors. If `kubectl` isn't on PATH, install `kustomize` standalone and run `kustomize build .` instead — same expected output.

- [ ] **Step 7: Commit**

```bash
git add agents/outreach-bot/cronjob.yaml agents/outreach-bot/deployment.yaml \
        agents/outreach-bot/service.yaml agents/outreach-bot/cloudflared/deployment.yaml \
        agents/outreach-bot/kustomization.yaml
git commit -m "feat(outreach-bot): add Kubernetes manifests (CronJob, Deployment, Service, cloudflared)"
```

---

### Task 9: CI — GitHub Actions (lint, test, build, push to GHCR)

**Files:**
- Create: `.github/workflows/outreach-bot.yml`

**Interfaces:**
- Consumes: `agents/outreach-bot/src/requirements-dev.txt` (Task 1), `agents/outreach-bot/tests/` (Tasks 1–6), `agents/outreach-bot/Dockerfile` (Task 1/7).
- Produces: `ghcr.io/julianalvarez95/outreach-bot:latest`, pushed only from `main`. This is the first CI workflow in this repo.

- [ ] **Step 1: Write the workflow**

```yaml
# .github/workflows/outreach-bot.yml
name: outreach-bot

on:
  push:
    paths:
      - "agents/outreach-bot/**"
      - ".github/workflows/outreach-bot.yml"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r agents/outreach-bot/src/requirements-dev.txt
      - run: pip install ruff
      - run: ruff check agents/outreach-bot/src
      - run: cd agents/outreach-bot && python -m pytest tests/ -v

  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/build-push-action@v6
        with:
          context: agents/outreach-bot
          push: false

  push:
    needs: build
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: agents/outreach-bot
          push: true
          tags: ghcr.io/julianalvarez95/outreach-bot:latest
```

- [ ] **Step 2: Validate the workflow YAML syntax locally**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/outreach-bot.yml'))" && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/outreach-bot.yml
git commit -m "ci(outreach-bot): add lint/test/build/push GitHub Actions workflow"
```

- [ ] **Step 4: Push the branch and confirm the workflow runs**

This step needs your explicit go-ahead since it pushes to the remote — after committing, push the branch (or open a PR) and check the Actions tab. The `test` job (lint + the full pytest suite from Tasks 1–6) should go green without touching any real credentials, since every test mocks its external calls. The `push` job only runs on `main`.

---

### Task 10: Rollout — secrets, Cloudflare Tunnel, Kapso template, end-to-end test

This task is operational, not code — it's what turns the working image into a live bot. Each step needs an external account/dashboard action from you; none of it can be scripted or tested automatically.

- [ ] **Step 1: Create the Google Sheet**

Create a Google Sheet, first tab named `contactos` (or set `WORKSHEET_NAME` to whatever you name it), header row exactly: `nombre`, `telefono`, `status`, `last_update`. Add at least one test contact with `status=pending` and your own phone number (digits only, no `+`) for the end-to-end test in Step 6.

- [ ] **Step 2: Create a Google service account and share the Sheet with it**

In Google Cloud Console: create a service account, enable the Sheets API for the project, generate a JSON key. Share the Sheet (Editor access) with the service account's `client_email`. Keep the JSON key on hand for Step 4 — this is `GOOGLE_SERVICE_ACCOUNT_JSON`.

- [ ] **Step 3: Submit and get approval for the WhatsApp templates via Kapso**

In the Kapso dashboard: submit two Meta-approved templates — `outreach_intro` (body parameter `{{customer_name}}` → mapped as `nombre` in `kapso_client.send_template`) and `outreach_followup`. Template approval can take time; submit these first, before the rest of this task, so approval isn't the last blocker. Also register the webhook: point it at `https://<your-tunnel-hostname>/webhook`, and set a webhook signing secret in the dashboard — this becomes `KAPSO_WEBHOOK_SECRET`.

- [ ] **Step 4: Create the Kubernetes secret**

```bash
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl create secret generic outreach-bot-secrets \
  --from-literal=KAPSO_API_KEY='<kapso-project-api-key>' \
  --from-literal=KAPSO_PHONE_NUMBER_ID='<whatsapp-phone-number-id>' \
  --from-literal=KAPSO_WEBHOOK_SECRET='<webhook-signing-secret-from-step-3>' \
  --from-literal=GOOGLE_SERVICE_ACCOUNT_JSON='<service-account-json-oneline-from-step-2>' \
  --from-literal=SPREADSHEET_ID='<google-sheet-id-from-step-1>' \
  --from-literal=OPENAI_API_KEY='<openai-api-key>' \
  --from-literal=SCHEDULING_LINK='<google-calendar-appointment-or-calendly-link>' \
  --from-literal=TELEGRAM_BOT_TOKEN='<telegram-bot-token>' \
  --from-literal=TELEGRAM_CHAT_ID='<telegram-chat-id>' \
  --from-literal=TUNNEL_TOKEN='<cloudflare-tunnel-token>'
```

- [ ] **Step 5: Create the Cloudflare Tunnel and public hostname**

In the Cloudflare Zero Trust dashboard: create a tunnel, copy its token (`TUNNEL_TOKEN`, already used in Step 4), and add a Public Hostname route pointing to `http://outreach-bot-webhook.default.svc.cluster.local:8000` (the in-cluster Service from Task 8). This is the hostname you registered as the Kapso webhook URL in Step 3.

- [ ] **Step 6: Deploy and verify end-to-end with your own phone number**

Let ArgoCD sync (or trigger manually), confirm all three workloads are running (`outreach-bot-sender` CronJob, `outreach-bot-webhook` Deployment, `outreach-bot-cloudflared` Deployment), then trigger the CronJob once by hand:

```bash
KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl create job --from=cronjob/outreach-bot-sender outreach-bot-sender-test
```

Confirm you receive the WhatsApp template on your own phone (the test contact from Step 1), reply to it, and confirm: (a) the webhook responds, (b) the Sheet's `status` moves from `sent` to `in_conversation`/`qualified`/`handoff`/`not_interested` as expected, (c) a qualifying or handoff-worthy reply triggers a Telegram notification. Only after this succeeds with your own number should real contacts be added to the Sheet.

---

## Self-Review

**Spec coverage:** every section of `docs/superpowers/specs/2026-08-10-outreach-bot-design.md` maps to a task — packaging (Task 1/7/8), Sheet schema/state machine (Task 2, enforced by `sender.py`/`webhook.py` status transitions in Tasks 5–6), data flow A/B (Tasks 5–6), `llm.py` interface addendum (Task 4, matches the `Decision`/`decide()` shape agreed in chat), error handling (all 5 scenarios covered by webhook/sender tests, Cloudflare-down is inherently untestable in unit tests and is covered operationally by Kapso's own retry policy), observability (Phoenix in Task 6, VictoriaMetrics in Tasks 1/5), secrets (Task 10), rollout/CI-CD (Tasks 9–10).

**Placeholder scan:** no TBD/TODO; every step has runnable code or an exact command with an expected result.

**Type consistency:** `Decision(action, reply_text, reasoning)` defined in Task 4 is used identically in Task 6's tests and implementation. `sheets_client` row dicts always carry `row_number` (Task 2) and are consumed that way in Tasks 5–6. `kapso_client.get_history` returns `{"role", "content"}` dicts consumed identically by `llm.decide`'s `history` parameter (Task 4) and `webhook.py`'s cap check (Task 6).
