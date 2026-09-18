from __future__ import annotations

import base64
import json
import shutil
import threading
import types
from pathlib import Path

import pytest

from tui_gateway import server


class _OwnedElsewhere:
    reason = "session_owned_elsewhere"

    def __str__(self) -> str:
        return "session is already active in another live client"


def _session(workspace: Path, profile_home: Path) -> dict:
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "durable-contract-test",
        "cwd": str(workspace),
        "profile_home": str(profile_home),
        "attached_images": [],
        "image_counter": 0,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "inflight_turn": None,
        "running": False,
        "last_active": 0.0,
        "cols": 80,
    }


def _request(method: str, params: dict) -> dict:
    response = server.handle_request(
        {"jsonrpc": "2.0", "id": "m3-contract", "method": method, "params": params}
    )
    assert response is not None
    return response


def _one_page_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 32 32] /Contents 4 0 R >>",
        b"<< /Length 4 >>\nstream\nq\nQ\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode())
        payload.extend(obj)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(payload)


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        (
            "image.attach_bytes",
            {"content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nprobe").decode(), "filename": "probe.png"},
        ),
        (
            "pdf.attach",
            {"content_base64": base64.b64encode(b"%PDF-probe").decode(), "filename": "probe.pdf"},
        ),
        (
            "file.attach",
            {
                "data_url": "data:application/octet-stream;base64," + base64.b64encode(b"probe").decode(),
                "name": "probe.bin",
            },
        ),
    ],
)
def test_attachment_staging_refuses_foreign_owner_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method: str,
    payload: dict,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    session = _session(workspace, profile_home)
    server._sessions["owned"] = session

    def forbidden(*_args, **_kwargs):
        raise AssertionError("attachment mutation ran before ownership admission")

    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args, **_kwargs: _OwnedElsewhere())
    monkeypatch.setattr(server, "_start_agent_build", forbidden)
    monkeypatch.setattr(server, "_stage_session_file_attachment", forbidden)
    monkeypatch.setattr(server, "_queue_attached_image", forbidden)
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/homebrew/bin/pdftoppm" if name == "pdftoppm" else None)

    try:
        response = _request(method, {"session_id": "owned", **payload})
    finally:
        server._sessions.pop("owned", None)

    assert response["error"]["code"] == 4090
    assert response["error"]["data"] == {"reason": "session_owned_elsewhere"}
    assert session["attached_images"] == []
    assert not (profile_home / "images").exists()
    assert not (profile_home / "attachments").exists()


def test_file_attach_rejects_oversized_data_url_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_FILE_ATTACH_MAX_BYTES", 10)

    def forbidden_decode(*_args, **_kwargs):
        raise AssertionError("oversized base64 reached decoder")

    monkeypatch.setattr(base64, "b64decode", forbidden_decode)
    with pytest.raises(server._AttachmentTooLarge):
        server._decode_attachment_data_url("data:application/octet-stream;base64," + ("A" * 20))


def test_file_attach_accepts_exact_cap_and_rejects_cap_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_FILE_ATTACH_MAX_BYTES", 10)
    exact = base64.b64encode(b"x" * 10).decode()
    over = base64.b64encode(b"x" * 11).decode()

    assert server._decode_attachment_data_url(exact) == b"x" * 10
    with pytest.raises(server._AttachmentTooLarge):
        server._decode_attachment_data_url(over)


def test_remote_file_attach_cap_returns_4018_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    server._sessions["remote-over"] = _session(workspace, profile_home)
    monkeypatch.setattr(server, "_FILE_ATTACH_MAX_BYTES", 10)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_resolve_gateway_attachment_path", lambda _raw: None)

    payload = base64.b64encode(b"x" * 11).decode()
    try:
        response = _request(
            "file.attach",
            {
                "session_id": "remote-over",
                "data_url": f"data:application/octet-stream;base64,{payload}",
                "name": "too-large.bin",
            },
        )
    finally:
        server._sessions.pop("remote-over", None)

    assert response["error"]["code"] == 4018
    assert not (profile_home / "attachments").exists()


@pytest.mark.parametrize("inside_workspace", [True, False])
def test_gateway_visible_file_cap_is_checked_before_reference_or_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inside_workspace: bool,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    source_root = workspace if inside_workspace else tmp_path / "outside"
    source_root.mkdir(exist_ok=True)
    source = source_root / "too-large.bin"
    source.write_bytes(b"x" * 11)

    server._sessions["visible-over"] = _session(workspace, profile_home)
    monkeypatch.setattr(server, "_FILE_ATTACH_MAX_BYTES", 10)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_resolve_gateway_attachment_path", lambda _raw: source)

    try:
        response = _request(
            "file.attach",
            {"session_id": "visible-over", "path": str(source), "name": source.name},
        )
    finally:
        server._sessions.pop("visible-over", None)

    assert response["error"]["code"] == 4018
    assert not (profile_home / "attachments").exists()


