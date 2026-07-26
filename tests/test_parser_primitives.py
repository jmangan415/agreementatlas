"""Unit tests for the deterministic agreement parser.

The deterministic layer is regex-driven and fails silently -- an unsupported
numbering style still yields clauses, rules and a graph, just wrong ones. Every
test here pins behaviour that a real document has already broken at least once.

All fixtures are synthetic. Never paste licensed agreement text into this file;
use `scripts/parse_health.py` against `knowledge/sources` for real documents.
"""

from __future__ import annotations

import unittest

from legal_ingest import (
    GENERIC_CLASSIFICATION,
    actor_from_text,
    classify_instrument,
    clean_lines,
    extract_definitions,
    extract_precedence,
    extract_rules,
    family_title,
    find_version,
    instruments_for_reference,
    ladder_phrases,
    modality_and_polarity,
    paragraph_stream,
    parse_clauses,
    parse_iso_date,
    run_in_heading,
    split_definition_block,
    split_list_group,
    title_block,
    validated_classification,
    version_chains,
)
from legal_schema import Instrument, PartyRole


def instrument(**overrides) -> Instrument:
    defaults = {
        "id": "instrument:test",
        "family_id": "family:test",
        "source": "test-agreement.md",
        "title": "Test Master Agreement",
        "instrument_class": "MASTER",
        "instrument_type": "MSA",
        "version": "",
        "effective_date": "2026-01-01",
        "signature_date": "",
        "term_start": "",
        "term_end": "",
        "sha256": "0" * 64,
        "title_evidence": "Test Master Agreement",
    }
    defaults.update(overrides)
    return Instrument(**defaults)


def sections(text: str) -> list[str]:
    return [section for section, _, _ in paragraph_stream(clean_lines(text))]


class TitleBlockTests(unittest.TestCase):
    """Cover pages lead with furniture; the title is rarely the first line."""

    def test_date_banner_is_not_taken_as_the_title(self) -> None:
        # A real schedule titled itself "April 2024", which then matched every
        # instrument cross-reference by word overlap and broke precedence.
        title, _ = title_block(
            ["April 2024", "License Model Schedule", "Effective 1 May"]
        )
        self.assertEqual(title, "License Model Schedule")

    def test_version_and_page_furniture_are_skipped(self) -> None:
        for furniture in ("v3.0", "Version 2.1", "Page 1 of 40", "CONFIDENTIAL"):
            with self.subTest(furniture=furniture):
                title, _ = title_block([furniture, "Cloud Services Agreement"])
                self.assertEqual(title, "Cloud Services Agreement")

    def test_a_normal_first_line_is_still_the_title(self) -> None:
        title, _ = title_block(["Master Subscription Agreement", "between X and Y"])
        self.assertEqual(title, "Master Subscription Agreement")

    def test_all_furniture_falls_back_rather_than_returning_empty(self) -> None:
        title, _ = title_block(["April 2024", "v1.0"])
        self.assertTrue(title)


class SectionNumberingTests(unittest.TestCase):
    """Numbering styles differ between vendors and even between instruments."""

    def test_inline_numbered_headings(self) -> None:
        text = "1. Definitions\n\nSome text.\n\n2. Licence Grant\n\nMore text.\n"
        self.assertEqual(sections(text), ["1", "2"])

    def test_bare_number_on_its_own_line_after_its_heading(self) -> None:
        # Two-column PDFs put the number in a gutter, so converters emit it after
        # the text it labels. This produced 100% "Preamble" clauses.
        text = "Licence Grant\n\n3.0\n\nProvider grants Customer a licence.\n"
        self.assertIn("3.0", sections(text))

    def test_subsection_number_keeps_its_clause_text(self) -> None:
        text = (
            "Allocation of Licences. Customer may allocate licences to Affiliates.\n\n"
            "3.3\n\nprovided Customer remains responsible.\n"
        )
        collected = list(paragraph_stream(clean_lines(text)))
        self.assertTrue(any(section == "3.3" for section, _, _ in collected))
        body = " ".join(paragraph for _, _, paragraph in collected)
        self.assertIn("may allocate licences to Affiliates", body)

    def test_section_heading_becomes_the_heading_not_the_body(self) -> None:
        text = "Restrictions\n\n5.0\n\nCustomer must not resell the Service.\n"
        for section, heading, paragraph in paragraph_stream(clean_lines(text)):
            if section == "5.0":
                self.assertEqual(heading, "Restrictions")
                self.assertNotIn("Restrictions", paragraph)

    def test_consecutive_bare_numbers_do_not_crash(self) -> None:
        self.assertTrue(sections("Direct Orders. Text here.\n\n6.0\n\n6.1\n\nMore.\n"))

    def test_run_in_heading_takes_only_the_lead_sentence(self) -> None:
        self.assertEqual(
            run_in_heading("Grant of Licence. Provider grants a licence to Customer."),
            "Grant of Licence",
        )

    def test_run_in_heading_rejects_a_whole_paragraph(self) -> None:
        self.assertEqual(run_in_heading("x" * 200), "")


