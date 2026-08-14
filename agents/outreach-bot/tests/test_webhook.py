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


def _outbound_status_payload():
    return {
        "message": {
            "id": "wamid.2",
            "kapso": {"direction": "outbound", "status": "delivered"},
        },
        "conversation": {"id": "conv_1", "phone_number": "5491111", "phone_number_id": "123"},
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
def test_outbound_status_event_is_ignored(mock_telegram, mock_llm, mock_kapso, mock_sheets):
    resp = _signed_post(_outbound_status_payload())

    assert resp.json() == {"status": "ignored"}
    mock_sheets.get_contact_by_phone.assert_not_called()
    mock_llm.decide.assert_not_called()


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