@pytest.mark.parametrize(
    ("method", "filename", "raw_bytes"),
    [
        (
            "image.attach_bytes",
            "fixture.png",
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
            ),
        ),
        pytest.param(
            "pdf.attach",
            "fixture.pdf",
            _one_page_pdf(),
            marks=pytest.mark.skipif(
                shutil.which("pdftoppm") is None,
                reason="Poppler is required for the PDF fixture",
            ),
        ),
    ],
)
def test_v1_image_and_pdf_staging_return_only_path_free_descriptors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method: str,
    filename: str,
    raw_bytes: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    session = _session(workspace, profile_home)
    server._sessions["v1-media"] = session
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)

    try:
        response = _request(
            method,
            {
                "session_id": "v1-media",
                "attachment_contract_version": 1,
                "content_base64": base64.b64encode(raw_bytes).decode(),
                "filename": filename,
            },
        )
    finally:
        server._sessions.pop("v1-media", None)

    result = response["result"]
    descriptor = result["attachment"]
    assert descriptor["schema"] == "hermes.attachment"
    assert descriptor["version"] == 1
    assert descriptor["direction"] == "inbound"
    assert descriptor["name"] == filename
    assert descriptor["size_bytes"] == len(raw_bytes)
    serialized = json.dumps(result, sort_keys=True)
    assert "path" not in result
    assert "pages" not in result
    assert "ref_text" not in serialized
    assert str(profile_home) not in serialized
    staged = server._peek_staged_mobile_attachment(session, descriptor["id"])
    assert staged is not None
    assert staged["descriptor"] == descriptor
    assert staged["image_paths"]
    assert session["attached_images"] == []
    consumed = server._consume_staged_mobile_attachment(session, descriptor["id"])
    assert consumed is not None
    server._restore_staged_mobile_attachment(session, consumed)
    assert server._peek_staged_mobile_attachment(session, descriptor["id"]) is not None
    assert session["attached_images"] == []


def test_v1_prompt_requires_integer_version_and_exactly_one_attachment_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    server._sessions["v1-shape"] = _session(workspace, profile_home)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args, **_kwargs: None)

    try:
        boolean_version = _request(
            "prompt.submit",
            {
                "session_id": "v1-shape",
                "attachment_contract_version": True,
                "attachment_ids": ["a"],
                "text": "fixture",
            },
        )
        multiple_ids = _request(
            "prompt.submit",
            {
                "session_id": "v1-shape",
                "attachment_contract_version": 1,
                "attachment_ids": ["a", "b"],
                "text": "fixture",
            },
        )
    finally:
        server._sessions.pop("v1-shape", None)

    assert boolean_version["error"]["code"] == 4015
    assert multiple_ids["error"]["code"] == 4015
    assert multiple_ids["error"]["message"] == (
        "attachment_ids must contain exactly one opaque id"
    )


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("pdf.attach", {"path": "/tmp/fixture.pdf"}),
        ("file.attach", {"path": "/tmp/fixture.txt"}),
    ],
)
def test_v1_attachment_contract_rejects_client_paths_before_session_mutation(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    payload: dict,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("raw-path v1 request reached session mutation")

    monkeypatch.setattr(server, "_sess_attachment_mutation", forbidden)
    if method == "pdf.attach":
        monkeypatch.setattr(shutil, "which", lambda _name: "/opt/homebrew/bin/pdftoppm")

    response = _request(
        method,
        {
            "session_id": "not-consulted",
            "attachment_contract_version": 1,
            **payload,
        },
    )

    assert response["error"]["code"] == 4016
    assert response["error"]["message"] == "path is not allowed for attachment contract v1"


def test_v1_staging_errors_do_not_expose_gateway_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    session = _session(workspace, profile_home)
    server._sessions["sanitized-errors"] = session
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)

    sentinel = "/private/gateway/secret-path"

    def fail_image(*_args, **_kwargs):
        raise OSError(sentinel)

    monkeypatch.setattr(server, "_queue_attached_image", fail_image)
    try:
        image_response = _request(
            "image.attach_bytes",
            {
                "session_id": "sanitized-errors",
                "attachment_contract_version": 1,
                "filename": "fixture.png",
                "content_base64": base64.b64encode(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                    )
                ).decode("ascii"),
            },
        )
        assert image_response["error"]["message"] == "image staging failed"
        assert sentinel not in json.dumps(image_response)

        monkeypatch.setattr(shutil, "which", lambda _name: "/opt/homebrew/bin/pdftoppm")
        monkeypatch.setattr(
            server.subprocess,
            "run",
            lambda *_args, **_kwargs: types.SimpleNamespace(
                returncode=1,
                stderr=sentinel,
                stdout="",
            ),
        )
        pdf_response = _request(
            "pdf.attach",
            {
                "session_id": "sanitized-errors",
                "attachment_contract_version": 1,
                "filename": "fixture.pdf",
                "content_base64": base64.b64encode(_one_page_pdf()).decode("ascii"),
            },
        )
        assert pdf_response["error"]["message"] == "PDF conversion failed"
        assert sentinel not in json.dumps(pdf_response)

        def fail_file(*_args, **_kwargs):
            raise OSError(sentinel)

        monkeypatch.setattr(server, "_stage_session_file_attachment", fail_file)
        file_response = _request(
            "file.attach",
            {
                "session_id": "sanitized-errors",
                "attachment_contract_version": 1,
                "name": "fixture.txt",
                "data_url": "data:text/plain;base64,WA==",
            },
        )
        assert file_response["error"]["message"] == "file staging failed"
        assert sentinel not in json.dumps(file_response)
    finally:
        server._sessions.pop("sanitized-errors", None)


