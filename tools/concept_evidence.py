#!/usr/bin/env python3
"""Collect concept-coverage evidence for ``config/concept_map.yaml``.

For every issuer in ``config/universe.csv`` this tool downloads the SEC
companyfacts document through :class:`edgarpit.sec_client.SecClient` (the same
rate limiter, User-Agent validation and raw cache the pipeline uses) and
writes a report of:

- every candidate concept of every canonical field, with the values carried
  by the issuer's annual filings (10-K/40-F/20-F and amendments) for the
  issuer's last three fiscal years,
- which candidate the current ranks pick in each annual accession, and
- the largest concepts that map to no field, to surface coverage gaps.

The report is evidence for the ranks in ``config/concept_map.yaml``.  It is a
tool, not part of the pipeline, and never runs in CI.  The cache directory is
given with ``--cache`` and is never committed.

Usage::

    export SEC_USER_AGENT='Name email@example.com'
    uv run python tools/concept_evidence.py --cache /tmp/ep-b/evidence
    uv run python tools/concept_evidence.py --cache /tmp/ep-b/evidence --offline

``--offline`` serves the same documents from the cache instead of the
network, so re-running after a ranks change produces the "after" table
without new requests.  The report pair defaults to
``<cache>/concept-evidence.md`` and ``<cache>/concept-evidence.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from edgarpit.concepts import load_concept_map
from edgarpit.config import ConfigError, Settings
from edgarpit.rawcache import RawCache
from edgarpit.sec_client import SecClient, companyfacts_url

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.csv"
CONCEPT_MAP_PATH = REPO_ROOT / "config" / "concept_map.yaml"

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "40-F", "40-F/A", "20-F", "20-F/A"})
KEPT_TAXONOMIES = ("us-gaap", "ifrs-full")
FY_COUNT = 3
DURATION_MIN_DAYS = 350
DURATION_MAX_DAYS = 380
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

REPORT_MD_NAME = "concept-evidence.md"
REPORT_JSON_NAME = "concept-evidence.json"


class ConceptEvidenceError(RuntimeError):
    """The evidence report cannot be built."""


def find_user_agent() -> str:
    """Return ``SEC_USER_AGENT`` from the environment or a repository ``.env``.

    In a worktree the ``.env`` lives at the main checkout, so the search walks
    up from the repository root and takes the first ``.env`` that defines the
    key.
    """
    value = os.environ.get("SEC_USER_AGENT", "")
    if value:
        return value
    for directory in (REPO_ROOT, *REPO_ROOT.parents):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SEC_USER_AGENT="):
                value = line.partition("=")[2].strip().strip("'\"")
                if value:
                    return value
    return ""


def validate_user_agent(value: str) -> str:
    """Return the agent when it is "Name email", else raise."""
    agent = value.strip()
    tokens = agent.split()
    if len(tokens) < 2 or not _EMAIL_RE.fullmatch(tokens[-1]):
        raise ConceptEvidenceError(
            "SEC_USER_AGENT must look like 'Name email' (for example "
            "'Jane Doe jane@example.com'); set it in the environment or in .env. "
            f"Got: {value!r}"
        )
    return agent


def read_universe(path: Path) -> list[dict[str, str]]:
    """Read ``config/universe.csv`` into ticker/cik/name/country/filer_type rows."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ConceptEvidenceError(f"universe {path} is empty")
    return rows


def unit_family(unit: str) -> str | None:
    """Classify a companyfacts unit key: currency, shares, currency_per_share."""
    if unit.endswith("/shares"):
        return "currency_per_share"
    if unit == "shares":
        return "shares"
    if _CURRENCY_RE.fullmatch(unit):
        return "currency"
    return None


@dataclass(frozen=True)
class AnnualFact:
    """One fact entry carried by an annual filing."""

    taxonomy: str
    concept: str
    unit: str
    value: float
    start: str | None
    end: str
    fy: int
    form: str
    accession: str

    @property
    def kind(self) -> str:
        return "duration" if self.start is not None else "instant"

    @property
    def duration_days(self) -> int | None:
        if self.start is None:
            return None
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days


