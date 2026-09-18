from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.mobile_devices import (
    _reset_for_tests as reset_devices,
    complete_pairing,
    create_pairing_code,
)
from hermes_cli.dashboard_auth.mobile_rate_limit import (
    _reset_for_tests as reset_rate_limits,
)
from hermes_cli.mobile_artifacts import (
    AttachmentDescriptorInvalid,
    MobileArtifactCorrupt,
    MobileArtifactExpired,
    MobileArtifactNotFound,
    MobileArtifactScopeMismatch,
    MobileArtifactStore,
    MobileArtifactTooLarge,
    MOBILE_ARTIFACT_MAX_BYTES,
    _reset_for_tests as reset_artifact_stores,
    load_mobile_artifact,
    publish_mobile_artifact,
    publish_mobile_artifact_path,
    validate_attachment_descriptor,
)
from tui_gateway import server as gateway_server


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_devices()
    reset_rate_limits()
    reset_artifact_stores()
    yield tmp_path
    reset_devices()
    reset_rate_limits()
    reset_artifact_stores()


@pytest.fixture
def loopback_client():
    clear_providers()
    previous = (
        getattr(web_server.app.state, "bound_host", None),
        getattr(web_server.app.state, "bound_port", None),
        getattr(web_server.app.state, "auth_required", None),
    )
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield client
    (
        web_server.app.state.bound_host,
        web_server.app.state.bound_port,
        web_server.app.state.auth_required,
    ) = previous


def _fixture() -> dict:
    path = Path(__file__).parents[1] / "fixtures" / "mobile_attachment_descriptor_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _paired_credential():
    pairing = create_pairing_code(device_name="M3 Artifact Test")
    return complete_pairing(code=pairing.code, device_name="M3 Artifact Test")


def test_v1_fixture_is_canonical_and_identical_on_all_surfaces() -> None:
    fixture = _fixture()
    inbound = validate_attachment_descriptor(fixture["inbound"])
    outbound = validate_attachment_descriptor(fixture["outbound"])

    live_event = {
        "event": "message.complete",
        "attachments": gateway_server._canonical_attachment_descriptors([outbound]),
    }
    stored = [
        {
            "role": "assistant",
            "content": "safe",
            "display_metadata": {"attachments": [outbound]},
        }
    ]
    # session.resume and session.history both use this canonical transcript
    # projection; call it independently to guard against accidental mutation.
    resumed_message = gateway_server._history_to_messages(stored)[0]
    history_message = gateway_server._history_to_messages(stored)[0]

    assert live_event["attachments"] == resumed_message["attachments"]
    assert resumed_message["attachments"] == history_message["attachments"]
    assert inbound["direction"] == "inbound"
    assert "download" not in inbound
    assert outbound["direction"] == "outbound"
    assert outbound["download"]["path"] == "/api/mobile/artifacts/download"
    serialized = json.dumps(fixture, sort_keys=True)
    assert "ref_text" not in serialized
    assert "ref_path" not in serialized
    assert "raw_path" not in serialized


def test_descriptor_rejects_extra_path_or_reference_fields() -> None:
    descriptor = dict(_fixture()["inbound"])
    descriptor["raw_path"] = "/private/secret"
    with pytest.raises(AttachmentDescriptorInvalid):
        validate_attachment_descriptor(descriptor)

    descriptor = dict(_fixture()["inbound"])
    descriptor["ref_text"] = "@file:private"
    with pytest.raises(AttachmentDescriptorInvalid):
        validate_attachment_descriptor(descriptor)

    descriptor = dict(_fixture()["inbound"])
    descriptor["mime_type"] = "text/plain\r\nx-injected: yes"
    with pytest.raises(AttachmentDescriptorInvalid):
        validate_attachment_descriptor(descriptor)


def test_store_survives_reopen_and_enforces_exact_scope(tmp_path: Path) -> None:
    root = tmp_path / "artifact-store"
    store = MobileArtifactStore(root, max_bytes=16, ttl_seconds=60, clock=lambda: 100.0)
    descriptor = store.publish(
        data=b"bounded-result",
        profile="default",
        session_id="session-1",
        filename="result.txt",
        mime_type="text/plain",
    )

    reopened = MobileArtifactStore(root, max_bytes=16, ttl_seconds=60, clock=lambda: 101.0)
    loaded = reopened.load(
        artifact_id=descriptor["id"], profile="default", session_id="session-1"
    )
    assert loaded.data == b"bounded-result"
    assert loaded.descriptor == descriptor

    with pytest.raises(MobileArtifactScopeMismatch):
        reopened.load(
            artifact_id=descriptor["id"], profile="default", session_id="session-2"
        )
    with pytest.raises(MobileArtifactNotFound):
        reopened.load(
            artifact_id="../etc/passwd", profile="default", session_id="session-1"
        )


