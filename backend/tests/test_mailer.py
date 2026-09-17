from types import SimpleNamespace

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
