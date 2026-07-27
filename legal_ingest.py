from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Callable, Iterable, Sequence

from markitdown import MarkItDown

from legal_schema import (
    SCHEMA_VERSION,
    AgreementFamily,
    Amendment,
    Clause,
    CrossReference,
    DefinedTerm,
    EvidenceSpan,
    Instrument,
    OperativeRule,
    PartyRole,
    PrecedenceRule,
    Relationship,
    empty_scope,
    normalise_text,
    record,
    scope_label,
    stable_id,
)

SUPPORTED = {".pdf", ".txt", ".md", ".csv", ".xls", ".xlsx", ".docx", ".pptx"}
PAGE_NOISE = (
    re.compile(r"^Page\s+\d+(?:\s+of\s+\d+)?$", re.I),
    re.compile(r"^OpenText .+ v\d", re.I),
    re.compile(r"^LEGAL AND COMPLIANCE\s*\|", re.I),
)
HEADING = re.compile(r"^(?P<section>\d+(?:\.\d+)*)\.\s+(?P<title>.+)$")
# Two-column agreements place the section number in a gutter, so converters emit it
# on its own line -- before the text it labels in some documents, after it in others.
# The trailing period is optional: "1.1." and "1.1" are both written.
SECTION_ONLY = re.compile(r"^(?P<section>\d{1,2}(?:\.\d{1,2})?)\.?$")
LIST_ITEM = re.compile(r"(?<!\w)\((?P<label>[a-z]|\d+)\)\s+", re.I)
SECTION_REF = re.compile(
    r"\b(?:section|clause|paragraph)\s+(?P<section>\d+(?:\.\d+)*(?:\([a-z]\))?)",
    re.I,
)
DEFINED_TERM = re.compile(
    # Optional list label: "A)", "A.", or a section number such as "1.1".
    r'^(?:(?:\d+(?:\.\d+)*|[A-Za-z])[\.\)]?\s+)?\s*[“"](?P<term>[^”"]{1,100})[”"]\s+'
    r"(?:means?|has the meaning)\s+(?P<body>.+)$",
    re.I,
)
ROLE_NAMES = (
    # Vendors write second person as often as they write "Customer": Oracle, Cisco and
    # Red Hat all say "You". Leaving it out left the commonest party in the
    # corpus unregistered -- 929 rules whose actor was nobody we knew.
    "You",
    "End User",
    "Provider",
    "Customer",
    "Licensee",
    "Licensor",
    "Supplier",
    "Client",
    "Reseller",
    "Affiliate",
    "Authorised User",
    "Authorized User",
    "Subprocessor",
    "Data Controller",
    "Data Processor",
    "each party",
    "either party",
    "party",
)
MODALITY_PATTERNS = (
    ("SHALL", r"\bshall\b"),
    ("MUST", r"\bmust\b"),
    ("MAY", r"\bmay\b"),
    ("WILL", r"\bwill\b"),
    ("CAN", r"\bcan(?:not)?\b"),
)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "document"


def compact(value: str) -> str:
    return normalise_text(value)


# A letter stranded from its word: "L icensee", "o ther". The article "a" and
# the pronoun "I" are excluded or ordinary English reads as corruption, and the
# lookbehind drops the possessive so "Software's ability" is left alone.
STRANDED_LETTER = re.compile(r"(?<![A-Za-z'’])(?![aAI]\s)([A-Za-z])\s+([a-z]{2,})\b")
# A word broken in two: "contr act", "Docu mentation".
BROKEN_WORD = re.compile(r"\b([A-Za-z]{2,})\s+([a-z]{2,})\b")
# Hyphenation surviving a line break: "informa- tion".
BROKEN_HYPHEN = re.compile(r"\b([A-Za-z]{2,})-\s+([a-z]{2,})\b")


def repair_split_words(text: str) -> str:
    """Rejoin words that PDF extraction broke apart.

    Some publishers emit literal spaces inside words, so no extractor and no
    tolerance setting recovers them -- the spaces are in the text layer.

    The document is its own dictionary: a pair is rejoined only when the joined
    form already appears in the same document as a whole word. That guard is
    what makes it safe to consider every pair, including ones beginning with
    "a": "a definition" is left alone because "adefinition" is not a word,
    while "a nd" becomes "and" because it is.
    """

    # Counts, not just membership. The vocabulary is drawn from the damaged text,
    # so the broken fragments are themselves in it -- "informa" is a token of
    # this document. What separates a fragment from a word is how often each
    # form appears: "information" occurs throughout while "informa" occurs once,
    # whereas "under" occurs constantly and "understand" rarely.
    counts = Counter(word.lower() for word in re.findall(r"\b[A-Za-z]{2,}\b", text))
    if not counts:
        return text
    # Keep the separators so the text can be rebuilt exactly as it was, minus
    # the joins. Evidence is quoted verbatim, so stray edits would break it.
    pieces = re.split(r"(\s+)", text)
    output: list[str] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        head = piece.rstrip("-")
        following = pieces[index + 2] if index + 2 < len(pieces) else ""
        # A fragment often carries punctuation it never lost: "icensee's",
        # "ontrol.". Join the letters and keep the tail exactly as it was.
        tail_match = re.match(r"([a-z]{2,})(.*)$", following, re.S)
        tail = tail_match.group(1) if tail_match else ""
        trailing = tail_match.group(2) if tail_match else ""
        merged = f"{head}{tail}"
        joinable = (
            head
            and tail
            and head.isalpha()
            and counts[merged.lower()] > 0
            # A single stray letter is never a word. Otherwise the joined form
            # has to be better attested than the fragment, which is what stops
            # "under stand" being welded in a document that says "under" often.
            and (len(head) == 1 or counts[merged.lower()] > counts[head.lower()])
        )
        if joinable:
            pieces[index + 2] = f"{merged}{trailing}"
            index += 2
            continue
        output.append(piece)
        index += 1
    return "".join(output)


def extract_source_text(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    return repair_split_words(
        MarkItDown(enable_plugins=False).convert(path).text_content
    )


# Dot-leader table-of-contents rows repeat every heading and must not be mistaken
# for the headings themselves.
TOC_ROW = re.compile(r"\.{4,}\s*\d*\s*$")


def detect_pdf_headings(path: Path, max_pages: int = 120) -> dict[str, int]:
    """Recover heading lines from PDF typography, returning {text: level}.

    Many agreements carry no section numbering at all: their hierarchy is font
    size and weight. Text conversion discards that, so every clause collapses into
    a single "Preamble". The information is present in the PDF -- read it rather
    than guess at it from flattened prose.
    """

    if path.suffix.lower() != ".pdf":
        return {}
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - optional at runtime
        return {}

    measured: list[tuple[float, str]] = []
    sizes: Counter = Counter()
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages[:max_pages]:
                for line in page.extract_text_lines(extra_attrs=["size"]):
                    chars = line.get("chars") or []
                    text = compact(str(line.get("text", "")))
                    if not chars or not text:
                        continue
                    size = round(median(char["size"] for char in chars), 1)
                    sizes[size] += len(text)
                    measured.append((size, text))
    except Exception:  # pragma: no cover - malformed PDFs must not break ingestion
        return {}
    if not measured:
        return {}

    body = sizes.most_common(1)[0][0]
    candidates = [
        (size, text)
        for size, text in measured
        # A heading is short, larger than body text, and not a contents row.
        if size > body * 1.05
        and 2 < len(text) <= 90
        and not TOC_ROW.search(text)
        and not text.isdigit()
    ]
    if not candidates:
        return {}
    # Repeated lines are running headers, not section headings.
    frequency = Counter(text for _, text in candidates)
    by_size: defaultdict[float, set[str]] = defaultdict(set)
    for size, text in candidates:
        if frequency[text] <= 4 and not TITLE_FURNITURE.match(text):
            by_size[size].add(text)
    # Cover-page title fragments are large but few. A real heading level recurs.
    structural = sorted(
        (size for size, texts in by_size.items() if len(texts) >= 3), reverse=True
    )
    levels = {size: index + 1 for index, size in enumerate(structural[:3])}
    return {
        text: levels[size]
        for size in structural[:3]
        for text in by_size[size]
        if size in levels
    }


def clean_lines(text: str) -> list[str]:
    output: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = compact(raw.replace("\f", ""))
        if line.startswith("# Source:"):
            continue
        if any(pattern.match(line) for pattern in PAGE_NOISE):
            continue
        output.append(line)
    while output and not output[0]:
        output.pop(0)
    while output and not output[-1]:
        output.pop()
    return output


# Cover pages routinely lead with a date banner, version stamp or confidentiality
# notice before the actual document title.
TITLE_FURNITURE = re.compile(
    r"^(?:"
    r"(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\s+\d{4}"
    r"|\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september"
    r"|october|november|december)\s+\d{4}"
    r"|v(?:ersion)?\.?\s*\d+(?:\.\d+)*"
    r"|page\s+\d+(?:\s+of\s+\d+)?"
    r"|confidential|internal use only|contents|table of contents"
    r"|\d{4}"
    r")\.?$",
    re.I,
)

# Copyright and trademark banners sit above the title on many cover pages and are
# not anchored to the end of the line, so they need a search rather than a match.
TITLE_BANNER = re.compile(
    r"^\s*(?:copyright\b|©|\(c\)\s|all rights reserved\b"
    r"|the information contained herein\b)",
    re.I,
)


DOCUMENT_NOUN = re.compile(
    # Plurals matter: Cisco ranks "Offer Descriptions, Service Descriptions", and
    # a singular-only pattern reads that as prose rather than a document name.
    r"\b(agreements?|addend(?:um|a)|amendments?|schedules?|annex(?:es)?|appendix"
    r"|appendices|attachments?|terms|conditions|polic(?:y|ies)|handbooks?"
    r"|glossar(?:y|ies)|authorisations?|authorizations?|licen[cs]es?|eula"
    r"|guides?|exhibits?|supplements?|descriptions?|rights|notifications?)\b",
    re.I,
)


def title_fragment(value: str) -> bool:
    """Whether a line can be part of a cover-page title rather than body text."""

    if not value or len(value) > 90:
        return False
    if HEADING.match(value) or value.endswith((".", ";", ":", ",")):
        return False
    return not value[0].islower()


def title_block(lines: Sequence[str]) -> tuple[str, str]:
    meaningful = [line.lstrip("#").strip() for line in lines[:30] if line]
    meaningful = [line for line in meaningful if line]
    if not meaningful:
        return "Untitled agreement", ""
    evidence = " ".join(meaningful[:8])
    window = [
        line
        for line in meaningful[:20]
        if not TITLE_FURNITURE.match(line)
        and not TITLE_BANNER.match(line)
        # Two-column PDFs emit the gutter numbers as a block before any text, so
        # the title can sit ten lines down behind "1." "1.1." "1.2." ... A bare
        # section number is never the name of a document.
        and not SECTION_ONLY.match(line)
        and not re.match(r"^effective\b", line, re.I)
    ][:8]
    if not window:
        return meaningful[0], evidence
    if len(window) > 2:
        # Many covers print a one-word document tab ("Addendum", "Agreement")
        # above the real title. Alone it names a class, not this instrument, and
        # every sibling document then shares the same useless title.
        window = [
            line
            for line in window
            if not (len(line.split()) == 1 and DOCUMENT_NOUN.fullmatch(line))
        ] or window
    # The title comes first on the page, so prefer the earliest candidate that
    # names a document rather than the longest -- a clickwrap paragraph in block
    # capitals is longer than any title and would otherwise always win.
    for start in range(min(len(window), 5)):
        joined = ""
        for offset in range(3):
            index = start + offset
            if index >= len(window) or not title_fragment(window[index]):
                break
            # Only a wrapped title continues onto the next line. A wrap is short;
            # a full-width line of prose is the body starting, not the title
            # continuing.
            if offset and len(window[index]) > 45:
                break
            candidate = f"{joined} {window[index]}".strip()
            if len(candidate) > 110:
                break
            joined = candidate
        if joined and DOCUMENT_NOUN.search(joined):
            return joined, evidence
    return window[0], evidence


# A classifier receives (title, source filename, full text) and returns
# (instrument_class, instrument_type) or None to decline.
InstrumentClassifier = Callable[[str, str, str], "tuple[str, str] | None"]

# What the deterministic rules return when nothing matched.
GENERIC_CLASSIFICATION = ("MASTER", "AGREEMENT")

# Only these pairings are accepted from a model; anything else is discarded.
INSTRUMENT_TAXONOMY: dict[str, set[str]] = {
    "MASTER": {"MSA", "EULA", "GTC", "AGREEMENT"},
    "ORDER": {"ORDER_SCHEDULE", "ORDER_FORM", "SOW"},
    "ANNEX": {
        "LICENSE_MODEL_ANNEX",
        "PRODUCT_TERMS",
        "SERVICE_DESCRIPTION",
        "ADDITIONAL_LICENSE_AUTHORIZATIONS",
    },
    "ADDENDUM": {"DPA", "BAA", "SECURITY_ADDENDUM"},
    "AMENDMENT": {"AMENDMENT"},
    "POLICY": {"SLA", "SUPPORT_POLICY", "AUP"},
}


def validated_classification(value: object) -> tuple[str, str] | None:
    """Accept a model classification only if it is a known class/type pairing."""

    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    instrument_class = str(value[0]).strip().upper()
    instrument_type = str(value[1]).strip().upper()
    if instrument_type in INSTRUMENT_TAXONOMY.get(instrument_class, set()):
        return instrument_class, instrument_type
    return None


def classify_instrument(title: str, source: str, text: str) -> tuple[str, str]:
    """Classify from title/filename first; body text is only a tie-breaker."""

    title_signal = f"{title} {Path(source).stem.replace('-', ' ')}".lower()
    body_signal = compact(text[:2500]).lower()
    ordered = (
        (
            r"\b(data processing (?:addendum|agreement)|dpa)\b",
            ("ADDENDUM", "DPA"),
        ),
        (
            r"\b(business associate agreement|baa)\b",
            ("ADDENDUM", "BAA"),
        ),
        (
            r"\bsecurity addendum\b",
            ("ADDENDUM", "SECURITY_ADDENDUM"),
        ),
        (
            r"\b(order schedule|order form)\b",
            ("ORDER", "ORDER_SCHEDULE"),
        ),
        (
            r"\b(statement of work|sow)\b",
            ("ORDER", "SOW"),
        ),
        (
            r"\b(licen[cs]e model (?:schedule|annex))\b",
            ("ANNEX", "LICENSE_MODEL_ANNEX"),
        ),
        (
            r"\b(product terms|product specific terms)\b",
            ("ANNEX", "PRODUCT_TERMS"),
        ),
        (
            r"\bamendment\b",
            ("AMENDMENT", "AMENDMENT"),
        ),
        (
            r"\b(service level agreement|service level schedule|sla)\b",
            ("POLICY", "SLA"),
        ),
        (
            r"\bsupport policy\b",
            ("POLICY", "SUPPORT_POLICY"),
        ),
        (
            r"\b(acceptable use policy|aup)\b",
            ("POLICY", "AUP"),
        ),
        (
            r"\b(end user licen[cs]e agreement|eula)\b",
            ("MASTER", "EULA"),
        ),
        (
            r"\b(master (?:cloud |services |customer )?agreement|msa|mca)\b",
            ("MASTER", "MSA"),
        ),
        (
            r"\b(general terms(?: and conditions)?|gtc)\b",
            ("MASTER", "GTC"),
        ),
    )
    for pattern, classification in ordered:
        if re.search(pattern, title_signal, re.I):
            return classification

    # Structural/body signals are intentionally weaker than the title block.
    if re.search(r"\bentered under\b|\bgoverned by the .+ agreement\b", body_signal):
        return "ORDER", "ORDER_FORM"
    if re.search(r"\bdata (?:controller|processor)\b", body_signal) and re.search(
        r"\bpersonal data\b", body_signal
    ):
        return "ADDENDUM", "DPA"
    return "MASTER", "AGREEMENT"


def parse_iso_date(value: str) -> str:
    value = compact(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value, flags=re.I))
    formats = (
        "%d %B %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
    )
    for date_format in formats:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return ""