def test_store_rejects_oversize_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "artifact-store"
    store = MobileArtifactStore(root, max_bytes=4)
    with pytest.raises(MobileArtifactTooLarge):
        store.publish(
            data=b"12345",
            profile="default",
            session_id="session-1",
            filename="too-large.bin",
            mime_type="application/octet-stream",
        )
    assert not root.exists()


def test_store_rejects_payload_integrity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "artifact-store"
    store = MobileArtifactStore(root, max_bytes=32, ttl_seconds=60, clock=lambda: 100.0)
    descriptor = store.publish(
        data=b"expected-result",
        profile="default",
        session_id="session-1",
        filename="result.txt",
        mime_type="text/plain",
    )
    (root / f"{descriptor['id']}.blob").write_bytes(b"tampered-result")

    with pytest.raises(MobileArtifactCorrupt):
        store.load(
            artifact_id=descriptor["id"],
            profile="default",
            session_id="session-1",
        )


def test_store_expires_and_removes_artifact(tmp_path: Path) -> None:
    now = [100.0]
    root = tmp_path / "artifact-store"
    store = MobileArtifactStore(root, max_bytes=16, ttl_seconds=5, clock=lambda: now[0])
    descriptor = store.publish(
        data=b"result",
        profile="default",
        session_id="session-1",
        filename="result.bin",
        mime_type="application/octet-stream",
    )
    now[0] = 106.0
    with pytest.raises(MobileArtifactExpired):
        store.load(
            artifact_id=descriptor["id"], profile="default", session_id="session-1"
        )
    assert list(root.iterdir()) == []


def test_authenticated_download_route_is_opaque_scoped_and_repeatable(
    loopback_client: TestClient,
) -> None:
    credential = _paired_credential()
    descriptor = publish_mobile_artifact(
        data=b"downloadable-result",
        profile="default",
        session_id="session-1",
        filename="result.txt",
        mime_type="text/plain",
    )
    request_body = {
        "device_id": credential.device_id,
        "device_secret": credential.device_secret,
        "artifact_id": descriptor["id"],
        "profile": "default",
        "session_id": "session-1",
    }

    first = loopback_client.post("/api/mobile/artifacts/download", json=request_body)
    second = loopback_client.post("/api/mobile/artifacts/download", json=request_body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == b"downloadable-result"
    assert second.content == b"downloadable-result"
    assert first.headers["content-type"].startswith("text/plain")
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["x-hermes-attachment-schema"] == "hermes.attachment/1"
    assert "result.txt" in first.headers["content-disposition"]

    wrong_scope = loopback_client.post(
        "/api/mobile/artifacts/download",
        json={**request_body, "session_id": "session-2"},
    )
    assert wrong_scope.status_code == 404

    arbitrary_path = loopback_client.post(
        "/api/mobile/artifacts/download",
        json={**request_body, "path": "/etc/passwd"},
    )
    assert arbitrary_path.status_code == 422

    bad_credential = loopback_client.post(
        "/api/mobile/artifacts/download",
        json={**request_body, "device_secret": "wrong"},
    )
    assert bad_credential.status_code == 401


def test_publish_path_reads_regular_file_once_and_returns_path_free_descriptor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated" / "report.txt"
    source.parent.mkdir()
    source.write_bytes(b"bounded-result")

    descriptor = publish_mobile_artifact_path(
        path=source,
        profile="default",
        session_id="session-1",
    )
    payload = load_mobile_artifact(
        artifact_id=descriptor["id"],
        profile="default",
        session_id="session-1",
    )

    assert payload.data == b"bounded-result"
    assert descriptor["name"] == "report.txt"
    assert descriptor["mime_type"] == "text/plain"
    assert str(source) not in json.dumps(descriptor)


def test_publish_path_rejects_sparse_file_above_cap_before_reading(
    tmp_path: Path,
) -> None:
    source = tmp_path / "oversized.bin"
    with source.open("wb") as handle:
        handle.seek(MOBILE_ARTIFACT_MAX_BYTES)
        handle.write(b"x")

    with pytest.raises(MobileArtifactTooLarge):
        publish_mobile_artifact_path(
            path=source,
            profile="default",
            session_id="session-1",
        )

    artifact_root = tmp_path / "artifacts" / "mobile-v1"
    assert not artifact_root.exists() or list(artifact_root.iterdir()) == []


def test_publish_path_rejects_symlink_source(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("not accepted through a symlink", encoding="utf-8")
    source = tmp_path / "alias.txt"
    source.symlink_to(target)

    with pytest.raises(MobileArtifactCorrupt):
        publish_mobile_artifact_path(
            path=source,
            profile="default",
            session_id="session-1",
        )
