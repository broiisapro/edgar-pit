"""Immutable raw cache for SEC responses (decision A-4).

Layout under ``root``::

    raw/submissions/<cik10>/<fetched_at>.json.zst
    raw/submissions_page/<cik10>/<page-stem>/<fetched_at>.json.zst
    raw/companyfacts/<cik10>/<fetched_at>.json.zst
    raw/headers/<cik10>/<accession>/<fetched_at>.hdr.sgml.zst
    raw/manifest.jsonl

``<fetched_at>`` is UTC ``YYYYMMDDTHHMMSSffffffZ`` (microseconds).  Bodies are
stored zstd-compressed; a file is written to a temp name and linked into place
with an exclusive create, so an existing path is never overwritten.  Every
write appends a manifest row with the SHA-256 and size of the *uncompressed*
body.  Readers take the latest ``<fetched_at>`` per key and accept compressed
(``.zst``) and plain files, which is how the uncompressed fixture tree under
``tests/fixtures/sec`` is served as an offline cache.

``python -m edgarpit.rawcache verify DIR`` prints a per-issuer inventory and
``manifest OK``, or the mismatches and exit code 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard

MANIFEST_NAME = "manifest.jsonl"

ENDPOINT_SUFFIX: dict[str, str] = {
    "submissions": ".json",
    "submissions_page": ".json",
    "companyfacts": ".json",
    "headers": ".hdr.sgml",
}

_STAMP_RE = re.compile(r"\d{8}T\d{6}\d{6}Z")
_ENDPOINTS = tuple(ENDPOINT_SUFFIX)


def utc_stamp(moment: datetime | None = None) -> str:
    """Return a cache file stamp: UTC ``YYYYMMDDTHHMMSSffffffZ``."""
    return (moment or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")


def _decompress(data: bytes) -> bytes:
    return zstandard.ZstdDecompressor().decompress(data)


def _read_body(path: Path) -> bytes:
    data = path.read_bytes()
    if path.name.endswith(".zst"):
        return _decompress(data)
    return data


def _candidate_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name != MANIFEST_NAME and not path.name.endswith(".tmp")
    )


def _stamp_of(name: str, suffix: str) -> str | None:
    stem = name
    if stem.endswith(".zst"):
        stem = stem[: -len(".zst")]
    if not stem.endswith(suffix):
        return None
    stamp = stem[: -len(suffix)]
    return stamp if _STAMP_RE.fullmatch(stamp) else None


def _latest_file(directory: Path, suffix: str) -> Path | None:
    stamped = []
    for path in _candidate_files(directory):
        stamp = _stamp_of(path.name, suffix)
        if stamp is not None:
            stamped.append((stamp, path))
    if not stamped:
        return None
    return max(stamped)[1]


@dataclass
class VerifyReport:
    """Outcome of :meth:`RawCache.verify`."""

    checked: int = 0
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    inventory: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched and not self.unlisted


class RawCache:
    """Immutable, manifest-hashed store for raw SEC response bodies."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / MANIFEST_NAME

    def path_for(self, endpoint: str, cik: str, key: str, stamp: str) -> Path:
        """Return the compressed-cache path for a stamp (always ``.zst``)."""
        suffix = ENDPOINT_SUFFIX.get(endpoint)
        if suffix is None:
            raise ValueError(f"unknown cache endpoint {endpoint!r}")
        parts: list[str] = ["raw", endpoint, cik]
        if key:
            parts.append(key)
        parts.append(f"{stamp}{suffix}.zst")
        return self.root.joinpath(*parts)

    def write(
        self,
        endpoint: str,
        cik: str,
        key: str,
        body: bytes,
        *,
        url: str,
        http_status: int,
    ) -> Path:
        """Store ``body`` under a fresh stamp; never overwrite an existing file."""
        stamp = utc_stamp()
        final = self.path_for(endpoint, cik, key, stamp)
        final.parent.mkdir(parents=True, exist_ok=True)
        compressed = zstandard.ZstdCompressor().compress(body)
        tmp = final.with_name(f"{final.name}.tmp")
        tmp.write_bytes(compressed)
        try:
            os.link(tmp, final)
        except FileExistsError:
            raise FileExistsError(f"refusing to overwrite existing cache file {final}") from None
        finally:
            tmp.unlink(missing_ok=True)
        row = {
            "path": final.relative_to(self.root).as_posix(),
            "url": url,
            "endpoint": endpoint,
            "cik": cik,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "fetched_at": stamp,
            "http_status": http_status,
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return final

    def latest(self, endpoint: str, cik: str, key: str | None = None) -> bytes | None:
        """Return the body stored under the newest stamp for this key, or ``None``."""
        suffix = ENDPOINT_SUFFIX.get(endpoint)
        if suffix is None:
            raise ValueError(f"unknown cache endpoint {endpoint!r}")
        directory = self.raw_dir / endpoint / cik
        if key:
            directory = directory / key
        path = _latest_file(directory, suffix)
        if path is None:
            return None
        return _read_body(path)

    def keys(self, endpoint: str, cik: str) -> list[str]:
        """Return the sorted page/accession keys stored for a CIK and endpoint."""
        directory = self.raw_dir / endpoint / cik
        if not directory.is_dir():
            return []
        return sorted(entry.name for entry in directory.iterdir() if entry.is_dir())

    def manifest_rows(self) -> list[dict[str, Any]]:
        if not self.manifest_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def verify(self) -> VerifyReport:
        """Check every manifest row against the stored files and vice versa."""
        report = VerifyReport()
        if not self.manifest_path.is_file():
            report.missing.append(MANIFEST_NAME)
            return report
        listed: set[str] = set()
        for row in self.manifest_rows():
            report.checked += 1
            relative = row["path"]
            listed.add(relative)
            counts = report.inventory.setdefault(row["cik"], {})
            counts[row["endpoint"]] = counts.get(row["endpoint"], 0) + 1
            path = self.root / relative
            if not path.is_file():
                report.missing.append(relative)
                continue
            body = _read_body(path)
            if hashlib.sha256(body).hexdigest() != row["sha256"] or len(body) != row["bytes"]:
                report.mismatched.append(relative)
        for path in sorted(self.raw_dir.rglob("*")):
            if not path.is_file() or path.name == MANIFEST_NAME or path.name.endswith(".tmp"):
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative not in listed:
                report.unlisted.append(relative)
        return report


def _print_report(report: VerifyReport) -> None:
    for cik in sorted(report.inventory):
        counts = report.inventory[cik]
        total = sum(counts.values())
        detail = ", ".join(
            f"{endpoint}={counts[endpoint]}" for endpoint in _ENDPOINTS if endpoint in counts
        )
        print(f"{cik}: {total} files ({detail})")
    for relative in report.missing:
        print(f"missing: {relative}", file=sys.stderr)
    for relative in report.mismatched:
        print(f"sha256 mismatch: {relative}", file=sys.stderr)
    for relative in report.unlisted:
        print(f"unlisted: {relative}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m edgarpit.rawcache")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="check the manifest against the stored files")
    verify.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify":
        report = RawCache(args.root).verify()
        _print_report(report)
        if report.ok:
            print("manifest OK")
            return 0
        return 1
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":
    raise SystemExit(main())
