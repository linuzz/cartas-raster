"""Shared data records. JSON on disk stays a plain dict so the manifest is diffable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class IndexRecord:
    chart_id: str
    token: str
    name: str
    scale: int | None
    scale_label: str | None
    title: str
    uuid: str
    status: str | None
    revision_date: str | None
    change_date: str | None
    page_url: str | None


@dataclass(frozen=True)
class RemoteFile:
    """A chart whose ZIP URL answered HEAD with a zip payload."""

    chart_id: str
    record: IndexRecord
    source_url: str
    zip_name: str
    content_length: int
    last_modified: str | None
    etag: str | None


@dataclass(frozen=True)
class Unresolved:
    chart_id: str
    title: str
    reason: str
    page_url: str | None = None


@dataclass(frozen=True)
class BsbInfo:
    name: str | None = None
    number: str | None = None
    source_edition: int | None = None
    raster_edition: int | None = None
    source_date: str | None = None
    ntm_edition: str | None = None
    ntm_date: str | None = None


@dataclass
class PreparedChart:
    """A downloaded chart ready to publish or to propose."""

    remote: RemoteFile
    sha256: str
    size: int
    bsb: BsbInfo
    zip_datetime: datetime
    zip_path: str


@dataclass
class OpenPullRequest:
    number: int
    chart_id: str
    sha256: str


@dataclass
class ReviewDecision:
    opens: list[PreparedChart] = field(default_factory=list)
    supersede: list[OpenPullRequest] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)
    skipped_rejected: list[str] = field(default_factory=list)
    skipped_cap: list[str] = field(default_factory=list)
