"""TelegramNotifier: real-sending path, fully mocked. No real sends here."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.databases import manager as dbm
from app.identity import models as im
from app.jobs.telegram_notifier import TelegramNotifier, build_default_notifier
from app.jobs.runner import LoggingNotifier


def _notifier(seed):
    return TelegramNotifier(seed["data_dir"], bot_token="TEST_TOKEN")


def test_send_success(seed):
    n = _notifier(seed)
    with dbm.identity_session(seed["data_dir"]) as s:
        u = s.get(im.User, seed["owner"])
        u.telegram_id = 777001
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock())
    with patch.object(n, "_get_bot", return_value=mock_bot):
        assert n.send(seed["owner"], "تذكير تجريبي") is True
    mock_bot.send_message.assert_called_once()
    _, kwargs = mock_bot.send_message.call_args
    assert kwargs["chat_id"] == 777001


def test_send_no_telegram_id_returns_false(seed):
    n = _notifier(seed)
    with dbm.identity_session(seed["data_dir"]) as s:
        u = s.get(im.User, seed["owner"])
        u.telegram_id = None
    assert n.send(seed["owner"], "hi") is False


def test_send_api_error_returns_false_for_retry(seed):
    n = _notifier(seed)
    with dbm.identity_session(seed["data_dir"]) as s:
        s.get(im.User, seed["owner"]).telegram_id = 777002
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(side_effect=Exception("network down"))
    with patch.object(n, "_get_bot", return_value=mock_bot):
        assert n.send(seed["owner"], "hi") is False


def test_default_is_log_only(seed, monkeypatch):
    monkeypatch.delenv("REMINDER_DELIVERY", raising=False)
    n = build_default_notifier(seed["data_dir"])
    assert isinstance(n, LoggingNotifier)


def test_telegram_mode_without_token_falls_back_to_log(seed, monkeypatch):
    monkeypatch.setenv("REMINDER_DELIVERY", "telegram")
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", "")
    n = build_default_notifier(seed["data_dir"])
    assert isinstance(n, LoggingNotifier)
