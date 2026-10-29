from mylib import parse


def test_parse_strips_text():
    assert parse("  hello ").text == "hello"
