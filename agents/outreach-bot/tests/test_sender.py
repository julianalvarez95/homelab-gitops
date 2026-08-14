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
