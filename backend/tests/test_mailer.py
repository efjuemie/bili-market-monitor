import smtplib
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.mailer import Mailer, price_alert_email


def test_price_email_contains_plain_text_purchase_link_and_cover():
    product = SimpleNamespace(title="示例", current_price="105.00", available=True, detail_url="https://mall.bilibili.com/item", cover_url="https://i0.hdslb.com/cover.jpg")
    subject, text, html = price_alert_email(product, "110.00", "2026-09-17T14:30:00+08:00")
    assert "https://mall.bilibili.com/item" in text
    assert "cover.jpg" in html
    assert "105.00" in subject


def test_unconfigured_smtp_reports_unsent_without_marking_delivery():
    settings = Settings(smtp_host="", smtp_username="", smtp_password="", smtp_from_email="")
    assert Mailer(settings).send("user@example.com", "subject", "text") is False


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.login_args = None
        self.sent = False
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.sent = True


def _smtp_settings(**overrides):
    values = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sender@example.com",
        "smtp_password": "not-a-real-secret",
        "smtp_from_email": "sender@example.com",
        "smtp_use_ssl": False,
        "smtp_use_tls": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_ssl_smtp_does_not_starttls(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    settings = _smtp_settings(smtp_port=465, smtp_use_ssl=True, smtp_use_tls=False)

    assert Mailer(settings).send("user@example.com", "subject", "text") is True

    server = _FakeSMTP.instances[0]
    assert (server.host, server.port, server.timeout) == ("smtp.example.com", 465, 20)
    assert server.starttls_called is False
    assert server.login_args == ("sender@example.com", "not-a-real-secret")
    assert server.sent is True


def test_starttls_smtp_uses_starttls(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    settings = _smtp_settings()

    assert Mailer(settings).send("user@example.com", "subject", "text") is True

    server = _FakeSMTP.instances[0]
    assert server.starttls_called is True
    assert server.sent is True


def test_smtp_security_modes_cannot_be_enabled_together():
    with pytest.raises(ValueError, match="SMTP_USE_SSL.*SMTP_USE_TLS"):
        _smtp_settings(smtp_use_ssl=True, smtp_use_tls=True)


def test_smtp_exception_is_logged_without_secret_and_re_raised(monkeypatch, caplog):
    class _BrokenSMTP(_FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    monkeypatch.setattr(smtplib, "SMTP", _BrokenSMTP)
    settings = _smtp_settings()

    with pytest.raises(smtplib.SMTPAuthenticationError):
        Mailer(settings).send("recipient@example.com", "subject", "text")

    assert "r***@example.com" in caplog.text
    assert "not-a-real-secret" not in caplog.text
