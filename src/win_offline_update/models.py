"""Records shared by the update sources and download commands."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UpdateFile:
    name: str
    url: str
    sha256: str | None = None
    sha1: str | None = None


@dataclass(frozen=True, slots=True)
class Update:
    id: str
    title: str
    product: str = ""
    architecture: str | None = None
    classification: str = ""
    date: str = ""
    kb: str | None = None
    source: str = "Microsoft Update Catalog"
    details_url: str = ""
    files: tuple[UpdateFile, ...] = ()
    route: str = "See the Microsoft release notes for installation instructions."


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    name: str
    size: int
    sha256: str
    source_url: str


@dataclass(frozen=True, slots=True)
class DownloadManifest:
    update: Update
    files: tuple[DownloadedFile, ...]
    schema_version: int = 1