def iter_annual_facts(document: dict[str, Any]) -> list[AnnualFact]:
    """Extract the kept-taxonomy entries of annual filings from a document."""
    facts = document.get("facts")
    if not isinstance(facts, dict):
        raise ConceptEvidenceError("companyfacts document has no 'facts' object")
    found: list[AnnualFact] = []
    for taxonomy in KEPT_TAXONOMIES:
        concepts = facts.get(taxonomy)
        if not isinstance(concepts, dict):
            continue
        for concept, entry in concepts.items():
            units = entry.get("units") if isinstance(entry, dict) else None
            if not isinstance(units, dict):
                continue
            for unit, rows in units.items():
                if not isinstance(unit, str) or not isinstance(rows, list):
                    continue
                for row in rows:
                    form = row.get("form")
                    if form not in ANNUAL_FORMS:
                        continue
                    value = row.get("val")
                    start = row.get("start")
                    end = row.get("end")
                    fy = row.get("fy")
                    accession = row.get("accn")
                    if (
                        not isinstance(value, (int, float))
                        or not isinstance(end, str)
                        or not isinstance(fy, int)
                        or not isinstance(accession, str)
                    ):
                        continue
                    found.append(
                        AnnualFact(
                            taxonomy=taxonomy,
                            concept=concept,
                            unit=unit,
                            value=float(value),
                            start=start if isinstance(start, str) else None,
                            end=end,
                            fy=fy,
                            form=form,
                            accession=accession,
                        )
                    )
    return found


@dataclass(frozen=True)
class FieldSpec:
    """The kind and unit family of one canonical field."""

    field: str
    kind: str
    unit_family: str


