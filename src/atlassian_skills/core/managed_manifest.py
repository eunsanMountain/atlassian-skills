from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes

_MANIFEST_PREFIX = "<!-- atls:managed "
_MANIFEST_SUFFIX = " -->"
_LEGACY_PREFIX = "<!-- atls:binding "
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:-]*\Z")
#: One field tuple per manifest version, in canonical order. The order is part of
#: the format: a document whose fields are the right set in the wrong order is
#: refused rather than reordered, so a writer cannot drift.
#:
#: v3 adds `authority`. It lives in the manifest rather than in a sidecar because a
#: sidecar can be lost, copied away from its document, or left behind pointing at a
#: file that has moved on -- and the answer to "may this file publish?" must travel
#: inside the file it is about.
_FIELD_NAMES_BY_VERSION: dict[int, tuple[str, ...]] = {
    2: (
        "v",
        "page",
        "site",
        "remote_version",
        "remote_storage",
        "base_md",
        "assets",
        "converter",
        "profile",
        "passthrough",
    ),
    3: (
        "v",
        "page",
        "site",
        "remote_version",
        "remote_storage",
        "base_md",
        "authority",
        "assets",
        "converter",
        "profile",
        "passthrough",
    ),
}

#: Read these; write the newest. A v2 document is not rewritten until something
#: succeeds that has the right to rewrite it -- a pull, a push, or a record.
SUPPORTED_MANAGED_MANIFEST_VERSIONS: tuple[int, ...] = tuple(sorted(_FIELD_NAMES_BY_VERSION))
CURRENT_MANAGED_MANIFEST_VERSION: int = max(_FIELD_NAMES_BY_VERSION)

#: Managed Markdown admits one authority. The exact-XHTML workflow keeps its own,
#: in its own sidecar, because inline metadata inside XHTML would change the bytes
#: that get published.
_MANAGED_AUTHORITIES = ("md",)

#: What a profile says about which representation publishes. Only profiles whose
#: meaning is known appear here: an unknown profile is unknown, not a contradiction,
#: and calling it one would refuse a document a newer writer produced legitimately.
_PROFILE_AUTHORITY: dict[str, str] = {
    "markdown-first": "md",
    "xhtml-exact": "xhtml",
}

# Kept for the callers that predate versioned field sets. The v2 spelling, because
# that is what they were written against.
_FIELD_NAMES = _FIELD_NAMES_BY_VERSION[2]
_FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)\Z")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL_COMMENT_RE = re.compile(
    r"<!-- (?:atls:(?:managed|operation)|cfxmark:(?:notice|migrations|migration|asset))\b.*? -->"
)
_ASSET_COMMENT_RE = re.compile(
    r"<!-- cfxmark:asset "
    r"materialization=(?P<materialization>local|remote-only) "
    r"src=(?P<src>\S+) "
    r"remote_id=(?P<remote_id>\S+) "
    r"remote_version=(?P<remote_version>[1-9][0-9]*) "
    r"remote_name=(?P<remote_name>\S+) "
    r"sha256=(?P<sha256>sha256:[0-9a-f]{64}) -->"
)
_REMOTE_ID_RE = re.compile(r"[A-Za-z0-9._:-]+\Z")


