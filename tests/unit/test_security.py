"""الأسرار والجلسات وإخفاء البيانات في السجلات (SRS §NFR-003، §NFR-004، AC-011)."""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import JsonFormatter, mask_phone, redact
from app.core.security import (
    RateLimiter,
    SessionCodec,
    csrf_token_for,
    hash_password,
    hash_public_token,
    new_public_token,
    verify_csrf_token,
    verify_password,
    verify_public_token,
)


class TestPasswords:
    def test_hash_is_argon2id_and_verifies(self):
        digest = hash_password("a-strong-password-1234")
        assert digest.startswith("$argon2id$")
        assert verify_password(digest, "a-strong-password-1234")

    def test_wrong_password_fails(self):
        digest = hash_password("a-strong-password-1234")
        assert not verify_password(digest, "wrong")

    def test_a_corrupt_hash_fails_closed(self):
        assert not verify_password("not-a-hash", "anything")

    def test_two_hashes_of_the_same_password_differ(self):
        assert hash_password("same-password") != hash_password("same-password")


class TestPublicTokens:
    def test_token_is_long_and_url_safe(self):
        token = new_public_token()
        assert len(token) >= 40
        assert token.replace("-", "").replace("_", "").isalnum()

    def test_only_the_hash_is_stored(self):
        token = new_public_token()
        digest = hash_public_token(token)
        assert token not in digest
        assert len(digest) == 64

    def test_verification_is_exact(self):
        token = new_public_token()
        assert verify_public_token(token, hash_public_token(token))
        assert not verify_public_token(token + "x", hash_public_token(token))
        assert not verify_public_token(token, "")


class TestSessionAndCsrf:
    def test_session_round_trip(self):
        codec = SessionCodec()
        value = codec.dumps({"uid": "u1", "sid": "s1"})
        assert codec.loads(value) == {"uid": "u1", "sid": "s1"}

    def test_a_tampered_cookie_is_rejected(self):
        codec = SessionCodec()
        value = codec.dumps({"uid": "u1"})
        assert codec.loads(value[:-3] + "aaa") is None

    def test_an_expired_cookie_is_rejected(self):
        codec = SessionCodec(max_age_seconds=-1)
        assert codec.loads(codec.dumps({"uid": "u1"})) is None

    def test_csrf_token_is_bound_to_the_session(self):
        assert verify_csrf_token("sid-1", csrf_token_for("sid-1"))
        assert not verify_csrf_token("sid-2", csrf_token_for("sid-1"))
        assert not verify_csrf_token("sid-1", "")


class TestRateLimiter:
    def test_allows_up_to_the_limit_then_blocks(self):
        limiter = RateLimiter(max_events=3, window_seconds=60)
        assert [limiter.hit("k", now=0) for _ in range(4)] == [True, True, True, False]

    def test_the_window_slides(self):
        limiter = RateLimiter(max_events=1, window_seconds=10)
        assert limiter.hit("k", now=0)
        assert not limiter.hit("k", now=5)
        assert limiter.hit("k", now=11)

    def test_reset_clears_a_key(self):
        limiter = RateLimiter(max_events=1, window_seconds=60)
        limiter.hit("k", now=0)
        limiter.reset("k")
        assert limiter.hit("k", now=1)

    def test_keys_are_independent(self):
        limiter = RateLimiter(max_events=1, window_seconds=60)
        assert limiter.hit("a", now=0)
        assert limiter.hit("b", now=0)


class TestRedaction:
    def test_api_key_in_a_url_is_removed(self):
        text = "GET https://api.hydrawise.com/api/v1/statusschedule.php?api_key=SUPERSECRET&x=1"
        assert "SUPERSECRET" not in redact(text)
        assert "api_key=***" in redact(text)

    def test_the_configured_secret_is_removed_anywhere(self):
        # المفتاح مضبوط في conftest.
        assert "test-api-key-abcdef" not in redact("boom test-api-key-abcdef boom")

    @pytest.mark.parametrize(
        ("number", "expected"),
        [("96812345218", "968****5218"), ("+968 9123 4567", "968****4567")],
    )
    def test_phone_masking(self, number, expected):
        assert mask_phone(number) == expected

    def test_short_numbers_are_fully_masked(self):
        assert set(mask_phone("12345")) == {"*"}

    def test_json_formatter_redacts_and_stays_one_line(self):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1,
            "url=https://x/y.php?api_key=SUPERSECRET", None, None,
        )
        record.endpoint = "statusschedule.php"
        line = JsonFormatter().format(record)
        assert "\n" not in line
        payload = json.loads(line)
        assert "SUPERSECRET" not in payload["message"]
        assert payload["endpoint"] == "statusschedule.php"

    def test_formatter_keeps_arabic_readable(self):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "بدأ الجمع", None, None)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "بدأ الجمع"
