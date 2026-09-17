import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from app.errors import AppError


@dataclass(frozen=True)
class BiliProductSnapshot:
    cluster_id: int
    title: str
    cover_url: Optional[str]
    detail_url: str
    available: bool
    current_price: Optional[Decimal]
    reference_price: Optional[Decimal]
    purchase_button_text: Optional[str]
    delivery_mode: Optional[str]
    recent_avg_price: Optional[Decimal]
    recent_deal_price: Optional[Decimal]
    recent_deal_time_text: Optional[str]


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        text = str(value).replace(",", "").replace("¥", "").strip()
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _url(value: Any) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return "https://mall.bilibili.com" + value
    return value


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _price_from_button(button: Dict[str, Any]) -> Optional[Decimal]:
    price_desc = button.get("buttonPriceDescVO") or {}
    integer = price_desc.get("priceIntegerPart")
    decimal = price_desc.get("priceDecimalPart")
    if integer not in (None, ""):
        combined = str(integer)
        if decimal not in (None, ""):
            combined += "." + str(decimal).zfill(2)
        return _decimal(combined)

    # The button subtext can contain a label such as "¥129" or "最低价 ¥129".
    subtext = button.get("buttonSubText")
    if subtext:
        match = re.search(r"\d+(?:\.\d{1,2})?", str(subtext).replace(",", ""))
        if match:
            return _decimal(match.group(0))
    return None


def _unwrap(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "code" in payload and payload.get("code") not in (0, "0", None):
        raise AppError("BILI_API_INVALID_RESPONSE", "B站接口返回业务错误", 502)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AppError("BILI_API_INVALID_RESPONSE", "B站接口返回格式无效", 502)
    # Some responses put the useful object below data.result.
    result = data.get("result")
    return result if isinstance(result, dict) else data


def parse_cluster_info(payload: Dict[str, Any], cluster_id: int) -> BiliProductSnapshot:
    """Convert the volatile B站 response into the stable business snapshot.

    Only the purchase button is allowed to provide ``current_price``. Recent
    transactions and average prices are intentionally kept separate so a sold
    out item can never be reported as currently purchasable.
    """
    if not isinstance(payload, dict):
        raise AppError("BILI_API_INVALID_RESPONSE", "B站接口返回格式无效", 502)
    root = _unwrap(payload)
    button = root.get("clusterPurchaseButton")
    if not isinstance(button, dict):
        raise AppError("BILI_API_INVALID_RESPONSE", "B站接口返回格式无效", 502)

    current_price = _price_from_button(button)
    button_text = _first_text(button.get("buttonText"))
    disabled = button.get("buttonDisabled")
    sold_out = bool(button_text and ("售罄" in button_text or "售完" in button_text))
    available = disabled is False and current_price is not None and not sold_out

    price_floor = root.get("clusterPriceFloorVO") or {}
    price_tag = price_floor.get("priceTag") or {}
    reference_price = _decimal(price_tag.get("price"))
    if reference_price is None:
        reference_price = _decimal(price_tag.get("firstPrice"))

    header = root.get("clusterHeaderFloorVO") or {}
    image_list = header.get("clusterImgList") or []
    first_image = image_list[0] if image_list else None
    if isinstance(first_image, dict):
        first_image = first_image.get("url") or first_image.get("src")
    cover_url = _url(first_image)
    title = _first_text(
        header.get("title"),
        header.get("clusterName"),
        root.get("clusterName"),
        root.get("title"),
    ) or f"B站市集商品 {cluster_id}"

    detail_url = None
    header_items = header.get("header") or []
    if isinstance(header_items, dict):
        header_items = [header_items]
    for item in header_items:
        if isinstance(item, dict):
            extra = item.get("shareExtra") or {}
            detail_url = _url(extra.get("url"))
            if detail_url:
                break
    detail_url = detail_url or f"https://mall.bilibili.com/neul-next/resell/detail.html?clusterId={cluster_id}"

    recent = root.get("clusterRecentBuyFloorVO") or {}
    deals = recent.get("recentDeals") or []
    latest = deals[0] if deals and isinstance(deals[0], dict) else {}

    return BiliProductSnapshot(
        cluster_id=cluster_id,
        title=title,
        cover_url=cover_url,
        detail_url=detail_url,
        available=available,
        current_price=current_price if available else None,
        reference_price=reference_price,
        purchase_button_text=button_text,
        delivery_mode=_first_text(button.get("deliveryModeDesc"), root.get("deliveryModeDesc")),
        recent_avg_price=_decimal(recent.get("avgPrice")),
        recent_deal_price=_decimal(latest.get("dealPrice")),
        recent_deal_time_text=_first_text(latest.get("dealTimeText"), latest.get("dealTime")),
    )