def read_field_specs(path: Path) -> dict[str, FieldSpec]:
    """Read ``kind`` and ``unit_family`` per field from the concept map YAML.

    :func:`edgarpit.concepts.load_concept_map` deliberately exposes only field
    names and ``(taxonomy, concept)`` lookups, so the tool reads the two
    filtering attributes directly from the same file.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    fields = raw.get("fields") if isinstance(raw, dict) else None
    if not isinstance(fields, dict):
        raise ConceptEvidenceError(f"{path} has no 'fields' mapping")
    specs: dict[str, FieldSpec] = {}
    for name, definition in fields.items():
        if not isinstance(definition, dict):
            raise ConceptEvidenceError(f"field {name!r} is not a mapping")
        specs[name] = FieldSpec(
            field=name,
            kind=str(definition.get("kind")),
            unit_family=str(definition.get("unit_family")),
        )
    return specs


def matches_field(fact: AnnualFact, spec: FieldSpec, mapping: MappingIndex) -> bool:
    """True when a fact maps to the field and has the shape the field promises."""
    mapped = mapping.get((fact.taxonomy, fact.concept))
    if mapped is None or mapped[0] != spec.field:
        return False
    if fact.kind != spec.kind:
        return False
    if unit_family(fact.unit) != spec.unit_family:
        return False
    if spec.kind == "duration":
        days = fact.duration_days
        if days is None or not DURATION_MIN_DAYS <= days <= DURATION_MAX_DAYS:
            return False
    return True


MappingIndex = dict[tuple[str, str], tuple[str, int]]


def pick_per_accession(
    facts: list[AnnualFact], spec: FieldSpec, mapping: MappingIndex
) -> dict[str, str]:
    """Map each annual accession to the lowest-rank candidate concept in it."""
    picks: dict[str, str] = {}
    picked_rank: dict[str, int] = {}
    for fact in facts:
        if not matches_field(fact, spec, mapping):
            continue
        rank = mapping[(fact.taxonomy, fact.concept)][1]
        if fact.accession not in picks or rank < picked_rank[fact.accession]:
            picks[fact.accession] = f"{fact.taxonomy}:{fact.concept}"
            picked_rank[fact.accession] = rank
    return picks


def latest_fy_row(
    facts: list[AnnualFact], spec: FieldSpec, fy: int, mapping: MappingIndex
) -> dict[tuple[str, str], AnnualFact] | None:
    """The latest-period candidate row per concept for one fiscal year."""
    candidates = [fact for fact in facts if fact.fy == fy and matches_field(fact, spec, mapping)]
    if not candidates:
        return None
    latest_end = max(fact.end for fact in candidates)
    chosen: dict[tuple[str, str], AnnualFact] = {}
    for fact in candidates:
        if fact.end != latest_end:
            continue
        key = (fact.taxonomy, fact.concept)
        current = chosen.get(key)
        if (
            current is None
            or fact.value > current.value
            or (fact.value == current.value and fact.accession > current.accession)
        ):
            chosen[key] = fact
    return chosen or None


def unmapped_large(
    facts: list[AnnualFact], fys: list[int], mapping: MappingIndex, top: int
) -> list[dict[str, Any]]:
    """Largest unmapped currency duration/instant concepts in the latest fiscal year."""
    if not fys:
        return []
    latest = fys[0]
    candidates = [
        fact
        for fact in facts
        if fact.fy == latest
        and mapping.get((fact.taxonomy, fact.concept)) is None
        and unit_family(fact.unit) == "currency"
        and (
            (
                fact.kind == "duration"
                and fact.duration_days is not None
                and DURATION_MIN_DAYS <= fact.duration_days <= DURATION_MAX_DAYS
            )
            or fact.kind == "instant"
        )
    ]
    best: dict[tuple[str, str], AnnualFact] = {}
    for fact in candidates:
        key = (fact.taxonomy, fact.concept)
        current = best.get(key)
        if current is None or abs(fact.value) > abs(current.value):
            best[key] = fact
    ordered = sorted(best.values(), key=lambda fact: abs(fact.value), reverse=True)[:top]
    return [
        {
            "concept": f"{fact.taxonomy}:{fact.concept}",
            "kind": fact.kind,
            "unit": fact.unit,
            "value": fact.value,
            "period_start": fact.start,
            "period_end": fact.end,
            "accession": fact.accession,
        }
        for fact in ordered
    ]


@dataclass
class IssuerEvidence:
    """Everything the report needs about one issuer."""

    ticker: str
    cik: str
    name: str
    country: str
    filer_type: str
    fys: list[int]
    facts: list[AnnualFact]
    fields: dict[str, dict[str, Any]]
    unmapped: list[dict[str, Any]]
    latest_accession: str | None
    error: str | None = None


def build_issuer(
    row: dict[str, str],
    document: dict[str, Any],
    specs: dict[str, FieldSpec],
    mapping: MappingIndex,
    top_unmapped: int,
) -> IssuerEvidence:
    """Extract the per-field evidence for one issuer document."""
    facts = iter_annual_facts(document)
    fys = sorted({fact.fy for fact in facts}, reverse=True)[:FY_COUNT]
    fields: dict[str, dict[str, Any]] = {}
    for name, spec in specs.items():
        candidates: list[dict[str, Any]] = []
        for fy in fys:
            chosen = latest_fy_row(facts, spec, fy, mapping)
            if chosen is None:
                continue
            for (taxonomy, concept), fact in sorted(
                chosen.items(), key=lambda item: (item[1].end, item[0][0], item[0][1])
            ):
                candidates.append(
                    {
                        "fy": fy,
                        "taxonomy": taxonomy,
                        "concept": concept,
                        "rank": mapping[(taxonomy, concept)][1],
                        "unit": fact.unit,
                        "value": fact.value,
                        "period_start": fact.start,
                        "period_end": fact.end,
                        "accession": fact.accession,
                    }
                )
        fields[name] = {
            "candidates": candidates,
            "picks": pick_per_accession(facts, spec, mapping),
        }
    latest_accession: str | None = None
    if facts:
        newest = max(facts, key=lambda fact: (fact.fy, fact.end, fact.accession))
        latest_accession = newest.accession
    return IssuerEvidence(
        ticker=row["ticker"],
        cik=row["cik"].zfill(10),
        name=row["name"],
        country=row["country"],
        filer_type=row["filer_type"],
        fys=fys,
        facts=facts,
        fields=fields,
        unmapped=unmapped_large(facts, fys, mapping, top_unmapped),
        latest_accession=latest_accession,
    )


def mapping_index(concept_map_path: Path, specs: dict[str, FieldSpec]) -> MappingIndex:
    """``(taxonomy, concept) -> (field, rank)`` for every mapped candidate.

    The map is read from the YAML so the tool also sees the field lists;
    validation stays in :func:`edgarpit.concepts.load_concept_map`, which this
    function calls first.
    """
    load_concept_map(concept_map_path)
    raw = yaml.safe_load(concept_map_path.read_text(encoding="utf-8"))
    fields = raw["fields"]
    mapping: MappingIndex = {}
    for name in specs:
        definition = fields[name]
        for taxonomy, key in (("us-gaap", "us-gaap"), ("ifrs-full", "ifrs-full")):
            concepts = definition.get(key)
            if concepts is None:
                concepts = definition.get(key.replace("-", "_"), [])
            for rank, concept in enumerate(concepts, start=1):
                mapping[(taxonomy, concept)] = (name, rank)
    return mapping


def collect(
    universe_path: Path,
    concept_map_path: Path,
    cache_dir: Path,
    *,
    offline: bool,
    limit: int | None,
    top_unmapped: int,
) -> list[IssuerEvidence]:
    """Fetch (or read from cache) every issuer and build its evidence."""
    rows = read_universe(universe_path)
    specs = read_field_specs(concept_map_path)
    mapping = mapping_index(concept_map_path, specs)
    results: list[IssuerEvidence] = []
    if offline:
        client = SecClient(Settings(), offline_dir=cache_dir)
    else:
        client = SecClient(
            Settings(sec_user_agent=validate_user_agent(find_user_agent())),
            cache=RawCache(cache_dir),
        )
    try:
        for index, row in enumerate(rows):
            if limit is not None and index >= limit:
                break
            cik = row["cik"].zfill(10)
            print(f"[{cik}] {row['ticker']} {row['name']}", file=sys.stderr, flush=True)
            try:
                document = client.get_json(companyfacts_url(cik))
            except Exception as error:
                print(f"[{cik}] FETCH FAILED: {type(error).__name__}: {error}", file=sys.stderr)
                results.append(
                    IssuerEvidence(
                        ticker=row["ticker"],
                        cik=cik,
                        name=row["name"],
                        country=row["country"],
                        filer_type=row["filer_type"],
                        fys=[],
                        facts=[],
                        fields={},
                        unmapped=[],
                        latest_accession=None,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
                continue
            results.append(build_issuer(row, document, specs, mapping, top_unmapped))
    finally:
        client.close()
    return results


def short(concept: str) -> str:
    """Strip the taxonomy prefix from a concept for the summary table."""
    return concept.split(":", 1)[1] if ":" in concept else concept


def latest_fy_pick(evidence: dict[str, Any] | None, fy: int | None) -> str | None:
    """The lowest-rank candidate in the issuer's latest fiscal year, or None."""
    if not evidence or fy is None:
        return None
    candidates = [row for row in evidence["candidates"] if row["fy"] == fy]
    if not candidates:
        return None
    best = min(candidates, key=lambda row: (row["rank"] or 999, row["concept"]))
    return f"{best['taxonomy']}:{best['concept']}"


