from app.core.rate_limit import SlidingWindowLimiter


def test_sliding_window_limiter_is_configurable_and_resettable():
    limiter = SlidingWindowLimiter()
    assert limiter.allow("login:test", 2, 60)
    assert limiter.allow("login:test", 2, 60)
    assert not limiter.allow("login:test", 2, 60)
    limiter.reset("login:test")
    assert limiter.allow("login:test", 2, 60)
