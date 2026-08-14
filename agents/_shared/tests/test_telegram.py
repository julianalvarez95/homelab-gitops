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


def test_split_hard_slices_a_single_unbreakable_line():
    chunks = telegram.split_telegram_message("a" * 25, max_chars=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 5]


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
def test_send_telegram_raises_on_http_error(mock_post):
    mock_post.return_value.raise_for_status.side_effect = Exception("boom")
    telegram.TELEGRAM_BOT_TOKEN = "bot123:ABC"

    try:
        telegram.send_telegram("hola")
        assert False, "expected raise_for_status to propagate"
    except Exception as e:
        assert str(e) == "boom"


@patch("telegram.requests.post")
def test_push_metrics_fails_open_on_exception(mock_post):
    mock_post.side_effect = Exception("VM caído")
    telegram.push_metrics(["agent_run_success{agent=\"x\"} 1"])  # must not raise


def test_push_metrics_noop_without_lines():
    with patch("telegram.requests.post") as mock_post:
        telegram.push_metrics([])
        mock_post.assert_not_called()
