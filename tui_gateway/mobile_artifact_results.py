"""Path-free mobile presentation for agent-produced ``MEDIA:`` results.

Only the iOS pocket source enters this lane. Producer paths stay server-side:
accepted regular files are copied into the bounded artifact store, while live
and durable transcript surfaces receive canonical ``hermes.attachment/1``
descriptors plus sanitized display text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.platforms.base import BasePlatformAdapter
from hermes_cli.mobile_artifacts import (
    MobileArtifactError,
    publish_mobile_artifact_path,
    validate_attachment_descriptor,
)

logger = logging.getLogger(__name__)

MOBILE_ARTIFACT_SOURCE = "ios-pocket"
MOBILE_ARTIFACT_MAX_PER_MESSAGE = 8

_DELIVERY_DIRECTIVE_RE = re.compile(
    r"(?i)\[\[(?:audio_as_voice|as_document)\]\]"
)
_MEDIA_DIRECTIVE_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])MEDIA:\s*")


@dataclass(frozen=True)
class MobileArtifactProjection:
    display_text: str
    attachments: list[dict[str, Any]]
    failed_count: int


def profile_name_from_home(profile_home: str | Path | None) -> str:
    """Map an immediate ``profiles/<name>`` home to its wire profile name."""

    if not profile_home:
        return "default"
    try:
        home = Path(profile_home).expanduser()
        candidate = home.name if home.parent.name == "profiles" else "default"
        from hermes_cli.profiles import normalize_profile_name, validate_profile_name

        normalized = normalize_profile_name(candidate)
        validate_profile_name(normalized)
        return normalized
    except (OSError, RuntimeError, TypeError, ValueError):
        return "default"


def _sanitize_mobile_line(line: str) -> str:
    """Remove delivery controls and MEDIA path directives from one display line."""

    had_newline = line.endswith("\n")
    body = line[:-1] if had_newline else line
    body = body.rstrip("\r")
    body = _DELIVERY_DIRECTIVE_RE.sub("", body)
    match = _MEDIA_DIRECTIVE_RE.search(body)
    if match is not None:
        body = body[: match.start()].rstrip()
    if not body.strip():
        return ""
    return body + ("\n" if had_newline else "")


def strict_mobile_display_text(text: str) -> str:
    """Return user-visible text with no explicit MEDIA path directive."""

    if not isinstance(text, str) or not text:
        return ""
    lines = text.splitlines(keepends=True)
    cleaned = "".join(_sanitize_mobile_line(line) for line in lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _canonical_history_attachments(value: Any, role: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or role not in {"user", "assistant"}:
        return []
    expected_direction = "inbound" if role == "user" else "outbound"
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in value:
        try:
            descriptor = validate_attachment_descriptor(raw)
        except (TypeError, ValueError):
            continue
        descriptor_id = str(descriptor.get("id") or "")
        if (
            descriptor.get("direction") == expected_direction
            and descriptor_id not in seen_ids
        ):
            seen_ids.add(descriptor_id)
            result.append(descriptor)
    return result


def _strict_mobile_history_text(text: str, role: str) -> str:
    safe_text = strict_mobile_display_text(text)
    if role == "user" and (
        "@file:" in safe_text.casefold() or "file://" in safe_text.casefold()
    ):
        return ""
    return safe_text


def project_mobile_history_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return a path-free transcript row for an ``ios-pocket`` response."""

    projected = dict(message)
    role = str(projected.get("role") or "")
    raw_text = projected.get("text")
    if not isinstance(raw_text, str):
        raw_text = projected.get("content")
    if not isinstance(raw_text, str):
        raw_text = ""

    metadata = projected.get("display_metadata")
    has_attachment_metadata = isinstance(metadata, dict) and "attachments" in metadata
    if isinstance(metadata, dict) and "attachments" in metadata:
        raw_attachments = metadata.get("attachments")
    else:
        raw_attachments = projected.get("attachments")
    attachments = _canonical_history_attachments(raw_attachments, role)

    display_text = metadata.get("display_text") if isinstance(metadata, dict) else None
    safe_text = _strict_mobile_history_text(
        display_text if isinstance(display_text, str) else raw_text,
        role,
    )

    if role in {"user", "assistant"}:
        projected["text"] = safe_text
        if "content" in projected:
            projected["content"] = safe_text
    display_content = projected.get("display_content")
    if isinstance(display_content, str):
        projected["display_content"] = _strict_mobile_history_text(
            display_content,
            role,
        )
    if has_attachment_metadata:
        projected["attachments"] = attachments
        projected["display_metadata"] = {
            "display_text": safe_text,
            "attachments": attachments,
        }
    elif isinstance(metadata, dict) and isinstance(metadata.get("display_text"), str):
        safe_metadata = dict(metadata)
        safe_metadata["display_text"] = _strict_mobile_history_text(
            str(metadata["display_text"]),
            role,
        )
        projected["display_metadata"] = safe_metadata
    return projected


