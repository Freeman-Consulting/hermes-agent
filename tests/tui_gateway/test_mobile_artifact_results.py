from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_cli.mobile_artifacts import (
    MOBILE_ARTIFACT_MAX_BYTES,
    _reset_for_tests as reset_artifact_stores,
    load_mobile_artifact,
)
from hermes_cli.web_routers import sessions as session_routes
from tui_gateway import server
from tui_gateway.mobile_artifact_results import (
    MobileArtifactStreamFilter,
    profile_name_from_home,
    project_mobile_artifact_result,
    project_mobile_history_message,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_MEDIA_DELIVERY_STRICT", raising=False)
    reset_artifact_stores()
    yield tmp_path
    reset_artifact_stores()


def test_mobile_history_projection_replaces_raw_content_with_contract_metadata() -> None:
    descriptor = {
        "schema": "hermes.attachment",
        "version": 1,
        "id": "0123456789abcdef0123456789abcdef",
        "direction": "outbound",
        "kind": "file",
        "name": "result.csv",
        "mime_type": "text/csv",
        "size_bytes": 4,
        "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
        "download": {
            "transport": "paired_device_http",
            "method": "POST",
            "path": "/api/mobile/artifacts/download",
        },
    }
    raw_path = "/private/gateway/result.csv"

    projected = project_mobile_history_message(
        {
            "role": "assistant",
            "content": f"Done.\nMEDIA:{raw_path}",
            "display_metadata": {
                "display_text": "Done.",
                "attachments": [descriptor],
            },
        }
    )

    assert projected["content"] == "Done."
    assert projected["text"] == "Done."
    assert projected["attachments"] == [descriptor]
    assert raw_path not in json.dumps(projected, sort_keys=True)


def test_mobile_history_projection_fails_closed_without_metadata() -> None:
    raw_path = "/private/gateway/unpersisted.csv"
    projected = project_mobile_history_message(
        {"role": "assistant", "content": f"Done.\nMEDIA:{raw_path}"}
    )

    assert projected["content"] == "Done."
    assert projected["text"] == "Done."
    assert raw_path not in json.dumps(projected, sort_keys=True)


def test_mobile_history_projection_fails_closed_for_user_reference_without_metadata() -> None:
    raw_path = "/private/gateway/upload.txt"
    projected = project_mobile_history_message(
        {"role": "user", "content": f"Review this.\n@file:{raw_path}"}
    )

    assert projected["content"] == ""
    assert projected["text"] == ""
    assert raw_path not in json.dumps(projected, sort_keys=True)


def test_mobile_history_projection_fails_closed_for_user_file_url_without_metadata() -> None:
    raw_path = "/private/gateway/upload.txt"
    projected = project_mobile_history_message(
        {"role": "user", "content": f"Review this.\nfile://{raw_path}"}
    )

    assert projected["content"] == ""
    assert projected["text"] == ""
    assert raw_path not in json.dumps(projected, sort_keys=True)


def test_mobile_history_projection_scrubs_stale_metadata_and_compaction_text() -> None:
    raw_path = "/private/gateway/stale.csv"
    projected = project_mobile_history_message(
        {
            "role": "assistant",
            "content": "Safe.",
            "display_content": f"Summary.\nMEDIA:{raw_path}",
            "display_metadata": {
                "display_text": f"Visible.\nMEDIA:{raw_path}",
                "display_kind": "assistant",
            },
        }
    )

    assert projected["display_content"] == "Summary."
    assert projected["display_metadata"]["display_text"] == "Visible."
    assert raw_path not in json.dumps(projected, sort_keys=True)


@pytest.mark.asyncio
async def test_rest_history_source_applies_fail_closed_mobile_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = "/private/gateway/unpersisted.csv"

    class _DB:
        def close(self) -> None:
            pass

        def resolve_resume_session_id(self, session_id: str) -> str:
            return session_id

        def get_messages(self, *_args, **_kwargs):
            return [
                {
                    "role": "assistant",
                    "content": f"Done.\nMEDIA:{raw_path}",
                }
            ]

    monkeypatch.setattr(
        session_routes,
        "_open_session_db_for_profile",
        lambda *_args, **_kwargs: _DB(),
    )
    monkeypatch.setattr(
        session_routes,
        "_resolve_session_id",
        lambda _db, session_id: session_id,
    )

    result = await session_routes.get_session_messages(
        "durable-session",
        profile="default",
        limit=10,
        offset=0,
        order="oldest",
        include_compacted=False,
        source="ios-pocket",
    )

    assert result["messages"][0]["content"] == "Done."
    assert result["messages"][0]["text"] == "Done."
    assert raw_path not in json.dumps(result, sort_keys=True)

    non_mobile_result = await session_routes.get_session_messages(
        "durable-session",
        profile="default",
        limit=10,
        offset=0,
        order="oldest",
        include_compacted=False,
        source=None,
    )
    assert non_mobile_result["messages"][0]["content"].endswith(raw_path)


def test_mobile_result_publishes_media_and_removes_gateway_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated" / "summary.txt"
    source.parent.mkdir()
    source.write_bytes(b"authoritative artifact")
    raw = f"Ready for review.\nMEDIA:{source}"

    projection = project_mobile_artifact_result(
        response_text=raw,
        source="ios-pocket",
        profile="default",
        session_id="session-1",
        session_key="session-1",
    )

    assert projection is not None
    assert projection.display_text == "Ready for review."
    assert projection.failed_count == 0
    assert len(projection.attachments) == 1
    descriptor = projection.attachments[0]
    assert descriptor["name"] == "summary.txt"
    assert descriptor["direction"] == "outbound"
    assert descriptor["download"]["path"] == "/api/mobile/artifacts/download"
    assert str(source) not in json.dumps(descriptor)
    payload = load_mobile_artifact(
        artifact_id=descriptor["id"],
        profile="default",
        session_id="session-1",
    )
    assert payload.data == b"authoritative artifact"


def test_non_mobile_result_is_untouched(tmp_path: Path) -> None:
    source = tmp_path / "desktop.txt"
    source.write_text("desktop", encoding="utf-8")
    raw = f"MEDIA:{source}"

    projection = project_mobile_artifact_result(
        response_text=raw,
        source="desktop",
        profile="default",
        session_id="session-1",
        session_key="session-1",
    )

    assert projection is None
    assert not (tmp_path / "artifacts" / "mobile-v1").exists()


def test_unavailable_media_is_redacted_without_logging_or_returning_path(
    caplog,
) -> None:
    raw = "Result follows.\nMEDIA:/does/not/exist/private-result.txt"

    projection = project_mobile_artifact_result(
        response_text=raw,
        source="ios-pocket",
        profile="default",
        session_id="session-1",
        session_key="session-1",
    )

    assert projection is not None
    assert projection.attachments == []
    assert projection.failed_count == 1
    assert "Attachment unavailable" in projection.display_text
    assert "MEDIA:" not in projection.display_text
    assert "/does/not/exist" not in projection.display_text
    assert "/does/not/exist" not in caplog.text


def test_unavailable_extensionless_media_is_redacted_without_docker_path_log(
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    raw_path = "/does/not/exist/private-extensionless-result"

    projection = project_mobile_artifact_result(
        response_text=f"Result follows.\nMEDIA:{raw_path}",
        source="ios-pocket",
        profile="default",
        session_id="session-1",
        session_key="session-1",
    )

    assert projection is not None
    assert projection.attachments == []
    assert projection.failed_count == 1
    assert raw_path not in projection.display_text
    assert raw_path not in caplog.text


def test_relative_media_directive_is_redacted_even_when_not_publishable() -> None:
    projection = project_mobile_artifact_result(
        response_text="Ready.\nMEDIA:relative/private-result.bin",
        source="ios-pocket",
        profile="default",
        session_id="session-1",
    )

    assert projection is not None
    assert projection.attachments == []
    assert projection.failed_count == 1
    assert "MEDIA:" not in projection.display_text
    assert "relative/private-result.bin" not in projection.display_text


def test_duplicate_media_directives_publish_once(tmp_path: Path) -> None:
    source = tmp_path / "same.pdf"
    source.write_bytes(b"%PDF-1.4 bounded")
    raw = f"MEDIA:{source}\nMEDIA:{source}"

    projection = project_mobile_artifact_result(
        response_text=raw,
        source="ios-pocket",
        profile="default",
        session_id="session-1",
        session_key="session-1",
    )

    assert projection is not None
    assert len(projection.attachments) == 1
    assert projection.failed_count == 0


def test_oversized_media_fails_before_read_without_logging_gateway_path(
    tmp_path: Path,
    caplog,
) -> None:
    source = tmp_path / "oversized-secret.bin"
    with source.open("wb") as handle:
        handle.truncate(MOBILE_ARTIFACT_MAX_BYTES + 1)
    projection = project_mobile_artifact_result(
        response_text=f"MEDIA:{source}",
        source="ios-pocket",
        profile="default",
        session_id="session-1",
    )

    assert projection is not None
    assert projection.attachments == []
    assert projection.failed_count == 1
    assert str(source) not in caplog.text


def test_stream_filter_holds_split_media_line_and_never_emits_path() -> None:
    stream = MobileArtifactStreamFilter()

    visible = "".join(
        [
            stream.feed("First line.\nME"),
            stream.feed("DIA:/private/gateway/result.png\nFinal"),
            stream.finish(),
        ]
    )

    assert visible == "First line.\nFinal"
    assert "MEDIA:" not in visible
    assert "/private/gateway" not in visible


def test_stream_filter_removes_delivery_directives() -> None:
    stream = MobileArtifactStreamFilter()
    visible = stream.feed(
        "[[audio_as_voice]]\n[[as_document]]\nMEDIA:/private/a.ogg\nText.\n"
    ) + stream.finish()

    assert visible == "Text.\n"


def test_stream_filter_does_not_line_buffer_ordinary_text() -> None:
    stream = MobileArtifactStreamFilter()

    assert stream.feed("Ordinary streaming text.") == "Ordinary streaming text."
    assert stream.finish() == ""


def test_profile_name_derives_only_immediate_named_profile(tmp_path: Path) -> None:
    assert profile_name_from_home(tmp_path) == "default"
    assert profile_name_from_home(tmp_path / "profiles" / "work") == "work"
    assert profile_name_from_home(tmp_path / "nested" / "work") == "default"


def test_latest_assistant_display_metadata_merge_preserves_existing_fields(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("session-1", source="ios-pocket")
    db.append_message(
        "session-1",
        "assistant",
        content="model-facing path",
        display_metadata={"reactions": [{"emoji": "thumb", "author": "user"}]},
    )

    assert db.merge_latest_message_display_metadata(
        "session-1",
        role="assistant",
        display_metadata={"attachments": [], "display_text": "safe"},
    )

    row = db.get_messages("session-1")[-1]
    assert row["content"] == "model-facing path"
    assert row["display_metadata"]["display_text"] == "safe"
    assert row["display_metadata"]["attachments"] == []
    assert row["display_metadata"]["reactions"] == [
        {"emoji": "thumb", "author": "user"}
    ]


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def merge_latest_message_display_metadata(
        self,
        session_id: str,
        *,
        role: str,
        display_metadata: dict,
    ) -> bool:
        self.calls.append((session_id, role, display_metadata))
        return True


def test_server_projection_makes_live_and_history_descriptors_identical(
    tmp_path: Path,
) -> None:
    source = tmp_path / "generated.png"
    source.write_bytes(b"not-real-png-but-bounded")
    raw = f"Generated.\nMEDIA:{source}"
    result = {
        "final_response": raw,
        "messages": [
            {"role": "user", "content": "make it"},
            {"role": "assistant", "content": raw},
        ],
    }
    db = _RecordingDB()
    agent = SimpleNamespace(session_id="session-1", _session_db=db)
    session = {
        "source": "ios-pocket",
        "session_key": "session-1",
        "profile_home": str(tmp_path),
    }

    attachments = server._apply_mobile_artifact_projection(result, session, agent)

    assert attachments == result["attachments"]
    assert "MEDIA:" not in result["final_response"]
    assert str(source) not in result["final_response"]
    history = server._history_to_messages(result["messages"])
    assert history[-1]["text"] == "Generated."
    assert history[-1]["attachments"] == attachments
    assert server._canonical_attachment_descriptors(attachments) == history[-1]["attachments"]
    assert db.calls == [
        (
            "session-1",
            "assistant",
            result["messages"][-1]["display_metadata"],
        )
    ]
