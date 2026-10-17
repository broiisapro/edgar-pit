"""Canonical field definitions: load and query ``config/concept_map.yaml``.

The concept map is the only place a canonical field name is defined
(decision S-8). Each field carries its ``kind`` (duration or instant), its
``unit_family`` and ordered concept lists per taxonomy; rank 1 is the preferred
concept.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

DEFAULT_CONCEPT_MAP_PATH = Path(__file__).resolve().parents[1] / "config" / "concept_map.yaml"


class ConceptMapError(ValueError):
    """The concept map is missing, malformed or ambiguous."""


class ConceptSpec(BaseModel):
    """One canonical field's definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    kind: Literal["duration", "instant"]
    unit_family: Literal["currency", "shares", "currency_per_share"]
    us_gaap: list[str] = Field(validation_alias=AliasChoices("us-gaap", "us_gaap"))
    ifrs_full: list[str] = Field(validation_alias=AliasChoices("ifrs-full", "ifrs_full"))


class ConceptMap:
    """The canonical field map, indexed by ``(taxonomy, concept)``.

    A ``(taxonomy, concept)`` pair maps to exactly one field; a concept that
    appears under two fields (or twice under one) is a load error.
    """

    def __init__(self, specs: Sequence[ConceptSpec]) -> None:
        if not specs:
            raise ConceptMapError("concept map defines no fields")
        index: dict[tuple[str, str], tuple[str, int]] = {}
        by_field: dict[str, ConceptSpec] = {}
        for spec in specs:
            if spec.field in by_field:
                raise ConceptMapError(f"field {spec.field!r} is defined twice")
            if not spec.us_gaap and not spec.ifrs_full:
                raise ConceptMapError(f"field {spec.field!r} lists no concepts")
            by_field[spec.field] = spec
            for taxonomy, concepts in (("us-gaap", spec.us_gaap), ("ifrs-full", spec.ifrs_full)):
                for rank, concept in enumerate(concepts, start=1):
                    key = (taxonomy, concept)
                    if key in index:
                        raise ConceptMapError(
                            f"concept {taxonomy}:{concept} is mapped more than once "
                            f"({index[key][0]} and {spec.field})"
                        )
                    index[key] = (spec.field, rank)
        self._index = index
        self._by_field = by_field

    @property
    def fields(self) -> list[str]:
        """Canonical field names in the order they appear in the file."""
        return list(self._by_field)

    def lookup(self, taxonomy: str, concept: str) -> tuple[str, int] | None:
        """Map ``(taxonomy, concept)`` to ``(field, rank)`` or ``None``.

        ``rank`` is 1-based: 1 is the field's preferred concept.
        """
        return self._index.get((taxonomy, concept))


def load_concept_map(path: Path | None = None) -> ConceptMap:
    """Load the concept map from ``path`` (default ``config/concept_map.yaml``).

    Raises :class:`ConceptMapError` when the file cannot be read, is not a
    ``fields`` mapping, or contains a field definition that is invalid,
    duplicated or maps a concept twice.
    """
    source = DEFAULT_CONCEPT_MAP_PATH if path is None else Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConceptMapError(f"cannot read concept map {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConceptMapError(f"invalid YAML in concept map {source}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise ConceptMapError(f"concept map {source} must contain a top-level 'fields' mapping")
    specs: list[ConceptSpec] = []
    for name, definition in raw["fields"].items():
        if not isinstance(definition, dict):
            raise ConceptMapError(f"field {name!r} must be a mapping of its attributes")
        try:
            specs.append(ConceptSpec.model_validate({"field": name, **definition}))
        except ValidationError as exc:
            raise ConceptMapError(f"invalid definition for field {name!r}: {exc}") from exc
    return ConceptMap(specs)