class DefinitionBlockTests(unittest.TestCase):
    """Agreements routinely run an entire definitions article into one block."""

    BLOCK = (
        '"Affiliate" means an entity under common control; '
        '"Customer Data" means data supplied by Customer; '
        '"Service" means the cloud service ordered under an Order.'
    )

    def test_packed_block_splits_into_one_clause_per_term(self) -> None:
        prefix, definitions = split_definition_block(self.BLOCK)
        self.assertEqual(prefix, "")
        self.assertEqual(
            [term for term, _ in definitions], ["Affiliate", "Customer Data", "Service"]
        )

    def test_single_definition_is_left_alone(self) -> None:
        _, definitions = split_definition_block('"Affiliate" means an entity.')
        self.assertEqual(definitions, [])

    def test_text_before_the_first_definition_is_preserved(self) -> None:
        prefix, definitions = split_definition_block("In this Agreement: " + self.BLOCK)
        self.assertEqual(prefix, "In this Agreement:")
        self.assertEqual(len(definitions), 3)

    def test_definitions_survive_into_extracted_terms(self) -> None:
        # A definition block containing lettered sub-parts was previously consumed
        # by list detection, so only the first definition was ever recognised.
        text = (
            "1. Definitions\n\n"
            '"Confidential Information" means information which: (a) is marked '
            "confidential; or (b) would reasonably be understood to be confidential; "
            '"Service" means the cloud service.\n'
        )
        clauses, _ = parse_clauses(instrument(), text)
        terms = {item.term for item in extract_definitions("family:test", clauses)}
        self.assertIn("Confidential Information", terms)
        self.assertIn("Service", terms)


