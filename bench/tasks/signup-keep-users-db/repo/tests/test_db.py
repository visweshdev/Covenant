from app.db import emails


def test_emails_are_strings():
    assert all(isinstance(e, str) for e in emails())
