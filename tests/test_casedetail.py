"""Tests for the shared CNR case-detail parser.

The two portals return the same page shape with different column counts and
inconsistent class-name casing, so both are exercised against the same parser.
"""

from datetime import date

import pytest

from bharat_courts.casedetail import parse_case_detail, parse_flexible_date

# ------------------------------------------------------------------
# Date handling — these pages mix three formats
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27-06-2025", date(2025, 6, 27)),
        ("2026-08-17", date(2026, 8, 17)),
        ("08th September 2026", date(2026, 9, 8)),
        ("29th October 2018", date(2018, 10, 29)),
        ("1 March 2020", date(2020, 3, 1)),
        ("", None),
        ("--", None),
        ("None", None),
        ("not a date", None),
    ],
)
def test_parse_flexible_date(raw, expected):
    assert parse_flexible_date(raw) == expected


# ------------------------------------------------------------------
# High Court page
# ------------------------------------------------------------------


def test_hc_case_detail_core(hcservices_case_detail_html):
    d = parse_case_detail(hcservices_case_detail_html, cnr="GJHC240464312025")

    assert d.cnr_number == "GJHC240464312025"
    # HC has no Case Type row; it is derived from the registration number
    assert d.case_type == "LPA"
    assert d.filing_number == "LPA /20486/2025"
    assert d.filing_date == date(2025, 6, 27)
    assert d.registration_date == date(2025, 7, 21)

    # the fields plain case search leaves empty
    assert d.next_hearing_date == date(2026, 9, 8)
    assert d.case_stage == "192-NOTICE & ADJOURNED MATTERS"
    assert d.coram == "HONOURABLE MR.JUSTICE A B EXAMPLE"
    assert d.bench_type == "DIVISION"
    assert d.state == "GUJARAT"
    assert d.first_hearing_date is None
    assert d.is_disposed is False


def test_hc_parties_survive_malformed_br(hcservices_case_detail_html):
    """Entries run together across malformed </br>, and bar numbers must not
    be mistaken for the "N)" numbering."""
    d = parse_case_detail(hcservices_case_detail_html)

    assert [p.name for p in d.petitioners] == ["ABC INDUSTRIES LTD"]
    assert d.petitioners[0].advocate.startswith("MS. PRIYA SHARMA(1234)")

    assert len(d.respondents) == 3
    assert d.respondents[2].name == "BRANCH MANAGER, FIRST RESPONDENT BANK"
    # "(3802)" must not split the entry
    assert d.respondents[1].advocate == "MR. A B MEHTA(3802)"
    # an unrepresented party simply has no advocate line
    assert d.respondents[0].advocate == "NOTICE THROUGH RPAD NOT RECEIVED BACK"


def test_hc_reads_both_history_tables(hcservices_case_detail_html):
    """HC splits history across "on Filing Number" and "Case History"."""
    d = parse_case_detail(hcservices_case_detail_html)
    assert len(d.history) == 3
    assert d.history[0].hearing_date == date(2025, 7, 11)
    assert d.history[0].cause_list_type == "OFFICE OBJECTIONS BOARD"
    assert d.history[1].hearing_date == date(2026, 9, 8)
    assert d.history[1].business_date == date(2026, 8, 6)
    # header rows must not leak in as hearings
    assert all(h.purpose != "Purpose of Hearing" for h in d.history)


def test_hc_acts_and_orders(hcservices_case_detail_html):
    d = parse_case_detail(
        hcservices_case_detail_html, base_url="https://hcservices.ecourts.gov.in/hcservices"
    )
    assert [(a.act, a.sections) for a in d.acts] == [("LETTERS PATENT, 1865", "15")]

    assert len(d.orders) == 1
    order = d.orders[0]
    assert order.order_date == date(2025, 7, 25)
    assert order.order_type == "LPA/872/2025"
    assert order.judge == "HONOURABLE MR. JUSTICE C D SAMPLE"
    assert order.pdf_url.startswith("https://hcservices.ecourts.gov.in/hcservices/cases/")


# ------------------------------------------------------------------
# District Court page
# ------------------------------------------------------------------


def test_district_case_detail_core(districtcourts_case_detail_html):
    d = parse_case_detail(districtcourts_case_detail_html, cnr="GJRJ060015282018")

    assert d.cnr_number == "GJRJ060015282018"
    assert d.case_type == "SPCS - SPECIAL CIVIL SUIT"
    assert d.first_hearing_date == date(2018, 10, 29)
    assert d.next_hearing_date == date(2026, 8, 17)
    assert d.case_stage == "PLAINTIFF EVIDENCE"
    # district reports a combined court/judge where HC reports coram
    assert d.court_number_and_judge == "1-PRINCIPAL SENIOR CIVIL JUDGE & ADDL. CJM"
    assert d.coram == ""


def test_district_cnr_strips_trailing_note(districtcourts_case_detail_html):
    """The page appends "(Note the CNR number...)" to the value."""
    d = parse_case_detail(districtcourts_case_detail_html)
    assert d.cnr_number == "GJRJ060015282018"


def test_district_parties(districtcourts_case_detail_html):
    d = parse_case_detail(districtcourts_case_detail_html)
    assert len(d.petitioners) == 2
    assert len(d.respondents) == 4
    assert d.respondents[1].name == "SECOND RESPONDENT"
    assert d.respondents[1].advocate == ""
    assert d.respondents[3].advocate == "R A SAMPLE"


def test_district_history_has_no_header_row(districtcourts_case_detail_html):
    """District history tables are 4 columns with no header."""
    d = parse_case_detail(districtcourts_case_detail_html)
    assert len(d.history) == 3
    first = d.history[0]
    assert first.hearing_date == date(2026, 8, 17)
    assert first.business_date == date(2026, 7, 18)
    assert first.purpose == "PLAINTIFF EVIDENCE"
    assert first.judge == "PRINCIPAL SENIOR CIVIL JUDGE"
    assert first.cause_list_type == ""


def test_district_acts_lowercase_class_and_orders(districtcourts_case_detail_html):
    """acts_table here vs Acts_table on HC; orders are 3 columns not 5."""
    d = parse_case_detail(districtcourts_case_detail_html)
    assert d.acts[0].act == "SPECIFIC RELIEF ACT, 1963"
    assert d.acts[0].sections == "34,38,32"  # trailing comma stripped
    assert len(d.orders) == 1
    assert d.orders[0].order_date == date(2019, 11, 13)
    assert d.orders[0].judge == ""


# ------------------------------------------------------------------
# Degenerate input
# ------------------------------------------------------------------


def test_empty_page_yields_empty_detail():
    d = parse_case_detail("<html><body></body></html>", cnr="GJHC240464312025")
    assert d.cnr_number == "GJHC240464312025"
    assert d.petitioners == [] and d.history == [] and d.orders == []
