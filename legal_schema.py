"""Canonical schema-v3 records for the AgreementAtlas legal graph.

The JSON graph shown by the browser is a projection of these records.  It is
deliberately not the system of record: each legal conclusion points back to one
or more exact evidence spans in an uploaded instrument.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "3.0"
SPACE = re.compile(r"\s+")

# Section ids the parser invented rather than read. Documents whose headings
# are typographic (bold letters, no numbers) still need distinct section keys,
# so the parser counts headings -- but a counter is bookkeeping, not a citation,
# and showing "Section 29" for a schedule that never prints a 29 claims
# something the document does not say. Synthesised ids carry this prefix so
# every consumer can tell them from numbers the document actually prints;
# stable ids are always built from the unprefixed value, so clause identity
# (and therefore carried enrichment) is unaffected.
SYNTHETIC_SECTION_PREFIX = "~"


def printed_section_id(value: str) -> str:
    """The section id as the document prints it, or "" if the parser made it up."""

    text = str(value or "").strip()
    if text.startswith(SYNTHETIC_SECTION_PREFIX):
        return ""
    return text


def normalise_text(value: str) -> str:
    return SPACE.sub(" ", value.replace("\u200b", "")).strip()


def stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(normalise_text(str(part)).lower() for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def record(value: object) -> dict[str, Any]:
    return asdict(value)


@dataclass
class AgreementFamily:
    id: str
    title: str
    instrument_ids: list[str]
    candidate: bool = True
    schema_version: str = SCHEMA_VERSION


@dataclass
class Instrument:
    id: str
    family_id: str
    source: str
    title: str
    instrument_class: str
    instrument_type: str
    version: str
    effective_date: str
    signature_date: str
    term_start: str
    term_end: str
    sha256: str
    title_evidence: str
    schema_version: str = SCHEMA_VERSION

    @property
    def document_type(self) -> str:
        """Legacy field retained for documents.jsonl and the existing UI."""

        return self.instrument_type

    def public_record(self) -> dict[str, Any]:
        value = record(self)
        value["document_type"] = self.document_type
        return value


@dataclass
class PartyRole:
    id: str
    family_id: str
    instrument_id: str
    entity_name: str
    role: str
    is_signatory: bool
    evidence_span_id: str
    schema_version: str = SCHEMA_VERSION


@dataclass
class EvidenceSpan:
    id: str
    instrument_id: str
    clause_id: str
    source: str
    section_id: str
    text: str
    start: int
    end: int
    purpose: str
    schema_version: str = SCHEMA_VERSION


@dataclass
class Clause:
    id: str
    document_id: str
    family_id: str
    source: str
    section_id: str
    section_path: str
    heading: str
    sequence: int
    text: str
    clause_kind: str = "CLAUSE"
    parent_clause_id: str = ""
    list_group_id: str = ""
    list_label: str = ""
    chapeau_clause_id: str = ""
    evidence_span_ids: list[str] = field(default_factory=list)
    scope: dict[str, list[str]] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


@dataclass
class DefinedTerm:
    id: str
    family_id: str
    instrument_id: str
    clause_id: str
    term: str
    definition: str
    evidence_span_ids: list[str]
    schema_version: str = SCHEMA_VERSION


@dataclass
class Offering:
    """A named, licensable configuration: a licence model, edition or plan.

    The ontology described instruments, clauses, rules and defined terms -- a
    legal-document ontology, correct and incomplete. The questions a licensing
    reader actually asks turn on a thing it had no node for. "What is a Named
    User" is not a definition lookup; OpenText licenses Standard, Occasional,
    Actuate, Concurrent, ECD, LiquidOffice and Exceed onDemand Named Users on
    materially different terms, and the answer is "which one is on your order".

    Nor are these written as definitions. They are sections of a licence model
    schedule, and they inherit: "The license model terms and limitations
    applicable to the Occasional Named User License Model are identical to those
    that apply to Software licensed under the Standard Named User License Model
    except that: (i) ...". That sentence is an edge, and storing it as prose
    threw away the only statement of what an Occasional Named User actually is.

    `metric` is what is counted -- a user, a CPU, a transaction, a page.
    `basis` is what one unit is measured per. `inherits_from` names another
    offering, and `exceptions` holds what this one changes about it.
    """

    id: str
    family_id: str
    instrument_id: str
    clause_id: str
    name: str
    metric: str = ""
    basis: str = ""
    inherits_from: str = ""
    exceptions: list[str] = field(default_factory=list)
    summary: str = ""
    evidence_span_ids: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


@dataclass
class OperativeRule:
    id: str
    family_id: str
    document_id: str
    clause_id: str
    source: str
    section_id: str
    section_path: str
    effect: str
    modality: str
    polarity: str
    actor: str
    action: str
    object: str
    scope: dict[str, list[str]]
    conditions: list[str]
    carve_outs: list[str]
    cross_refs: list[str]
    evidence_span_ids: list[str]
    evidence: str
    summary: str
    extraction_method: str = "deterministic"
    model: str = ""
    schema_version: str = SCHEMA_VERSION

    @property
    def rule_type(self) -> str:
        return self.effect

    def compatibility_record(self) -> dict[str, Any]:
        value = record(self)
        value["rule_type"] = self.rule_type
        value["structured_scope"] = value["scope"]
        value["scope"] = " · ".join(
            part for part in (self.section_path, scope_label(self.scope)) if part
        )
        return value


@dataclass
class PrecedenceRule:
    id: str
    family_id: str
    higher_instrument_id: str
    lower_instrument_id: str
    subject_scope: dict[str, list[str]]
    source_clause_id: str
    evidence_span_ids: list[str]
    status: str = "RESOLVED"
    rationale: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass
class CrossReference:
    id: str
    family_id: str
    source_clause_id: str
    target_clause_id: str
    reference_text: str
    relationship_type: str
    evidence_span_ids: list[str]
    status: str
    schema_version: str = SCHEMA_VERSION


@dataclass
class Amendment:
    id: str
    family_id: str
    amendment_instrument_id: str
    source_clause_id: str
    target_instrument_id: str
    target_section_id: str
    target_clause_id: str
    operation: str
    replacement_text: str
    effective_date: str
    evidence_span_ids: list[str]
    status: str
    schema_version: str = SCHEMA_VERSION


@dataclass
class Relationship:
    id: str
    family_id: str
    source: str
    target: str
    type: str
    label: str
    evidence_span_ids: list[str] = field(default_factory=list)
    scope: dict[str, list[str]] = field(default_factory=dict)
    status: str = "RESOLVED"
    schema_version: str = SCHEMA_VERSION


def empty_scope() -> dict[str, list[str]]:
    return {
        "products": [],
        "license_models": [],
        "entities": [],
        "territories": [],
        "subject_matter": [],
    }


def scope_label(scope: dict[str, list[str]] | str | None) -> str:
    if isinstance(scope, str):
        return scope
    if not scope:
        return "General"
    values: list[str] = []
    for key in (
        "products",
        "license_models",
        "entities",
        "territories",
        "subject_matter",
    ):
        values.extend(str(value) for value in scope.get(key, []) if value)
    return " · ".join(dict.fromkeys(values)) or "General"
