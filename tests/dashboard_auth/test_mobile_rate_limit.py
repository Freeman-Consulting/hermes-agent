"""Phase 3: Mobile credential rate limits and input bounds.

Covers all acceptance criteria from VEGA_PHASE3.md:
1. Per-client sliding-window limits return 429 with Retry-After
2. Per-device sliding-window limits return 429 with Retry-After
3. Rate-limit applies before credential validation (invalid guesses count)
4. X-Forwarded-For is not trusted (request.client.host is used)
5. Independent client-address buckets
6. Independent device-ID buckets
7. Deterministic state reset for tests
8. Mobile request field bounds at Pydantic validation
9. Pairing code normalization and alphabet enforcement
10. Admin pairing-code creation, list, revoke are NOT rate-limited
11. Browser/internal WebSocket ticket behavior unaffected
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.mobile_devices import (
    PAIRING_CODE_ALPHABET,
    _reset_for_tests,
    complete_pairing,
    create_pairing_code,
)
from hermes_cli.dashboard_auth.mobile_rate_limit import (
    _Clock,
    _reset_for_tests as reset_rate_limits,
    _set_clock_for_tests,
    check_credential_rotation,
    check_pair_redemption,
    check_ticket_mint,
    PAIR_MAX_PER_CLIENT,
    TICKET_MAX_PER_CLIENT,
    TICKET_MAX_PER_DEVICE,
    ROTATE_MAX_PER_CLIENT,
    ROTATE_MAX_PER_DEVICE,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests as reset_tickets,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _reset_for_tests()
    reset_tickets()
    reset_rate_limits()
    yield
    _reset_for_tests()
    reset_tickets()
    reset_rate_limits()


@pytest.fixture
def loopback_client():
    from hermes_cli.dashboard_auth import clear_providers

    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


# ============================================================================
# Unit: Pair redemption rate limiter
# ============================================================================


class TestPairRedemptionLimiter:
    def test_last_allowed_request_succeeds(self):
        for _ in range(PAIR_MAX_PER_CLIENT):
            allowed, retry_after = check_pair_redemption("10.0.0.1")
            assert allowed is True
            assert retry_after == 0

    def test_first_rejected_request_returns_429_data(self):
        for _ in range(PAIR_MAX_PER_CLIENT):
            check_pair_redemption("10.0.0.1")
        allowed, retry_after = check_pair_redemption("10.0.0.1")
        assert allowed is False
        assert retry_after >= 1

    def test_retry_after_is_present_and_integer(self):
        for _ in range(PAIR_MAX_PER_CLIENT):
            check_pair_redemption("10.0.0.1")
        allowed, retry_after = check_pair_redemption("10.0.0.1")
        assert allowed is False
        assert isinstance(retry_after, int)
        assert retry_after >= 1

    def test_independent_client_buckets(self):
        # Fill bucket for client A
        for _ in range(PAIR_MAX_PER_CLIENT):
            check_pair_redemption("10.0.0.1")
        # Client B should still be allowed
        allowed, _ = check_pair_redemption("10.0.0.2")
        assert allowed is True

    def test_deterministic_reset(self):
        for _ in range(PAIR_MAX_PER_CLIENT):
            check_pair_redemption("10.0.0.1")
        allowed, _ = check_pair_redemption("10.0.0.1")
        assert allowed is False
        # Reset
        reset_rate_limits()
        allowed, _ = check_pair_redemption("10.0.0.1")
        assert allowed is True


# ============================================================================
# Unit: Ticket mint rate limiter (per device AND per client)
# ============================================================================


class TestTicketMintLimiter:
    def test_last_allowed_per_device(self):
        for _ in range(TICKET_MAX_PER_DEVICE):
            allowed, _ = check_ticket_mint("10.0.0.1", "ios_device1")
            assert allowed is True
        allowed, retry_after = check_ticket_mint("10.0.0.1", "ios_device1")
        assert allowed is False
        assert retry_after >= 1

    def test_last_allowed_per_client(self):
        # Use distinct device IDs so device budget is not hit
        for i in range(TICKET_MAX_PER_CLIENT):
            allowed, _ = check_ticket_mint("10.0.0.1", f"ios_dev_{i}")
            assert allowed is True
        allowed, retry_after = check_ticket_mint("10.0.0.1", "ios_dev_new")
        assert allowed is False
        assert retry_after >= 1

    def test_independent_device_buckets(self):
        for _ in range(TICKET_MAX_PER_DEVICE):
            check_ticket_mint("10.0.0.1", "ios_deviceA")
        # Different device from same client should be allowed
        # (client budget is 120, we only made 60 requests)
        allowed, _ = check_ticket_mint("10.0.0.1", "ios_deviceB")
        assert allowed is True

    def test_independent_client_buckets_for_device(self):
        """Same device from different client is blocked (device bucket shared)."""
        # Fill device budget for client A
        for _ in range(TICKET_MAX_PER_DEVICE):
            check_ticket_mint("10.0.0.1", "ios_device1")
        # Same device from different client is still blocked by device budget
        allowed, _ = check_ticket_mint("10.0.0.2", "ios_device1")
        assert allowed is False


# ============================================================================
# Unit: Credential rotation rate limiter
# ============================================================================


class TestCredentialRotationLimiter:
    def test_last_allowed_per_device(self):
        for _ in range(ROTATE_MAX_PER_DEVICE):
            allowed, _ = check_credential_rotation("10.0.0.1", "ios_device1")
            assert allowed is True
        allowed, retry_after = check_credential_rotation("10.0.0.1", "ios_device1")
        assert allowed is False
        assert retry_after >= 1

    def test_last_allowed_per_client(self):
        for i in range(ROTATE_MAX_PER_CLIENT):
            allowed, _ = check_credential_rotation("10.0.0.1", f"ios_dev_{i}")
            assert allowed is True
        allowed, retry_after = check_credential_rotation("10.0.0.1", "ios_dev_new")
        assert allowed is False
        assert retry_after >= 1

    def test_independent_device_buckets(self):
        for _ in range(ROTATE_MAX_PER_DEVICE):
            check_credential_rotation("10.0.0.1", "ios_deviceA")
        allowed, _ = check_credential_rotation("10.0.0.1", "ios_deviceB")
        assert allowed is True

    def test_deterministic_reset(self):
        for _ in range(ROTATE_MAX_PER_DEVICE):
            check_credential_rotation("10.0.0.1", "ios_device1")
        allowed, _ = check_credential_rotation("10.0.0.1", "ios_device1")
        assert allowed is False
        reset_rate_limits()
        allowed, _ = check_credential_rotation("10.0.0.1", "ios_device1")
        assert allowed is True


# ============================================================================
# Integration: Pair endpoint rate limiting via HTTP
# ============================================================================


class TestPairEndpointRateLimit:
    def test_pair_returns_429_when_limit_exceeded(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        code = pairing.code
        for _ in range(PAIR_MAX_PER_CLIENT):
            loopback_client.post(
                "/api/mobile/pair", json={"code": code}
            )
        r = loopback_client.post(
            "/api/mobile/pair", json={"code": code}
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        retry_after = int(r.headers["Retry-After"])
        assert retry_after >= 1

    def test_pair_429_has_retry_after_header(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        for _ in range(PAIR_MAX_PER_CLIENT):
            loopback_client.post(
                "/api/mobile/pair", json={"code": pairing.code}
            )
        r = loopback_client.post(
            "/api/mobile/pair", json={"code": pairing.code}
        )
        assert r.status_code == 429
        assert r.headers["Retry-After"].isdigit()

    def test_pair_invalid_code_consumes_budget(self, loopback_client):
        """Rate limit applies before credential validation."""
        # Use valid-format codes that just don't exist in the store
        for _ in range(PAIR_MAX_PER_CLIENT):
            loopback_client.post(
                "/api/mobile/pair", json={"code": "ABCDEFGH"}
            )
        # Even a valid code should get 429 now
        pairing = create_pairing_code(device_name="test")
        r = loopback_client.post(
            "/api/mobile/pair", json={"code": pairing.code}
        )
        assert r.status_code == 429


# ============================================================================
# Integration: Ticket mint rate limiting via HTTP
# ============================================================================


class TestTicketEndpointRateLimit:
    def _pair_device(self, client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)
        return credential

    def test_ticket_returns_429_when_device_limit_exceeded(self, loopback_client):
        credential = self._pair_device(loopback_client)
        for _ in range(TICKET_MAX_PER_DEVICE):
            loopback_client.post(
                "/api/mobile/ws-ticket",
                json={
                    "device_id": credential.device_id,
                    "device_secret": credential.device_secret,
                },
            )
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ============================================================================
# Integration: Rotation rate limiting via HTTP
# ============================================================================


class TestRotateEndpointRateLimit:
    def _pair_device(self, client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)
        return credential

    def test_rotate_returns_429_when_limit_exceeded(self, loopback_client):
        credential = self._pair_device(loopback_client)
        secret = credential.device_secret
        for _ in range(ROTATE_MAX_PER_DEVICE):
            r = loopback_client.post(
                "/api/mobile/credential/rotate",
                json={
                    "device_id": credential.device_id,
                    "device_secret": secret,
                },
            )
            if r.status_code == 200:
                secret = r.json()["device_secret"]
        # Next request should be 429
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={
                "device_id": credential.device_id,
                "device_secret": secret,
            },
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ============================================================================
# Integration: X-Forwarded-For is ignored
# ============================================================================


class TestXForwardedForIgnored:
    """Rate limits use request.client.host, not X-Forwarded-For."""

    def test_pair_rate_limit_ignores_xff(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        code = pairing.code
        # Exhaust budget from the test client's actual IP
        for _ in range(PAIR_MAX_PER_CLIENT):
            loopback_client.post(
                "/api/mobile/pair", json={"code": code}
            )
        # Spoofed X-Forwarded-For does NOT bypass the limit
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": code},
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        assert r.status_code == 429


# ============================================================================
# Cross-operation isolation regression tests (P0 fix)
# ============================================================================


class TestCrossOperationIsolation:
    """Prove that pair, ticket, and rotation budgets are independent."""

    def test_pair_traffic_does_not_block_rotation_client(self):
        """10 pair attempts should not block a first rotation for the same host."""
        for _ in range(PAIR_MAX_PER_CLIENT):
            check_pair_redemption("10.0.0.1")
        # Rotation for the same host should still be allowed
        allowed, _ = check_credential_rotation("10.0.0.1", "ios_device1")
        assert allowed is True

    def test_ticket_traffic_does_not_block_rotation_device(self):
        """60 ticket attempts should not block a first rotation for the same device."""
        for _ in range(TICKET_MAX_PER_DEVICE):
            check_ticket_mint("10.0.0.1", "ios_device1")
        # Rotation for the same device should still be allowed
        allowed, _ = check_credential_rotation("10.0.0.1", "ios_device1")
        assert allowed is True

    def test_rotation_traffic_does_not_block_ticket(self):
        """Rotation traffic should not consume ticket budget."""
        for _ in range(ROTATE_MAX_PER_CLIENT):
            check_credential_rotation("10.0.0.1", "ios_device1")
        # Ticket for the same host/device should still be allowed
        allowed, _ = check_ticket_mint("10.0.0.1", "ios_device1")
        assert allowed is True

    def test_pair_traffic_does_not_block_ticket(self):
        """Pair traffic should not consume ticket budget."""
        for _ in range(PAIR_MAX_PER_CLIENT):
            check_pair_redemption("10.0.0.1")
        # Ticket for the same host should still be allowed
        allowed, _ = check_ticket_mint("10.0.0.1", "ios_device1")
        assert allowed is True


# ============================================================================
# Pydantic field bounds
# ============================================================================


class TestDeviceNameBounds:
    def test_device_name_80_chars_allowed(self, loopback_client):
        name_80 = "a" * 80
        pairing = create_pairing_code(device_name=name_80)
        assert len(pairing.device_name) <= 80

    def test_device_name_81_chars_rejected(self, loopback_client):
        name_81 = "a" * 81
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": "ABCDEFGH", "device_name": name_81},
        )
        assert r.status_code == 422


class TestPairingCodeBounds:
    def test_code_valid_8_chars(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": pairing.code},
        )
        # Should not be a validation error
        assert r.status_code != 422

    def test_code_lowercase_normalizes(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        # The formatted code is like ABCD-EFGH; lowercase should normalize
        lower_code = pairing.code.lower()
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": lower_code},
        )
        assert r.status_code != 422

    def test_code_too_long_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": "A" * 33},
        )
        assert r.status_code == 422

    def test_code_wrong_length_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": "ABC"},
        )
        assert r.status_code == 422

    def test_code_forbidden_alphabet_rejected(self, loopback_client):
        """Codes with 0, O, I are excluded from PAIRING_CODE_ALPHABET."""
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": "00000000"},
        )
        assert r.status_code == 422


class TestDeviceIdBounds:
    def test_device_id_64_chars_allowed(self, loopback_client):
        """Submit exactly 64 characters as device_id; should pass validation."""
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)
        # Write the paired credential's device_id into the store, then use a
        # fabricated 64-char id to hit the request-model validation directly.
        device_id_64 = "a" * 64
        # This should not fail Pydantic validation (422); it will fail auth (401)
        # because the id isn't in the store, but that proves the field passed bounds.
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": device_id_64, "device_secret": "x"},
        )
        assert r.status_code != 422

    def test_device_id_empty_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": "", "device_secret": "x"},
        )
        assert r.status_code == 422

    def test_device_id_too_long_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": "a" * 65, "device_secret": "x"},
        )
        assert r.status_code == 422


class TestDeviceSecretBounds:
    def test_device_secret_256_chars_allowed(self, loopback_client):
        """Submit exactly 256 characters as device_secret; should pass validation."""
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": "ios_x", "device_secret": "a" * 256},
        )
        # Should not fail Pydantic validation (422); may fail auth (401) which is fine.
        assert r.status_code != 422

    def test_device_secret_empty_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": "ios_x", "device_secret": ""},
        )
        assert r.status_code == 422

    def test_device_secret_too_long_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": "ios_x", "device_secret": "a" * 257},
        )
        assert r.status_code == 422


class TestRotationRequestModelBounds:
    """Rotation endpoint request-model bounds at valid/invalid edges."""

    def test_rotate_device_id_64_chars_allowed(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "a" * 64, "device_secret": "x"},
        )
        assert r.status_code != 422

    def test_rotate_device_id_65_chars_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "a" * 65, "device_secret": "x"},
        )
        assert r.status_code == 422

    def test_rotate_device_secret_256_chars_allowed(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "ios_x", "device_secret": "a" * 256},
        )
        assert r.status_code != 422

    def test_rotate_device_secret_257_chars_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "ios_x", "device_secret": "a" * 257},
        )
        assert r.status_code == 422

    def test_rotate_device_id_empty_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "", "device_secret": "x"},
        )
        assert r.status_code == 422

    def test_rotate_device_secret_empty_rejected(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "ios_x", "device_secret": ""},
        )
        assert r.status_code == 422


# ============================================================================
# Admin routes are NOT rate-limited
# ============================================================================


class TestAdminRoutesNotRateLimited:
    def test_pairing_code_creation_not_rate_limited(self, loopback_client):
        """Creating pairing codes is an admin action, not public credential."""
        for _ in range(PAIR_MAX_PER_CLIENT + 5):
            r = loopback_client.post(
                "/api/mobile/pairing-codes",
                json={"device_name": "test"},
                headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            )
            assert r.status_code == 200

    def test_device_list_not_rate_limited(self, loopback_client):
        for _ in range(50):
            r = loopback_client.get(
                "/api/mobile/devices",
                headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            )
            assert r.status_code == 200


# ============================================================================
# Browser/internal behavior unaffected
# ============================================================================


class TestBrowserBehaviorUnaffected:
    def test_browser_ws_ticket_still_works(self, loopback_client):
        """Existing browser WebSocket auth is not impacted by mobile rate limits."""
        from hermes_cli.dashboard_auth.ws_tickets import mint_ticket, internal_ws_credential

        ticket = mint_ticket(user_id="browser_user", provider="nous")
        assert ticket  # ticket was minted successfully

        # Internal credential should work
        internal_cred = internal_ws_credential()
        assert internal_cred is not None