def test_v1_file_attachment_is_server_associated_and_path_free_on_all_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    profile_home = tmp_path / "profile"
    workspace.mkdir()
    profile_home.mkdir()
    session = _session(workspace, profile_home)
    server._sessions["v1-file"] = session

    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_wait_agent_for_prompt",
        lambda *_args, **_kwargs: None,
    )

    captured: dict = {}

    def fake_run_prompt_submit(_rid, _sid, _session, model_text, **kwargs):
        captured["model_text"] = model_text
        captured.update(kwargs)
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", fake_run_prompt_submit)

    class _ImmediateThread:
        def __init__(self, target=None, **_kwargs):
            self._target = target

        def start(self):
            assert self._target is not None
            self._target()

    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)

    fixture_bytes = b"fixture-only general file"
    data_url = "data:text/plain;base64," + base64.b64encode(fixture_bytes).decode()
    try:
        staged = _request(
            "file.attach",
            {
                "session_id": "v1-file",
                "attachment_contract_version": 1,
                "data_url": data_url,
                "name": "fixture.txt",
            },
        )
        descriptor = staged["result"]["attachment"]
        attachment_id = descriptor["id"]

        assert set(staged["result"]) == {"attached", "uploaded", "attachment"}
        assert descriptor["schema"] == "hermes.attachment"
        assert descriptor["version"] == 1
        assert descriptor["direction"] == "inbound"
        assert descriptor["name"] == "fixture.txt"
        assert descriptor["size_bytes"] == len(fixture_bytes)
        staged_wire = json.dumps(staged["result"], sort_keys=True)
        assert "@file:" not in staged_wire
        assert str(profile_home) not in staged_wire
        assert str(workspace) not in staged_wire

        safe_text = "Summarize the attached fixture."
        submitted = _request(
            "prompt.submit",
            {
                "session_id": "v1-file",
                "attachment_contract_version": 1,
                "attachment_ids": [attachment_id],
                "text": safe_text,
            },
        )
    finally:
        server._sessions.pop("v1-file", None)

    assert submitted["result"]["status"] == "streaming"
    assert submitted["result"]["attachments"] == [descriptor]
    assert captured["model_text"].startswith(safe_text)
    assert "@file:" in captured["model_text"]
    assert captured["display_metadata"] == {
        "display_text": safe_text,
        "attachments": [descriptor],
    }
    assert captured["image_paths"] == []

    persisted = {
        "role": "user",
        "content": captured["model_text"],
        "display_metadata": captured["display_metadata"],
    }
    history_message, = server._history_to_messages([persisted])
    assert history_message["text"] == safe_text
    assert history_message["attachments"] == [descriptor]
    assert "@file:" not in json.dumps(history_message, sort_keys=True)
    assert str(profile_home) not in json.dumps(history_message, sort_keys=True)

    inflight = server._inflight_snapshot(session)
    assert inflight is not None
    assert inflight["user"] == safe_text
    assert inflight["attachments"] == [descriptor]
    assert server._peek_staged_mobile_attachment(session, attachment_id) is None

    (attachment_only,) = server._history_to_messages(
        [
            {
                "role": "user",
                "content": "",
                "display_metadata": {
                    "display_text": "",
                    "attachments": [descriptor],
                },
            }
        ]
    )
    assert attachment_only["text"] == ""
    assert attachment_only["attachments"] == [descriptor]

    malicious_metadata = {
        "display_text": "safe",
        "attachments": [{"path": "/gateway/private/fixture.txt"}],
    }
    (malformed_attachment_message,) = server._history_to_messages(
        [{"role": "user", "content": "safe", "display_metadata": malicious_metadata}]
    )
    assert "attachments" not in malformed_attachment_message
    assert malformed_attachment_message["display_metadata"] == {
        "display_text": "safe",
        "attachments": [],
    }
    assert "/gateway/private" not in json.dumps(malformed_attachment_message)
