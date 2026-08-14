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
