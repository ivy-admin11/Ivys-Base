"""ivy_core.sheets_logger: auth + low-level snapshot writes only.

Every test mocks the Google Sheets client entirely — none of these ever
make a real network call.
"""

from unittest.mock import MagicMock

import config
from ivy_core import sheets_logger


def _fake_service(tab_name="Sharp Picks", calls=None):
    calls = calls if calls is not None else []
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": tab_name}}]
    }

    def record(name):
        def _execute():
            calls.append(name)
            return {}
        return _execute

    update_mock = service.spreadsheets.return_value.values.return_value.update
    update_mock.return_value.execute.side_effect = record("update")
    clear_mock = service.spreadsheets.return_value.values.return_value.clear
    clear_mock.return_value.execute.side_effect = record("clear")
    return service, calls, update_mock, clear_mock


def test_write_snapshot_not_configured_when_spreadsheet_id_missing(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "")
    result = sheets_logger.write_snapshot(["A"], [["1"]])
    assert result["status"] == "not_configured"


def test_write_snapshot_not_configured_when_auth_unavailable(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "sheet123")
    monkeypatch.setattr(sheets_logger, "get_sheets_service", lambda: None)
    result = sheets_logger.write_snapshot(["A"], [["1"]])
    assert result["status"] == "not_configured"


def test_write_snapshot_not_configured_when_tab_missing(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_SHEETS_PICKS_TAB", "Sharp Picks")
    service, calls, *_ = _fake_service(tab_name="Some Other Tab")
    monkeypatch.setattr(sheets_logger, "get_sheets_service", lambda: service)
    result = sheets_logger.write_snapshot(["A"], [["1"]])
    assert result["status"] == "not_configured"


def test_write_snapshot_uses_raw_value_input_option(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_SHEETS_PICKS_TAB", "Sharp Picks")
    service, calls, update_mock, clear_mock = _fake_service()
    monkeypatch.setattr(sheets_logger, "get_sheets_service", lambda: service)

    result = sheets_logger.write_snapshot(["Sport", "Side"], [["NFL", "A -3"]])

    assert result["status"] == "success"
    _, kwargs = update_mock.call_args
    assert kwargs["valueInputOption"] == "RAW"


def test_write_snapshot_clears_stale_rows_only_after_successful_write(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_SHEETS_PICKS_TAB", "Sharp Picks")
    service, calls, update_mock, clear_mock = _fake_service()
    monkeypatch.setattr(sheets_logger, "get_sheets_service", lambda: service)

    sheets_logger.write_snapshot(["Sport"], [["NFL"], ["NBA"]])

    assert calls == ["update", "clear"], "clear must happen strictly after a successful update"


def test_write_snapshot_reports_error_not_success_on_exception(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "sheet123")
    monkeypatch.setattr(config, "GOOGLE_SHEETS_PICKS_TAB", "Sharp Picks")
    service, calls, update_mock, clear_mock = _fake_service()
    update_mock.return_value.execute.side_effect = RuntimeError("network exploded")
    monkeypatch.setattr(sheets_logger, "get_sheets_service", lambda: service)

    result = sheets_logger.write_snapshot(["Sport"], [["NFL"]])

    assert result["status"] == "error"
    clear_mock.return_value.execute.assert_not_called()


def test_write_summary_not_configured_when_missing_config(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SHEETS_SPREADSHEET_ID", "")
    result = sheets_logger.write_summary(["Scope"], [["Overall"]])
    assert result["status"] == "not_configured"


def test_get_sheets_service_returns_none_on_auth_failure(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_SERVICE_ACCOUNT_KEY", "/nonexistent/key.json")
    service = sheets_logger.get_sheets_service()
    assert service is None
