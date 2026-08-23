"""Shared parser for the full case page returned by a CNR lookup.

Both portals answer a CNR search with the same *page*, not the JSON envelope
that case searches use:

- High Court (``hcservices``) returns the HTML directly.
- District (``ecourtindia_v6``) returns JSON with the HTML in ``casetype_list``.

The markup is close enough to share one parser. Where it differs it differs in
column count rather than in structure, so layout is detected from the row
shape instead of a portal flag:

- history rows are 5 columns on High Court (cause list type, judge, business
  date, hearing date, purpose) and 4 on district (no cause list type);
- order rows are 5 columns on High Court (number, order-on, judge, date,
  details) and 3 on district (number, date, details).

Two traps the class names set:

- the acts table is ``Acts_table`` on High Court and ``acts_table`` on
  district, so class matching has to be case-insensitive;
- ``transfer_table`` means *Document Details* on High Court but *case
  transfers* on district, so it is matched by heading, never by class.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from bharat_courts.models import ActEntry, CaseDetail, CaseOrder, HearingEntry, PartyEntry

logger = logging.getLogger(__name__)

#: "08th September 2026" — used by the Case Status block on both portals.
_LONG_DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})")

#: Party blocks read "1) NAME" then an optional "Advocate- X, Y" line.
#: The numbering cannot be anchored to line starts: the markup uses malformed
#: ``</br>`` tags, so entries often run together ("...R.A.SHARMA7)  ALTAF").
#: The lookbehind keeps bar-council numbers from being mistaken for numbering —
#: "PLEADER(1)" is preceded by "(" and "PATEL(3802)" by a digit, so neither
#: splits, while "SHARMA7)" does.
_PARTY_SPLIT_RE = re.compile(r"(?<![\d(])\d{1,3}\)\s*")
_ADVOCATE_RE = re.compile(r"^advocate\s*[-:]?\s*", re.I)

_NULLISH = {"", "-", "--", "none", "null", "na", "n/a"}


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def parse_flexible_date(raw: str) -> date | None:
    """Parse the several date shapes these pages mix.

    The Case Details block uses ``DD-MM-YYYY`` while Case Status spells dates
    out ("08th September 2026"), and some fields arrive ISO.
    """
    text = (raw or "").strip()
    if text.lower() in _NULLISH:
        return None

    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    m = _LONG_DATE_RE.search(text)
    if m:
        day, month, year = m.groups()
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(f"{day} {month} {year}", fmt).date()
            except ValueError:
                pass

    logger.debug("Could not parse date: %s", raw)
    return None


def _labelled_values(table: Tag | None) -> dict[str, str]:
    """Read a label/value table into a dict.

    Rows carry either one or two label/value pairs, so cells are walked in
    twos rather than assuming a fixed width.
    """
    out: dict[str, str] = {}
    if table is None:
        return out
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        for i in range(0, len(cells) - 1, 2):
            label = _text(cells[i]).rstrip(":").strip()
            value = _text(cells[i + 1])
            if label:
                out[label.lower()] = value
    return out


def _table_after_heading(soup: BeautifulSoup, heading: str) -> Tag | None:
    """Find the first table following a heading whose text matches."""
    node = soup.find(string=lambda s: bool(s) and s.strip().lower() == heading.lower())
    if node is None:
        return None
    parent = node.find_parent()
    return parent.find_next("table") if parent else None


def _parties(soup: BeautifulSoup, css_class: str) -> list[PartyEntry]:
    """Parse a petitioner/respondent block.

    Both portals tag these with the same class but different elements — a
    ``<span>`` on High Court, a ``<ul>`` on district — so the class is matched
    and the element type ignored. Entries are separated by "1)", "2)" … and an
    advocate line is optional: unrepresented parties simply have none.
    """
    node = soup.find(class_=css_class)
    if node is None:
        return []

    # <br> carries the line structure here; turn it into real newlines.
    for br in node.find_all("br"):
        br.replace_with("\n")
    raw = node.get_text("\n")
    raw = raw.replace("\xa0", " ")

    entries: list[PartyEntry] = []
    for chunk in _PARTY_SPLIT_RE.split(raw):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        name, advocate = "", ""
        for line in lines:
            if _ADVOCATE_RE.match(line):
                advocate = _ADVOCATE_RE.sub("", line).strip()
            elif not name:
                name = line
        if name:
            entries.append(PartyEntry(name=name, advocate=advocate))
    return entries


def _acts(soup: BeautifulSoup) -> list[ActEntry]:
    table = soup.find("table", class_=re.compile(r"acts_table", re.I))
    if table is None:
        return []
    out = []
    for row in table.find_all("tr")[1:]:  # first row is the header
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2:
            act = _text(cells[0])
            if act:
                out.append(ActEntry(act=act, sections=_text(cells[1]).rstrip(",")))
    return out


def _history(soup: BeautifulSoup) -> list[HearingEntry]:
    """Read every history table on the page, in document order.

    High Court pages carry two: "Case History on Filing Number" covers the
    pre-registration hearings and "Case History" the rest. Both are real
    listings, so both are included rather than picking one.

    Both the row scan and the cell scan are scoped to their nearest ancestor,
    because Gujarat HC nests the orders table *inside* the second history
    table. An unqualified ``find_all("tr")`` returns the orders — header row
    and all — as hearings, and an unqualified ``find_all(["td", "th"])`` on
    the wrapping row pulls the nested table's cells up into it, which puts the
    whole orders table back in the count as one more bogus five-column row.
    Verified on GJHC240464312025: 16 real listings, and 6 rows of order links
    reading "View" that must not become diary entries.
    """
    tables = soup.find_all("table", class_=re.compile(r"history_table", re.I))
    if not tables:
        return []
    out: list[HearingEntry] = []
    rows = [r for t in tables for r in t.find_all("tr") if r.find_parent("table") is t]
    for row in rows:
        cells = [_text(c) for c in row.find_all(["td", "th"]) if c.find_parent("tr") is row]
        if not cells or not any(cells):
            continue
        # High Court prints a header row per table; district prints none.
        joined = " ".join(cells).lower()
        if "purpose of hearing" in joined or "cause list type" in joined:
            continue
        if len(cells) >= 5:
            cause_list_type, judge, business, hearing, purpose = cells[:5]
        elif len(cells) == 4:
            cause_list_type = ""
            judge, business, hearing, purpose = cells
        else:
            continue
        out.append(
            HearingEntry(
                hearing_date=parse_flexible_date(hearing),
                business_date=parse_flexible_date(business),
                purpose=purpose,
                judge=judge,
                cause_list_type=cause_list_type,
            )
        )
    return out


def _orders(soup: BeautifulSoup, base_url: str = "") -> list[CaseOrder]:
    table = soup.find("table", class_=re.compile(r"order_table", re.I))
    if table is None:
        return []
    out = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        values = [_text(c) for c in cells]
        if not values or not any(values):
            continue
        if len(values) >= 5:
            _num, order_on, judge, order_date = values[0], values[1], values[2], values[3]
        elif len(values) >= 3:
            _num, order_on, judge, order_date = values[0], "", "", values[1]
        else:
            continue
        parsed = parse_flexible_date(order_date)
        if parsed is None:
            continue
        link = row.find("a", href=True)
        href = link["href"] if link else ""
        if href and base_url and href.startswith(("cases/", "/")):
            href = base_url.rstrip("/") + "/" + href.lstrip("/")
        out.append(
            CaseOrder(
                order_date=parsed,
                order_type=order_on or "Order",
                judge=judge,
                pdf_url=href,
            )
        )
    return out


def parse_case_detail(html: str, *, cnr: str = "", base_url: str = "") -> CaseDetail:
    """Parse a full case page into a :class:`CaseDetail`.

    Args:
        html: The case page markup. For district courts this is the value of
            ``casetype_list`` from the JSON response, not the response itself.
        cnr: CNR to fall back on when the page does not restate it. High Court
            pages print it hyphenated ("GJHC24-046431-2025"), so the passed
            value is preferred when available.
        base_url: Portal base, used to absolutise relative order PDF links.

    Returns:
        A CaseDetail. Missing sections yield empty lists rather than raising —
        a case with no orders yet is normal, not an error.
    """
    soup = BeautifulSoup(html, "lxml")

    details = _labelled_values(soup.find("table", class_=re.compile(r"case_details_table", re.I)))
    status = _labelled_values(_table_after_heading(soup, "Case Status"))

    def pick(src: dict[str, str], *keys: str) -> str:
        for k in keys:
            if src.get(k):
                return src[k]
        return ""

    page_cnr = pick(details, "cnr number").split("(")[0].strip()

    case_type = pick(details, "case type")
    if not case_type:
        # High Court omits the field and prefixes the type onto the
        # registration number instead, e.g. "LPA /872/2025".
        m = re.match(r"\s*([A-Za-z][A-Za-z.\s]*?)\s*/", pick(details, "registration number"))
        if m:
            case_type = m.group(1).strip()
    detail = CaseDetail(
        cnr_number=cnr or page_cnr.replace("-", ""),
        case_type=case_type,
        filing_number=pick(details, "filing number"),
        filing_date=parse_flexible_date(pick(details, "filing date")),
        registration_number=pick(details, "registration number"),
        registration_date=parse_flexible_date(pick(details, "registration date")),
        first_hearing_date=parse_flexible_date(pick(status, "first hearing date")),
        next_hearing_date=parse_flexible_date(pick(status, "next hearing date")),
        decision_date=parse_flexible_date(
            pick(status, "decision date", "date of decision", "case decision date")
        ),
        case_stage=pick(status, "case stage"),
        status=pick(status, "case status", "status"),
        coram=pick(status, "coram"),
        bench_type=pick(status, "bench type"),
        court_number_and_judge=pick(status, "court number and judge"),
        state=pick(status, "state"),
        district=pick(status, "district"),
        petitioners=_parties(soup, "Petitioner_Advocate_table"),
        respondents=_parties(soup, "Respondent_Advocate_table"),
        acts=_acts(soup),
        history=_history(soup),
        orders=_orders(soup, base_url),
    )

    logger.info(
        "Parsed case detail %s: %d parties, %d history rows, %d orders",
        detail.cnr_number or "?",
        len(detail.petitioners) + len(detail.respondents),
        len(detail.history),
        len(detail.orders),
    )
    return detail
