"""Convenience sources for the newest cumulative and intelligence updates."""

import re
from typing import cast

import httpx

from . import catalog
from .catalog import SourceError
from .models import Update, UpdateFile

DEFENDER = "https://www.microsoft.com/en-us/wdsi/defenderupdates"
WINDOWS_MEDIA = "https://www.microsoft.com/en-us/software-download/windows11"


def defender(architecture: str) -> Update:
    names = {"x64": "mpam-feX64.exe", "arm64": "mpam-fe.exe", "x86": "mpam-fe.exe"}
    if architecture not in names:
        raise SourceError(
            "Defender intelligence architecture must be x64, arm64, or x86."
        )
    link_arch = "" if architecture == "x86" else f"&arch={architecture}"
    return Update(
        f"defender-{architecture}",
        f"Latest Microsoft Defender security intelligence ({architecture})",
        "Microsoft Defender Antivirus",
        architecture,
        "Definition Updates",
        source="Microsoft Security Intelligence",
        details_url=DEFENDER,
        files=(
            UpdateFile(
                names[architecture],
                f"https://go.microsoft.com/fwlink/?LinkID=121721{link_arch}",
            ),
        ),
        route="Run the Microsoft Defender intelligence installer inside Windows.",
    )


async def latest(client: httpx.AsyncClient, release: str, architecture: str) -> Update:
    if re.fullmatch(r"\d{2}H[12]", release) is None or architecture not in {
        "x64",
        "arm64",
    }:
        raise SourceError(
            "Specify a Windows release such as 25H2 and architecture x64 or arm64."
        )
    try:
        response = await client.get(f"https://wufeed.rybbt.com/win11/{release}.json")
        response.raise_for_status()
        kb = cast(str, response.json()["updates"][architecture]["cu"]["kb"])
        if not isinstance(kb, str) or re.fullmatch(r"KB\d+", kb) is None:
            raise ValueError("invalid KB")
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        raise SourceError(
            "Latest lookup unavailable. Search Microsoft Catalog by KB or title."
        ) from error
    matches = [
        item
        for item in await catalog.search(client, kb)
        if item.kb == kb
        and item.architecture == architecture
        and item.product.casefold() == "windows 11"
        and re.search(rf"\bversion {release}\b", item.title, re.IGNORECASE)
    ]
    if len(matches) != 1:
        raise SourceError(f"Search for {kb} and select the matching Catalog update ID.")
    return await catalog.show(client, matches[0].id)