def find_labelled_date(text: str, labels: str) -> str:
    months = (
        "January|February|March|April|May|June|July|August|September|"
        "October|November|December"
    )
    match = re.search(
        rf"(?:{labels})(?:\s+as\s+of)?(?:\s+date)?\s*[:\-]?\s*"
        rf"((?:\d{{1,2}}(?:st|nd|rd|th)?\s+)?(?:{months})\s+\d{{4}}|"
        rf"(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?[,]?\s+\d{{4}}|"
        rf"\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{4}})",
        text,
        re.I,
    )
    return parse_iso_date(match.group(1)) if match else ""


VERSION_PATTERNS = (
    # "version 5.4", "Version: 2.3"
    r"\bversion:?\s+([0-9][A-Za-z0-9._-]*)",
    # Cisco stamps every page "Controlled Doc. # EDCS-24218913 Ver: 6.0".
    r"\bver\.?:?\s*([0-9]+(?:\.[0-9]+)*)\b",
    # "v.7-2026", "v3.0", "v070124" -- SAP, OpenText, Siemens and Oracle each
    # use one of these. The lookbehind rather than \b is deliberate: in a
    # filename the "v" follows an underscore, where \b does not match.
    # Trailing (?![A-Za-z0-9]) rather than \b: in "..._v1.4_2024-03-19.pdf" the
    # character after the version is "_", where \b does not match either.
    r"(?<![A-Za-z0-9])v\.?\s?([0-9]+(?:[.-][0-9]+)+)(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])v([0-9]{6})(?![A-Za-z0-9])",
)


def find_version(text: str, filename: str = "") -> str:
    """Recover a document version string from its text, falling back to the name.

    Version identifies which edition of a schedule is in front of you, and a
    corpus routinely holds several. Without it a superseded schedule is
    indistinguishable from the one in force.
    """

    for source in (text[:3000], text[-3000:], filename):
        for pattern in VERSION_PATTERNS:
            match = re.search(pattern, source, re.I)
            if match:
                return match.group(1)
    return ""


