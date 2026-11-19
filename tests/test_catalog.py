from pathlib import Path

import pytest

FIXTURES = Path(__file__).with_name("fixtures")


def test_search_keeps_products_distinct() -> None:
    from wuo.catalog import parse_search

    results = parse_search((FIXTURES / "catalog-search.html").read_text())
    assert [
        (result.id, result.architecture, result.classification) for result in results
    ] == [
        ("42ed69d5-18ef-4071-a136-4ffcf105e069", "arm64", "Security Updates"),
        ("950b2422-28ad-4493-a96c-03725fd0d8c2", "x64", "Security Updates"),
    ]
    assert [result.kb for result in results] == ["KB5132061", "KB5132061"]


def test_search_rejects_unrecognized_page() -> None:
    from wuo.catalog import SourceError, parse_search

    with pytest.raises(SourceError, match="search"):
        parse_search("<title>Access denied</title>")
    assert parse_search('<div id="ctl00_catalogBody_noResults"></div>') == []


def test_download_dialog_includes_checkpoint_and_sha256() -> None:
    from wuo.catalog import parse_files

    files = parse_files((FIXTURES / "catalog-download.html").read_text())
    assert [file.name.split("_")[0] for file in files] == [
        "windows11.0-kb5043080-x64",
        "windows11.0-kb5132061-x64",
    ]
    assert (
        files[0].sha256
        == "819632610a101af11c72907fb13ff05b5fb4222c0e263ee9a03bcc1d1d5f26a4"
    )
