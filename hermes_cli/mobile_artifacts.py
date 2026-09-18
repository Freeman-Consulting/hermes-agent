"""Versioned mobile attachment descriptors and scoped artifact storage.

The public contract is intentionally path-free. Inbound attachments and outbound
artifacts use the same descriptor shape; only outbound descriptors carry the
fixed authenticated download route. Artifact bytes live below a controlled
profile root under server-minted opaque ids. Client requests can name only an
opaque id plus the already-selected profile/session scope, never a filesystem
path.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

ATTACHMENT_DESCRIPTOR_SCHEMA = "hermes.attachment"
ATTACHMENT_DESCRIPTOR_VERSION = 1
MOBILE_ARTIFACT_DOWNLOAD_PATH = "/api/mobile/artifacts/download"
MOBILE_ARTIFACT_MAX_BYTES = 50 * 1024 * 1024
MOBILE_ARTIFACT_TTL_SECONDS = 7 * 24 * 60 * 60

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}$")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_DIRECTIONS = frozenset({"inbound", "outbound"})
_ALLOWED_KINDS = frozenset({"image", "pdf", "file"})
_ALLOWED_METADATA_KEYS = frozenset({"width_px", "height_px", "page_count"})
_METADATA_MAX_BYTES = 16 * 1024
_STORE_VERSION = 1


class MobileArtifactError(Exception):
    """Base class for the mobile artifact contract."""


class MobileArtifactNotFound(MobileArtifactError):
    """The opaque id is unknown or no longer available."""


class MobileArtifactExpired(MobileArtifactError):
    """The artifact exceeded its declared retention window."""


class MobileArtifactScopeMismatch(MobileArtifactError):
    """The artifact exists but belongs to another profile/session."""


class MobileArtifactTooLarge(MobileArtifactError):
    """The artifact exceeds the server-side byte cap."""


class MobileArtifactCorrupt(MobileArtifactError):
    """The stored bytes or metadata failed integrity validation."""


class AttachmentDescriptorInvalid(ValueError):
    """A descriptor failed the v1 wire schema."""


@dataclass(frozen=True)
class MobileArtifactPayload:
    data: bytes
    descriptor: dict[str, Any]


def _safe_filename(value: str) -> str:
    name = Path(str(value or "").replace("\\", "/")).name.strip()
    name = "".join("_" if ord(ch) < 32 or ord(ch) == 127 else ch for ch in name)
    name = name.replace("/", "_").replace("\\", "_").strip(" .")
    if not name:
        name = "artifact.bin"
    if len(name) > 180:
        suffix = Path(name).suffix[:20]
        stem_budget = max(1, 180 - len(suffix))
        name = f"{Path(name).stem[:stem_budget]}{suffix}"
    return name


def _normalized_mime_type(value: str, *, filename: str = "") -> str:
    candidate = str(value or "").split(";", 1)[0].strip().lower()
    if not candidate and filename:
        candidate = (mimetypes.guess_type(filename)[0] or "").lower()
    if not candidate:
        candidate = "application/octet-stream"
    if not _MIME_RE.fullmatch(candidate):
        raise AttachmentDescriptorInvalid("invalid MIME type")
    return candidate


def _normalized_session_id(value: str) -> str:
    session_id = str(value or "").strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise MobileArtifactScopeMismatch("invalid session scope")
    return session_id


def _normalized_profile(value: str) -> str:
    from hermes_cli.profiles import normalize_profile_name, validate_profile_name

    profile = normalize_profile_name(str(value or "default"))
    validate_profile_name(profile)
    return profile


def _normalized_metadata(metadata: Mapping[str, Any] | None) -> dict[str, int]:
    if not metadata:
        return {}
    unknown = set(metadata) - _ALLOWED_METADATA_KEYS
    if unknown:
        raise AttachmentDescriptorInvalid("unknown descriptor metadata field")
    result: dict[str, int] = {}
    for key, raw in metadata.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise AttachmentDescriptorInvalid(f"invalid {key}")
        result[key] = raw
    return result


def build_attachment_descriptor(
    *,
    direction: str,
    kind: str,
    name: str,
    mime_type: str,
    size_bytes: int,
    sha256: str,
    descriptor_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    downloadable: bool = False,
) -> dict[str, Any]:
    """Build and validate the canonical v1 descriptor.

    The returned object contains no raw path, model-facing reference, URL token,
    or credential. The fixed download route is present only for outbound
    artifacts and still requires paired-device authentication plus exact
    profile/session scope in the request body.
    """

    normalized_direction = str(direction or "").strip().lower()
    normalized_kind = str(kind or "").strip().lower()
    if normalized_direction not in _ALLOWED_DIRECTIONS:
        raise AttachmentDescriptorInvalid("invalid attachment direction")
    if normalized_kind not in _ALLOWED_KINDS:
        raise AttachmentDescriptorInvalid("invalid attachment kind")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise AttachmentDescriptorInvalid("invalid attachment byte count")
    digest = str(sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise AttachmentDescriptorInvalid("invalid attachment checksum")
    opaque_id = str(descriptor_id or secrets.token_hex(16)).strip().lower()
    if not _ID_RE.fullmatch(opaque_id):
        raise AttachmentDescriptorInvalid("invalid attachment id")
    if downloadable and normalized_direction != "outbound":
        raise AttachmentDescriptorInvalid("only outbound artifacts are downloadable")

    descriptor: dict[str, Any] = {
        "schema": ATTACHMENT_DESCRIPTOR_SCHEMA,
        "version": ATTACHMENT_DESCRIPTOR_VERSION,
        "id": opaque_id,
        "direction": normalized_direction,
        "kind": normalized_kind,
        "name": _safe_filename(name),
        "mime_type": _normalized_mime_type(mime_type, filename=name),
        "size_bytes": size_bytes,
        "sha256": digest,
    }
    normalized_metadata = _normalized_metadata(metadata)
    if normalized_metadata:
        descriptor["metadata"] = normalized_metadata
    if downloadable:
        descriptor["download"] = {
            "transport": "paired_device_http",
            "method": "POST",
            "path": MOBILE_ARTIFACT_DOWNLOAD_PATH,
        }
    return descriptor


def build_inbound_attachment_descriptor(
    *,
    kind: str,
    name: str,
    mime_type: str,
    data: bytes,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = bytes(data)
    return build_attachment_descriptor(
        direction="inbound",
        kind=kind,
        name=name,
        mime_type=mime_type,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        metadata=metadata,
    )


def validate_attachment_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an untrusted descriptor and return its canonical form."""

    if not isinstance(value, Mapping):
        raise AttachmentDescriptorInvalid("descriptor must be an object")
    allowed = {
        "schema",
        "version",
        "id",
        "direction",
        "kind",
        "name",
        "mime_type",
        "size_bytes",
        "sha256",
        "metadata",
        "download",
    }
    if set(value) - allowed:
        raise AttachmentDescriptorInvalid("unknown descriptor field")
    if value.get("schema") != ATTACHMENT_DESCRIPTOR_SCHEMA:
        raise AttachmentDescriptorInvalid("unsupported descriptor schema")
    if value.get("version") != ATTACHMENT_DESCRIPTOR_VERSION:
        raise AttachmentDescriptorInvalid("unsupported descriptor version")
    raw_size = value.get("size_bytes")
    if isinstance(raw_size, bool) or not isinstance(raw_size, int):
        raise AttachmentDescriptorInvalid("invalid attachment byte count")
    raw_metadata = value.get("metadata")
    if raw_metadata is not None and not isinstance(raw_metadata, Mapping):
        raise AttachmentDescriptorInvalid("descriptor metadata must be an object")
    expected_download = value.get("direction") == "outbound"
    canonical = build_attachment_descriptor(
        direction=str(value.get("direction") or ""),
        kind=str(value.get("kind") or ""),
        name=str(value.get("name") or ""),
        mime_type=str(value.get("mime_type") or ""),
        size_bytes=raw_size,
        sha256=str(value.get("sha256") or ""),
        descriptor_id=str(value.get("id") or ""),
        metadata=raw_metadata,
        downloadable=expected_download,
    )
    if value.get("download") != canonical.get("download"):
        raise AttachmentDescriptorInvalid("invalid download contract")
    if dict(value) != canonical:
        raise AttachmentDescriptorInvalid("descriptor is not canonical")
    return canonical