def make_instrument(
    path: Path,
    text: str,
    family_id: str = "",
    classifier: InstrumentClassifier | None = None,
) -> Instrument:
    lines = clean_lines(text)
    title, title_evidence = title_block(lines)
    instrument_class, instrument_type = classify_instrument(title, path.name, text)
    if classifier and (instrument_class, instrument_type) == GENERIC_CLASSIFICATION:
        # Only consult the model where the deterministic rules gave up. A title such
        # as "Service Description" or "Additional License Authorizations" names no
        # recognised instrument, but a reader of the first page knows what it is.
        refined = validated_classification(classifier(title, path.name, text))
        if refined:
            instrument_class, instrument_type = refined
    version = find_version(text, path.name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    instrument_id = stable_id("instrument", path.name, digest)
    return Instrument(
        id=instrument_id,
        family_id=family_id,
        source=path.name,
        title=title,
        instrument_class=instrument_class,
        instrument_type=instrument_type,
        version=version,
        effective_date=find_labelled_date(text[:5000], r"effective"),
        signature_date=find_labelled_date(text[-5000:], r"signed|signature"),
        term_start=find_labelled_date(text, r"term start|commencement"),
        term_end=find_labelled_date(text, r"term end|expiry|expiration"),
        sha256=digest,
        title_evidence=title_evidence,
    )


def run_in_heading(value: str) -> str:
    """Take the leading run-in heading from a clause line, e.g. "Grant of License."."""

    value = compact(value)
    if not value:
        return ""
    first = value.split(".", 1)[0].strip()
    return first if 0 < len(first) <= 90 else ""


HEADING_VERB = re.compile(
    r"\b(shall|will|must|means?|includes?|agrees?|applies|are|is|has|have)\b", re.I
)


def looks_like_heading(value: str) -> bool:
    """Decide whether the text after a section number names the clause or *is* it.

    "1. DEFINITIONS" is a heading. "2.3. "Broadcom Software" means ..." is the
    clause itself, and treating it as a heading silently deletes the operative
    text -- definitions and precedence language disappear from the clause body
    while looking perfectly healthy in the section index.
    """

    value = value.strip()
    if not value or len(value) > 90:
        return False
    if value[0] in "“\"'(":
        # A quoted term opening the line is a definition, never a section title.
        return False
    if value.rstrip().endswith((",", ";", ":")):
        return False
    # Headings are noun phrases. A finite verb means we are already reading prose.
    return not HEADING_VERB.search(value)


def paragraph_stream(
    lines: Sequence[str], headings: dict[str, int] | None = None
) -> Iterable[tuple[str, str, str]]:
    """Yield section id, heading and compact paragraphs.

    `headings` maps an exact line to its typographic level, for documents whose
    hierarchy is font size rather than numbering.
    """

    headings = headings or {}
    counters: defaultdict[int, int] = defaultdict(int)
    section = "Preamble"
    heading = "Preamble"
    previous = ""
    buffer: list[str] = []

    def flush() -> str:
        value = compact(" ".join(buffer))
        buffer.clear()
        return value

    for line in lines:
        heading_match = HEADING.match(line)
        if heading_match and len(line) < 220:
            title = compact(heading_match.group("title"))
            value = flush()
            if value:
                previous = value
                yield section, heading, value
            section = heading_match.group("section")
            if looks_like_heading(title):
                heading = title.rstrip(".")
            else:
                # The remainder is the clause, not a name for it. Keep the text and
                # take a run-in heading from its opening phrase.
                heading = run_in_heading(title) or f"Clause {section}"
                buffer.append(title)
            continue
        level = headings.get(line)
        if level:
            value = flush()
            if value:
                previous = value
                yield section, heading, value
            counters[level] += 1
            for deeper in [key for key in counters if key > level]:
                counters.pop(deeper)
            section = ".".join(
                str(counters[key]) for key in sorted(counters) if key <= level
            )
            heading = line.rstrip(".")
            continue
        number_match = SECTION_ONLY.match(line)
        if number_match:
            number = number_match.group("section")
            # The line before the number labels it: a section title for "X.0", or the
            # run-in heading plus start of clause text for "X.Y". A blank line often
            # separates a standalone title from its number, so fall back to the last
            # short paragraph already emitted.
            carried = ""
            reuse_previous = False
            if buffer and len(buffer[-1]) < 200:
                carried = buffer.pop()
            elif not buffer and looks_like_heading(previous):
                # Only reclaim an already-emitted paragraph when it reads as a
                # title. Where the gutter number *precedes* its text the previous
                # paragraph belongs to the section before this one.
                carried = previous
                reuse_previous = True
            value = flush()
            if value:
                previous = value
                yield section, heading, value
            section = number
            derived = run_in_heading(carried)
            if number.endswith(".0"):
                heading = derived or f"Section {number}"
            else:
                heading = derived or f"Clause {number}"
                if carried and not reuse_previous:
                    buffer.append(carried)
            continue
        if line.startswith("#"):
            if buffer:
                value = flush()
                if value:
                    yield section, heading, value
            continue
        if not line:
            value = flush()
            if value:
                previous = value
                yield section, heading, value
            continue
        buffer.append(line)
    value = flush()
    if value:
        yield section, heading, value


def infer_scope(
    instrument: Instrument, heading: str, text: str
) -> dict[str, list[str]]:
    scope = empty_scope()
    combined = f"{heading} {text}"
    if instrument.instrument_class == "ORDER":
        product = re.sub(
            r"\b(acme|order|schedule|form|statement of work|sow)\b",
            "",
            instrument.title,
            flags=re.I,
        )
        product = compact(product)
        if product:
            scope["products"].append(product)
    for subject, pattern in (
        ("personal data", r"\bpersonal data\b|\bdata processing\b"),
        ("security", r"\bsecurity\b|\bcredential"),
        ("service levels", r"\bservice level\b|\buptime\b|\bavailability\b"),
        ("support", r"\bsupport\b|\bincident\b"),
        ("permitted use", r"\bpermitted use\b|\baccess rights\b|\brestriction"),
        ("fees", r"\bfees?\b|\binvoice"),
        ("confidentiality", r"\bconfidential"),
        ("assignment", r"\bassign|\btransfer"),
    ):
        if re.search(pattern, combined, re.I):
            scope["subject_matter"].append(subject)
    territory = re.search(
        r"\b(UK and Ireland|United Kingdom|Ireland|EU|EEA)\b", combined
    )
    if territory:
        scope["territories"].append(territory.group(1))
    return scope


def split_list_group(paragraph: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(LIST_ITEM.finditer(paragraph))
    if len(matches) < 2:
        return "", []
    prefix = paragraph[: matches[0].start()].strip()
    if not prefix.endswith(":"):
        return "", []
    items: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(paragraph)
        item = paragraph[match.end() : end].strip(" ;")
        if item:
            items.append((match.group("label").lower(), item))
    return prefix.rstrip(":").strip(), items


DEFINITION_START = re.compile(
    r'[“"](?P<term>[^”"]{1,100})[”"]\s+(?:means?|has the meaning)\b', re.I
)


def split_definition_block(paragraph: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a packed definitions paragraph into one clause per defined term.

    Agreements routinely run an entire definitions article together as a single
    block. Left whole it collapses every definition into one clause, so only the
    first is ever recognised.
    """

    matches = list(DEFINITION_START.finditer(paragraph))
    if len(matches) < 2:
        return "", []
    prefix = paragraph[: matches[0].start()].strip(" ;")
    output: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(paragraph)
        body = paragraph[match.start() : end].strip(" ;")
        if body:
            output.append((compact(match.group("term")), body))
    return prefix, output


def parse_clauses(
    instrument: Instrument, text: str, headings: dict[str, int] | None = None
) -> tuple[list[Clause], list[EvidenceSpan]]:
    clauses: list[Clause] = []
    spans: list[EvidenceSpan] = []
    section_counts: defaultdict[str, int] = defaultdict(int)
    issued_clause_ids: set[str] = set()
    sequence = 0

    def add_clause(
        section: str,
        heading: str,
        value: str,
        *,
        kind: str = "CLAUSE",
        parent: str = "",
        list_group: str = "",
        list_label: str = "",
        chapeau: str = "",
    ) -> Clause:
        nonlocal sequence
        sequence += 1
        section_counts[section] += 1
        display_section = (
            section
            if section_counts[section] == 1
            else f"{section}.{section_counts[section]}"
        )
        clause_id = stable_id(
            "clause", instrument.id, section, kind, list_label, compact(value)
        )
        if clause_id in issued_clause_ids:
            # Boilerplate repeats: the same sentence under the same section
            # number produced the same id twice, and every id-keyed lookup then
            # resolved to whichever record was written last. Disambiguate the
            # repeat rather than the first occurrence, so ids already referenced
            # by extracted rules and enrichment checkpoints stay valid.
            occurrence = 2
            while True:
                candidate = stable_id(
                    "clause",
                    instrument.id,
                    section,
                    kind,
                    list_label,
                    compact(value),
                    str(occurrence),
                )
                if candidate not in issued_clause_ids:
                    clause_id = candidate
                    break
                occurrence += 1
        issued_clause_ids.add(clause_id)
        span_id = stable_id("span", clause_id, value, "clause")
        clause = Clause(
            id=clause_id,
            document_id=instrument.id,
            family_id=instrument.family_id,
            source=instrument.source,
            section_id=display_section,
            section_path=f"{section} {heading}".strip(),
            heading=heading,
            sequence=sequence,
            text=compact(value),
            clause_kind=kind,
            parent_clause_id=parent,
            list_group_id=list_group,
            list_label=list_label,
            chapeau_clause_id=chapeau,
            evidence_span_ids=[span_id],
            scope=infer_scope(instrument, heading, value),
        )
        spans.append(
            EvidenceSpan(
                id=span_id,
                instrument_id=instrument.id,
                clause_id=clause.id,
                source=instrument.source,
                section_id=display_section,
                text=clause.text,
                start=0,
                end=len(clause.text),
                purpose=kind.lower(),
            )
        )
        clauses.append(clause)
        return clause

    for section, heading, paragraph in paragraph_stream(clean_lines(text), headings):
        if re.fullmatch(
            r"(?:version\s+)?[vV]?\d+(?:\.\d+)+|effective\s+.+",
            paragraph,
            re.I,
        ):
            continue
        # Definition blocks take priority over list detection: lettered sub-parts
        # inside a single definition are part of that definition, not a chapeau list
        # of operative obligations.
        definition_prefix, definitions = split_definition_block(paragraph)
        if definitions:
            if definition_prefix:
                add_clause(section, heading, definition_prefix)
            for term, body in definitions:
                add_clause(section, f"Definition of {term}", body)
            continue
        chapeau_text, items = split_list_group(paragraph)
        if items:
            group_id = stable_id("list", instrument.id, section, paragraph)
            chapeau = add_clause(
                section,
                heading,
                chapeau_text,
                kind="CHAPEAU",
                list_group=group_id,
            )
            for label, item in items:
                add_clause(
                    section,
                    heading,
                    item,
                    kind="LIST_ITEM",
                    parent=chapeau.id,
                    list_group=group_id,
                    list_label=label,
                    chapeau=chapeau.id,
                )
            continue
        add_clause(section, heading, paragraph)
    return clauses, spans


def evidence_for_clause(clause: Clause) -> list[str]:
    return list(clause.evidence_span_ids)


# A proper noun that is not at the start of a sentence, so ordinary capitalised
# openings are not mistaken for names.
PROPER_NOUN = re.compile(
    r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\b"
)
SENTENCE_OPENERS = frozenset(
    """the this that if in on for a an and or no any all each such where when
    you your we our us it its not notwithstanding except subject provided upon
    during to at by
    with without under over from as is are be been being will shall may must
    """.split()
)


def vendor_names(scan: str) -> list[str]:
    """The vendor's own name, as its documents capitalise it.

    Derived rather than listed: whoever uploaded their agreements is the vendor,
    and the name they repeat throughout is theirs. Titles were tried first and
    were useless -- a family's titles share "Terms" and "Appendix" far more than
    they share "Broadcom".

    The vendor is a party to every instrument it publishes but is named rather
    than given a role word, so ROLE_NAMES never sees it: "Red Hat may modify",
    "Cisco will provide", "IBM is not responsible".
    """

    counts: Counter[str] = Counter()
    for match in PROPER_NOUN.finditer(scan):
        candidate = match.group(1)
        if candidate.split()[0].lower() in SENTENCE_OPENERS:
            continue
        if NOT_A_PERSON.search(candidate) or DOCUMENT_NOUN.search(candidate):
            continue
        if candidate.lower() in {role.lower() for role in ROLE_NAMES}:
            continue
        counts[candidate] += 1
    # Rank by how often the name is used. Preferring the longest form instead
    # produced "SAP Business" for SAP: the vendor's bare name is always the more
    # frequent, and a two-word name like "Red Hat" wins on its own count because
    # the pattern prefers the longer match when both are present.
    ranked = [name for name, count in counts.most_common(6) if count >= 5]
    return ranked[:1]


def extract_parties(
    family_id: str,
    instruments: Sequence[Instrument],
    clauses: Sequence[Clause],
) -> list[PartyRole]:
    output: list[PartyRole] = []
    seen: set[tuple[str, str, str]] = set()
    by_instrument: defaultdict[str, list[Clause]] = defaultdict(list)
    for clause in clauses:
        by_instrument[clause.document_id].append(clause)
    # Once for the family, not once per instrument. Per document the commonest
    # proper noun drifts to whatever that document is about -- "Registered
    # Capacity", "Cisco API" -- and each of those became a party, which would
    # then make an actor named after a licence metric look perfectly valid.
    family_vendors = vendor_names(" ".join(clause.text for clause in clauses))

    for instrument in instruments:
        relevant = by_instrument[instrument.id]
        preamble = " ".join(
            clause.text
            for clause in relevant[:8]
            if clause.section_id.startswith("Preamble")
        )
        source_clause = relevant[0] if relevant else None
        if not source_clause:
            continue
        explicit = re.search(
            r"\bbetween\s+(?P<a>.{2,160}?)\s*\([“\"](?P<ar>[^”\"]+)[”\"]\)"
            r"\s+and\s+(?P<b>.{2,180}?)\s*\([“\"](?P<br>[^”\"]+)[”\"]\)",
            preamble,
            re.I,
        )
        candidates: list[tuple[str, str, bool]] = []
        if explicit:
            candidates.extend(
                [
                    (
                        compact(explicit.group("a")),
                        compact(explicit.group("ar")),
                        "identified" not in explicit.group("a").lower(),
                    ),
                    (
                        compact(explicit.group("b")),
                        compact(explicit.group("br")),
                        "identified" not in explicit.group("b").lower(),
                    ),
                ]
            )
        scan = " ".join(clause.text for clause in relevant)
        # The vendor is a party to every instrument it publishes, but it is
        # named rather than given a role word, so ROLE_NAMES never sees it:
        # "Red Hat may modify", "Cisco will provide". house_words already finds
        # it -- the words shared by most titles in a family are the vendor's.
        for name in family_vendors:
            if re.search(rf"\b{re.escape(name)}\b", scan):
                candidates.append((name, "Provider", False))
        for role in ROLE_NAMES:
            if re.search(rf"\b{re.escape(role)}\b", scan, re.I):
                candidates.append((role.title(), role.title(), False))
        for entity_name, role, is_signatory in candidates:
            key = (instrument.id, entity_name.lower(), role.lower())
            if key in seen:
                continue
            seen.add(key)
            output.append(
                PartyRole(
                    id=stable_id("role", instrument.id, entity_name, role),
                    family_id=family_id,
                    instrument_id=instrument.id,
                    entity_name=entity_name[:240],
                    role=role[:120],
                    is_signatory=is_signatory,
                    evidence_span_id=source_clause.evidence_span_ids[0],
                )
            )
    return output


def modality_and_polarity(text: str) -> tuple[str, str, str]:
    lower = text.lower()
    modality = "OTHER"
    for candidate, pattern in MODALITY_PATTERNS:
        if re.search(pattern, lower):
            modality = candidate
            break
    # "only" is deliberately absent. "Licensee may only allocate Licences to its
    # own Clients" grants a right subject to a limit; reading it as a negation
    # produced a PROHIBITION whose modality was MAY -- a self-contradiction that
    # also threw away the grant. The limit is captured as a carve-out instead.
    negative = bool(
        re.search(
            r"\b(shall not|must not|may not|will not|cannot|can not|"
            r"not permitted|prohibited)\b",
            lower,
        )
    )
    polarity = "NEGATIVE" if negative else "POSITIVE"
    if negative:
        effect = "PROHIBITION"
    elif modality == "MAY":
        effect = "PERMISSION"
    elif modality in {"MUST", "SHALL", "WILL"}:
        effect = "OBLIGATION"
    else:
        effect = ""
    return effect, modality, polarity


# "may not be allocated", "cannot be shared", "shall be provided" -- the thing
# before the modal is what the action is done *to*, not who does it.
PASSIVE_CONSTRUCTION = re.compile(
    r"\b(?:shall|must|may|will|can|cannot|can\s+not)\s+(?:not\s+|only\s+)*be\s+\w+(?:ed|en)\b",
    re.I,
)


def actor_from_text(text: str, roles: Sequence[PartyRole]) -> str:
    beginning = compact(text)[:180]
    subject_text = re.split(
        r"\b(?:shall|must|may|will|can(?:not)?)\b", beginning, maxsplit=1, flags=re.I
    )[0]
    passive = bool(PASSIVE_CONSTRUCTION.search(beginning))
    if passive:
        # A named party may still appear ("...may not be allocated by Licensee"),
        # so keep looking for one; but never fall back to the pre-modal noun
        # phrase, which in the passive is the object. "The Software Licenses may
        # not be allocated" recorded the licences themselves as the actor.
        for role in sorted(roles, key=lambda item: len(item.entity_name), reverse=True):
            for candidate in (role.entity_name, role.role):
                if candidate and re.search(
                    rf"\bby\s+(?:the\s+)?{re.escape(candidate)}\b", beginning, re.I
                ):
                    return candidate
        return ""
    subject_text = re.sub(r"^notwithstanding\s+.+?,\s*", "", subject_text, flags=re.I)
    candidates = sorted(
        {role.role for role in roles} | {role.entity_name for role in roles},
        key=len,
        reverse=True,
    )
    for candidate in candidates:
        if candidate and re.search(rf"\b{re.escape(candidate)}\b", subject_text, re.I):
            return candidate
    subject = re.match(
        r"^(?:notwithstanding\s+.+?,\s*)?(?P<actor>[A-Z][A-Za-z' -]{1,60}?)\s+"
        r"(?:shall|must|may|will|can)\b",
        beginning,
    )
    if not subject:
        return ""
    candidate = compact(subject.group("actor"))
    # The subject of an operative sentence is not always a person. "Following
    # restrictions apply", "Redistributions in binary form must retain...",
    # "This Agreement will control" all put a thing where a party belongs, and
    # naming it as the actor asserts a duty nobody owes. An actor has to be
    # someone the agreement could sue.
    return candidate if names_a_person(candidate) else ""


# Words that make a phrase a thing rather than a party. A party is a person, a
# named entity, or a role; a licence metric, a document or an activity is not.
NOT_A_PERSON = re.compile(
    r"\b(agreement|schedule|addendum|annex|appendix|terms|conditions|licen[cs]e"
    r"|software|documentation|redistributions?|restrictions?|provisions?|clause"
    r"|section|notice|copyright|warrant(?:y|ies)|liabilit(?:y|ies)|use|access"
    r"|data|information|service|product|credit|fee|payment|order|instance"
    r"|server|device|copy|copies|version|content|documents?|offers?|programs?)\b",
    re.I,
)


def names_a_person(value: str) -> bool:
    """Whether a phrase could be a party to an agreement."""

    text = compact(value)
    if not text or len(text.split()) > 5:
        return False
    if NOT_A_PERSON.search(text):
        return False
    # A party is named or role-titled, so it is capitalised; a sentence that
    # merely begins with a capital is not enough on its own, but combined with
    # the exclusions above it separates "Micro Focus" from "Following".
    return bool(re.match(r"^[A-Z]", text)) and not text.lower().startswith(
        ("following ", "each ", "any ", "all ", "no ", "such ")
    )


def extract_action(text: str) -> str:
    actions = (
        ("share_credentials", r"\bshare\b.+\bcredentials?"),
        ("reverse_engineer", r"\breverse engineer"),
        (
            "calculate_availability",
            r"\bavailability calculation|\bdowntime\b|\bexcluded from.+availability",
        ),
        ("allocate", r"\ballocat"),
        ("assign_or_transfer", r"\bassign|\btransfer|\bsublicen"),
        ("process_data", r"\bprocess|\bpersonal data|\bcustomer data"),
        ("protect", r"\bprotect|\bsecurity measure|\bsafeguard"),
        ("access_or_use", r"\baccess|\buse|\binstall|\bexecute"),
        ("pay", r"\bpay|\bfee|\binvoice"),
        ("audit", r"\baudit|\binspect|\brecord"),
        ("disclose", r"\bdisclos|\bconfidential"),
        ("terminate_or_suspend", r"\bterminat|\bsuspend"),
        ("retain_or_delete", r"\bretain|\bdelete|\bdestroy|\breturn"),
        ("meet_service_level", r"\bservice level|\buptime|\bavailability|\bcredit"),
        ("notify", r"\bnotify|\bnotice"),
        ("renew", r"\brenew|\bextension term"),
    )
    for label, pattern in actions:
        if re.search(pattern, text, re.I):
            return label
    # The list above is a closed set of licensing actions. Falling back to a
    # label like "govern" asserts an action the clause never mentions, so take
    # the verb the text actually uses and say nothing when there is none.
    verb = re.search(
        r"\b(?:shall|must|may|will|can(?:not)?)\s+(?:not\s+|only\s+)*"
        r"(?:be\s+)?([a-z]{3,})",
        text,
        re.I,
    )
    return verb.group(1).lower() if verb else "unspecified"


def extract_object(text: str) -> str:
    for label, pattern in (
        ("Software License", r"\bsoftware licen[cs]e"),
        ("Cloud Service", r"\bcloud service"),
        ("Affiliate access", r"\baffiliate"),
        ("Personal Data", r"\bpersonal data"),
        ("Customer Data", r"\bcustomer data"),
        ("Confidential Information", r"\bconfidential information"),
        ("Service Level", r"\bservice level|\buptime|\bavailability"),
        ("credentials", r"\bcredentials?"),
        ("Fees", r"\bfees?|\binvoice"),
        ("Agreement", r"\bagreement"),
    ):
        if re.search(pattern, text, re.I):
            return label
    return ""


def fragments(text: str, patterns: Sequence[str]) -> list[str]:
    values: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            value = compact(match.group(0)).strip(" ,")
            if value and value not in values:
                values.append(value)
    return values


MODAL_VERB = r"(?:shall|must|may|will|cannot|can\s+not|is\s+not\s+permitted)"

# Where one operative statement ends and the next begins. Either a sentence or
# semicolon boundary, or a conjunction that introduces a fresh modal verb --
# "and shall not", "but must not". A conjunction without a following modal joins
# one statement rather than starting another, so it must not split.
PROPOSITION_BREAK = re.compile(
    r"(?<=[a-z\)\"”])[.;]\s+(?=[A-Z“\"(])"
    rf"|,?\s+(?:and|but|or)\s+(?=(?:\w+\s+){{0,3}}?{MODAL_VERB}\b)",
    re.I,
)


def operative_propositions(text: str) -> list[str]:
    """Split a clause into the separate operative statements it makes.

    One clause routinely grants and forbids at once -- "Licensee may copy the
    Software for backup purposes but must not distribute it". Read whole, the
    negation anywhere in the clause made the entire thing a prohibition, so the
    permission was not merely lost but reported as its opposite.

    Fragments are exact substrings of the input, which keeps evidence quotable.
    """

    if len(re.findall(MODAL_VERB, text, re.I)) < 2:
        return [text]
    # Work in offsets rather than strings. Merging fragments by concatenation
    # invents whitespace that is not in the source, and the result can no longer
    # be located in the clause -- which silently costs the rule its exact span.
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for match in PROPOSITION_BREAK.finditer(text):
        bounds.append((cursor, match.start()))
        cursor = match.end()
    bounds.append((cursor, len(text)))

    merged: list[tuple[int, int]] = []
    for start, end in bounds:
        if start >= end:
            continue
        # A fragment with no modal of its own is a continuation, not a new
        # statement: extend the previous one to cover it.
        if merged and not re.search(MODAL_VERB, text[start:end], re.I):
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [text[start:end].strip() for start, end in merged] or [text]


def clause_rule(
    family_id: str,
    clause: Clause,
    roles: Sequence[PartyRole],
    clause_lookup: dict[str, Clause],
) -> OperativeRule | None:
    rules = clause_rules(family_id, clause, roles, clause_lookup)
    return rules[0] if rules else None


def clause_rules(
    family_id: str,
    clause: Clause,
    roles: Sequence[PartyRole],
    clause_lookup: dict[str, Clause],
    extra_spans: list[EvidenceSpan] | None = None,
) -> list[OperativeRule]:
    chapeau = clause_lookup.get(clause.chapeau_clause_id)
    if chapeau:
        # A list item inherits its modality from the chapeau ("Customer shall
        # not: (a) ...; (b) ..."). Splitting here would strip that inheritance.
        return [
            rule
            for rule in [
                build_rule(
                    family_id,
                    clause,
                    roles,
                    f"{chapeau.text} {clause.text}",
                    clause.text,
                    chapeau,
                )
            ]
            if rule
        ]
    extra_spans = [] if extra_spans is None else extra_spans
    propositions = operative_propositions(clause.text)
    output: list[OperativeRule] = []
    fallback_actor = ""
    for proposition in propositions:
        span = proposition_span(clause, proposition)
        rule = build_rule(
            family_id,
            clause,
            roles,
            proposition,
            proposition,
            None,
            fallback_actor,
            span,
        )
        if rule:
            fallback_actor = fallback_actor or rule.actor
            if span:
                extra_spans.append(span)
            output.append(rule)
    return output


def build_rule(
    family_id: str,
    clause: Clause,
    roles: Sequence[PartyRole],
    semantic_text: str,
    evidence_text: str,
    chapeau: Clause | None,
    fallback_actor: str = "",
    span: EvidenceSpan | None = None,
) -> OperativeRule | None:
    effect, modality, polarity = modality_and_polarity(semantic_text)
    if not effect:
        if re.search(r"\bexcluded from\b|\bdoes not count\b", semantic_text, re.I):
            effect, modality, polarity = "EXCLUSION", "OTHER", "NEGATIVE"
        elif re.search(r"\bsole (?:monetary )?remedy\b", semantic_text, re.I):
            effect, modality, polarity = "REMEDY", "OTHER", "POSITIVE"
        else:
            return None
    # "Customer shall X and shall not Y" names its subject once; the second
    # statement inherits it rather than being left with no actor.
    actor = actor_from_text(semantic_text, roles) or fallback_actor
    subject_matter = extract_object(semantic_text)
    if (
        actor
        and subject_matter
        and actor.strip().lower() == subject_matter.strip().lower()
    ):
        # The sentence's subject is the thing the rule is about, not a party
        # bound by it: "Confidential Information will not include...",
        # "Agreement will control in the event of any conflict". Naming it as
        # the actor claims a document owes an obligation to itself.
        actor = (
            ""
            if fallback_actor.strip().lower() == actor.strip().lower()
            else fallback_actor
        )
    conditions = fragments(
        evidence_text,
        (
            r"\bprovided(?:\s+that)?\b[^.;]+",
            r"\bif\b[^.;]+",
            r"\bsubject to\b[^.;]+",
        ),
    )
    carve_outs = fragments(
        evidence_text,
        (
            r"\bunless\b[^.;]+",
            r"\bexcept(?:\s+that|\s+as)?\b[^.;]+",
            r"\bnotwithstanding\b[^.;]+",
            r"\bas expressly (?:stated|permitted)\b[^.;]+",
            r"\bonly\b[^.;]+",
        ),
    )
    cross_refs = [match.group(0) for match in SECTION_REF.finditer(evidence_text)]
    span_ids = []
    if chapeau:
        span_ids.extend(chapeau.evidence_span_ids)
    # Prefer the narrower span when the rule covers only part of the clause.
    span_ids.extend([span.id] if span else clause.evidence_span_ids)
    # The quoted evidence is the statement itself, so a permission is not
    # evidenced by a paragraph that also forbids something else.
    evidence = evidence_text
    summary = compact(
        f"{actor or 'Contractual actor'} {effect.lower()} "
        f"{extract_action(semantic_text).replace('_', ' ')} "
        f"{extract_object(semantic_text)}"
    )
    return OperativeRule(
        id=stable_id("rule", clause.id, effect, actor, evidence),
        family_id=family_id,
        document_id=clause.document_id,
        clause_id=clause.id,
        source=clause.source,
        section_id=clause.section_id,
        section_path=clause.section_path,
        effect=effect,
        modality=modality,
        polarity=polarity,
        actor=actor,
        action=extract_action(semantic_text),
        object=subject_matter,
        scope=clause.scope,
        conditions=conditions,
        carve_outs=carve_outs,
        cross_refs=cross_refs,
        evidence_span_ids=span_ids,
        evidence=evidence,
        summary=summary,
    )


def extract_definitions(family_id: str, clauses: Sequence[Clause]) -> list[DefinedTerm]:
    output: list[DefinedTerm] = []
    for clause in clauses:
        match = DEFINED_TERM.match(clause.text)
        if not match:
            continue
        term = compact(match.group("term"))
        output.append(
            DefinedTerm(
                id=stable_id("definition", clause.document_id, term, clause.text),
                family_id=family_id,
                instrument_id=clause.document_id,
                clause_id=clause.id,
                term=term,
                definition=compact(match.group("body")),
                evidence_span_ids=evidence_for_clause(clause),
            )
        )
    return output


def proposition_span(clause: Clause, proposition: str) -> EvidenceSpan | None:
    """An evidence span covering one statement inside a clause.

    Without this a rule about a single sentence cites the whole paragraph, so a
    prohibition appears to be evidenced by text that also grants a permission.
    Exact offsets are what makes the quote checkable, which is the point.
    """

    start = clause.text.find(proposition)
    if start < 0 or proposition == clause.text:
        return None
    return EvidenceSpan(
        id=stable_id("span", clause.id, proposition, "proposition"),
        instrument_id=clause.document_id,
        clause_id=clause.id,
        source=clause.source,
        section_id=clause.section_id,
        text=proposition,
        start=start,
        end=start + len(proposition),
        purpose="proposition",
    )


def extract_rules(
    family_id: str,
    clauses: Sequence[Clause],
    parties: Sequence[PartyRole],
    extra_spans: list[EvidenceSpan] | None = None,
) -> list[OperativeRule]:
    by_instrument: defaultdict[str, list[PartyRole]] = defaultdict(list)
    for party in parties:
        by_instrument[party.instrument_id].append(party)
    lookup = {clause.id: clause for clause in clauses}
    output: list[OperativeRule] = []
    for clause in clauses:
        output.extend(
            clause_rules(
                family_id,
                clause,
                by_instrument[clause.document_id],
                lookup,
                extra_spans,
            )
        )
    return output


TITLE_STOPWORDS = {"of", "and", "the", "for", "to", "a", "an", "in", "or"}


def house_words(instruments: Sequence[Instrument]) -> frozenset[str]:
    """Title words so common in this family that they identify nothing.

    Usually the vendor's name. "Cisco's Data Access Terms" shares "cisco" with
    every Cisco title, which is enough to resolve a reference to a document the
    clause never mentioned. Derived per family rather than hardcoded, because the
    vendor is whoever uploaded their agreements.
    """

    if len(instruments) < 3:
        return frozenset()
    counts: Counter[str] = Counter()
    for instrument in instruments:
        counts.update(set(re.findall(r"[a-z]{4,}", instrument.title.lower())))
    threshold = len(instruments) / 2
    return frozenset(word for word, count in counts.items() if count > threshold)


# Words that appear in almost every instrument title in this domain. A phrase that
# overlaps a title only on one of these has not identified a document -- matching on
# it invents precedence rules between instruments the clause never mentioned.
GENERIC_WORDS = {
    "this",
    "agreement",
    "agreements",
    "schedule",
    "order",
    "master",
    "addendum",
    "service",
    "services",
    "software",
    "licence",
    "license",
    "licensed",
    "terms",
    "conditions",
    "support",
    "product",
    "products",
    "customer",
    "general",
    "other",
    "provided",
    "applicable",
    "document",
    "documents",
}


def title_initials(title: str) -> str:
    """Initials of a title's significant words, e.g. "ALA" for a Micro Focus ALA."""

    return "".join(
        word[0]
        for word in re.findall(r"[A-Za-z]+", title)
        if word.lower() not in TITLE_STOPWORDS
    ).upper()


# "the terms of this Product Appendix control" points at the document doing the
# talking. Matching only a leading "this " misses it, and the phrase then resolves
# by title overlap to an arbitrary sibling -- stating the rule about the wrong
# document and inverting it.
SELF_REFERENCE = re.compile(
    r"\bthis\s+(?:\w+\s+){0,3}?"
    r"(?:agreement|addendum|amendment|schedule|annex|appendix|attachment|terms"
    r"|conditions|polic(?:y|ies)|handbook|glossary|licen[cs]e|eula|exhibit"
    r"|supplement|document)\b",
    re.I,
)

# Ordered most specific first. "Exhibit" is deliberately absent: an exhibit is a
# part of the document citing it, not a sibling instrument to rank against.
CLASS_REFERENCES: tuple[tuple[str, str], ...] = (
    (r"\b(?:base|master|foundation|main|principal|underlying)\s+agreement\b", "MASTER"),
    (r"\b(?:transaction document|order form|purchase order|product order)\b", "ORDER"),
    (r"\b(?:attachment|appendix|annex|schedule|use rights|product terms)\b", "ANNEX"),
    (r"\baddendum\b", "ADDENDUM"),
    (r"\bamendment\b", "AMENDMENT"),
    (r"\bthe agreement\b", "MASTER"),
)

REFERENCE_TAIL = re.compile(
    r"\b(?:to the extent|in the event|unless|that|which|where|whether|if)\b", re.I
)


def reference_head(phrase: str) -> str:
    """Trim a reference to the document name, dropping its qualifying tail.

    "Broadcom's global Data Processing Addendum (DPA) to the extent one is in
    place between the Parties" names one document; everything after "to the
    extent" is a condition on the rule, not part of the name.
    """

    return compact(REFERENCE_TAIL.split(phrase, maxsplit=1)[0]).strip(" .,;:")


def acronym_tokens(phrase: str) -> set[str]:
    """Uppercase tokens that could be a document acronym.

    Conflict and liability clauses are routinely set entirely in capitals, so a
    token being uppercase proves nothing on its own -- what matters is that it is
    short and later matches some title's initials. Three characters minimum:
    two-letter tokens such as "EU" in "the EU Standard Contractual Clauses"
    collide with initials like "EUA" and invert the rule they appear in.
    """

    return {
        token
        for token in re.findall(r"\b[A-Z]{3,6}\b", phrase)
        if token not in {"THE", "AND", "FOR", "ANY", "ALL", "THIS", "USE", "CASE"}
    }


def names_document(phrase: str) -> bool:
    """Whether a phrase plausibly names an instrument rather than being prose.

    Precedence extraction splits sentences on commas, so most candidates are
    fragments of argument. Treating those as document references is how a clause
    disclaiming unspecified third-party terms becomes a rule about a sibling
    schedule it never mentions.
    """

    words = re.findall(r"[A-Za-z]+", phrase)
    if not words or len(words) > 10:
        return False
    return bool(DOCUMENT_NOUN.search(phrase)) or bool(acronym_tokens(phrase))


def instruments_for_reference(
    phrase: str,
    source: Instrument,
    instruments: Sequence[Instrument],
    *,
    require_name: bool = True,
) -> list[Instrument]:
    """Resolve a reference phrase to every instrument it can mean.

    Agreements routinely rank a *class* of document rather than a named one --
    "an Attachment prevails over this IPAA", "the applicable ALA". Collapsing that
    to a single arbitrary instrument states the rule about one document and stays
    silent about its siblings, which is worse than saying nothing.

    `require_name` demands the phrase actually name a document, which is right
    when the caller has split a sentence into candidate references. Callers that
    pass a whole clause and ask "which instrument is this about" must turn it off.
    """

    if SELF_REFERENCE.search(phrase):
        return [source]
    if require_name:
        phrase = reference_head(phrase)
        if not names_document(phrase):
            return []
    lower = phrase.lower()
    # Agreements refer to their siblings by acronym far more often than by full
    # title -- "the applicable ALA", "an Attachment prevails over this IPAA".
    # Word-overlap scoring cannot see those at all: the acronym shares no token
    # with the title it stands for.
    acronyms = acronym_tokens(phrase)
    if acronyms:
        by_acronym = [
            instrument
            for instrument in instruments
            if any(
                acronym in title_initials(instrument.title)
                # "these API Terms" against "Baseline API License Terms": the
                # acronym is a word of the title, not its initials.
                or re.search(rf"\b{re.escape(acronym)}\b", instrument.title)
                for acronym in acronyms
            )
        ]
        if by_acronym:
            return by_acronym
    reference_words = {
        word
        for word in re.findall(r"[a-z]{4,}", lower)
        if word not in GENERIC_WORDS and word not in house_words(instruments)
    }
    if reference_words:
        scores = [
            (
                len(
                    reference_words
                    & {
                        word
                        for word in re.findall(r"[a-z]{4,}", instrument.title.lower())
                    }
                ),
                instrument,
            )
            for instrument in instruments
        ]
        best = max((score for score, _ in scores), default=0)
        if best:
            return [instrument for score, instrument in scores if score == best]
    types = (
        ("data processing", "DPA"),
        ("dpa", "DPA"),
        ("order schedule", "ORDER_SCHEDULE"),
        ("order form", "ORDER_FORM"),
        ("master agreement", "MSA"),
        ("cloud master", "MSA"),
        ("eula", "EULA"),
        ("license model", "LICENSE_MODEL_ANNEX"),
        ("service level", "SLA"),
        ("support policy", "SUPPORT_POLICY"),
    )
    for needle, instrument_type in types:
        if needle in lower:
            matches = [
                item for item in instruments if item.instrument_type == instrument_type
            ]
            if matches:
                return matches
    # Drafters name a class as often as a document: "the base agreement", "an
    # Attachment". Resolution is then only as good as the classification, so a
    # reference matching more than a handful of instruments is treated as too
    # vague to support a rule rather than fanned out across all of them.
    for pattern, instrument_class in CLASS_REFERENCES:
        if re.search(pattern, lower):
            matches = [
                item
                for item in instruments
                if item.instrument_class == instrument_class and item.id != source.id
            ]
            if matches and len(matches) <= 3:
                return matches
    words = {word for word in re.findall(r"[a-z]{4,}", lower)}
    scored = [
        (
            len(words & set(re.findall(r"[a-z]{4,}", instrument.title.lower()))),
            instrument,
        )
        for instrument in instruments
    ]
    best = max((score for score, _ in scored), default=0)
    # This last resort counts generic words too, so one shared word is not evidence
    # that the phrase names this instrument.
    if best < (2 if require_name else 1):
        return []
    return [instrument for score, instrument in scored if score == best]


def instrument_for_reference(
    phrase: str,
    source: Instrument,
    instruments: Sequence[Instrument],
    *,
    require_name: bool = True,
) -> Instrument | None:
    """The single best instrument a reference names, or None if it names none."""

    matches = instruments_for_reference(
        phrase, source, instruments, require_name=require_name
    )
    return matches[0] if matches else None


def subject_scope(text: str, *, default: str = "") -> dict[str, list[str]]:
    scope = empty_scope()
    for value, pattern in (
        ("personal data", r"\bpersonal[- ]data\b|\bprocessing of personal data\b"),
        ("product-specific permitted use", r"\bpermitted use\b"),
        ("product capacity", r"\bcapacity\b"),
        ("ordered cloud service", r"\bordered cloud service\b"),
        ("definitions", r"\bdefinitions?\b"),
        ("service levels", r"\bservice levels?\b"),
    ):
        if re.search(pattern, text, re.I):
            scope["subject_matter"].append(value)
    if not scope["subject_matter"] and default:
        scope["subject_matter"].append(default)
    return scope


def explicit_subject_scope(*values: str) -> dict[str, list[str]]:
    scope = empty_scope()
    scope["subject_matter"].extend(value for value in values if value)
    return scope


PRECEDENCE_LADDER = re.compile(
    # The ranked list is introduced by a colon, or the converter has already split
    # the ranks off into their own clauses and the trigger ends the clause. Anchoring
    # on that is what separates the introducer from the run-in heading of the same
    # name -- "13.8 Complete Agreement and Order of Precedence. The Agreement ..."
    # is a section title, not a ladder.
    r"\b(?:following order of (?:precedence|priority)"
    r"|order of (?:precedence|priority)"
    r"|in the following order)\b"
    r"[^:.]{0,90}?\s*(?::|$)",
    re.I,
)
# "from the document with the greatest control to the least" states rank 1 = winner;
# the ascending phrasing means the *last* item listed wins. Getting this backwards
# inverts every rule in the ladder, so read it rather than assume.
LADDER_ASCENDING = re.compile(
    r"\bascending\b|\bleast\b[^.]{0,40}\bgreatest\b|\blowest\b[^.]{0,40}\bhighest\b",
    re.I,
)


def ladder_phrases(text: str, followers: Sequence[str]) -> list[str]:
    """Return the ranked document phrases of an order-of-precedence clause.

    Agreements state precedence as a ranked list far more often than as a pairwise
    "X prevails over Y". The list is written either inline after a colon or as
    enumerated items that clause splitting turns into separate clauses, so both
    shapes have to be read.
    """

    match = PRECEDENCE_LADDER.search(text)
    if not match:
        return []
    tail = text[match.end() :].strip()
    # Strip the enumerator that survives conversion, e.g. "(1) the Transaction Document".
    tail = re.sub(r"^\(?\d+\)\s*", "", tail)
    if len(tail) >= 15:
        phrases = re.split(r"[;,]|\band\b", tail, flags=re.I)
    else:
        # The ranks were split into their own clauses; each follower is one rank.
        phrases = []
        stop = False
        for follower in followers:
            item = re.sub(r"^\(?\d+\)\s*", "", follower.strip())
            # A rank is a short document name. Once prose resumes the list is over.
            head = re.split(r"(?<=[a-z])\.\s+[A-Z]", item)[0]
            if not head or len(head) > 200:
                break
            # Splitting is imperfect, so one "clause" may still hold several
            # ranks: "... (4) Supplemental Terms; (5) these General Terms".
            phrases.extend(re.split(r";|\band\b", head, flags=re.I))
            if head != item:
                # Prose resumed inside this item, so the ranked list ends here.
                stop = True
            if stop:
                break
    cleaned = [compact(phrase).strip(" .;,") for phrase in phrases]
    return [phrase for phrase in cleaned if len(phrase) > 3][:8]


def extract_precedence(
    family_id: str,
    instruments: Sequence[Instrument],
    clauses: Sequence[Clause],
) -> list[PrecedenceRule]:
    output: list[PrecedenceRule] = []
    instrument_lookup = {item.id: item for item in instruments}
    by_instrument: defaultdict[str, list[Clause]] = defaultdict(list)
    for clause in clauses:
        by_instrument[clause.document_id].append(clause)

    def add(
        higher: Instrument | None,
        lower: Instrument | None,
        clause: Clause,
        scope: dict[str, list[str]],
        rationale: str,
    ) -> None:
        if not higher or not lower or higher.id == lower.id:
            return
        key = (higher.id, lower.id, scope_label(scope), clause.id)
        if any(
            (
                item.higher_instrument_id,
                item.lower_instrument_id,
                scope_label(item.subject_scope),
                item.source_clause_id,
            )
            == key
            for item in output
        ):
            return
        output.append(
            PrecedenceRule(
                id=stable_id("precedence", *key),
                family_id=family_id,
                higher_instrument_id=higher.id,
                lower_instrument_id=lower.id,
                subject_scope=scope,
                source_clause_id=clause.id,
                evidence_span_ids=evidence_for_clause(clause),
                rationale=rationale,
            )
        )

    for clause in clauses:
        text = clause.text
        # Suffix-tolerant: real drafting says "prevails"/"controls"/"inconsistency",
        # and \bprevail\b matches none of them.
        if not re.search(
            r"\b(conflict\w*|inconsisten\w*|precedence|priorit\w*|prevail\w*"
            r"|control\w*|govern\w*|supersede\w*)\b",
            text,
            re.I,
        ):
            continue
        source = instrument_lookup[clause.document_id]
        dpa = instrument_for_reference("data processing addendum", source, instruments)
        order = instrument_for_reference("order schedule", source, instruments)
        master = instrument_for_reference("master agreement", source, instruments)
        handled = False
        if (
            source.instrument_class == "MASTER"
            and re.search(r"\bdata processing addendum\b", text, re.I)
            and re.search(r"\border schedule\b", text, re.I)
        ):
            add(
                dpa,
                order,
                clause,
                explicit_subject_scope("personal data"),
                "DPA is first for personal-data processing",
            )
            add(
                dpa,
                source,
                clause,
                explicit_subject_scope("personal data"),
                "DPA is first for personal-data processing",
            )
            add(
                order,
                source,
                clause,
                explicit_subject_scope("ordered cloud service"),
                "Order Schedule is next for the ordered service",
            )
            handled = True
        elif source.instrument_type == "DPA" and re.search(
            r"\bprevails?.+\bover\b|\bcontrols?.+\bover\b", text, re.I
        ):
            for lower in instruments:
                if lower.instrument_class in {"ORDER", "MASTER"}:
                    add(
                        source,
                        lower,
                        clause,
                        explicit_subject_scope("personal data"),
                        "DPA expressly prevails",
                    )
            handled = True
        elif source.instrument_class == "ORDER" and re.search(
            r"\bprevails?|controls?\b", text, re.I
        ):
            add(
                source,
                master,
                clause,
                subject_scope(text, default="product-specific terms"),
                "Order Schedule expressly controls for its product scope",
            )
            handled = True

        # Ranked "order of precedence" list -- the commonest real-world form.
        if not handled and PRECEDENCE_LADDER.search(text):
            followers = [
                candidate.text
                for candidate in by_instrument[source.id]
                if candidate.sequence > clause.sequence
            ][:8]
            phrases = ladder_phrases(text, followers)
            # Each rank may name a class of document, so a rung holds several
            # instruments that rank equally against the rungs above and below.
            ranked: list[list[Instrument]] = []
            seen_ids: set[str] = set()
            for phrase in phrases:
                rung = [
                    candidate
                    for candidate in instruments_for_reference(
                        phrase, source, instruments
                    )
                    if candidate.id not in seen_ids
                ]
                if rung:
                    seen_ids.update(candidate.id for candidate in rung)
                    ranked.append(rung)
            if LADDER_ASCENDING.search(text):
                ranked.reverse()
            # A ladder is transitive: record every pair, not only adjacent rungs, so
            # "does the order beat the master agreement" is answerable in one hop.
            for position, rung in enumerate(ranked):
                for higher in rung:
                    for lower_rung in ranked[position + 1 :]:
                        for lower in lower_rung:
                            add(
                                higher,
                                lower,
                                clause,
                                subject_scope(text, default="all conflicting terms"),
                                "stated order of precedence",
                            )
            if len(ranked) > 1:
                handled = True

        # Generic "X controls over Y and Z" form, read one sentence at a time so the
        # losing instruments are the ones this sentence actually names.
        generic = False
        if not handled:
            for sentence in re.split(r"(?<=[.;])\s+", text):
                match = re.search(
                    r"(?P<higher>this [A-Za-z ]+|(?:the )?[A-Za-z ]+?)\s+"
                    r"(?:prevails?|controls?)\s+(?:and\s+controls\s+)?over\s+"
                    r"(?P<lowers>.+)$",
                    sentence,
                    re.I,
                )
                if not match:
                    continue
                generic = True
                higher = instrument_for_reference(
                    compact(match.group("higher")), source, instruments
                )
                for phrase in re.split(r",|\band\b", match.group("lowers"), flags=re.I):
                    lower = instrument_for_reference(
                        compact(phrase), source, instruments
                    )
                    add(
                        higher,
                        lower,
                        clause,
                        subject_scope(sentence),
                        "express controls-over language",
                    )

        # Real agreements frequently omit "over" and name the conflicting instruments
        # earlier in the sentence: "In the event of any conflict between definitions
        # found in this License Model Schedule and definitions found in the EULA, the
        # definitions found in this License Model Schedule will control."
        if (
            not handled
            and not generic
            # A bare present-tense verb is only a precedence statement when the
            # clause frames a conflict; "Red Hat controls the repository" is not.
            and re.search(
                r"\b(conflict\w*|inconsisten\w*|precedence|priorit\w*)\b", text, re.I
            )
        ):
            # One clause often carries several independent precedence statements
            # ("...the Schedule shall take precedence. ...the order shall take
            # precedence."). Read each sentence on its own, or the losing
            # instrument of one sentence is attributed to the winner of another.
            for sentence in re.split(r"(?<=[.;])\s+", text):
                winner = re.search(
                    r"(?P<higher>(?:this|the)\s+[A-Za-z][A-Za-z ]{2,70}?)\s+"
                    # Real drafting says "the terms of this Product Appendix control"
                    # and "the Schedule shall take precedence" at least as often as
                    # "shall control"; requiring the modal loses most of it.
                    r"(?:(?:will|shall)\s+)?"
                    r"(?:controls?|prevails?|governs?|takes?\s+precedence)\b",
                    sentence,
                    re.I,
                )
                if not winner:
                    continue
                higher = instrument_for_reference(
                    compact(winner.group("higher")), source, instruments
                )
                seen: set[str] = set()
                for phrase in re.split(
                    r"\bbetween\b|[,;]|\band\b", sentence, flags=re.I
                ):
                    lower = instrument_for_reference(
                        compact(phrase), source, instruments
                    )
                    if not lower or lower.id in seen:
                        continue
                    seen.add(lower.id)
                    add(
                        higher,
                        lower,
                        clause,
                        subject_scope(sentence),
                        "express controls language naming the conflicting instruments",
                    )

        # "the order listed above" refers to instrument mentions in an earlier clause.
        if re.search(r"\border listed above\b", text, re.I):
            previous = [
                candidate
                for candidate in by_instrument[source.id]
                if candidate.sequence < clause.sequence
                and candidate.section_path == clause.section_path
            ]
            if previous:
                mentions: list[Instrument] = []
                prior_text = previous[-1].text
                for phrase in re.split(r"[,;]|\bthen\b", prior_text, flags=re.I):
                    candidate = instrument_for_reference(phrase, source, instruments)
                    if candidate and candidate.id not in {item.id for item in mentions}:
                        mentions.append(candidate)
                for high, low in zip(mentions, mentions[1:], strict=False):
                    add(
                        high,
                        low,
                        clause,
                        subject_scope(text),
                        "resolved order-listed-above back-reference",
                    )

    # A graph must never assert that each of two instruments outranks the other.
    # It happens when one clause ranks a class ("Supplemental Terms") that another
    # clause ranks individually, and no reader could act on the result. Where the
    # documents contradict, state neither direction rather than pick one.
    directed = {
        (item.higher_instrument_id, item.lower_instrument_id) for item in output
    }
    contradictory = {
        frozenset(pair) for pair in directed if (pair[1], pair[0]) in directed
    }
    if contradictory:
        output = [
            item
            for item in output
            if frozenset((item.higher_instrument_id, item.lower_instrument_id))
            not in contradictory
        ]
    return output


def extract_cross_references(
    family_id: str,
    clauses: Sequence[Clause],
) -> list[CrossReference]:
    by_instrument_section: dict[tuple[str, str], Clause] = {}
    for clause in clauses:
        base = clause.section_id.split(".", 2)
        by_instrument_section[(clause.document_id, clause.section_id)] = clause
        by_instrument_section.setdefault(
            (clause.document_id, ".".join(base[:2])), clause
        )
    output: list[CrossReference] = []
    for clause in clauses:
        for match in SECTION_REF.finditer(clause.text):
            target = by_instrument_section.get(
                (clause.document_id, match.group("section"))
            )
            context = clause.text[max(0, match.start() - 30) : match.end() + 30]
            relation = (
                "SUBJECT_TO"
                if re.search(r"\bsubject to\b", context, re.I)
                else "CROSS_REFERENCES"
            )
            output.append(
                CrossReference(
                    id=stable_id("crossref", clause.id, match.group(0)),
                    family_id=family_id,
                    source_clause_id=clause.id,
                    target_clause_id=target.id if target else "",
                    reference_text=match.group(0),
                    relationship_type=relation,
                    evidence_span_ids=evidence_for_clause(clause),
                    status="RESOLVED" if target else "UNRESOLVED",
                )
            )
        for match in re.finditer(
            r"\bin accordance with the ([A-Z][A-Za-z ]+Policy)(?:\s+at\s+\S+)?",
            clause.text,
        ):
            output.append(
                CrossReference(
                    id=stable_id("crossref", clause.id, match.group(0)),
                    family_id=family_id,
                    source_clause_id=clause.id,
                    target_clause_id="",
                    reference_text=match.group(0),
                    relationship_type="INCORPORATES_BY_REFERENCE",
                    evidence_span_ids=evidence_for_clause(clause),
                    status="UNRESOLVED",
                )
            )
    return output


def extract_amendments(
    family_id: str,
    instruments: Sequence[Instrument],
    clauses: Sequence[Clause],
) -> list[Amendment]:
    output: list[Amendment] = []
    instrument_lookup = {item.id: item for item in instruments}
    for clause in clauses:
        source = instrument_lookup[clause.document_id]
        if source.instrument_class != "AMENDMENT" and not re.search(
            r"\b(is hereby deleted|is amended by|deleted in its entirety)\b",
            clause.text,
            re.I,
        ):
            continue
        section_match = SECTION_REF.search(clause.text)
        if not section_match:
            continue
        lower = clause.text.lower()
        if "deleted and replaced" in lower:
            operation = "REPLACE"
        elif "amended by adding" in lower:
            operation = "ADD"
        elif "deleted in its entirety" in lower:
            operation = "DELETE"
        else:
            continue
        # The whole clause is the haystack here, not a reference phrase.
        target = instrument_for_reference(
            clause.text, source, instruments, require_name=False
        )
        if target and target.id == source.id:
            masters = [
                item for item in instruments if item.instrument_class == "MASTER"
            ]
            target = masters[0] if masters else None
        target_clause = next(
            (
                item
                for item in clauses
                if target
                and item.document_id == target.id
                and item.section_id.startswith(section_match.group("section"))
            ),
            None,
        )
        replacement = ""
        quoted = re.search(r'[“"](.+)[”"]', clause.text)
        if quoted:
            replacement = compact(quoted.group(1))
        output.append(
            Amendment(
                id=stable_id("amendment", clause.id, section_match.group("section")),
                family_id=family_id,
                amendment_instrument_id=source.id,
                source_clause_id=clause.id,
                target_instrument_id=target.id if target else "",
                target_section_id=section_match.group("section"),
                target_clause_id=target_clause.id if target_clause else "",
                operation=operation,
                replacement_text=replacement,
                effective_date=source.effective_date,
                evidence_span_ids=evidence_for_clause(clause),
                status="RESOLVED" if target_clause else "UNRESOLVED",
            )
        )
    return output


def precedence_path(
    higher: str, lower: str, rules: Sequence[PrecedenceRule]
) -> list[PrecedenceRule]:
    adjacency: defaultdict[str, list[PrecedenceRule]] = defaultdict(list)
    for rule in rules:
        if rule.status == "RESOLVED":
            adjacency[rule.higher_instrument_id].append(rule)
    queue: list[tuple[str, list[PrecedenceRule]]] = [(higher, [])]
    visited = {higher}
    while queue:
        current, path = queue.pop(0)
        for rule in adjacency[current]:
            if rule.lower_instrument_id == lower:
                return [*path, rule]
            if rule.lower_instrument_id not in visited:
                visited.add(rule.lower_instrument_id)
                queue.append((rule.lower_instrument_id, [*path, rule]))
    return []


def version_sort_key(instrument: Instrument) -> tuple:
    """Order editions of one document from oldest to newest.

    Prefers the effective date, which is unambiguous. Falls back to the version
    string read as a sequence of integers, so v7-2026 sorts above v1-2021 and
    v091524 above v061524 without needing to know each vendor's scheme.
    """

    numbers = tuple(int(part) for part in re.findall(r"\d+", instrument.version))
    return (instrument.effective_date or "", numbers, instrument.source)


def version_chains(instruments: Sequence[Instrument]) -> list[list[Instrument]]:
    """Group instruments that are successive editions of the same document.

    A corpus commonly holds a superseded schedule beside the current one -- the
    vendor publishes both. Treated as peers they are equally citable, and an
    answer may quote terms that were replaced. They are the same document when
    their titles and classification agree; only the date or version differs.
    """

    groups: defaultdict[tuple[str, str, str], list[Instrument]] = defaultdict(list)
    for instrument in instruments:
        # Strip whatever distinguishes the editions from the grouping key, or each
        # edition forms its own group and no chain is ever found. Siemens prints
        # "UNIVERSAL CUSTOMER AGREEMENT Status: March 19th, 2024" -- same document,
        # three different titles.
        key = re.split(
            r"\b(?:status|version|ver|dated?|last\s+modified|effective)\b",
            instrument.title,
            maxsplit=1,
            flags=re.I,
        )[0]
        key = re.sub(
            r"(?<![A-Za-z0-9])v\.?\s?[0-9][A-Za-z0-9._-]*", "", key, flags=re.I
        )
        key = re.sub(r"[^a-z0-9]+", " ", key.lower()).strip()
        groups[(key, instrument.instrument_class, instrument.instrument_type)].append(
            instrument
        )
    return [
        sorted(members, key=version_sort_key)
        # Every member needs a version or a date. With one missing, that edition
        # sorts to the front as though it were the oldest, and the chain is
        # recorded backwards -- asserting that a superseded edition replaced the
        # one in force. Saying nothing is the safe failure here.
        for members in groups.values()
        if len(members) > 1
        and all(item.version or item.effective_date for item in members)
    ]


def short_instrument_name(instrument: Instrument) -> str:
    """A recognisable short name for an instrument, for use in a family label."""

    title = re.split(
        r"\s+(?:Status|Version|Ver)\b", instrument.title, maxsplit=1, flags=re.I
    )[0]
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip(" .,;:-")
    # Long titles crowd the label; the leading words are the identifying ones.
    words = title.split()
    return " ".join(words[:5]) if len(words) > 5 else title


def family_title(instruments: Sequence[Instrument]) -> str:
    """Name a family after the set of documents in it, not after one of them.

    Naming the family "<first title> family" produced a node that looked like a
    duplicate of the document it was named after -- in the graph the two are
    indistinguishable once the label is truncated, and a reader reasonably asks
    why the agreement appears twice.
    """

    if not instruments:
        return "Empty agreement family"
    names = list(dict.fromkeys(short_instrument_name(item) for item in instruments))
    count = f"{len(instruments)} document{'s' if len(instruments) != 1 else ''}"
    listed = " + ".join(names[:2])
    if len(names) > 2:
        listed = f"{listed} + {len(names) - 2} more"
    return (
        f"Agreement family — {listed} ({count})"
        if listed
        else f"Agreement family ({count})"
    )


def build_relationships(
    family: AgreementFamily,
    instruments: Sequence[Instrument],
    clauses: Sequence[Clause],
    parties: Sequence[PartyRole],
    definitions: Sequence[DefinedTerm],
    rules: Sequence[OperativeRule],
    precedence: Sequence[PrecedenceRule],
    cross_refs: Sequence[CrossReference],
    amendments: Sequence[Amendment],
) -> list[Relationship]:
    output: list[Relationship] = []
    by_instrument = {item.id: item for item in instruments}

    def add(
        source: str,
        target: str,
        relation: str,
        label: str,
        evidence: Sequence[str] = (),
        scope: dict[str, list[str]] | None = None,
        status: str = "RESOLVED",
    ) -> None:
        if not source or not target:
            return
        key = (source, target, relation, scope_label(scope))
        if any(
            (item.source, item.target, item.type, scope_label(item.scope)) == key
            for item in output
        ):
            return
        output.append(
            Relationship(
                id=stable_id("relationship", *key),
                family_id=family.id,
                source=source,
                target=target,
                type=relation,
                label=label,
                evidence_span_ids=list(dict.fromkeys(evidence)),
                scope=scope or empty_scope(),
                status=status,
            )
        )

    for instrument in instruments:
        add(instrument.id, family.id, "BELONGS_TO", "belongs to agreement family")
    for chain in version_chains(instruments):
        # Newest first: every later edition supersedes all earlier ones, so a
        # reader landing on any edition can see it is not the operative text.
        for position, newer in enumerate(reversed(chain)):
            for older in list(reversed(chain))[position + 1 :]:
                add(
                    newer.id,
                    older.id,
                    "SUPERSEDES",
                    f"{newer.version or newer.effective_date} supersedes "
                    f"{older.version or older.effective_date}",
                )
    for clause in clauses:
        add(
            clause.document_id,
            clause.id,
            "CONTAINS",
            "contains clause",
            clause.evidence_span_ids,
        )
        if clause.parent_clause_id:
            add(
                clause.parent_clause_id,
                clause.id,
                "HAS_LIST_ITEM",
                "chapeau governs enumerated item",
                clause.evidence_span_ids,
            )
    for party in parties:
        add(
            party.instrument_id,
            party.id,
            "HAS_ROLE",
            "identifies contractual party or role",
            [party.evidence_span_id],
        )
    for definition in definitions:
        add(
            definition.id,
            definition.clause_id,
            "SUPPORTED_BY",
            "defined by exact text",
            definition.evidence_span_ids,
        )
    for rule in rules:
        add(
            rule.id,
            rule.clause_id,
            "SUPPORTED_BY",
            "supported by exact clause text",
            rule.evidence_span_ids,
            rule.scope,
        )
    for precedence_rule in precedence:
        add(
            precedence_rule.higher_instrument_id,
            precedence_rule.lower_instrument_id,
            "CONTROLS_FOR_DEFINED_SCOPE",
            precedence_rule.rationale or "controls for defined scope",
            precedence_rule.evidence_span_ids,
            precedence_rule.subject_scope,
            precedence_rule.status,
        )
        add(
            precedence_rule.id,
            precedence_rule.source_clause_id,
            "SUPPORTED_BY",
            "precedence derived from exact clause",
            precedence_rule.evidence_span_ids,
        )
    for reference in cross_refs:
        if reference.target_clause_id:
            add(
                reference.source_clause_id,
                reference.target_clause_id,
                reference.relationship_type,
                reference.reference_text,
                reference.evidence_span_ids,
                status=reference.status,
            )
    for amendment in amendments:
        if amendment.target_clause_id:
            add(
                amendment.source_clause_id,
                amendment.target_clause_id,
                "AMENDS",
                f"{amendment.operation.lower()} effective {amendment.effective_date}",
                amendment.evidence_span_ids,
                status=amendment.status,
            )

    # Resolve language showing an order entered under/governed by a master.
    masters = [item for item in instruments if item.instrument_class == "MASTER"]
    for instrument in instruments:
        if instrument.instrument_class != "ORDER":
            continue
        for clause in clauses:
            if clause.document_id != instrument.id or not re.search(
                r"\b(entered under|governed by)\b", clause.text, re.I
            ):
                continue
            target = instrument_for_reference(
                clause.text, instrument, instruments, require_name=False
            )
            if not target or target.id == instrument.id:
                target = masters[0] if masters else None
            if target:
                add(
                    instrument.id,
                    target.id,
                    "ENTERED_UNDER",
                    "expressly entered under or governed by",
                    clause.evidence_span_ids,
                )

    # Link competing definitions and identify a controller only when precedence supports it.
    definitions_by_term: defaultdict[str, list[DefinedTerm]] = defaultdict(list)
    for definition in definitions:
        definitions_by_term[definition.term.casefold()].append(definition)
    for term_definitions in definitions_by_term.values():
        if len(term_definitions) < 2:
            continue
        ordered = sorted(
            term_definitions,
            key=lambda item: (
                by_instrument[item.instrument_id].effective_date or "0000-00-00",
                item.id,
            ),
            reverse=True,
        )
        for later, earlier in zip(ordered, ordered[1:], strict=False):
            add(
                later.id,
                earlier.id,
                "REDEFINES",
                "same defined term appears in another instrument",
                [*later.evidence_span_ids, *earlier.evidence_span_ids],
            )
        for candidate in term_definitions:
            for other in term_definitions:
                if candidate.id == other.id:
                    continue
                path = precedence_path(
                    candidate.instrument_id, other.instrument_id, precedence
                )
                if not path:
                    continue
                evidence = [span for step in path for span in step.evidence_span_ids]
                add(
                    candidate.id,
                    other.id,
                    "CONTROLLING_DEFINITION",
                    "definition controls through scoped instrument precedence",
                    [*candidate.evidence_span_ids, *evidence],
                    path[-1].subject_scope,
                )

    # Defined-term usages point to a local or precedence-backed controlling definition.
    controlling_targets = {
        item.target: item.source
        for item in output
        if item.type == "CONTROLLING_DEFINITION"
    }
    definition_by_id = {item.id: item for item in definitions}
    for rule in rules:
        for _term, candidates in definitions_by_term.items():
            display_term = candidates[0].term
            if not re.search(rf"\b{re.escape(display_term)}s?\b", rule.evidence):
                continue
            local = next(
                (
                    definition
                    for definition in candidates
                    if definition.instrument_id == rule.document_id
                ),
                None,
            )
            target_id = local.id if local else candidates[0].id
            target_id = controlling_targets.get(target_id, target_id)
            definition = definition_by_id[target_id]
            add(
                rule.id,
                target_id,
                "USES_TERM",
                f"uses defined term {definition.term}",
                rule.evidence_span_ids,
            )

    # Relate competing operative rules only through evidence-backed precedence.
    for higher_rule in rules:
        for lower_rule in rules:
            # Action alone is far too coarse: "access_or_use" covers most of a licence
            # agreement, so matching on it pairs almost every rule with almost every
            # other one. Require the same object too, which also excludes rules whose
            # object could not be identified -- those cannot be shown to conflict.
            same_object = (
                bool(higher_rule.object) and higher_rule.object == lower_rule.object
            )
            same_legal_subject = (
                higher_rule.action == lower_rule.action and same_object
            ) or (
                {
                    higher_rule.action,
                    lower_rule.action,
                }
                == {"allocate", "access_or_use"}
                and "affiliate" in f"{higher_rule.object} {lower_rule.object}".lower()
            )
            if (
                higher_rule.id == lower_rule.id
                or higher_rule.document_id == lower_rule.document_id
                or not same_legal_subject
            ):
                continue
            path = precedence_path(
                higher_rule.document_id, lower_rule.document_id, precedence
            )
            if not path:
                continue
            precedence_evidence = [
                span for step in path for span in step.evidence_span_ids
            ]
            relation = (
                "OVERRIDES"
                if higher_rule.polarity != lower_rule.polarity
                else "QUALIFIES"
            )
            add(
                higher_rule.id,
                lower_rule.id,
                relation,
                "resolved through scoped instrument precedence",
                [
                    *higher_rule.evidence_span_ids,
                    *lower_rule.evidence_span_ids,
                    *precedence_evidence,
                ],
                path[-1].subject_scope,
            )
    return output


def canonical_graph(
    family: AgreementFamily,
    instruments: Sequence[Instrument],
    clauses: Sequence[Clause],
    parties: Sequence[PartyRole],
    definitions: Sequence[DefinedTerm],
    rules: Sequence[OperativeRule],
    precedence: Sequence[PrecedenceRule],
    cross_refs: Sequence[CrossReference],
    amendments: Sequence[Amendment],
    relationships: Sequence[Relationship],
    *,
    build_mode: str = "baseline",
    enrichment: dict | None = None,
) -> dict:
    nodes: list[dict] = [
        {
            "id": family.id,
            "label": family.title,
            "type": "agreement_family",
            "description": "Candidate family formed from this isolated upload session.",
        }
    ]
    for instrument in instruments:
        nodes.append(
            {
                "id": instrument.id,
                "label": instrument.title,
                "type": "document",
                "description": (
                    f"{instrument.instrument_class} / {instrument.instrument_type}; "
                    f"effective {instrument.effective_date or 'unknown'}"
                ),
                "source": instrument.source,
                "instrument_class": instrument.instrument_class,
                "instrument_type": instrument.instrument_type,
                "effective_date": instrument.effective_date,
            }
        )
    for party in parties:
        nodes.append(
            {
                "id": party.id,
                "label": party.role,
                "type": "party_or_role",
                "description": party.entity_name,
                "document_id": party.instrument_id,
            }
        )
    for clause in clauses:
        nodes.append(
            {
                "id": clause.id,
                "label": f"{clause.section_id} — {clause.heading}",
                "type": "clause",
                "description": clause.text,
                "document_id": clause.document_id,
                "source": clause.source,
                "section": clause.section_id,
                "scope": scope_label(clause.scope),
                "clause_kind": clause.clause_kind,
                "evidence_span_ids": clause.evidence_span_ids,
            }
        )
    for definition in definitions:
        nodes.append(
            {
                "id": definition.id,
                "label": f"Definition: “{definition.term}”",
                "type": "definition",
                "description": definition.definition,
                "document_id": definition.instrument_id,
                "clause_id": definition.clause_id,
                "evidence_span_ids": definition.evidence_span_ids,
            }
        )
    for rule in rules:
        nodes.append(
            {
                "id": rule.id,
                "label": f"{rule.effect}: {rule.action.replace('_', ' ')}",
                "type": "llm_rule" if rule.extraction_method == "lmstudio" else "rule",
                "description": rule.summary,
                "document_id": rule.document_id,
                "clause_id": rule.clause_id,
                "source": rule.source,
                "section": rule.section_id,
                "section_path": rule.section_path,
                "scope": scope_label(rule.scope),
                "structured_scope": rule.scope,
                "rule_type": rule.effect,
                "effect": rule.effect,
                "modality": rule.modality,
                "polarity": rule.polarity,
                "actor": rule.actor,
                "action": rule.action,
                "object": rule.object,
                "conditions": rule.conditions,
                "carve_outs": rule.carve_outs,
                "cross_refs": rule.cross_refs,
                "evidence": rule.evidence,
                "evidence_span_ids": rule.evidence_span_ids,
                "model": rule.model,
            }
        )
    for item in precedence:
        nodes.append(
            {
                "id": item.id,
                "label": f"Precedence: {scope_label(item.subject_scope)}",
                "type": "precedence_rule",
                "description": item.rationale,
                "clause_id": item.source_clause_id,
                "structured_scope": item.subject_scope,
                "evidence_span_ids": item.evidence_span_ids,
                "status": item.status,
            }
        )
    for amendment in amendments:
        nodes.append(
            {
                "id": amendment.id,
                "label": f"Amendment: {amendment.operation} §{amendment.target_section_id}",
                "type": "amendment",
                "description": amendment.replacement_text,
                "document_id": amendment.amendment_instrument_id,
                "clause_id": amendment.source_clause_id,
                "effective_date": amendment.effective_date,
                "status": amendment.status,
                "evidence_span_ids": amendment.evidence_span_ids,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "AgreementAtlas canonical legal records",
        "build_mode": build_mode,
        "documents": [item.public_record() for item in instruments],
        "nodes": nodes,
        "relationships": [record(item) for item in relationships],
        "unresolved": {
            "cross_references": sum(item.status != "RESOLVED" for item in cross_refs),
            "amendments": sum(item.status != "RESOLVED" for item in amendments),
            "precedence": sum(item.status != "RESOLVED" for item in precedence),
        },
        "enrichment": enrichment or {},
    }


def atomic_write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        for item in records:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    temporary.replace(path)


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, ensure_ascii=False)
    temporary.replace(path)


def graph_text(instrument: Instrument, clauses: Sequence[Clause]) -> str:
    parts = [
        f"[DOCUMENT] {instrument.title}",
        f"[SOURCE] {instrument.source}",
        f"[INSTRUMENT_CLASS] {instrument.instrument_class}",
        f"[INSTRUMENT_TYPE] {instrument.instrument_type}",
        f"[VERSION] {instrument.version or 'unknown'}",
        f"[EFFECTIVE_DATE] {instrument.effective_date or 'unknown'}",
    ]
    for clause in clauses:
        parts.extend(
            [
                "",
                f"[CLAUSE_ID] {clause.id}",
                f"[SECTION] {clause.section_id}",
                f"[SECTION_PATH] {clause.section_path}",
                f"[SCOPE] {scope_label(clause.scope)}",
                f"[CLAUSE_TEXT] {clause.text}",
            ]
        )
    return "\n".join(parts).strip() + "\n"


def rebuild_workspace(
    root: Path, classifier: InstrumentClassifier | None = None
) -> dict:
    sources = root / "sources"
    input_dir = root / "input"
    legal_dir = root / "legal"
    output_dir = root / "output"
    raw_dir = legal_dir / "raw"
    for directory in (sources, input_dir, legal_dir, output_dir, raw_dir):
        directory.mkdir(parents=True, exist_ok=True)

    source_payloads: list[tuple[Path, str, Instrument]] = []
    skipped: list[tuple[str, str]] = []
    for source in sorted(path for path in sources.iterdir() if path.is_file()):
        if source.suffix.lower() not in SUPPORTED:
            continue
        try:
            text = extract_source_text(source)
        except Exception as error:  # noqa: BLE001 - one bad upload must not stop the build
            # A corpus is a pile of files someone dropped in a folder: scanned PDFs,
            # spreadsheets whose converter is not installed, corrupt downloads. Skip
            # the file and report it; do not abandon the other thirty documents.
            skipped.append((source.name, f"{type(error).__name__}: {error}"))
            continue
        if not text.strip():
            skipped.append((source.name, "no extractable text (scanned or empty)"))
            continue
        source_payloads.append(
            (source, text, make_instrument(source, text, classifier=classifier))
        )
    family_id = stable_id("family", *(item.sha256 for _, _, item in source_payloads))
    instruments: list[Instrument] = []
    clauses: list[Clause] = []
    spans: list[EvidenceSpan] = []
    for source, text, instrument in source_payloads:
        instrument.family_id = family_id
        parsed_clauses, parsed_spans = parse_clauses(
            instrument, text, detect_pdf_headings(source)
        )
        instruments.append(instrument)
        clauses.extend(parsed_clauses)
        spans.extend(parsed_spans)
        (raw_dir / f"{source.stem}.txt").write_text(text, encoding="utf-8")
        (input_dir / f"{source.stem}.txt").write_text(
            graph_text(instrument, parsed_clauses), encoding="utf-8"
        )

    family = AgreementFamily(
        id=family_id,
        title=family_title(instruments),
        instrument_ids=[item.id for item in instruments],
    )
    parties = extract_parties(family_id, instruments, clauses)
    definitions = extract_definitions(family_id, clauses)
    rules = extract_rules(family_id, clauses, parties, spans)
    precedence = extract_precedence(family_id, instruments, clauses)
    cross_refs = extract_cross_references(family_id, clauses)
    amendments = extract_amendments(family_id, instruments, clauses)
    relationships = build_relationships(
        family,
        instruments,
        clauses,
        parties,
        definitions,
        rules,
        precedence,
        cross_refs,
        amendments,
    )
    graph = canonical_graph(
        family,
        instruments,
        clauses,
        parties,
        definitions,
        rules,
        precedence,
        cross_refs,
        amendments,
        relationships,
    )
    compatibility_rules = [item.compatibility_record() for item in rules]
    clause_by_id = {item.id: item for item in clauses}
    instrument_by_id = {item.id: item for item in instruments}
    for item in precedence:
        clause = clause_by_id[item.source_clause_id]
        higher = instrument_by_id[item.higher_instrument_id]
        lower = instrument_by_id[item.lower_instrument_id]
        compatibility_rules.append(
            {
                "id": item.id,
                "family_id": family_id,
                "document_id": clause.document_id,
                "clause_id": clause.id,
                "source": clause.source,
                "section_id": clause.section_id,
                "section_path": clause.section_path,
                "scope": scope_label(item.subject_scope),
                "structured_scope": item.subject_scope,
                "rule_type": "PRECEDENCE",
                "effect": "PRECEDENCE",
                "modality": "OTHER",
                "polarity": "POSITIVE",
                "actor": higher.title,
                "action": "control_precedence",
                "object": lower.title,
                "conditions": [],
                "carve_outs": [],
                "cross_refs": [],
                "evidence_span_ids": item.evidence_span_ids,
                "evidence": clause.text,
                "summary": item.rationale,
                "extraction_method": "deterministic",
                "model": "",
                "schema_version": SCHEMA_VERSION,
            }
        )

    record_sets: dict[str, Iterable[dict]] = {
        "agreement_families.jsonl": [record(family)],
        "instruments.jsonl": [record(item) for item in instruments],
        "documents.jsonl": [item.public_record() for item in instruments],
        "parties.jsonl": [record(item) for item in parties],
        "clauses.jsonl": [record(item) for item in clauses],
        "evidence_spans.jsonl": [record(item) for item in spans],
        "defined_terms.jsonl": [record(item) for item in definitions],
        "operative_rules.jsonl": [record(item) for item in rules],
        "rules.jsonl": compatibility_rules,
        "precedence_rules.jsonl": [record(item) for item in precedence],
        "cross_references.jsonl": [record(item) for item in cross_refs],
        "amendments.jsonl": [record(item) for item in amendments],
        "relationships.jsonl": [record(item) for item in relationships],
    }
    for name, items in record_sets.items():
        atomic_write_jsonl(legal_dir / name, items)
    atomic_write_json(
        legal_dir / "schema.json",
        {
            "schema_version": SCHEMA_VERSION,
            "build_mode": "baseline",
            "canonical_records": sorted(record_sets),
        },
    )
    atomic_write_json(output_dir / "legal_relationship_graph.json", graph)
    enriched = output_dir / "legal_relationship_graph_enriched.json"
    if enriched.exists():
        enriched.unlink()
    return {
        "schema_version": SCHEMA_VERSION,
        "build_mode": "baseline",
        "documents": len(instruments),
        "instruments": len(instruments),
        "parties": len(parties),
        "clauses": len(clauses),
        "definitions": len(definitions),
        "rules": len(rules),
        "precedence_rules": len(precedence),
        "cross_references": len(cross_refs),
        "amendments": len(amendments),
        "graph_nodes": len(graph["nodes"]),
        "graph_relationships": len(graph["relationships"]),
        "skipped": [{"file": name, "reason": reason} for name, reason in skipped],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build AgreementAtlas schema-v3 legal records and graph projection."
    )
    parser.add_argument("--root", type=Path, default=Path("knowledge"))
    parser.add_argument(
        "--classify-model",
        default="",
        help=(
            "LM Studio model id used to classify documents the deterministic rules "
            "cannot place. One call per document; ignored if LM Studio is "
            "unreachable."
        ),
    )
    args = parser.parse_args()
    classifier = None
    if args.classify_model:
        # Imported lazily so the deterministic build never requires a model.
        from legal_graph_service import lm_instrument_classifier
        from lmstudio_client import LMStudioClient

        classifier = lm_instrument_classifier(LMStudioClient(), args.classify_model)
    print(
        json.dumps(
            rebuild_workspace(args.root.resolve(), classifier=classifier), indent=2
        )
    )


if __name__ == "__main__":
    main()
