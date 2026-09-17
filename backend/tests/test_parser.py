import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.errors import AppError
from app.services.bili_market.parser import parse_cluster_info

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_available_parser_uses_purchase_button_and_normalizes_urls():
    snapshot = parse_cluster_info(load("available.json"), 10000002733)
    assert snapshot.available is True
    assert snapshot.current_price == Decimal("129.00")
    assert snapshot.reference_price == Decimal("139.00")
    assert snapshot.cover_url.startswith("https://")
    assert snapshot.detail_url.endswith("clusterId=10000002733")


def test_sold_out_parser_never_uses_recent_deal_as_current_price():
    snapshot = parse_cluster_info(load("sold_out.json"), 10000011281)
    assert snapshot.available is False
    assert snapshot.current_price is None
    assert snapshot.recent_deal_price == Decimal("150.00")


def test_business_error_payload_cannot_be_treated_as_sold_out():
    with pytest.raises(AppError) as error:
        parse_cluster_info(load("error_payload.json"), 10000011281)
    assert error.value.code == "BILI_API_INVALID_RESPONSE"


def test_missing_data_payload_is_invalid():
    with pytest.raises(AppError) as error:
        parse_cluster_info({"code": 0}, 10000011281)
    assert error.value.code == "BILI_API_INVALID_RESPONSE"


def test_success_payload_without_purchase_button_is_invalid():
    with pytest.raises(AppError) as error:
        parse_cluster_info(load("missing_button.json"), 10000011281)
    assert error.value.code == "BILI_API_INVALID_RESPONSE"
