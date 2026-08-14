import os

import requests

KAPSO_API_KEY = os.environ["KAPSO_API_KEY"]
KAPSO_PHONE_NUMBER_ID = os.environ["KAPSO_PHONE_NUMBER_ID"]
KAPSO_TEMPLATE_LANG = os.environ.get("KAPSO_TEMPLATE_LANG", "es_AR")

BASE_URL = f"https://api.kapso.ai/meta/whatsapp/v24.0/{KAPSO_PHONE_NUMBER_ID}"
CONVERSATIONS_URL = "https://api.kapso.ai/platform/v1/whatsapp/conversations"
HEADERS = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}


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
    resp = requests.post(f"{BASE_URL}/messages", json=body, headers=HEADERS, timeout=10)
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
    resp = requests.post(f"{BASE_URL}/messages", json=body, headers=HEADERS, timeout=10)
    print(f"Kapso send_text response: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()


def get_history(phone, limit=10):
    # Two calls because Kapso has no single "history for this phone number"
    # endpoint: /whatsapp/conversations resolves phone -> conversation id,
    # then /messages is filtered by that id. Verified against docs.kapso.ai;
    # confirm conversation_id is the right filter param against a live
    # sandbox before go-live (Task 10) — no REST example for this specific
    # filter was in the docs, only the equivalent TS SDK method signature.
    conv_resp = requests.get(
        CONVERSATIONS_URL,
        params={"phone_number": phone, "limit": 1},
        headers=HEADERS,
        timeout=10,
    )
    conv_resp.raise_for_status()
    conversations = conv_resp.json()["data"]
    if not conversations:
        return []

    msgs_resp = requests.get(
        f"{BASE_URL}/messages",
        params={"conversation_id": conversations[0]["id"], "limit": limit},
        headers=HEADERS,
        timeout=10,
    )
    msgs_resp.raise_for_status()
    messages = msgs_resp.json()["data"]
    return [
        {
            "role": "assistant" if m["kapso"]["direction"] == "outbound" else "user",
            "content": m.get("text", {}).get("body", ""),
        }
        for m in reversed(messages)
    ]