def infer_attachment_kind(*, mime_type: str, filename: str) -> str:
    normalized = _normalized_mime_type(mime_type, filename=filename)
    if normalized.startswith("image/"):
        return "image"
    if normalized == "application/pdf" or Path(filename).suffix.lower() == ".pdf":
        return "pdf"
    return "file"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


class MobileArtifactStore:
    """Restart-safe, profile/session-scoped artifact store."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = MOBILE_ARTIFACT_MAX_BYTES,
        ttl_seconds: int = MOBILE_ARTIFACT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = int(ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()

    def _paths(self, artifact_id: str) -> tuple[Path, Path]:
        normalized = str(artifact_id or "").strip().lower()
        if not _ID_RE.fullmatch(normalized):
            raise MobileArtifactNotFound("artifact not found")
        return self.root / f"{normalized}.blob", self.root / f"{normalized}.json"

    def publish(
        self,
        *,
        data: bytes,
        profile: str,
        session_id: str,
        filename: str,
        mime_type: str,
        kind: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = bytes(data)
        if not payload:
            raise MobileArtifactCorrupt("artifact is empty")
        if len(payload) > self.max_bytes:
            raise MobileArtifactTooLarge("artifact exceeds server byte cap")
        normalized_profile = _normalized_profile(profile)
        normalized_session = _normalized_session_id(session_id)
        safe_name = _safe_filename(filename)
        normalized_mime = _normalized_mime_type(mime_type, filename=safe_name)
        normalized_kind = kind or infer_attachment_kind(
            mime_type=normalized_mime, filename=safe_name
        )
        now = float(self._clock())

        with self._lock:
            self.prune_expired(now=now)
            while True:
                artifact_id = secrets.token_hex(16)
                data_path, metadata_path = self._paths(artifact_id)
                if not data_path.exists() and not metadata_path.exists():
                    break
            descriptor = build_attachment_descriptor(
                direction="outbound",
                kind=normalized_kind,
                name=safe_name,
                mime_type=normalized_mime,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                descriptor_id=artifact_id,
                metadata=metadata,
                downloadable=True,
            )
            record = {
                "store_version": _STORE_VERSION,
                "profile": normalized_profile,
                "session_id": normalized_session,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "descriptor": descriptor,
            }
            encoded_record = json.dumps(
                record, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded_record) > _METADATA_MAX_BYTES:
                raise MobileArtifactCorrupt("artifact metadata is oversized")
            _atomic_write(data_path, payload)
            try:
                _atomic_write(metadata_path, encoded_record)
            except Exception:
                data_path.unlink(missing_ok=True)
                raise
            return descriptor

    def _load_record(self, metadata_path: Path) -> dict[str, Any]:
        if metadata_path.is_symlink():
            raise MobileArtifactCorrupt("artifact metadata is invalid")
        try:
            with metadata_path.open("rb") as handle:
                raw = handle.read(_METADATA_MAX_BYTES + 1)
        except FileNotFoundError as exc:
            raise MobileArtifactNotFound("artifact not found") from exc
        if len(raw) > _METADATA_MAX_BYTES:
            raise MobileArtifactCorrupt("artifact metadata is oversized")
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MobileArtifactCorrupt("artifact metadata is invalid") from exc
        if not isinstance(record, dict) or record.get("store_version") != _STORE_VERSION:
            raise MobileArtifactCorrupt("artifact metadata is invalid")
        return record

    def load(
        self,
        *,
        artifact_id: str,
        profile: str,
        session_id: str,
    ) -> MobileArtifactPayload:
        normalized_profile = _normalized_profile(profile)
        normalized_session = _normalized_session_id(session_id)
        data_path, metadata_path = self._paths(artifact_id)
        with self._lock:
            record = self._load_record(metadata_path)
            if (
                record.get("profile") != normalized_profile
                or record.get("session_id") != normalized_session
            ):
                raise MobileArtifactScopeMismatch("artifact not found in requested scope")
            try:
                expires_at = float(record["expires_at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MobileArtifactCorrupt("artifact metadata is invalid") from exc
            if expires_at <= float(self._clock()):
                self._delete_paths(data_path, metadata_path)
                raise MobileArtifactExpired("artifact expired")
            descriptor_raw = record.get("descriptor")
            if not isinstance(descriptor_raw, dict):
                raise MobileArtifactCorrupt("artifact descriptor is missing")
            try:
                descriptor = validate_attachment_descriptor(descriptor_raw)
            except AttachmentDescriptorInvalid as exc:
                raise MobileArtifactCorrupt("artifact descriptor is invalid") from exc
            if descriptor["id"] != str(artifact_id).strip().lower():
                raise MobileArtifactCorrupt("artifact id does not match metadata")
            if data_path.is_symlink():
                raise MobileArtifactCorrupt("artifact data is invalid")
            try:
                with data_path.open("rb") as handle:
                    payload = handle.read(self.max_bytes + 1)
            except FileNotFoundError as exc:
                raise MobileArtifactNotFound("artifact not found") from exc
            if len(payload) > self.max_bytes:
                raise MobileArtifactCorrupt("artifact exceeds store byte cap")
            if len(payload) != descriptor["size_bytes"]:
                raise MobileArtifactCorrupt("artifact size mismatch")
            if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
                raise MobileArtifactCorrupt("artifact checksum mismatch")
            return MobileArtifactPayload(data=payload, descriptor=descriptor)

    def _delete_paths(self, data_path: Path, metadata_path: Path) -> None:
        data_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)

    def prune_expired(self, *, now: float | None = None) -> int:
        if not self.root.exists():
            return 0
        deadline = float(self._clock() if now is None else now)
        removed = 0
        with self._lock:
            for metadata_path in self.root.glob("*.json"):
                artifact_id = metadata_path.stem
                if not _ID_RE.fullmatch(artifact_id):
                    continue
                data_path, expected_metadata_path = self._paths(artifact_id)
                if metadata_path != expected_metadata_path:
                    continue
                try:
                    record = self._load_record(metadata_path)
                    expires_at = float(record["expires_at"])
                except (MobileArtifactError, KeyError, TypeError, ValueError):
                    continue
                if expires_at <= deadline:
                    self._delete_paths(data_path, metadata_path)
                    removed += 1
        return removed


_store_lock = threading.Lock()
_stores: dict[str, MobileArtifactStore] = {}


def _reset_for_tests() -> None:
    with _store_lock:
        _stores.clear()


def _profile_home(profile: str) -> tuple[str, Path]:
    from hermes_constants import get_process_hermes_home, named_profile_home

    normalized = _normalized_profile(profile)
    process_home = get_process_hermes_home().expanduser().resolve()
    named_home = named_profile_home(process_home)
    root_home = named_home.parent.parent if named_home is not None else process_home
    profile_home = root_home if normalized == "default" else root_home / "profiles" / normalized
    if not profile_home.is_dir():
        raise MobileArtifactScopeMismatch("profile scope does not exist")
    return normalized, profile_home


def _store_for_profile(profile: str) -> tuple[str, MobileArtifactStore]:
    normalized, profile_home = _profile_home(profile)
    key = str(profile_home)
    with _store_lock:
        store = _stores.get(key)
        if store is None:
            store = MobileArtifactStore(profile_home / "artifacts" / "mobile-v1")
            _stores[key] = store
    return normalized, store


def publish_mobile_artifact(
    *,
    data: bytes,
    profile: str,
    session_id: str,
    filename: str,
    mime_type: str,
    kind: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized, store = _store_for_profile(profile)
    return store.publish(
        data=data,
        profile=normalized,
        session_id=session_id,
        filename=filename,
        mime_type=mime_type,
        kind=kind,
        metadata=metadata,
    )


def load_mobile_artifact(
    *, artifact_id: str, profile: str, session_id: str
) -> MobileArtifactPayload:
    normalized, store = _store_for_profile(profile)
    return store.load(
        artifact_id=artifact_id,
        profile=normalized,
        session_id=session_id,
    )
