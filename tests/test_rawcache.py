"""Tests for the immutable raw cache (decision A-4)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import zstandard

from edgarpit.rawcache import RawCache, document_key, utc_stamp

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
REPO_ROOT = Path(__file__).parent.parent

STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")


def test_utc_stamp_shape() -> None:
    stamp = utc_stamp()
    assert STAMP_RE.fullmatch(stamp)
    assert stamp.endswith("Z")


def test_write_layout_manifest_and_latest(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    body = b'{"hello": "world"}'
    path = cache.write(
        "submissions",
        "0001000275",
        "",
        body,
        url="https://data.sec.gov/submissions/CIK0001000275.json",
        http_status=200,
    )
    assert path.parent == tmp_path / "raw" / "submissions" / "0001000275"
    assert STAMP_RE.fullmatch(path.name.removesuffix(".json.zst"))
    assert path.name.endswith(".json.zst")
    stored = path.read_bytes()
    assert zstandard.ZstdDecompressor().decompress(stored) == body
    rows = cache.manifest_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["path"] == path.relative_to(tmp_path).as_posix()
    assert row["url"] == "https://data.sec.gov/submissions/CIK0001000275.json"
    assert row["endpoint"] == "submissions"
    assert row["cik"] == "0001000275"
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert row["bytes"] == len(body)
    assert row["http_status"] == 200
    assert row["fetched_at"] in path.name
    assert cache.latest("submissions", "0001000275") == body
    assert cache.latest("submissions", "9999999999") is None


def test_latest_prefers_the_newest_stamp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    stamps = iter(["20260101T000000000001Z", "20260101T000000000002Z"])
    monkeypatch.setattr("edgarpit.rawcache.utc_stamp", lambda: next(stamps))
    url = "https://data.sec.gov/submissions/CIK0001000275.json"
    first = cache.write("submissions", "0001000275", "", b"one", url=url, http_status=200)
    second = cache.write("submissions", "0001000275", "", b"two", url=url, http_status=200)
    assert first != second
    assert cache.latest("submissions", "0001000275") == b"two"


def test_write_never_overwrites(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    monkeypatch.setattr("edgarpit.rawcache.utc_stamp", lambda: "20260101T000000000000Z")
    url = "https://data.sec.gov/submissions/CIK0001000275.json"
    path = cache.write("submissions", "0001000275", "", b"first", url=url, http_status=200)
    with pytest.raises(FileExistsError):
        cache.write("submissions", "0001000275", "", b"second", url=url, http_status=200)
    assert cache.latest("submissions", "0001000275") == b"first"
    assert path.read_bytes() != b"second"
    assert not list(path.parent.glob("*.tmp"))
    assert len(cache.manifest_rows()) == 1


def test_keys_and_page_layout(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    url = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"
    cache.write(
        "submissions_page",
        "0000320193",
        "CIK0000320193-submissions-001",
        b"{}",
        url=url,
        http_status=200,
    )
    cache.write(
        "submissions_page",
        "0000320193",
        "CIK0000320193-submissions-000",
        b"{}",
        url=url,
        http_status=200,
    )
    cache.write(
        "headers",
        "0000320193",
        "0000320193-19-000066",
        b"<SEC-HEADER>",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019319000066/0000320193-19-000066.hdr.sgml",
        http_status=200,
    )
    assert cache.keys("submissions_page", "0000320193") == [
        "CIK0000320193-submissions-000",
        "CIK0000320193-submissions-001",
    ]
    assert cache.keys("headers", "0000320193") == ["0000320193-19-000066"]
    assert cache.keys("submissions", "0000320193") == []
    assert cache.latest("submissions_page", "0000320193", "CIK0000320193-submissions-001") == b"{}"
    assert cache.latest("headers", "0000320193", "0000320193-19-000066") == b"<SEC-HEADER>"
    assert cache.latest("headers", "0000320193", "0000320193-99-999999") is None
    with pytest.raises(ValueError, match="unknown cache endpoint"):
        cache.latest("nope", "0000320193")


def test_company_tickers_uses_its_global_slot(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    body = b'{"0": {"cik_str": 320193, "ticker": "AAPL"}}'
    url = "https://www.sec.gov/files/company_tickers.json"
    path = cache.write("company_tickers", "", "", body, url=url, http_status=200)
    assert path.parent == tmp_path / "raw" / "company_tickers" / "_"
    assert path.name.endswith(".json.zst")
    row = cache.manifest_rows()[0]
    assert row["endpoint"] == "company_tickers"
    assert row["cik"] == ""
    assert row["path"].startswith("raw/company_tickers/_/")
    assert cache.latest("company_tickers", "") == body
    assert cache.latest("company_tickers", "_") == body
    assert cache.latest("company_tickers", "0000320193") is None
    report = cache.verify()
    assert report.ok
    assert report.inventory == {"": {"company_tickers": 1}}


def test_latest_reads_uncompressed_fixture_files(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    directory = tmp_path / "raw" / "submissions" / "0001000275"
    directory.mkdir(parents=True)
    (directory / "20260101T000000000000Z.json").write_bytes(b'{"cik": "1000275"}')
    assert cache.latest("submissions", "0001000275") == b'{"cik": "1000275"}'


# --- XBRL frames and companyconcept (decision A-36) --------------------------

FRAME_KEY = "dei__EntityPublicFloat__USD__CY2026Q3I"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/EntityPublicFloat/USD/CY2026Q3I.json"
CONCEPT_KEY = "ifrs-full__Assets"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK0001000275/ifrs-full/Assets.json"


def test_frames_live_in_the_global_slot(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    body = b'{"taxonomy": "dei", "tag": "EntityPublicFloat", "data": []}'
    path = cache.write("frames", "", FRAME_KEY, body, url=FRAME_URL, http_status=200)
    assert path.parent == tmp_path / "raw" / "frames" / "_" / FRAME_KEY
    assert path.name.endswith(".json.zst")
    assert STAMP_RE.fullmatch(path.name.removesuffix(".json.zst"))
    assert cache.latest("frames", "", FRAME_KEY) == body
    assert cache.latest("frames", "_", FRAME_KEY) == body
    assert cache.keys("frames", "") == [FRAME_KEY]
    row = cache.manifest_rows()[0]
    assert row["endpoint"] == "frames"
    assert row["cik"] == ""
    assert row["path"].startswith(f"raw/frames/_/{FRAME_KEY}/")
    assert row["url"] == FRAME_URL


def test_companyconcept_lives_under_the_issuer(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    body = b'{"cik": 1000275, "taxonomy": "ifrs-full", "tag": "Assets", "units": {}}'
    path = cache.write("companyconcept", RY, CONCEPT_KEY, body, url=CONCEPT_URL, http_status=200)
    assert path.parent == tmp_path / "raw" / "companyconcept" / RY / CONCEPT_KEY
    assert path.name.endswith(".json.zst")
    assert cache.latest("companyconcept", RY, CONCEPT_KEY) == body
    assert cache.latest("companyconcept", "0000320193", CONCEPT_KEY) is None
    assert cache.keys("companyconcept", RY) == [CONCEPT_KEY]
    row = cache.manifest_rows()[0]
    assert row["endpoint"] == "companyconcept"
    assert row["cik"] == RY
    assert row["path"].startswith(f"raw/companyconcept/{RY}/{CONCEPT_KEY}/")


def test_verify_covers_the_xbrl_endpoints(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    cache.write("frames", "", FRAME_KEY, b'{"data": []}', url=FRAME_URL, http_status=200)
    cache.write(
        "companyconcept", RY, CONCEPT_KEY, b'{"units": {}}', url=CONCEPT_URL, http_status=200
    )
    report = cache.verify()
    assert report.ok
    assert report.checked == 2
    assert report.inventory == {"": {"frames": 1}, RY: {"companyconcept": 1}}


def test_xbrl_documents_read_uncompressed_fixture_files(tmp_path: Path) -> None:
    # The fixture tree stores the same layout uncompressed (decision A-4).
    cache = RawCache(tmp_path)
    frames = tmp_path / "raw" / "frames" / "_" / FRAME_KEY
    concept = tmp_path / "raw" / "companyconcept" / RY / CONCEPT_KEY
    frames.mkdir(parents=True)
    concept.mkdir(parents=True)
    (frames / "20260101T000000000000Z.json").write_bytes(b'{"data": []}')
    (concept / "20260101T000000000000Z.json").write_bytes(b'{"units": {}}')
    assert cache.latest("frames", "", FRAME_KEY) == b'{"data": []}'
    assert cache.latest("companyconcept", RY, CONCEPT_KEY) == b'{"units": {}}'


def test_verify_reports_bad_rows(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    url = "https://data.sec.gov/submissions/CIK0001000275.json"
    path = cache.write("submissions", "0001000275", "", b"body", url=url, http_status=200)
    report = cache.verify()
    assert report.ok
    assert report.checked == 1
    assert report.inventory == {"0001000275": {"submissions": 1}}

    path.write_bytes(zstandard.ZstdCompressor().compress(b"tampered"))
    report = cache.verify()
    assert not report.ok
    assert report.mismatched == [path.relative_to(tmp_path).as_posix()]
    assert report.missing == []

    path.unlink()
    (path.parent / "20260101T000000000000Z.json").write_bytes(b"extra")
    report = cache.verify()
    assert not report.ok
    assert report.missing == [path.relative_to(tmp_path).as_posix()]
    assert report.unlisted == [
        (path.parent / "20260101T000000000000Z.json").relative_to(tmp_path).as_posix()
    ]


def test_verify_without_manifest_is_not_ok(tmp_path: Path) -> None:
    report = RawCache(tmp_path / "empty").verify()
    assert not report.ok
    assert report.missing == ["manifest.jsonl"]


def test_verify_fixture_manifest() -> None:
    report = RawCache(FIXTURES).verify()
    assert report.ok, (report.missing[:5], report.mismatched[:5], report.unlisted[:5])
    assert report.checked == 499
    assert set(report.inventory) == {"", "0001000275", "0000320193", "0001594805"}
    assert report.inventory[""]["company_tickers"] == 1
    assert report.inventory[""]["fx"] == 1
    assert report.inventory["0000320193"]["filing_index"] == 11
    assert report.inventory["0000320193"]["documents"] == 19


def test_cli_verify_fixtures_and_tampered_cache(tmp_path: Path) -> None:
    good = subprocess.run(
        [sys.executable, "-m", "edgarpit.rawcache", "verify", str(FIXTURES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert good.returncode == 0, good.stderr
    assert "manifest OK" in good.stdout
    assert "0001000275: 210 files" in good.stdout
    assert (
        "0000320193: 109 files (submissions=1, submissions_page=1, companyfacts=1, "
        "headers=76, filing_index=11, documents=19)" in good.stdout
    )
    assert "_: 2 files (company_tickers=1, fx=1)" in good.stdout

    cache = RawCache(tmp_path)
    cache.write(
        "companyfacts",
        "0001000275",
        "",
        b"{}",
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0001000275.json",
        http_status=200,
    )
    for path in (tmp_path / "raw" / "companyfacts" / "0001000275").glob("*.zst"):
        path.write_bytes(zstandard.ZstdCompressor().compress(b"changed"))
    bad = subprocess.run(
        [sys.executable, "-m", "edgarpit.rawcache", "verify", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "sha256 mismatch" in bad.stderr


def test_manifest_is_jsonl(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    url = "https://data.sec.gov/uploads"
    cache.write("companyfacts", "0001000275", "", b"a", url=url, http_status=200)
    cache.write("companyfacts", "0001000275", "", b"b", url=url, http_status=200)
    lines = cache.manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["bytes"] for line in lines] == [1, 1]
    assert cache.latest("companyfacts", "0001000275") == b"b"


# --- Phase 2 endpoints: filing index, documents and FX (decision A-30) -------

RY = "0001000275"
RY_ACCESSION = "0001193125-23-285639"
INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/1000275/000119312523285639/"
    "0001193125-23-285639-index-headers.html"
)
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/1000275/000119312523285639/exhibit99-1.htm"
FX_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"


def test_filing_index_layout_and_latest(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    body = b"<html><body>Document Format Files</body></html>"
    path = cache.write("filing_index", RY, RY_ACCESSION, body, url=INDEX_URL, http_status=200)
    assert path.parent == tmp_path / "raw" / "filing_index" / RY / RY_ACCESSION
    assert path.name.endswith(".html.zst")
    assert STAMP_RE.fullmatch(path.name.removesuffix(".html.zst"))
    assert cache.latest("filing_index", RY, RY_ACCESSION) == body
    assert cache.keys("filing_index", RY) == [RY_ACCESSION]
    assert cache.latest("filing_index", RY, "0001193125-23-000001") is None
    row = cache.manifest_rows()[0]
    assert row["endpoint"] == "filing_index"
    assert row["cik"] == RY
    assert row["url"] == INDEX_URL


def test_document_key_shape_and_validation() -> None:
    assert document_key(RY_ACCESSION, "exhibit99-1.htm") == f"{RY_ACCESSION}__exhibit99-1.htm"
    with pytest.raises(ValueError, match="invalid accession"):
        document_key("0001193125-23-28563", "exhibit99-1.htm")
    with pytest.raises(ValueError, match="invalid document file name"):
        document_key(RY_ACCESSION, "")
    with pytest.raises(ValueError, match="invalid document file name"):
        document_key(RY_ACCESSION, "dir/exhibit.htm")
    with pytest.raises(ValueError, match="has no extension"):
        document_key(RY_ACCESSION, "exhibit")


def test_documents_keep_the_document_extension(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    html_key = document_key(RY_ACCESSION, "exhibit99-1.htm")
    text_key = document_key(RY_ACCESSION, "exhibit99-1.txt")
    html = cache.write(
        "documents", RY, html_key, b"<html>one</html>", url=DOCUMENT_URL, http_status=200
    )
    text = cache.write("documents", RY, text_key, b"plain", url=DOCUMENT_URL, http_status=200)
    assert html.parent == tmp_path / "raw" / "documents" / RY / html_key
    assert html.name.endswith(".htm.zst")
    assert text.name.endswith(".txt.zst")
    assert cache.latest("documents", RY, html_key) == b"<html>one</html>"
    assert cache.latest("documents", RY, text_key) == b"plain"
    assert cache.keys("documents", RY) == sorted([html_key, text_key])


def test_documents_latest_ignores_files_of_another_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A later plain file with the same stem but a different extension must not
    # shadow the document the key names.
    cache = RawCache(tmp_path)
    key = document_key(RY_ACCESSION, "exhibit99-1.htm")
    stamps = iter(["20260101T000000000001Z", "20260101T000000000002Z"])
    monkeypatch.setattr("edgarpit.rawcache.utc_stamp", lambda: next(stamps))
    cache.write("documents", RY, key, b"wanted", url=DOCUMENT_URL, http_status=200)
    directory = tmp_path / "raw" / "documents" / RY / key
    (directory / "20260101T000000000002Z.txt").write_bytes(b"stale")
    assert cache.latest("documents", RY, key) == b"wanted"


def test_documents_latest_needs_a_key_with_an_extension(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    with pytest.raises(ValueError, match="needs a key with a file extension"):
        cache.latest("documents", RY, RY_ACCESSION)
    with pytest.raises(ValueError, match="needs a key with a file extension"):
        cache.path_for("documents", RY, RY_ACCESSION, "20260101T000000000000Z")


def test_fx_uses_the_global_slot_and_verify_counts_it(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    body = b'{"observations": [{"d": "2023-10-31", "FXUSDCAD": {"v": "1.3659"}}]}'
    path = cache.write("fx", "", "FXUSDCAD", body, url=FX_URL, http_status=200)
    assert path.parent == tmp_path / "raw" / "fx" / "_" / "FXUSDCAD"
    assert path.name.endswith(".json.zst")
    assert cache.latest("fx", "", "FXUSDCAD") == body
    assert cache.latest("fx", "_", "FXUSDCAD") == body
    assert cache.latest("fx", "0001000275", "FXUSDCAD") is None
    assert cache.keys("fx", "") == ["FXUSDCAD"]
    row = cache.manifest_rows()[0]
    assert row["endpoint"] == "fx"
    assert row["cik"] == ""
    assert row["path"].startswith("raw/fx/_/FXUSDCAD/")
    report = cache.verify()
    assert report.ok
    assert report.inventory == {"": {"fx": 1}}


def test_verify_covers_the_phase2_endpoints(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    cache.write("filing_index", RY, RY_ACCESSION, b"<html/>", url=INDEX_URL, http_status=200)
    cache.write(
        "documents",
        RY,
        document_key(RY_ACCESSION, "exhibit99-1.htm"),
        b"<html>exhibit</html>",
        url=DOCUMENT_URL,
        http_status=200,
    )
    cache.write("fx", "", "FXUSDCAD", b"{}", url=FX_URL, http_status=200)
    report = cache.verify()
    assert report.ok
    assert report.checked == 3
    assert report.inventory[RY] == {"filing_index": 1, "documents": 1}
    assert report.inventory[""] == {"fx": 1}


def test_phase2_documents_read_uncompressed_fixture_files(tmp_path: Path) -> None:
    # The fixture tree stores the same layout uncompressed (decision A-4).
    cache = RawCache(tmp_path)
    key = document_key(RY_ACCESSION, "exhibit99-1.htm")
    directory = tmp_path / "raw" / "documents" / RY / key
    directory.mkdir(parents=True)
    (directory / "20260101T000000000000Z.htm").write_bytes(b"<html>fixture</html>")
    assert cache.latest("documents", RY, key) == b"<html>fixture</html>"
