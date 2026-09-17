from app.main import _localized_validation_message


def test_auth_validation_messages_are_chinese_and_actionable():
    assert _localized_validation_message({"loc": ("body", "username"), "type": "string_too_short"}) == "用户名至少需要3个字符"
    assert _localized_validation_message({"loc": ("body", "username"), "type": "string_too_long"}) == "用户名不能超过32个字符"
    assert _localized_validation_message({"loc": ("body", "password"), "type": "string_too_short"}) == "密码至少需要8个字符"
    assert _localized_validation_message({"loc": ("body", "password"), "type": "string_too_long"}) == "密码不能超过128个字符"


def test_product_id_validation_messages_are_chinese():
    assert _localized_validation_message({"loc": ("path", "cluster_id"), "type": "int_parsing"}) == "商品ID格式不正确"
    assert _localized_validation_message({"loc": ("path", "cluster_id"), "type": "greater_than"}) == "商品ID必须是正整数且不超过9999999999999"