def format_value(value: float) -> str:
    """Format a large money/shares value exactly, without float noise."""
    return str(int(value)) if float(value).is_integer() else f"{value:.4f}"


def render_markdown(
    results: list[IssuerEvidence], specs: dict[str, FieldSpec], rank_count: int
) -> str:
    """Render the full evidence report as Markdown."""
    lines: list[str] = []
    lines.append("# Concept evidence report")
    lines.append("")
    lines.append(
        "Annual filings (10-K/40-F/20-F and amendments), last three fiscal "
        "years as tagged (`fy`), one row per concept and fiscal year. "
        f"{rank_count} mapped `(taxonomy, concept)` pairs."
    )
    lines.append("")
    lines.append("## Coverage summary")
    lines.append("")
    lines.append("Picked concept in the issuer's latest fiscal year with annual facts:")
    lines.append("")
    header = "| field | " + " | ".join(issuer.ticker for issuer in results) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(results) + 1))
    for name in specs:
        cells: list[str] = []
        for issuer in results:
            if issuer.error is not None:
                cells.append("error")
                continue
            fy = issuer.fys[0] if issuer.fys else None
            pick = latest_fy_pick(issuer.fields.get(name), fy)
            cells.append(short(pick) if pick else "none")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")
    errors = [issuer for issuer in results if issuer.error is not None]
    if errors:
        lines.append("Issuers that could not be read:")
        lines.append("")
        for issuer in errors:
            lines.append(f"- {issuer.cik} {issuer.ticker}: {issuer.error}")
        lines.append("")
    for name, spec in specs.items():
        lines.append(f"## {name} ({spec.kind}, {spec.unit_family})")
        lines.append("")
        for issuer in results:
            lines.append(f"### {issuer.cik} {issuer.ticker} — {issuer.name}")
            lines.append("")
            if issuer.error is not None:
                lines.append(f"error: {issuer.error}")
                lines.append("")
                continue
            evidence = issuer.fields.get(name)
            candidates = evidence["candidates"] if evidence else []
            fys = issuer.fys
            lines.append(f"fiscal years: {', '.join(f'FY{fy}' for fy in fys) or 'none'}")
            lines.append("")
            if candidates:
                lines.append("| FY | concept (rank) | unit | value | period end | accession |")
                lines.append("|---|---|---|---|---|---|")
                for candidate in candidates:
                    rank = candidate["rank"]
                    label = (
                        f"{short(candidate['concept'])} (rank {rank})"
                        if rank
                        else (f"{short(candidate['concept'])} (unmapped)")
                    )
                    lines.append(
                        f"| FY{candidate['fy']} | {label} | {candidate['unit']} | "
                        f"{format_value(candidate['value'])} | {candidate['period_end']} | "
                        f"{candidate['accession']} |"
                    )
            else:
                lines.append("no candidate concept reported")
            lines.append("")
            picks = evidence["picks"] if evidence else {}
            if picks:
                lines.append("picked per accession:")
                lines.append("")
                for accession in sorted(picks):
                    lines.append(f"- {accession} -> {picks[accession]}")
                lines.append("")
        lines.append("")
    lines.append("## Largest unmapped concepts in the latest fiscal year")
    lines.append("")
    for issuer in results:
        if issuer.error is not None:
            continue
        lines.append(f"### {issuer.cik} {issuer.ticker}")
        lines.append("")
        if not issuer.unmapped:
            lines.append("none")
            lines.append("")
            continue
        lines.append("| concept | kind | unit | value | period | accession |")
        lines.append("|---|---|---|---|---|---|")
        for entry in issuer.unmapped:
            start = entry["period_start"] or ""
            lines.append(
                f"| {entry['concept']} | {entry['kind']} | {entry['unit']} | "
                f"{format_value(entry['value'])} | {start}..{entry['period_end']} | "
                f"{entry['accession']} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_json(
    results: list[IssuerEvidence], specs: dict[str, FieldSpec], cache_dir: Path, offline: bool
) -> str:
    """Render the evidence as machine-readable JSON (same content as the Markdown)."""
    payload: dict[str, Any] = {
        "cache": str(cache_dir),
        "offline": offline,
        "fields": {
            name: {"kind": spec.kind, "unit_family": spec.unit_family}
            for name, spec in specs.items()
        },
        "issuers": [
            {
                "ticker": issuer.ticker,
                "cik": issuer.cik,
                "name": issuer.name,
                "country": issuer.country,
                "filer_type": issuer.filer_type,
                "fiscal_years": issuer.fys,
                "latest_accession": issuer.latest_accession,
                "error": issuer.error,
                "fields": issuer.fields,
                "unmapped": issuer.unmapped,
            }
            for issuer in results
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect concept-coverage evidence from issuer companyfacts."
    )
    parser.add_argument(
        "--cache",
        type=Path,
        required=True,
        help="directory for the raw companyfacts cache (or to read with --offline)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="report directory (default: the cache directory)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="read the cache instead of the network (no User-Agent needed)",
    )
    parser.add_argument("--limit", type=int, default=None, help="first N issuers only")
    parser.add_argument(
        "--top-unmapped", type=int, default=12, help="unmapped concepts per issuer (default 12)"
    )
    parser.add_argument("--universe", type=Path, default=UNIVERSE_PATH)
    parser.add_argument("--concept-map", type=Path, default=CONCEPT_MAP_PATH)
    args = parser.parse_args(argv)
    report_dir = args.cache if args.report is None else args.report
    try:
        results = collect(
            args.universe,
            args.concept_map,
            args.cache,
            offline=args.offline,
            limit=args.limit,
            top_unmapped=args.top_unmapped,
        )
    except (ConfigError, ConceptEvidenceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    specs = read_field_specs(args.concept_map)
    mapping = mapping_index(args.concept_map, specs)
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / REPORT_MD_NAME
    json_path = report_dir / REPORT_JSON_NAME
    md_path.write_text(render_markdown(results, specs, len(mapping)), encoding="utf-8")
    json_path.write_text(render_json(results, specs, args.cache, args.offline), encoding="utf-8")
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
