from __future__ import annotations

from grumpycat.core.scrub import REDACTED, scrub_mapping, scrub_text, scrub_value


def test_scrub_text_patterns() -> None:
    assert scrub_text(None) is None
    assert scrub_text("") == ""
    out = scrub_text(
        "user jane.doe@example.test from 203.0.113.9 sent Bearer abcdefghijklmnop12 "
        "card 4111 1111 1111 1111 phone +1 555 123 4567 secret Ab1Ab1Ab1Ab1Ab1Ab1Ab1Ab1Ab1Ab1Ab1Ab1"
    )
    assert out is not None
    for needle in ("jane.doe", "203.0.113.9", "abcdefghijklmnop12", "4111", "555 123", "Ab1Ab1"):
        assert needle not in out
    assert out.count(REDACTED) >= 5


def test_scrub_text_leaves_ordinary_code_alone() -> None:
    s = "NoMethodError: undefined method `email' for nil in app/services/notifier.rb:42"
    assert scrub_text(s) == s


def test_scrub_mapping_redacts_keys_and_recurses_into_lists() -> None:
    data = {
        "Authorization": "Bearer x",
        "headers": [["Cookie", "a=b"], ["X-Forwarded-For", "203.0.113.9"]],
        "query": [["to", "someone@example.test"]],
        "nested": {"password": "hunter2", "note": "mail me at x@y.io", "n": 3},
        "api_key_id": "k1",
    }
    out = scrub_mapping(data)
    assert out is not None
    assert out["Authorization"] == REDACTED
    assert out["api_key_id"] == REDACTED  # substring match on api_key
    assert out["headers"][1][1] == REDACTED
    assert out["query"][0][1] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert "x@y.io" not in out["nested"]["note"]
    assert out["nested"]["n"] == 3


def test_scrub_depth_limit_and_none() -> None:
    deep: dict = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "x@y.io"}}}}}}}
    assert scrub_value(deep, depth=2) == {"a": {"b": REDACTED}}
    assert scrub_mapping(None) is None
    assert scrub_value(("x@y.io", 1)) == [REDACTED, 1]