class MobileArtifactStreamFilter:
    """Incremental filter that prevents split MEDIA paths from streaming."""

    _MARKERS = ("media:", "[[audio_as_voice]]", "[[as_document]]")

    def __init__(self) -> None:
        self._pending = ""
        self._discarding_media = False
        self._line_has_visible_text = False
        self._drop_directive_newline = False

    def _record_visible(self, text: str) -> None:
        if not text:
            return
        if "\n" in text:
            self._line_has_visible_text = bool(text.rsplit("\n", 1)[1])
        else:
            self._line_has_visible_text = True

    @classmethod
    def _marker_match(cls, value: str) -> tuple[int, str] | None:
        folded = value.casefold()
        matches = [
            (folded.find(marker), marker)
            for marker in cls._MARKERS
            if folded.find(marker) >= 0
        ]
        return min(matches, key=lambda item: item[0]) if matches else None

    @classmethod
    def _protected_suffix_length(cls, value: str) -> int:
        folded = value.casefold()
        maximum = min(len(folded), max(len(marker) for marker in cls._MARKERS) - 1)
        for length in range(maximum, 0, -1):
            suffix = folded[-length:]
            if any(marker.startswith(suffix) for marker in cls._MARKERS):
                return length
        return 0

    def feed(self, delta: str) -> str:
        if not isinstance(delta, str) or not delta:
            return ""
        self._pending += delta
        visible: list[str] = []

        while self._pending:
            if self._drop_directive_newline:
                if self._pending.startswith("\r\n"):
                    self._pending = self._pending[2:]
                    self._line_has_visible_text = False
                    self._drop_directive_newline = False
                    continue
                if self._pending.startswith("\n"):
                    self._pending = self._pending[1:]
                    self._line_has_visible_text = False
                    self._drop_directive_newline = False
                    continue
                self._drop_directive_newline = False

            if self._discarding_media:
                newline = self._pending.find("\n")
                if newline < 0:
                    self._pending = ""
                    break
                self._pending = self._pending[newline + 1 :]
                self._discarding_media = False
                if self._line_has_visible_text:
                    visible.append("\n")
                self._line_has_visible_text = False
                continue

            marker_match = self._marker_match(self._pending)
            if marker_match is None:
                protected = self._protected_suffix_length(self._pending)
                emit_end = len(self._pending) - protected
                if emit_end > 0:
                    emitted = self._pending[:emit_end]
                    visible.append(emitted)
                    self._record_visible(emitted)
                    self._pending = self._pending[emit_end:]
                break

            marker_index, marker = marker_match
            prefix = self._pending[:marker_index]
            visible.append(prefix)
            self._record_visible(prefix)
            self._pending = self._pending[marker_index + len(marker) :]
            if marker == "media:":
                self._discarding_media = True
            elif not self._line_has_visible_text:
                self._drop_directive_newline = True
            # Bracket directives are removed in-place; ordinary text after the
            # marker remains eligible for immediate streaming.

        return "".join(visible)

    def finish(self) -> str:
        if self._discarding_media:
            self._pending = ""
            self._discarding_media = False
            self._line_has_visible_text = False
            return ""
        pending = self._pending
        self._pending = ""
        return _sanitize_mobile_line(pending)


def project_mobile_artifact_result(
    *,
    response_text: str,
    source: str,
    profile: str,
    session_id: str,
    session_key: str = "",
) -> MobileArtifactProjection | None:
    """Publish safe MEDIA producers and build one authoritative projection."""

    if source != MOBILE_ARTIFACT_SOURCE or not isinstance(response_text, str):
        return None

    media, extracted_text = BasePlatformAdapter.extract_media(
        response_text,
        log_rejections=False,
    )
    display_text = strict_mobile_display_text(extracted_text)
    had_explicit_directive = bool(_MEDIA_DIRECTIVE_RE.search(response_text))
    if not media and not had_explicit_directive and display_text == response_text.strip():
        return None

    attachments: list[dict[str, Any]] = []
    failed_count = 0
    seen_paths: set[str] = set()

    for raw_path, _is_voice in media:
        safe_path = BasePlatformAdapter.validate_media_delivery_path(
            str(raw_path),
            session_key=session_key,
            log_rejections=False,
        )
        if safe_path is None:
            failed_count += 1
            continue
        if safe_path in seen_paths:
            continue
        seen_paths.add(safe_path)
        if len(attachments) >= MOBILE_ARTIFACT_MAX_PER_MESSAGE:
            failed_count += 1
            continue
        try:
            attachments.append(
                publish_mobile_artifact_path(
                    path=safe_path,
                    profile=profile,
                    session_id=session_id,
                )
            )
        except (MobileArtifactError, OSError, RuntimeError, ValueError) as exc:
            failed_count += 1
            logger.warning(
                "Mobile artifact publication skipped (%s)",
                type(exc).__name__,
            )

    if had_explicit_directive and not media:
        failed_count = max(1, failed_count)
    if failed_count:
        notice = "Attachment unavailable."
        display_text = f"{display_text}\n\n{notice}" if display_text else notice

    return MobileArtifactProjection(
        display_text=display_text,
        attachments=attachments,
        failed_count=failed_count,
    )