class ManagedManifestError(ValueError):
    """A stable, structured managed-file contract failure."""

    def __init__(self, reason: str, **context: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.context = {"reason": reason, **context}


@dataclass(frozen=True)
class ManagedAssetRecord:
    materialization: str
    src: str
    remote_id: str
    remote_version: int
    remote_name: str
    sha256: str

    def __post_init__(self) -> None:
        if self.materialization not in {"local", "remote-only"}:
            raise ManagedManifestError("invalid_asset_materialization")
        if not self.src or "\\" in self.src or self.src.startswith("/"):
            raise ManagedManifestError("invalid_asset_path")
        if any(part in {"", ".", ".."} for part in self.src.split("/")):
            raise ManagedManifestError("invalid_asset_path")
        if not _REMOTE_ID_RE.fullmatch(self.remote_id):
            raise ManagedManifestError("invalid_asset_remote_id")
        if self.remote_version < 1:
            raise ManagedManifestError("invalid_asset_remote_version")
        if not self.remote_name or "/" in self.remote_name or "\\" in self.remote_name:
            raise ManagedManifestError("invalid_asset_remote_name")
        _require_hash(self.sha256, field="sha256")


@dataclass(frozen=True)
class ManagedManifest:
    v: int
    page: str
    site: str
    remote_version: int
    remote_storage: str
    base_md: str
    assets: str
    converter: str
    profile: str
    passthrough: tuple[str, ...] = ()
    #: Which representation may publish this page. v2 documents do not carry it and
    #: default to `md`, which is what they always meant -- a managed Markdown file is
    #: a Markdown file. Making it explicit is what lets a later reader tell "this
    #: file publishes Markdown" from "nobody said".
    authority: str = "md"

    def __post_init__(self) -> None:
        # Version first, and before anything else can raise. A newer writer will add
        # fields this build has never heard of, and the useful answer is "upgrade",
        # not "unknown field `authority`" -- which is what the previous ordering
        # produced, indistinguishable from a corrupt document.
        if self.v > CURRENT_MANAGED_MANIFEST_VERSION:
            raise ManagedManifestError(
                "managed_manifest_newer_version",
                version=self.v,
                supported=list(SUPPORTED_MANAGED_MANIFEST_VERSIONS),
            )
        if self.v not in _FIELD_NAMES_BY_VERSION:
            raise ManagedManifestError("unsupported_managed_manifest_version", version=self.v)
        if not self.page.isdigit():
            raise ManagedManifestError("invalid_managed_page")
        if self.remote_version < 1:
            raise ManagedManifestError("invalid_remote_version")
        _require_hash(self.site, field="site")
        _require_hash(self.remote_storage, field="remote_storage")
        _require_hash(self.base_md, field="base_md")
        _require_hash(self.assets, field="assets")
        for field, value in (("converter", self.converter), ("profile", self.profile)):
            if not _ASCII_TOKEN_RE.fullmatch(value):
                raise ManagedManifestError("invalid_manifest_token", field=field)
        if self.authority not in _MANAGED_AUTHORITIES:
            raise ManagedManifestError("invalid_managed_authority", authority=self.authority)
        # A document that says two different things about which representation
        # publishes it. Whichever field the code happens to read decides what gets
        # sent, so the honest answer is neither.
        declared = _PROFILE_AUTHORITY.get(self.profile)
        if declared is not None and declared != self.authority:
            raise ManagedManifestError(
                "managed_manifest_self_contradictory",
                authority=self.authority,
                profile=self.profile,
                profile_implies=declared,
            )
        canonical = _canonical_passthrough(self.passthrough)
        if self.passthrough != canonical:
            object.__setattr__(self, "passthrough", canonical)


@dataclass(frozen=True)
class ManagedDocument:
    manifest: ManagedManifest
    content: str
    assets: tuple[ManagedAssetRecord, ...]


def _require_hash(value: str, *, field: str) -> None:
    if not _HASH_RE.fullmatch(value):
        raise ManagedManifestError("invalid_manifest_hash", field=field)


def _validate_passthrough_token(token: str) -> str:
    normalized = unicodedata.normalize("NFC", token)
    if not normalized or "--" in normalized or ">" in normalized:
        raise ManagedManifestError("invalid_passthrough_prefix", token=token)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ManagedManifestError("invalid_passthrough_prefix", token=token)
    if normalized.casefold().startswith(("atls:", "cfxmark:")):
        raise ManagedManifestError("reserved_passthrough_prefix", token=token)
    return normalized


def _canonical_passthrough(prefixes: Iterable[str]) -> tuple[str, ...]:
    normalized = {_validate_passthrough_token(prefix) for prefix in prefixes}
    return tuple(sorted(normalized, key=lambda item: item.encode("utf-8")))


def serialize_passthrough(prefixes: Iterable[str]) -> str:
    canonical = _canonical_passthrough(prefixes)
    if not canonical:
        return "-"
    return ",".join(quote(prefix, safe="-._~", encoding="utf-8", errors="strict") for prefix in canonical)


def parse_passthrough(value: str) -> tuple[str, ...]:
    if value == "-":
        return ()
    if not value:
        raise ManagedManifestError("invalid_passthrough_prefix")
    decoded: list[str] = []
    for encoded in value.split(","):
        if not encoded or _INVALID_PERCENT_RE.search(encoded):
            raise ManagedManifestError("invalid_percent_encoding", value=encoded)
        try:
            token = unquote_to_bytes(encoded).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ManagedManifestError("invalid_percent_encoding", value=encoded) from error
        decoded.append(_validate_passthrough_token(token))
    canonical = _canonical_passthrough(decoded)
    if serialize_passthrough(canonical) != value:
        raise ManagedManifestError("noncanonical_passthrough", value=value)
    return canonical


def _encode_asset_value(value: str, *, path: bool = False) -> str:
    return quote(unicodedata.normalize("NFC", value), safe="/-._~" if path else "-._~")


def _decode_asset_value(value: str, *, field: str) -> str:
    if not value or _INVALID_PERCENT_RE.search(value):
        raise ManagedManifestError("invalid_asset_percent_encoding", field=field)
    try:
        decoded = unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManagedManifestError("invalid_asset_percent_encoding", field=field) from error
    if _encode_asset_value(decoded, path=field == "src") != value:
        raise ManagedManifestError("noncanonical_asset_encoding", field=field)
    return unicodedata.normalize("NFC", decoded)


def serialize_asset_record(record: ManagedAssetRecord) -> str:
    return (
        "<!-- cfxmark:asset "
        f"materialization={record.materialization} "
        f"src={_encode_asset_value(record.src, path=True)} "
        f"remote_id={_encode_asset_value(record.remote_id)} "
        f"remote_version={record.remote_version} "
        f"remote_name={_encode_asset_value(record.remote_name)} "
        f"sha256={record.sha256} -->"
    )


def extract_asset_records(markdown: str) -> tuple[ManagedAssetRecord, ...]:
    records: list[ManagedAssetRecord] = []
    fence: tuple[str, int] | None = None
    for raw_line in markdown.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        fence_match = _FENCE_RE.fullmatch(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not fence_match.group(2).strip():
                fence = None
            continue
        if fence is not None:
            continue
        for match in _ASSET_COMMENT_RE.finditer(raw_line):
            records.append(
                ManagedAssetRecord(
                    materialization=match.group("materialization"),
                    src=_decode_asset_value(match.group("src"), field="src"),
                    remote_id=_decode_asset_value(match.group("remote_id"), field="remote_id"),
                    remote_version=int(match.group("remote_version")),
                    remote_name=_decode_asset_value(match.group("remote_name"), field="remote_name"),
                    sha256=match.group("sha256"),
                )
            )
    return tuple(dict.fromkeys(records))


def serialize_managed_manifest(manifest: ManagedManifest) -> str:
    """Write the manifest in its own version's field order.

    Reads any supported version and writes whichever one the manifest carries, so
    the decision to move a document from v2 to v3 belongs to the caller that has the
    right to rewrite it -- a successful pull, push or record -- and not to whatever
    happened to serialize it in passing.
    """

    names = _FIELD_NAMES_BY_VERSION.get(manifest.v)
    if names is None:
        raise ManagedManifestError("unsupported_managed_manifest_version", version=manifest.v)
    available = {
        "v": str(manifest.v),
        "page": manifest.page,
        "site": manifest.site,
        "remote_version": str(manifest.remote_version),
        "remote_storage": manifest.remote_storage,
        "base_md": manifest.base_md,
        "authority": manifest.authority,
        "assets": manifest.assets,
        "converter": manifest.converter,
        "profile": manifest.profile,
        "passthrough": serialize_passthrough(manifest.passthrough),
    }
    payload = " ".join(f"{name}={available[name]}" for name in names)
    return f"{_MANIFEST_PREFIX}{payload}{_MANIFEST_SUFFIX}"


def _outside_fence_manifest_lines(markdown: str) -> tuple[tuple[int, str], ...]:
    matches: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    for index, raw_line in enumerate(markdown.splitlines()):
        line = raw_line.removeprefix("\ufeff") if index == 0 else raw_line
        fence_match = _FENCE_RE.fullmatch(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not fence_match.group(2).strip():
                fence = None
            continue
        if fence is None and line.startswith(_MANIFEST_PREFIX) and line.endswith(_MANIFEST_SUFFIX):
            matches.append((index, line))
    return tuple(matches)


def _parse_manifest_line(line: str) -> ManagedManifest:
    if not line.startswith(_MANIFEST_PREFIX) or not line.endswith(_MANIFEST_SUFFIX):
        raise ManagedManifestError("invalid_managed_manifest")
    payload = line[len(_MANIFEST_PREFIX) : -len(_MANIFEST_SUFFIX)]
    fields: list[tuple[str, str]] = []
    for item in payload.split(" "):
        if not item or "=" not in item:
            raise ManagedManifestError("invalid_managed_manifest")
        name, value = item.split("=", 1)
        fields.append((name, value))
    names = tuple(name for name, _value in fields)
    if len(set(names)) != len(names):
        raise ManagedManifestError("duplicate_manifest_field")

    # `v` is read before the field set is judged, because the field set depends on
    # it. The previous order compared names against one fixed tuple, so a document
    # from a newer writer was reported as carrying an unknown field -- the same
    # answer a corrupt document gets, and the opposite of the useful one.
    values = dict(fields)
    if "v" not in values:
        raise ManagedManifestError("invalid_managed_manifest")
    try:
        version = int(values["v"])
    except ValueError as error:
        raise ManagedManifestError("invalid_manifest_integer") from error
    if version > CURRENT_MANAGED_MANIFEST_VERSION:
        # Deliberately before the field check and carrying no payload: §6.3 forbids
        # echoing a document this build could not read, and the fields of a version
        # it does not know are not evidence of anything it can act on.
        raise ManagedManifestError(
            "managed_manifest_newer_version",
            version=version,
            supported=list(SUPPORTED_MANAGED_MANIFEST_VERSIONS),
        )
    expected = _FIELD_NAMES_BY_VERSION.get(version)
    if expected is None:
        raise ManagedManifestError("unsupported_managed_manifest_version", version=version)
    if names != expected:
        unknown = sorted(set(names) - set(expected))
        raise ManagedManifestError(
            "unknown_manifest_field" if unknown else "noncanonical_manifest_field_order",
            fields=unknown or list(names),
            version=version,
        )
    try:
        remote_version = int(values["remote_version"])
    except ValueError as error:
        raise ManagedManifestError("invalid_manifest_integer") from error
    return ManagedManifest(
        v=version,
        page=values["page"],
        site=values["site"],
        remote_version=remote_version,
        remote_storage=values["remote_storage"],
        base_md=values["base_md"],
        assets=values["assets"],
        converter=values["converter"],
        profile=values["profile"],
        passthrough=parse_passthrough(values["passthrough"]),
        # v2 has no field for it and always meant `md`.
        authority=values.get("authority", "md"),
    )


def parse_managed_manifest(markdown: str) -> ManagedManifest:
    text = markdown.removeprefix("\ufeff")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    candidates = _outside_fence_manifest_lines(markdown)
    if first_line.startswith(_LEGACY_PREFIX):
        raise ManagedManifestError("legacy_binding_marker")
    if len(candidates) > 1:
        raise ManagedManifestError("duplicate_managed_manifest")
    if not candidates:
        if first_line.startswith("<!-- atls:managed"):
            raise ManagedManifestError("invalid_managed_manifest")
        raise ManagedManifestError("missing_managed_manifest")
    index, line = candidates[0]
    if index != 0 or first_line != line:
        raise ManagedManifestError("managed_manifest_not_first")
    return _parse_manifest_line(line)


def strip_managed_manifest(markdown: str) -> tuple[str, ManagedManifest]:
    manifest = parse_managed_manifest(markdown)
    text = markdown.removeprefix("\ufeff")
    _line, separator, content = text.partition("\n")
    if not separator:
        content = ""
    return content, manifest


def canonical_managed_content(markdown: str) -> str:
    text = markdown.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    fence: tuple[str, int] | None = None
    control_block_separator_pending = False
    for raw_line in text.splitlines(keepends=True):
        line_without_newline = raw_line.removesuffix("\n")
        fence_match = _FENCE_RE.fullmatch(line_without_newline)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not fence_match.group(2).strip():
                fence = None
            output.append(raw_line)
            continue
        if fence is not None:
            output.append(raw_line)
            continue
        if control_block_separator_pending and not line_without_newline.strip():
            control_block_separator_pending = False
            continue
        control_block_separator_pending = False
        stripped = _CONTROL_COMMENT_RE.sub("", line_without_newline)
        if not stripped.strip() and _CONTROL_COMMENT_RE.search(line_without_newline):
            control_block_separator_pending = True
            continue
        output.append(stripped + ("\n" if raw_line.endswith("\n") else ""))
    canonical = "".join(output).rstrip("\n") + "\n"
    return canonical


def canonical_content_sha256(markdown: str) -> str:
    digest = hashlib.sha256(canonical_managed_content(markdown).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonical_asset_set_sha256(records: Iterable[ManagedAssetRecord]) -> str:
    canonical_records = [
        {
            "materialization": record.materialization,
            "src": unicodedata.normalize("NFC", record.src),
            "remote_id": record.remote_id,
            "remote_version": record.remote_version,
            "remote_name": unicodedata.normalize("NFC", record.remote_name),
            "sha256": record.sha256,
        }
        for record in dict.fromkeys(records)
    ]
    canonical_records.sort(key=lambda record: tuple(str(record[field]).encode("utf-8") for field in record))
    payload = json.dumps(canonical_records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def parse_managed_document(
    markdown: str,
    *,
    assets: Iterable[ManagedAssetRecord] = (),
    verify_content: bool = True,
    verify_assets: bool = True,
) -> ManagedDocument:
    content, manifest = strip_managed_manifest(markdown)
    asset_records = tuple(assets)
    actual_content = canonical_content_sha256(content)
    if verify_content and actual_content != manifest.base_md:
        raise ManagedManifestError(
            "managed_content_tampered",
            expected=manifest.base_md,
            actual=actual_content,
        )
    actual_assets = canonical_asset_set_sha256(asset_records)
    if verify_assets and actual_assets != manifest.assets:
        raise ManagedManifestError(
            "managed_assets_tampered",
            expected=manifest.assets,
            actual=actual_assets,
        )
    return ManagedDocument(manifest=manifest, content=content, assets=asset_records)


__all__ = [
    "ManagedAssetRecord",
    "ManagedDocument",
    "ManagedManifest",
    "ManagedManifestError",
    "canonical_asset_set_sha256",
    "canonical_content_sha256",
    "canonical_managed_content",
    "extract_asset_records",
    "parse_managed_document",
    "parse_managed_manifest",
    "parse_passthrough",
    "serialize_managed_manifest",
    "serialize_asset_record",
    "serialize_passthrough",
    "strip_managed_manifest",
]
