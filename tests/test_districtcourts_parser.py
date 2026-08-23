"""Tests for District Courts parsers."""

from datetime import date
from pathlib import Path

import pytest

from bharat_courts.districtcourts.endpoints import (
    ajax_headers,
    components_js_url,
    parse_ajax_header_spec,
    parse_delimeter,
)
from bharat_courts.districtcourts.parser import (
    CaptchaError,
    InvalidRequestError,
    MalformedResponseError,
    ServerError,
    parse_ajax_response,
    parse_case_status_html,
    parse_cause_list_html,
    parse_complex_value,
    parse_court_orders_html,
    parse_option_tags,
    parse_state_options,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------
# AJAX response envelope
# ------------------------------------------------------------------


def test_parse_ajax_response_success():
    raw = '{"status": 1, "app_token": "abc123", "party_data": "<table></table>"}'
    result = parse_ajax_response(raw)
    assert result["status"] == 1
    assert result["app_token"] == "abc123"
    assert result["party_data"] == "<table></table>"


def test_parse_ajax_response_captcha_error():
    raw = '{"status": 0, "app_token": "xyz", "div_captcha": "<img...>"}'
    with pytest.raises(CaptchaError):
        parse_ajax_response(raw)


def test_parse_ajax_response_server_error():
    raw = '{"status": 1, "errormsg": "Session expired"}'
    with pytest.raises(ServerError):
        parse_ajax_response(raw)


def test_parse_ajax_response_invalid_request_is_distinguishable():
    """A stale anti-bot 'delimeter' header yields the portal's generic
    rejection. It must be its own type so the client can re-scrape + retry
    instead of surfacing it as an unrecoverable server error."""
    raw = (
        '{"errormsg": "<strong>Oops!</strong>There is something wrong.....!!!, '
        "Invalid Request...!Try once again <br/><a href='/ecourtindia_v6'>"
        'Click here to go Home Page</a>", "app_token": ""}'
    )
    with pytest.raises(InvalidRequestError):
        parse_ajax_response(raw)


def test_parse_ajax_response_invalid_request_older_wording():
    """The message already drifted once; match the stable phrase, not the dots."""
    raw = '{"errormsg": "There is something wrong..!!!, Invalid Request...!Try once again"}'
    with pytest.raises(InvalidRequestError):
        parse_ajax_response(raw)


def test_parse_ajax_response_bad_captcha_in_errormsg_is_captcha_error():
    """The portal reports a wrong CAPTCHA through the generic errormsg channel.
    It must surface as CaptchaError so the retry loop gets a fresh session
    rather than aborting the query on one bad OCR guess."""
    raw = '{"status": 1, "errormsg": "Invalid Captcha... ", "app_token": "t"}'
    with pytest.raises(CaptchaError):
        parse_ajax_response(raw)


def test_invalid_request_still_caught_as_server_error():
    """Existing callers that catch ServerError keep working."""
    raw = '{"errormsg": "Invalid Request...!Try once again"}'
    with pytest.raises(ServerError):
        parse_ajax_response(raw)


def test_parse_ajax_response_with_bom():
    raw = '\ufeff{"status": 1, "app_token": "tok1", "data": "ok"}'
    result = parse_ajax_response(raw)
    assert result["app_token"] == "tok1"


# The portal answers search endpoints with a plain empty string (or stray
# HTML) fairly often. Returning a fake {"status": 0} dict here made that
# indistinguishable from "0 matching cases" downstream (#26) — it must raise
# a retryable error instead.


def test_parse_ajax_response_non_json_raises():
    with pytest.raises(MalformedResponseError):
        parse_ajax_response("<html>error page</html>")


def test_parse_ajax_response_empty_body_raises():
    with pytest.raises(MalformedResponseError):
        parse_ajax_response("")


def test_parse_ajax_response_non_object_json_raises():
    with pytest.raises(MalformedResponseError):
        parse_ajax_response("[1, 2, 3]")


def test_malformed_response_still_caught_as_server_error():
    """Existing callers that catch ServerError keep working."""
    with pytest.raises(ServerError):
        parse_ajax_response("")


# ------------------------------------------------------------------
# Option tag parsing
# ------------------------------------------------------------------


def test_parse_option_tags():
    html = (FIXTURES_DIR / "districtcourts_districts.html").read_text()
    result = parse_option_tags(html)
    assert "1" in result
    assert result["1"] == "Patna"
    assert "24" in result
    assert result["24"] == "Araria"
    # Placeholder should be filtered out
    assert "" not in result
    assert len(result) == 5


def test_parse_option_tags_empty():
    result = parse_option_tags('<option value="0">Select district</option>')
    assert result == {}


def test_parse_option_tags_complexes():
    html = (FIXTURES_DIR / "districtcourts_complexes.html").read_text()
    result = parse_option_tags(html)
    assert len(result) == 3
    assert "1080010@2,3,4@Y" in result
    assert result["1080010@2,3,4@Y"] == "Civil Court, Patna Sadar"


# ------------------------------------------------------------------
# State dropdown parsing (#25 — codes drift; the live dropdown is truth)
# ------------------------------------------------------------------

_STATE_PAGE = """
<html><body>
<select name='sess_state_code' id='sess_state_code' onchange='fillDistrict(this.value);'>
<option value='0'>Select state</option>
<option value='26'  >Delhi</option>
<option value='7'  >Jharkhand</option>
<option value='1'  >Maharashtra</option>
</select>
<select id='sess_dist_code'><option value='9'>Decoy</option></select>
</body></html>
"""


def test_parse_state_options():
    states = parse_state_options(_STATE_PAGE)
    # only the state dropdown, not the district decoy; placeholder filtered
    assert states == {"26": "Delhi", "7": "Jharkhand", "1": "Maharashtra"}


def test_parse_state_options_missing_dropdown():
    assert parse_state_options("<html><body>maintenance page</body></html>") == {}


# ------------------------------------------------------------------
# Complex value parsing
# ------------------------------------------------------------------


def test_parse_complex_value_with_flag():
    code, ests, needs_est = parse_complex_value("1080010@2,3,4@Y")
    assert code == "1080010"
    assert ests == ["2", "3", "4"]
    assert needs_est is True


def test_parse_complex_value_no_flag():
    code, ests, needs_est = parse_complex_value("1080010@5,6@N")
    assert code == "1080010"
    assert ests == ["5", "6"]
    assert needs_est is False


def test_parse_complex_value_simple():
    code, ests, needs_est = parse_complex_value("12345")
    assert code == "12345"
    assert ests == []
    assert needs_est is False


# ------------------------------------------------------------------
# Case status HTML parsing
# ------------------------------------------------------------------


def test_parse_case_status_html():
    html = (FIXTURES_DIR / "districtcourts_case_status.html").read_text()
    results = parse_case_status_html(html)

    assert len(results) == 3

    case1 = results[0]
    assert case1.case_number == "CS/123/2024"
    assert case1.case_type == "CS"
    assert case1.petitioner == "Ram Kumar Singh"
    assert case1.respondent == "State of Bihar"
    assert case1.cnr_number == "BHAR010001232024"
    assert case1.registration_date == date(2024, 1, 15)
    assert case1.status == "Pending"
    assert case1.next_hearing_date == date(2026, 4, 25)

    case2 = results[1]
    assert case2.case_number == "CRA/456/2023"
    assert case2.petitioner == "Sita Devi"
    assert case2.respondent == "Manoj Kumar"
    assert case2.status == "Disposed"
    assert case2.next_hearing_date is None

    case3 = results[2]
    assert case3.case_number == "MJC/789/2024"
    assert case3.petitioner == "ABC Enterprises Pvt Ltd"
    # No CNR in onclick for case3
    assert case3.cnr_number == ""


def test_parse_case_status_html_live_format():
    """Test with the real portal format (4 columns, <br>Vs</br> separator)."""
    html = (FIXTURES_DIR / "districtcourts_case_status_live.html").read_text()
    results = parse_case_status_html(html)

    assert len(results) == 3

    case1 = results[0]
    assert case1.case_number == "Title Appeal/47/2024"
    assert case1.case_type == "Title Appeal"
    assert case1.petitioner == "Bankim Chand and 15 others"
    assert case1.respondent == "Surendra Prasad Sah and 32 others"
    assert case1.cnr_number == "BRPA010216322024"

    case2 = results[1]
    assert case2.petitioner == "Indu Devi and another"
    assert case2.respondent == "Arun Kumar Sharma and 2 others"
    assert case2.cnr_number == "BRPA010207032024"

    # Third row uses <strong> tags (the other format)
    case3 = results[2]
    assert case3.petitioner == "Rajesh Verma"
    assert case3.respondent == "Municipal Corporation"
    assert case3.cnr_number == "BRPA010099992024"


def test_parse_case_status_html_empty():
    result = parse_case_status_html("<div>No records found</div>")
    assert result == []


def test_parse_case_status_html_empty_table():
    html = "<table><thead><tr><th>Sr</th></tr></thead><tbody></tbody></table>"
    result = parse_case_status_html(html)
    assert result == []


# ------------------------------------------------------------------
# Court orders HTML parsing
# ------------------------------------------------------------------


def test_parse_court_orders_html():
    html = (FIXTURES_DIR / "districtcourts_court_orders.html").read_text()
    results = parse_court_orders_html(
        html, base_url="https://services.ecourts.gov.in/ecourtindia_v6"
    )

    assert len(results) == 2

    order1 = results[0]
    assert order1.order_date == date(2024, 3, 15)
    assert order1.order_type == "Interim Order"
    assert order1.judge == "Sri Amit Kumar, ADJ-1"
    assert "display_pdf.php" in order1.pdf_url

    order2 = results[1]
    assert order2.order_date == date(2024, 1, 10)
    assert order2.order_type == "Order"
    assert order2.pdf_url.startswith("https://")


def test_parse_court_orders_html_empty():
    result = parse_court_orders_html("<p>No orders</p>")
    assert result == []


# ------------------------------------------------------------------
# Cause list HTML parsing
# ------------------------------------------------------------------


def test_parse_cause_list_html():
    html = (FIXTURES_DIR / "districtcourts_cause_list.html").read_text()
    results = parse_cause_list_html(html)

    assert len(results) == 2

    entry1 = results[0]
    assert entry1.serial_number == 1
    assert entry1.case_number == "CS/100/2024"
    assert entry1.case_type == "CS"
    assert entry1.petitioner == "Rajesh Verma"
    assert entry1.respondent == "Municipal Corporation"
    assert entry1.advocate_petitioner == "Adv. A.K. Mishra"
    assert entry1.court_number == "Court No. 3"
    assert entry1.judge == "Sri R.K. Jha, ADJ-3"

    entry2 = results[1]
    assert entry2.case_number == "CRA/200/2023"
    assert entry2.petitioner == "State of Bihar"


def test_parse_cause_list_html_empty():
    result = parse_cause_list_html("<div>No cause list</div>")
    assert result == []


# ------------------------------------------------------------------
# Anti-bot "delimeter" scraping (portal change ~2026-07-22)
# ------------------------------------------------------------------


def test_parse_delimeter_extracts_rotating_secret():
    js = 'function ajaxCall(jsonobj)\n{\n\tvar delimeter="73vmgasjxcminndsf846Pq";\t\n'
    assert parse_delimeter(js) == "73vmgasjxcminndsf846Pq"


def test_parse_delimeter_accepts_single_quotes():
    assert parse_delimeter("var delimeter = 'abc123';") == "abc123"


def test_parse_delimeter_missing_returns_empty():
    """Absent literal must not raise — the POST then fails portal-side with a
    clear message instead of a confusing local exception."""
    assert parse_delimeter("var somethingElse = 1;") == ""


def test_components_js_url_prefers_cache_busted_reference():
    """Read the exact build the portal is serving, matching what a browser
    would load, rather than a bare path that may be cached differently."""
    html = '<script src="/ecourtindia_v6/js/components.js?v=1784899796"></script>'
    assert components_js_url(html).endswith("/js/components.js?v=1784899796")


def test_components_js_url_falls_back_to_bare_path():
    assert components_js_url("<html></html>").endswith("/js/components.js")


def test_ajax_headers_default_to_last_known_good_pair():
    """With no scraped spec, fall back to the pair the portal wanted as of
    2026-08-04 — both headers carry the same secret."""
    assert ajax_headers("SECRET") == {"delimeter": "SECRET", "Xgy786trbsd7y": "SECRET"}


def test_ajax_headers_omit_empty_delimeter():
    """Send no header at all rather than an empty one we know is wrong."""
    assert ajax_headers("") == {}


def test_ajax_headers_follow_scraped_spec():
    spec = {"delimeter": None, "somethingNew": None, "static": "xyz"}
    assert ajax_headers("SECRET", spec) == {
        "delimeter": "SECRET",
        "somethingNew": "SECRET",
        "static": "xyz",
    }


def test_ajax_headers_empty_spec_falls_back():
    """An unparseable components.js must not silently strip all headers."""
    assert ajax_headers("SECRET", {}) == {"delimeter": "SECRET", "Xgy786trbsd7y": "SECRET"}


# ------------------------------------------------------------------
# Anti-bot header-name scraping (portal change 2026-08-04, #17)
# ------------------------------------------------------------------


def test_parse_ajax_header_spec_reads_current_portal_shape():
    """Verbatim from the live components.js on 2026-08-04: the old fixed
    'abc: xyz' decoy is gone, replaced by a second header carrying the same
    rotating secret."""
    js = """
    function ajaxCall(jsonobj)
    {
        var delimeter="b64ttds7eew4";
        $.ajax({
            type: "POST",
            headers: {
        "delimeter": delimeter,
                "Xgy786trbsd7y":delimeter
            },
        });
    }
    """
    assert parse_ajax_header_spec(js) == {"delimeter": None, "Xgy786trbsd7y": None}


def test_parse_ajax_header_spec_reads_previous_portal_shape():
    """The pre-2026-08-04 shape (fixed decoy) must still parse, so a rollback
    on the portal side doesn't break us again."""
    js = 'var delimeter="x";\nheaders: {"delimeter": delimeter, "abc": "xyz"},'
    assert parse_ajax_header_spec(js) == {"delimeter": None, "abc": "xyz"}


def test_parse_ajax_header_spec_skips_blocks_without_the_secret():
    """components.js may hold unrelated ajax calls; only the block carrying the
    rotating secret is the anti-bot one."""
    js = 'headers: {"Accept": "application/json"},\nheaders: {"delimeter": delimeter},'
    assert parse_ajax_header_spec(js) == {"delimeter": None}


def test_parse_ajax_header_spec_ignores_unresolvable_identifiers():
    js = 'headers: {"delimeter": delimeter, "other": someUnknownVar},'
    assert parse_ajax_header_spec(js) == {"delimeter": None}


def test_parse_ajax_header_spec_missing_returns_empty():
    """Absent block must not raise — the caller falls back to known-good names."""
    assert parse_ajax_header_spec("var somethingElse = 1;") == {}
