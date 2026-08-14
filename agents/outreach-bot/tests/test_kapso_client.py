from unittest.mock import MagicMock, patch

import kapso_client


@patch("kapso_client.requests.post")
def test_send_template_builds_template_message_request(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.text = "{}"
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {}

    kapso_client.send_template("15551234567", "outreach_intro", {"nombre": "Ana"})

    url = mock_post.call_args[0][0]
    kwargs = mock_post.call_args[1]
    assert url == f"{kapso_client.BASE_URL}/messages"
    body = kwargs["json"]
    assert body["to"] == "15551234567"
    assert body["type"] == "template"
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
    assert body["type"] == "text"
    assert body["text"]["body"] == "hola"


@patch("kapso_client.requests.get")
def test_get_history_looks_up_conversation_then_messages_oldest_first(mock_get):
    conv_resp = MagicMock()
    conv_resp.raise_for_status = MagicMock()
    conv_resp.json.return_value = {"data": [{"id": "conv-1"}]}

    msgs_resp = MagicMock()
    msgs_resp.raise_for_status = MagicMock()
    msgs_resp.json.return_value = {
        "data": [
            {"text": {"body": "segundo"}, "kapso": {"direction": "outbound"}},
            {"text": {"body": "primero"}, "kapso": {"direction": "inbound"}},
        ]
    }
    mock_get.side_effect = [conv_resp, msgs_resp]

    history = kapso_client.get_history("15551234567", limit=11)

    conv_call, msgs_call = mock_get.call_args_list
    assert conv_call[1]["params"] == {"phone_number": "15551234567", "limit": 1}
    assert msgs_call[1]["params"]["conversation_id"] == "conv-1"
    assert msgs_call[1]["params"]["limit"] == 11
    assert history == [
        {"role": "user", "content": "primero"},
        {"role": "assistant", "content": "segundo"},
    ]


@patch("kapso_client.requests.get")
def test_get_history_returns_empty_when_no_conversation_yet(mock_get):
    conv_resp = MagicMock()
    conv_resp.raise_for_status = MagicMock()
    conv_resp.json.return_value = {"data": []}
    mock_get.return_value = conv_resp

    assert kapso_client.get_history("15551234567") == []
    mock_get.assert_called_once()


@patch("kapso_client.requests.get")
def test_get_history_defaults_missing_text_to_empty_string(mock_get):
    conv_resp = MagicMock()
    conv_resp.raise_for_status = MagicMock()
    conv_resp.json.return_value = {"data": [{"id": "conv-1"}]}

    msgs_resp = MagicMock()
    msgs_resp.raise_for_status = MagicMock()
    msgs_resp.json.return_value = {
        "data": [{"kapso": {"direction": "inbound"}}],  # e.g. a media message, no "text" key
    }
    mock_get.side_effect = [conv_resp, msgs_resp]

    history = kapso_client.get_history("15551234567")

    assert history == [{"role": "user", "content": ""}]
