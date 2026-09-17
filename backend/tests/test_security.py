from app.core.security import hash_password, hash_token, verify_password


def test_password_hash_is_argon2_and_verifies():
    password_hash = hash_password("correct horse battery staple")
    assert password_hash.startswith("$argon2")
    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong", password_hash)


def test_tokens_are_one_way_hashes():
    assert hash_token("token") != "token"
