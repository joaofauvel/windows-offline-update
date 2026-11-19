"""Adapter for the Microsoft Update Catalog's public pages."""

import base64
import binascii
import json
import re
from dataclasses import replace
from uuid import UUID

import httpx
from bs4 import BeautifulSoup

from .models import Update, UpdateFile

CATALOG = "https://www.catalog.update.microsoft.com"
UPDATE_LINK = re.compile(r"^([a-f0-9-]{36})_link$", re.IGNORECASE)
FILE_FIELD = re.compile(
    r"^\s*downloadInformation\[0\]\.files\[(\d+)\]\."
    r"(url|fileName|digest|sha256)\s*=\s*'([^'\r\n]*)';",
    re.MULTILINE,
)


class SourceError(ValueError):
    """A provider returned an unavailable or unrecognized result."""


def _update(
    update_id: str, title: str, product: str, kind: str, date: str = ""
) -> Update:
    arch = re.search(r"\b(arm64|x64|x86)(?:\b|_)", title, re.IGNORECASE)
    kb = re.search(r"\bKB\d+\b", title, re.IGNORECASE)
    route = "See the Microsoft release notes for installation instructions."
    if kind.casefold() == "drivers":
        route = "Extract the driver package and identify its signed INF and matching hardware."
    elif kind.casefold() == "upgrades":
        route = (
            "Use the supported Windows Setup or enablement workflow for this update."
        )
    elif "defender" in title.casefold() or "security platform" in title.casefold():
        route = (
            "Follow the Microsoft instructions for this security component in Windows."
        )
    return Update(
        update_id,
        title,
        product,
        arch.group(1).lower() if arch else None,
        kind,
        date,
        kb.group().upper() if kb else None,
        details_url=f"{CATALOG}/ScopedViewInline.aspx?updateid={update_id}",
        route=route,
    )


def parse_search(html: str) -> list[Update]:
    page = BeautifulSoup(html, "html.parser")
    results = []
    for link in page.find_all("a", id=UPDATE_LINK):
        match = UPDATE_LINK.fullmatch(str(link["id"]))
        row = link.find_parent("tr")
        if match is None or row is None:
            raise SourceError("Microsoft Catalog search result is missing its row.")
        columns = row.find_all("td", recursive=False)
        if len(columns) < 5:
            raise SourceError("Microsoft Catalog search columns changed.")
        product, kind, date = (cell.get_text(strip=True) for cell in columns[2:5])
        results.append(
            _update(match.group(1), link.get_text(strip=True), product, kind, date)
        )
    if not results and page.find(id="ctl00_catalogBody_noResults") is None:
        raise SourceError("Could not interpret the Microsoft Catalog search response.")
    return results


def _decode_digest(value: str, expected_length: int) -> str | None:
    if not value:
        return None
    try:
        digest = base64.b64decode(value, validate=True)
    except binascii.Error as error:
        raise SourceError("Microsoft Catalog supplied an invalid digest.") from error
    if len(digest) != expected_length:
        raise SourceError(
            "Microsoft Catalog supplied a digest with an unexpected length."
        )
    return digest.hex()


def parse_files(html: str) -> tuple[UpdateFile, ...]:
    fields: dict[int, dict[str, str]] = {}
    for index, name, value in FILE_FIELD.findall(html):
        record = fields.setdefault(int(index), {})
        if name in record:
            raise SourceError("Microsoft Catalog listed the same file field twice.")
        record[name] = value
    if not fields or sorted(fields) != list(range(len(fields))):
        raise SourceError("Could not resolve the complete Microsoft Catalog file list.")
    files = []
    for index in range(len(fields)):
        record = fields[index]
        if not record.get("url") or not record.get("fileName"):
            raise SourceError("Microsoft Catalog file entry is incomplete.")
        files.append(
            UpdateFile(
                record["fileName"],
                record["url"],
                _decode_digest(record.get("sha256", ""), 32),
                _decode_digest(record.get("digest", ""), 20),
            )
        )
    return tuple(files)


async def search(client: httpx.AsyncClient, query: str) -> list[Update]:
    response = await client.get(f"{CATALOG}/Search.aspx", params={"q": query})
    response.raise_for_status()
    return parse_search(response.text)


async def show(client: httpx.AsyncClient, update_id: str) -> Update:
    try:
        update_id = str(UUID(update_id))
    except ValueError as error:
        raise SourceError(
            "Use a Microsoft Catalog update ID from the search results."
        ) from error
    response = await client.get(
        f"{CATALOG}/ScopedViewInline.aspx", params={"updateid": update_id}
    )
    if response.status_code == 404 or (
        response.is_redirect
        and response.headers.get("Location", "").startswith("/Thanks.aspx?")
    ):
        raise SourceError(f"No public Microsoft Catalog entry for offer {update_id}.")
    response.raise_for_status()
    page = BeautifulSoup(response.text, "html.parser")
    title = page.find(id="ScopedViewHandler_titleText")
    if title is None or not title.get_text(strip=True):
        raise SourceError(f"Microsoft Catalog has no details for {update_id}.")

    def field(name: str, label: str) -> str:
        node = page.find(id=name)
        return (
            node.get_text(" ", strip=True).removeprefix(label).strip() if node else ""
        )

    update = _update(
        update_id,
        title.get_text(" ", strip=True),
        field("productsDiv", "Supported products:"),
        field("classificationDiv", "Classification:"),
    )
    architecture = field("archDiv", "Architecture:").casefold()
    declared = {
        "amd64": "x64",
        "x64": "x64",
        "arm64": "arm64",
        "x86": "x86",
        "arm": "arm",
    }.get(architecture)
    if declared:
        update = replace(update, architecture=declared)
    request = [{"size": 0, "updateID": update_id, "uidInfo": update_id}]
    dialog = await client.post(
        f"{CATALOG}/DownloadDialog.aspx", data={"updateIDs": json.dumps(request)}
    )
    dialog.raise_for_status()
    return replace(update, files=parse_files(dialog.text))
