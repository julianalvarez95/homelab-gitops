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
