from app import settings


def test_timeout_positive():
    assert settings.TIMEOUT > 0