class ListGroupTests(unittest.TestCase):
    """Chapeau plus lettered items: the negation lives in the chapeau."""

    def test_chapeau_and_items_are_separated(self) -> None:
        chapeau, items = split_list_group(
            "Customer must not: (a) resell the Service; (b) reverse engineer it."
        )
        self.assertEqual(chapeau, "Customer must not")
        self.assertEqual([label for label, _ in items], ["a", "b"])

    def test_list_items_inherit_the_chapeau_negation(self) -> None:
        text = (
            "5. Restrictions\n\n"
            "Customer must not: (a) resell the Service; (b) reverse engineer it.\n"
        )
        clauses, _ = parse_clauses(instrument(), text)
        items = [c for c in clauses if c.clause_kind == "LIST_ITEM"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertTrue(item.chapeau_clause_id)

    def test_a_paragraph_without_a_colon_is_not_a_list(self) -> None:
        chapeau, items = split_list_group("See (a) above and (b) below.")
        self.assertEqual((chapeau, items), ("", []))


class ModalityTests(unittest.TestCase):
    """Negation decides whether a clause permits or forbids."""

    def test_permission_obligation_and_prohibition(self) -> None:
        cases = {
            "Customer may use the Service.": "PERMISSION",
            "Customer must pay the Fees.": "OBLIGATION",
            "Customer must not resell the Service.": "PROHIBITION",
            "Customer may not assign this Agreement.": "PROHIBITION",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                effect, _, _ = modality_and_polarity(text)
                self.assertEqual(effect, expected)

    def test_negation_is_not_lost(self) -> None:
        _, _, negative = modality_and_polarity("Customer shall not disclose the terms.")
        _, _, positive = modality_and_polarity("Customer shall disclose the terms.")
        self.assertNotEqual(negative, positive)


class DateTests(unittest.TestCase):
    def test_common_formats_normalise_to_iso(self) -> None:
        for value in ("29 April 2024", "April 29, 2024", "2024-04-29"):
            with self.subTest(value=value):
                self.assertEqual(parse_iso_date(value), "2024-04-29")

    def test_nonsense_is_rejected_rather_than_guessed(self) -> None:
        for value in ("", "sometime in 2024", "the Effective Date"):
            with self.subTest(value=value):
                self.assertEqual(parse_iso_date(value), "")


class ClassificationTests(unittest.TestCase):
    """A body mention must never outrank the document's own title."""

    def test_master_agreement_is_not_reclassified_by_a_body_mention(self) -> None:
        body = "This Agreement covers data processing and personal data extensively."
        klass, _ = classify_instrument("Cloud Master Agreement", "master.pdf", body)
        self.assertEqual(klass, "MASTER")

    def test_addendum_and_order_are_distinguished_from_master(self) -> None:
        cases = {
            "Data Processing Addendum": "ADDENDUM",
            "StreamFlow Order Schedule": "ORDER",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                klass, _ = classify_instrument(title, "doc.pdf", "Some body text.")
                self.assertEqual(klass, expected)


class PrecedenceTests(unittest.TestCase):
    """Precedence is scoped, and real clauses often omit the word "over"."""

    def build(self, higher_title: str, lower_title: str, clause_text: str):
        higher = instrument(
            id="instrument:higher", title=higher_title, instrument_class="ANNEX"
        )
        lower = instrument(
            id="instrument:lower", title=lower_title, instrument_class="MASTER"
        )
        clauses, _ = parse_clauses(higher, f"1. Precedence\n\n{clause_text}\n")
        return extract_precedence("family:test", [higher, lower], clauses)

    def test_controls_over_form(self) -> None:
        rules = self.build(
            "Data Processing Addendum",
            "Cloud Master Agreement",
            "This Data Processing Addendum prevails and controls over the Cloud "
            "Master Agreement.",
        )
        self.assertTrue(rules)

    def test_will_control_form_without_the_word_over(self) -> None:
        # Real drafting names the loser first and omits "over" entirely; this form
        # produced zero precedence rules on a real schedule.
        rules = self.build(
            "License Model Schedule",
            "End User License Agreement",
            "In the event of any conflict between definitions found in this License "
            "Model Schedule and definitions found in the End User License Agreement, "
            "the definitions found in this License Model Schedule will control.",
        )
        self.assertTrue(rules)
        self.assertEqual(rules[0].higher_instrument_id, "instrument:higher")
        self.assertEqual(rules[0].lower_instrument_id, "instrument:lower")

    def test_precedence_carries_its_subject_scope(self) -> None:
        rules = self.build(
            "License Model Schedule",
            "End User License Agreement",
            "In the event of any conflict between definitions in this License Model "
            "Schedule and the End User License Agreement, the definitions in this "
            "License Model Schedule will control.",
        )
        self.assertTrue(rules)
        self.assertIn("definitions", rules[0].subject_scope.get("subject_matter", []))

    def test_ordinary_prose_does_not_create_precedence(self) -> None:
        self.assertFalse(
            self.build(
                "Order Schedule",
                "Cloud Master Agreement",
                "Provider will control access to the Service using its own systems.",
            )
        )


class ModelClassificationTests(unittest.TestCase):
    """A model may only refine a classification, never invent one."""

    def test_known_pairings_are_accepted(self) -> None:
        for value in (
            ("ANNEX", "SERVICE_DESCRIPTION"),
            ("ANNEX", "ADDITIONAL_LICENSE_AUTHORIZATIONS"),
            ("ADDENDUM", "DPA"),
            ("POLICY", "AUP"),
        ):
            with self.subTest(value=value):
                self.assertEqual(validated_classification(value), value)

    def test_case_and_whitespace_are_normalised(self) -> None:
        self.assertEqual(
            validated_classification((" annex ", "service_description")),
            ("ANNEX", "SERVICE_DESCRIPTION"),
        )

    def test_mismatched_or_invented_values_are_discarded(self) -> None:
        for value in (
            ("ANNEX", "DPA"),  # type belongs to another class
            ("MASTER", "SERVICE_DESCRIPTION"),
            ("CONTRACT", "MSA"),  # invented class
            ("ANNEX", "SOMETHING_NEW"),
            None,
            "ANNEX",
            ("ANNEX",),
        ):
            with self.subTest(value=value):
                self.assertIsNone(validated_classification(value))

    def test_generic_classification_is_what_triggers_the_model(self) -> None:
        # A document the deterministic rules cannot place must return the generic
        # pairing, otherwise the model is never consulted.
        self.assertEqual(
            classify_instrument("Service Description", "svc.pdf", "Some body text."),
            GENERIC_CLASSIFICATION,
        )
        self.assertNotEqual(
            classify_instrument("Data Processing Addendum", "dpa.pdf", "text"),
            GENERIC_CLASSIFICATION,
        )


if __name__ == "__main__":
    unittest.main()


class ClauseTextRetentionTests(unittest.TestCase):
    """A numbered line is not always a heading -- sometimes it *is* the clause."""

    def test_numbered_definition_stays_in_the_clause_text(self) -> None:
        # Treating everything after "2.3." as a section title deleted the
        # definition from the clause body, so a document with 54 defined terms
        # recorded none while still looking healthy in the section index.
        clauses, _ = parse_clauses(
            instrument(),
            '2.3. "Broadcom Software" means the computer software programs '
            "licensed under this Agreement.",
        )
        self.assertTrue(
            any("means the computer software" in clause.text for clause in clauses)
        )
        definitions = extract_definitions("family:test", clauses)
        self.assertEqual([item.term for item in definitions], ["Broadcom Software"])

    def test_a_genuine_section_title_is_still_a_heading(self) -> None:
        clauses, _ = parse_clauses(
            instrument(), "1. DEFINITIONS\n\nCapitalised terms have these meanings."
        )
        self.assertEqual(clauses[0].heading, "DEFINITIONS")
        self.assertNotIn("DEFINITIONS", clauses[0].text)

    def test_run_in_heading_keeps_the_sentence_that_follows_it(self) -> None:
        # "13.8 Complete Agreement and Order of Precedence. The Agreement ..."
        # put the operative text in the heading field, where nothing reads it.
        clauses, _ = parse_clauses(
            instrument(),
            "12.3. Order of Precedence. Any conflict between the documents shall "
            "be resolved by the order stated below.",
        )
        self.assertTrue(any("Any conflict between" in c.text for c in clauses))


class GutterNumberingTests(unittest.TestCase):
    """Two-column PDFs emit the section number on a line of its own."""

    def test_number_preceding_its_text_is_adopted(self) -> None:
        # Cloud Software Group prints "1.1." in the gutter above the clause. With
        # the trailing period unmatched, 62% of the agreement fell into Preamble.
        text = (
            "1.\n\nDefinitions\n\n1.1.\n\n"
            '"Affiliate" means any entity which controls a party.\n\n'
            "1.2.\n\n"
            '"Agreement" means the End User Agreement and any Order.\n'
        )
        self.assertEqual(sections(text)[-2:], ["1.1", "1.2"])

    def test_an_unrelated_previous_paragraph_is_not_stolen_as_a_heading(self) -> None:
        text = (
            '"Authorized Reseller" means Company authorised distributors.\n\n'
            "2.1.\n\nCustomer shall pay all fees when due.\n"
        )
        clauses = list(paragraph_stream(clean_lines(text)))
        body = [value for section, _, value in clauses if section == "2.1"]
        self.assertEqual(body, ["Customer shall pay all fees when due."])


class ReferenceResolutionTests(unittest.TestCase):
    """Resolving a reference phrase to the instrument it names."""

    def setUp(self) -> None:
        self.eula = instrument(
            id="i:eula", title="Micro Focus End User License Agreement"
        )
        self.ala = instrument(
            id="i:ala",
            title="Additional License Authorizations For AccuRev software products",
            instrument_class="ANNEX",
            instrument_type="ADDITIONAL_LICENSE_AUTHORIZATIONS",
        )
        self.all = [self.eula, self.ala]

    def test_acronym_resolves_by_title_initials(self) -> None:
        self.assertEqual(
            [
                item.id
                for item in instruments_for_reference(
                    "the applicable ALA", self.eula, self.all
                )
            ],
            ["i:ala"],
        )

    def test_two_letter_tokens_are_not_treated_as_acronyms(self) -> None:
        # "the EU Standard Contractual Clauses" matched "End User Agreement" by
        # initials and inverted the rule it appeared in.
        eua = instrument(id="i:eua", title="End User Agreement")
        self.assertEqual(
            instruments_for_reference(
                "the EU Standard Contractual Clauses", self.ala, [eua, self.ala]
            ),
            [],
        )

    def test_prose_fragments_do_not_name_a_document(self) -> None:
        for phrase in (
            "ANY OTHER TERMS PROVIDED TO CUSTOMER REGARDING CUSTOMER'S USE OF "
            "THE LICENSED SOFTWARE",
            "UNLESS A DIFFERENT WRITTEN AGREEMENT IS EXPRESSLY REFERENCED IN A "
            "PRODUCT ORDER OR EXECUTED BY BOTH PARTIES",
        ):
            with self.subTest(phrase=phrase[:30]):
                self.assertEqual(
                    instruments_for_reference(phrase, self.eula, self.all), []
                )

    def test_this_document_resolves_to_the_source_mid_phrase(self) -> None:
        # "the terms of this Product Appendix control" names the speaking
        # document; matching only a leading "this " picked an arbitrary sibling.
        self.assertEqual(
            instruments_for_reference(
                "the terms of this Product Appendix", self.ala, self.all
            ),
            [self.ala],
        )

    def test_a_qualifying_tail_is_trimmed_from_the_name(self) -> None:
        dpa = instrument(id="i:dpa", title="Data Processing Addendum")
        self.assertEqual(
            [
                item.id
                for item in instruments_for_reference(
                    "the Data Processing Addendum (DPA) to the extent one is in place "
                    "between the Parties",
                    self.eula,
                    [self.eula, dpa],
                )
            ],
            ["i:dpa"],
        )


class PrecedenceLadderTests(unittest.TestCase):
    """The ranked "order of precedence" list, the commonest real-world form."""

    def test_inline_ladder_is_read_in_order(self) -> None:
        self.assertEqual(
            ladder_phrases(
                "Any conflicting terms will be resolved according to the following "
                "order of precedence: the applicable Product Order, the applicable "
                "ALA, and this Agreement.",
                [],
            ),
            ["the applicable Product Order", "the applicable ALA", "this Agreement"],
        )

    def test_ranks_split_into_following_clauses_are_collected(self) -> None:
        self.assertEqual(
            ladder_phrases(
                "Any conflict shall be resolved according to the following order "
                "of precedence, from the document with the greatest control to "
                "the least",
                ["the Transaction Document", "the Data Processing Addendum"],
            ),
            ["the Transaction Document", "the Data Processing Addendum"],
        )

    def test_the_list_stops_when_prose_resumes(self) -> None:
        self.assertEqual(
            ladder_phrases(
                "subject to the following order of precedence",
                [
                    "an Order,",
                    "this End User Agreement. No terms in a purchase order add to "
                    "the Agreement.",
                ],
            ),
            ["an Order", "this End User Agreement"],
        )

    def test_a_section_heading_of_the_same_name_is_not_a_ladder(self) -> None:
        # "13.8 Complete Agreement and Order of Precedence. The Agreement ..."
        self.assertEqual(
            ladder_phrases(
                "Complete Agreement and Order of Precedence. The Agreement "
                "represents the complete agreement between the parties.",
                [],
            ),
            [],
        )

    def test_ladder_produces_transitive_rules_in_the_stated_direction(self) -> None:
        master = instrument(id="i:eua", source="eua.pdf", title="End User Agreement")
        schedule = instrument(
            id="i:but",
            source="but.pdf",
            title="Cloud Software Group Business Unit Terms",
            instrument_class="ANNEX",
            instrument_type="PRODUCT_TERMS",
        )
        clauses, _ = parse_clauses(
            master,
            "15.4. Order of Precedence. Any conflict between these terms and any "
            "supplementary terms is subject to the following order of precedence: "
            "the Business Unit Terms, and this End User Agreement.",
        )
        rules = extract_precedence("family:test", [master, schedule], clauses)
        self.assertEqual(
            [(r.higher_instrument_id, r.lower_instrument_id) for r in rules],
            [("i:but", "i:eua")],
        )


class VersionChainTests(unittest.TestCase):
    """A vendor publishes the superseded edition beside the one in force."""

    def test_version_is_read_from_text_or_filename(self) -> None:
        for text, name, expected in (
            ("Business Support Agreement version 5.4", "a.pdf", "5.4"),
            ("Controlled Doc. # EDCS-24218913 Ver: 6.0 Last Modified", "a.pdf", "6.0"),
            ("SAP Software Use Rights v.7-2026", "a.pdf", "7-2026"),
            # The "v" follows an underscore, where \b does not match.
            ("no version here", "UCA_v1.4_2024-03-19.pdf", "1.4"),
            ("no version here", "lic_toma_v070124_us.pdf", "070124"),
        ):
            with self.subTest(name=name):
                self.assertEqual(find_version(text, name), expected)

    def test_editions_are_chained_oldest_to_newest(self) -> None:
        chain = version_chains(
            [
                instrument(
                    id="i:new",
                    source="sur-v7.pdf",
                    title="Use Rights",
                    version="7-2026",
                ),
                instrument(
                    id="i:old",
                    source="sur-v1.pdf",
                    title="Use Rights",
                    version="1-2021",
                ),
            ]
        )
        self.assertEqual(
            [[item.id for item in group] for group in chain], [["i:old", "i:new"]]
        )

    def test_a_status_date_in_the_title_does_not_split_the_chain(self) -> None:
        # Siemens prints "UNIVERSAL CUSTOMER AGREEMENT Status: March 19th, 2024",
        # so every edition carries a different title for the same document.
        chain = version_chains(
            [
                instrument(
                    id="i:1.0",
                    source="a.pdf",
                    title="UCA Status: November 1, 2021",
                    version="1.0",
                ),
                instrument(
                    id="i:1.4",
                    source="b.pdf",
                    title="UCA Status: March 19th, 2024",
                    version="1.4",
                ),
                instrument(
                    id="i:1.1",
                    source="c.pdf",
                    title="UCA Status: April 25th, 2022",
                    version="1.1",
                ),
            ]
        )
        self.assertEqual(
            [[item.id for item in group] for group in chain],
            [["i:1.0", "i:1.1", "i:1.4"]],
        )

    def test_an_undated_edition_suppresses_the_chain(self) -> None:
        # Without a version it sorts first and the chain is recorded backwards,
        # asserting that a superseded edition replaced the one in force.
        self.assertEqual(
            version_chains(
                [
                    instrument(
                        id="i:a",
                        source="a.pdf",
                        title="Use Rights",
                        version="",
                        effective_date="",
                    ),
                    instrument(
                        id="i:b",
                        source="b.pdf",
                        title="Use Rights",
                        version="2.0",
                        effective_date="",
                    ),
                ]
            ),
            [],
        )


class ContradictionTests(unittest.TestCase):
    def test_opposing_precedence_rules_are_both_withdrawn(self) -> None:
        # One clause ranks a class, another ranks its members individually, and
        # the graph ends up asserting that each document outranks the other.
        alpha = instrument(id="i:a", source="a.pdf", title="Alpha Supplemental Terms")
        beta = instrument(id="i:b", source="b.pdf", title="Beta Offer Description")
        clauses, _ = parse_clauses(
            alpha,
            "1. Precedence. In the event of a conflict the Beta Offer Description "
            "prevails over the Alpha Supplemental Terms. In the event of a "
            "conflict the Alpha Supplemental Terms prevail over the Beta Offer "
            "Description.",
        )
        rules = extract_precedence("family:test", [alpha, beta], clauses)
        self.assertEqual(rules, [])


class FamilyTitleTests(unittest.TestCase):
    """The family is a set of documents, not a copy of one of them."""

    def test_family_is_named_after_the_set(self) -> None:
        title = family_title(
            [
                instrument(
                    id="i:a", title="End User License Agreement (UK and Ireland)"
                ),
                instrument(id="i:b", title="License Model Schedule"),
            ]
        )
        self.assertIn("2 documents", title)
        self.assertIn("End User License Agreement", title)
        self.assertIn("License Model Schedule", title)
        # The old label was "<first title> family", indistinguishable from the
        # document node it was named after once the graph truncated it.
        self.assertFalse(title.startswith("End User License Agreement"))

    def test_editions_of_one_document_are_not_listed_twice(self) -> None:
        title = family_title(
            [
                instrument(
                    id="i:1", title="UCA Status: November 1, 2021", version="1.0"
                ),
                instrument(
                    id="i:2", title="UCA Status: March 19th, 2024", version="1.4"
                ),
            ]
        )
        self.assertEqual(title.count("UCA"), 1)
        self.assertIn("2 documents", title)

    def test_an_empty_family_still_has_a_name(self) -> None:
        self.assertEqual(family_title([]), "Empty agreement family")


class MixedClauseTests(unittest.TestCase):
    """One clause commonly grants and forbids at the same time."""

    def rules_for(self, text: str):
        clauses, _ = parse_clauses(instrument(), f"1. Terms\n\n{text}\n")
        return extract_rules("family:test", clauses, [])

    def test_an_obligation_and_a_prohibition_are_kept_apart(self) -> None:
        rules = self.rules_for(
            "Customer shall maintain accurate records and shall not disclose "
            "the Software to any third party."
        )
        self.assertEqual(
            [(rule.effect, rule.polarity) for rule in rules],
            [("OBLIGATION", "POSITIVE"), ("PROHIBITION", "NEGATIVE")],
        )

    def test_a_permission_is_not_swallowed_by_a_later_prohibition(self) -> None:
        # Read whole, this clause reported that backup copies are forbidden --
        # the opposite of what it grants.
        rules = self.rules_for(
            "Licensee may copy the Software for backup purposes but must not "
            "distribute it."
        )
        effects = [rule.effect for rule in rules]
        self.assertIn("PERMISSION", effects)
        self.assertIn("PROHIBITION", effects)

    def test_evidence_quotes_the_statement_not_the_whole_clause(self) -> None:
        rules = self.rules_for(
            "Licensee may copy the Software but must not distribute it."
        )
        permission = next(rule for rule in rules if rule.effect == "PERMISSION")
        self.assertNotIn("must not", permission.evidence)

    def test_a_single_statement_is_not_split(self) -> None:
        for text in (
            "Customer shall pay all fees when due.",
            "Licensee shall not copy, modify or distribute the Software.",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(self.rules_for(text)), 1)

    def test_a_trailing_statement_inherits_the_named_actor(self) -> None:
        rules = self.rules_for(
            "Customer shall protect credentials and shall not share them."
        )
        self.assertTrue(all(rule.actor for rule in rules), [r.actor for r in rules])
        self.assertEqual(len({rule.actor for rule in rules}), 1)

    def test_chapeau_negation_still_reaches_its_list_items(self) -> None:
        # Splitting must not strip the modality a list item inherits.
        clauses, _ = parse_clauses(
            instrument(),
            "5. Restrictions\n\nCustomer shall not: (a) reverse engineer the "
            "Software; or (b) share administrator credentials.\n",
        )
        rules = extract_rules("family:test", clauses, [])
        items = [
            rule
            for rule in rules
            if "reverse engineer" in rule.evidence or "administrator" in rule.evidence
        ]
        self.assertTrue(items)
        for rule in items:
            self.assertEqual((rule.effect, rule.polarity), ("PROHIBITION", "NEGATIVE"))


class RuleFidelityTests(unittest.TestCase):
    """What each rule claims must match what its clause actually says."""

    def rules_and_spans(self, text: str):
        clauses, spans = parse_clauses(instrument(), f"1. Licensing\n\n{text}\n")
        collected = list(spans)
        rules = extract_rules("family:test", clauses, [], collected)
        return rules, {span.id: span for span in collected}, clauses[0]

    def test_may_only_is_a_limited_permission_not_a_prohibition(self) -> None:
        # "may only" reads as a grant subject to a limit. Treating "only" as a
        # negation produced PROHIBITION with modality MAY -- self-contradictory,
        # and it discarded the permission entirely.
        rules, _, _ = self.rules_and_spans(
            "Licensee may only allocate Software Licenses to Clients owned by "
            "Licensee or its Affiliates."
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].effect, "PERMISSION")
        self.assertEqual(rules[0].polarity, "POSITIVE")
        self.assertTrue(rules[0].carve_outs, "the limit must be recorded")

    def test_an_unmatched_action_uses_the_verb_in_the_text(self) -> None:
        # The action list is a closed set; falling back to "govern" asserted an
        # action the clause never mentions.
        rules, _, _ = self.rules_and_spans(
            "Software Licenses cannot be shared or exchanged between Clients."
        )
        self.assertEqual(len(rules), 1)
        self.assertNotEqual(rules[0].action, "govern")
        self.assertIn(rules[0].action, {"shared", "share"})

    def test_every_rule_cites_an_exact_slice_of_its_clause(self) -> None:
        rules, spans, clause = self.rules_and_spans(
            "Licensee must purchase a Software License for each Client. "
            "Licenses cannot be shared between Clients. "
            "A License may be re-allocated when a Client is decommissioned."
        )
        self.assertGreaterEqual(len(rules), 3)
        for rule in rules:
            span = spans[rule.evidence_span_ids[0]]
            self.assertEqual(
                clause.text[span.start : span.end].strip(),
                span.text,
                "span offsets must locate the quoted text",
            )
            self.assertIn(span.text, clause.text)

    def test_a_prohibition_does_not_cite_the_permission_beside_it(self) -> None:
        rules, spans, _ = self.rules_and_spans(
            "Licensee may copy the Software for backup purposes. "
            "Licensee must not distribute the Software."
        )
        prohibition = next(rule for rule in rules if rule.effect == "PROHIBITION")
        quoted = spans[prohibition.evidence_span_ids[0]].text
        self.assertNotIn("backup", quoted)


class PassiveVoiceActorTests(unittest.TestCase):
    """A passive clause names what is acted on, not who acts."""

    def roles(self):
        return [
            PartyRole(
                id="party:licensee",
                family_id="family:test",
                instrument_id="instrument:test",
                entity_name="Acme Ltd",
                role="Licensee",
                is_signatory=True,
                evidence_span_id="span:1",
            )
        ]

    def test_the_object_is_not_recorded_as_the_actor(self) -> None:
        # "The Software Licenses may not be allocated" recorded the licences
        # themselves as the actor: the noun before the modal is what the action
        # is done to.
        self.assertEqual(
            actor_from_text(
                "The Software Licenses may not be allocated to any other party",
                self.roles(),
            ),
            "",
        )

    def test_a_named_agent_in_a_passive_clause_is_still_found(self) -> None:
        self.assertEqual(
            actor_from_text(
                "The Software may not be copied by Licensee without consent",
                self.roles(),
            ),
            "Licensee",
        )

    def test_active_voice_is_unaffected(self) -> None:
        self.assertEqual(
            actor_from_text(
                "Licensee must purchase an additional Software License", self.roles()
            ),
            "Licensee",
        )

    def test_actor_and_object_are_never_the_same_phrase(self) -> None:
        clauses, _ = parse_clauses(
            instrument(),
            "3. Allocation\n\nThe Software Licenses may not be allocated to any "
            "other party.\n",
        )
        rules = extract_rules("family:test", clauses, self.roles())
        self.assertTrue(rules)
        for rule in rules:
            if rule.actor and rule.object:
                self.assertNotEqual(rule.actor.lower(), rule.object.lower())
