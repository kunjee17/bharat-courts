"""District Courts portal client.

Provides async access to services.ecourts.gov.in for:
- Case status lookup (by case number, party name)
- Court orders
- Cause list
- Dynamic court hierarchy discovery (states → districts → complexes → establishments)

Flow:
1. GET base URL — establishes SERVICES_SESSID cookie
2. POST /?p=casestatus/getCaptcha — gets initial app_token + CAPTCHA HTML
3. POST /?p=casestatus/fillDistrict — cascade dropdown
4. POST /?p=casestatus/set_data — store court selection in session
5. POST /?p=casestatus/submitCaseNo — search with CAPTCHA

Key difference from HC Services: every AJAX response returns a rotating
app_token that must be sent with the next request.

Since ~2026-07-22 the portal also gates every AJAX POST on anti-bot request
headers scraped from ``js/components.js`` — without them even the CAPTCHA-free
dropdowns return "Invalid Request". Both the secret and the header names
rotate. See :func:`bharat_courts.districtcourts.endpoints.parse_delimeter` and
:func:`bharat_courts.districtcourts.endpoints.parse_ajax_header_spec`.
"""

from __future__ import annotations

import logging
import random
import re

from bharat_courts.captcha import default_solver
from bharat_courts.captcha.base import CaptchaSolver
from bharat_courts.casedetail import parse_case_detail
from bharat_courts.config import BharatCourtsConfig
from bharat_courts.config import config as default_config
from bharat_courts.districtcourts import endpoints
from bharat_courts.districtcourts.parser import (
    CaptchaError,
    InvalidRequestError,
    parse_ajax_response,
    parse_case_status_html,
    parse_cause_list_html,
    parse_court_orders_html,
    parse_option_tags,
)
from bharat_courts.http import RateLimitedClient
from bharat_courts.models import CaseDetail, CaseInfo, CaseOrder, CauseListEntry

logger = logging.getLogger(__name__)


class DistrictCourtClient:
    """Async client for District Courts (services.ecourts.gov.in).

    Usage::

        async with DistrictCourtClient() as client:
            districts = await client.list_districts("8")  # Bihar
            complexes = await client.list_complexes("8", "1")  # Patna
            cases = await client.case_status(
                state_code="8", dist_code="1",
                court_complex_code="1080010", est_code="2",
                case_type="1", case_number="1", year="2024",
            )
    """

    def __init__(
        self,
        config: BharatCourtsConfig | None = None,
        captcha_solver: CaptchaSolver | None = None,
        http_client: RateLimitedClient | None = None,
    ):
        self._config = config or default_config
        self._captcha_solver = captcha_solver or default_solver()
        self._http = http_client or RateLimitedClient(self._config)
        self._owns_http = http_client is None
        self._app_token: str = ""
        self._delimeter: str = ""
        self._header_spec: dict[str, str | None] = {}

    async def __aenter__(self):
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args):
        if self._owns_http:
            await self._http.__aexit__(*args)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _anti_bot_state(self) -> tuple:
        """Snapshot of everything the anti-bot headers derive from.

        Both the secret *and* the header names rotate, so a re-scrape counts as
        progress if either changed.
        """
        return (self._delimeter, tuple(sorted(self._header_spec.items(), key=lambda kv: kv[0])))

    async def _refresh_anti_bot(self, home_html: str = "") -> str:
        """Scrape the current anti-bot headers from components.js.

        Picks up both the rotating ``delimeter`` secret and the header names
        that carry it. The portal rotates the secret roughly hourly (and has
        renamed the headers at least once), so this is fetched per session and
        re-fetched whenever a request comes back as :class:`InvalidRequestError`.
        """
        url = endpoints.components_js_url(home_html)
        try:
            js = await self._http.get_text(url, headers={"Referer": endpoints.BASE_URL + "/"})
        except Exception as e:  # network blip — keep the old value, let the POST report
            logger.warning("Could not fetch %s for delimeter: %s", url, e)
            return self._delimeter

        spec = endpoints.parse_ajax_header_spec(js)
        if spec:
            if spec != self._header_spec:
                logger.debug("anti-bot header names refreshed: %s", sorted(spec))
            self._header_spec = spec
        else:
            logger.warning("No anti-bot headers block found in %s — using fallback names", url)

        delimeter = endpoints.parse_delimeter(js)
        if not delimeter:
            logger.warning("No 'delimeter' literal found in %s — portal may have changed", url)
            return self._delimeter

        if delimeter != self._delimeter:
            logger.debug("delimeter refreshed: %s...", delimeter[:6])
        self._delimeter = delimeter
        return delimeter

    async def _init_session(self):
        """Load the main page to establish session cookie, then get initial token."""
        # GET the home page to get SERVICES_SESSID cookie
        resp = await self._http.get(
            endpoints.BASE_URL + "/",
            headers={"Referer": endpoints.BASE_URL + "/"},
        )
        logger.debug("Session init: status=%d", resp.status_code)

        # Every AJAX POST below is gated on these headers; fetch them before
        # the first one, resolving components.js from the page we just loaded.
        await self._refresh_anti_bot(resp.text)

        # Get initial app_token via getCaptcha call
        try:
            await self._post_ajax("casestatus/getCaptcha", {})
            token_preview = self._app_token[:8] if self._app_token else "empty"
            logger.debug("Got initial app_token: %s...", token_preview)
        except (CaptchaError, Exception) as e:
            logger.debug("getCaptcha init (non-critical): %s", e)

    async def _post_ajax(self, controller_action: str, data: dict) -> dict:
        """POST an AJAX request and handle token rotation.

        Appends ajax_req=true and app_token to the data, posts to the
        controller/action URL, parses the JSON response, and updates
        the stored app_token from the response.

        Also carries the scraped anti-bot headers. If the portal rejects the
        request as "Invalid Request" the secret (or the header names) has
        almost certainly rotated mid-session, so they are re-scraped and the
        request replayed once before the error is allowed to propagate.
        """
        url = endpoints.ajax_url(controller_action)

        async def send() -> dict:
            post_data = dict(data)
            post_data["ajax_req"] = "true"
            post_data["app_token"] = self._app_token

            headers = {"Referer": endpoints.BASE_URL + "/"}
            headers.update(endpoints.ajax_headers(self._delimeter, self._header_spec))

            resp = await self._http.post(url, data=post_data, headers=headers)
            return parse_ajax_response(resp.text)

        try:
            result = await send()
        except InvalidRequestError:
            stale = self._anti_bot_state()
            await self._refresh_anti_bot()
            if not self._delimeter or self._anti_bot_state() == stale:
                raise  # not a rotation — the portal is rejecting us for another reason
            logger.info("Portal rejected request as invalid; anti-bot headers rotated, retrying")
            result = await send()

        # Rotate token
        new_token = result.get("app_token", "")
        if new_token:
            self._app_token = new_token

        return result

    async def _solve_captcha(self) -> str:
        """Fetch a fresh CAPTCHA image and solve it."""
        captcha_url = endpoints.CAPTCHA_IMAGE_URL + "?" + str(random.random())
        resp = await self._http.get(
            captcha_url,
            headers={"Referer": endpoints.BASE_URL + "/"},
        )
        return await self._captcha_solver.solve(resp.content)

    async def _setup_court(
        self,
        *,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
    ):
        """Set the court selection in the server session.

        This must be called before any search query so the server knows
        which court complex to query.
        """
        form = endpoints.set_data_form(
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        await self._post_ajax("casestatus/set_data", form)

    async def _post_with_captcha_retry(
        self,
        controller_action: str,
        form_builder,
        *,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
        max_retries: int = 5,
    ) -> dict:
        """POST with automatic CAPTCHA retry.

        Each retry creates a fresh session (new cookies, new CAPTCHA).
        Also re-establishes the court selection via set_data.

        Args:
            controller_action: The /?p= path (e.g. "casestatus/submitCaseNo").
            form_builder: Callable(captcha: str) -> dict of form data.
            state_code: State code for court setup.
            dist_code: District code for court setup.
            court_complex_code: Court complex code for court setup.
            est_code: Establishment code (optional).
            max_retries: Number of attempts.

        Returns:
            Parsed AJAX response dict on success.

        Raises:
            CaptchaError: If all retries fail.
        """
        for attempt in range(max_retries):
            if attempt > 0:
                logger.info("CAPTCHA retry %d/%d — new session", attempt + 1, max_retries)

            # Fresh session + court setup
            await self._init_session()
            await self._setup_court(
                state_code=state_code,
                dist_code=dist_code,
                court_complex_code=court_complex_code,
                est_code=est_code,
            )

            # Solve CAPTCHA and submit
            captcha = await self._solve_captcha()
            form = form_builder(captcha)

            try:
                result = await self._post_ajax(controller_action, form)
                return result
            except CaptchaError:
                logger.warning("CAPTCHA attempt %d failed", attempt + 1)
                continue

        raise CaptchaError(f"CAPTCHA failed after {max_retries} attempts")

    # ------------------------------------------------------------------
    # Court hierarchy discovery (no CAPTCHA)
    # ------------------------------------------------------------------

    async def list_states(self) -> dict[str, str]:
        """Get available states/UTs.

        Returns:
            Dict mapping state code to state name.
        """
        # States are static, from the portal dropdown
        return {v: k for k, v in endpoints.DISTRICT_STATES.items()}

    async def list_districts(self, state_code: str) -> dict[str, str]:
        """Get districts for a state.

        Args:
            state_code: State code (e.g. "8" for Bihar).

        Returns:
            Dict mapping district code to district name.
        """
        await self._init_session()
        form = endpoints.fill_district_form(state_code=state_code)
        result = await self._post_ajax("casestatus/fillDistrict", form)
        dist_html = result.get("dist_list", "")
        return parse_option_tags(dist_html)

    async def list_complexes(self, state_code: str, dist_code: str) -> dict[str, str]:
        """Get court complexes for a district.

        Args:
            state_code: State code.
            dist_code: District code.

        Returns:
            Dict mapping complex value (``code@ests@flag``) to complex name.
            Use :func:`parse_complex_value` to extract the complex code
            and determine if establishment selection is needed.
        """
        await self._init_session()
        # Ensure district is filled first
        await self._post_ajax(
            "casestatus/fillDistrict",
            endpoints.fill_district_form(state_code=state_code),
        )
        form = endpoints.fill_complex_form(state_code=state_code, dist_code=dist_code)
        result = await self._post_ajax("casestatus/fillcomplex", form)
        complex_html = result.get("complex_list", "")
        return parse_option_tags(complex_html)

    async def list_establishments(
        self,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
    ) -> dict[str, str]:
        """Get establishments for a court complex.

        Only needed when the complex flag is 'Y'.

        Args:
            state_code: State code.
            dist_code: District code.
            court_complex_code: Raw complex code (without @ests@flag).

        Returns:
            Dict mapping establishment code to name.
        """
        await self._init_session()
        form = endpoints.fill_establishment_form(
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
        )
        result = await self._post_ajax("casestatus/fillCourtEstablishment", form)
        est_html = result.get("est_list", result.get("establishment_list", ""))
        return parse_option_tags(est_html)

    async def list_cause_list_courts(
        self,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
    ) -> dict[str, str]:
        """Get the courts dropdown for cause-list lookup.

        The cause-list form requires both ``court_no`` (the option's
        value, e.g. ``"1@2"``) and ``court_name`` (the option's display
        text, e.g. ``"District & Sessions Judge - DJ Div. Patna Sadar"``).
        Use this method to discover them; pass either the code through
        directly to :meth:`cause_list`, which will look up the matching
        name automatically.
        """
        await self._init_session()
        form = endpoints.fill_cause_list_form(
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        result = await self._post_ajax("cause_list/fillCauseList", form)
        html = result.get("cause_list", "")
        return parse_option_tags(html)

    async def list_case_types(
        self,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
    ) -> dict[str, str]:
        """Get available case types for a court.

        Args:
            state_code: State code.
            dist_code: District code.
            court_complex_code: Court complex code.
            est_code: Establishment code (if needed).

        Returns:
            Dict mapping case type code to name. Codes are returned in the
            portal's compound ``"<case_type>^<est_code>"`` format (e.g.
            ``"89^2": "ADMINISTRATIVE SUITE"``). Pass the full compound
            string back as ``case_type`` to :meth:`case_status` /
            :meth:`court_orders`; do not strip the suffix.
        """
        await self._init_session()
        await self._setup_court(
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        form = endpoints.fill_case_type_form(
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
            search_type="c_no",
        )
        result = await self._post_ajax("casestatus/fillCaseType", form)
        ct_html = result.get("casetype_list", "")
        return parse_option_tags(ct_html)

    # ------------------------------------------------------------------
    # Case status search
    # ------------------------------------------------------------------

    async def case_status_by_cnr(self, cnr: str) -> CaseDetail:
        """Look up a case by its CNR number.

        Needs no state/district/complex codes: a CNR identifies the
        establishment on its own, so this skips the ``set_data`` court setup
        that the other lookups require. That also means it cannot reuse
        :meth:`_post_with_captcha_retry`, which exists to re-establish that
        selection on every retry — the retry loop here is the same shape
        minus the court setup.

        Args:
            cnr: 16-character CNR, e.g. "GJRJ060015282018". Hyphens and
                spaces are stripped.

        Returns:
            A CaseDetail with the full case page: stage, judge, parties with
            advocates, acts, hearing history and interim orders.

        Raises:
            ValueError: If the CNR is not 16 alphanumeric characters.
            CaptchaError: If every CAPTCHA attempt failed.
        """
        cleaned = re.sub(r"[\s-]", "", cnr).upper()
        if len(cleaned) != 16 or not cleaned.isalnum():
            raise ValueError(f"CNR must be 16 alphanumeric characters, got {cnr!r}")

        last_error: Exception | None = None
        for attempt in range(5):
            if attempt > 0:
                logger.info("CAPTCHA retry %d/5 — new session", attempt + 1)
            await self._init_session()
            captcha = await self._solve_captcha()
            form = endpoints.case_status_by_cnr_form(cnr=cleaned, captcha=captcha)
            try:
                result = await self._post_ajax("cnr_status/searchByCNR", form)
            except CaptchaError as exc:
                last_error = exc
                logger.warning("CAPTCHA attempt %d failed", attempt + 1)
                continue
            html = result.get("casetype_list") or result.get("historytable") or ""
            if html:
                return parse_case_detail(html, cnr=cleaned, base_url=endpoints.BASE_URL)
            last_error = CaptchaError("No case data returned")

        raise last_error or CaptchaError("CNR lookup failed")

    async def case_status(
        self,
        *,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
        case_type: str,
        case_number: str,
        year: str,
    ) -> list[CaseInfo]:
        """Look up case status by case number.

        Args:
            state_code: State code (e.g. "8" for Bihar).
            dist_code: District code (e.g. "1" for Patna).
            court_complex_code: Court complex code (e.g. "1080010").
            est_code: Establishment code (if needed).
            case_type: Case type code from :meth:`list_case_types`.
            case_number: Case number.
            year: Registration year.

        Returns:
            List of matching CaseInfo objects.
        """

        def build_form(captcha: str) -> dict:
            return endpoints.case_status_by_number_form(
                state_code=state_code,
                dist_code=dist_code,
                court_complex_code=court_complex_code,
                est_code=est_code,
                case_type=case_type,
                case_number=case_number,
                year=year,
                captcha=captcha,
            )

        result = await self._post_with_captcha_retry(
            "casestatus/submitCaseNo",
            build_form,
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        html = result.get("case_data", "")
        return parse_case_status_html(html)

    async def case_status_by_party(
        self,
        *,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
        party_name: str,
        year: str,
        status_filter: str = "Both",
    ) -> list[CaseInfo]:
        """Search cases by party name.

        Args:
            state_code: State code.
            dist_code: District code.
            court_complex_code: Court complex code.
            est_code: Establishment code (if needed).
            party_name: Petitioner/respondent name (min 3 chars).
            year: Registration year (mandatory).
            status_filter: "Pending", "Disposed", or "Both".

        Returns:
            List of matching CaseInfo objects.
        """

        def build_form(captcha: str) -> dict:
            return endpoints.case_status_by_party_form(
                state_code=state_code,
                dist_code=dist_code,
                court_complex_code=court_complex_code,
                est_code=est_code,
                party_name=party_name,
                year=year,
                status_filter=status_filter,
                captcha=captcha,
            )

        result = await self._post_with_captcha_retry(
            "casestatus/submitPartyName",
            build_form,
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        html = result.get("party_data", "")
        return parse_case_status_html(html)

    # ------------------------------------------------------------------
    # Court orders
    # ------------------------------------------------------------------

    async def court_orders(
        self,
        *,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
        case_type: str,
        case_number: str,
        year: str,
    ) -> list[CaseOrder]:
        """Get court orders for a case.

        Args:
            state_code: State code.
            dist_code: District code.
            court_complex_code: Court complex code.
            est_code: Establishment code (if needed).
            case_type: Case type code.
            case_number: Case number.
            year: Registration year.

        Returns:
            List of CaseOrder objects.
        """

        def build_form(captcha: str) -> dict:
            return endpoints.court_orders_by_number_form(
                state_code=state_code,
                dist_code=dist_code,
                court_complex_code=court_complex_code,
                est_code=est_code,
                case_type=case_type,
                case_number=case_number,
                year=year,
                captcha=captcha,
            )

        result = await self._post_with_captcha_retry(
            "courtorder/submitCaseNo",
            build_form,
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        html = result.get("order_data", result.get("case_data", ""))
        return parse_court_orders_html(html, base_url=endpoints.BASE_URL)

    # ------------------------------------------------------------------
    # Cause list
    # ------------------------------------------------------------------

    async def cause_list(
        self,
        *,
        state_code: str,
        dist_code: str,
        court_complex_code: str,
        est_code: str = "",
        court_no: str,
        court_name: str = "",
        causelist_date: str = "",
        civil: bool = True,
    ) -> list[CauseListEntry]:
        """Get cause list for a court.

        Args:
            state_code: State code.
            dist_code: District code.
            court_complex_code: Court complex code.
            est_code: Establishment code (if needed).
            court_no: Court code from :meth:`list_cause_list_courts`
                (the option's ``value``).
            court_name: Court display name (the option's text). The
                portal validates against this — sending an empty
                ``court_name_txt`` triggers ``"Court Name is required"``.
                If left empty, this method calls
                :meth:`list_cause_list_courts` once to look up the
                matching name for ``court_no``.
            causelist_date: Date in DD-MM-YYYY format (defaults to today).
            civil: True for civil, False for criminal.

        Returns:
            List of CauseListEntry objects.
        """
        if not court_name:
            mapping = await self.list_cause_list_courts(
                state_code, dist_code, court_complex_code, est_code
            )
            court_name = mapping.get(court_no, "")
            if not court_name:
                raise ValueError(
                    f"court_no={court_no!r} not found in fillCauseList for "
                    f"complex={court_complex_code} est={est_code}. "
                    f"Available: {list(mapping)[:5]}{'...' if len(mapping) > 5 else ''}"
                )

        def build_form(captcha: str) -> dict:
            return endpoints.cause_list_form(
                state_code=state_code,
                dist_code=dist_code,
                court_complex_code=court_complex_code,
                est_code=est_code,
                court_no=court_no,
                court_name=court_name,
                causelist_date=causelist_date,
                civil=civil,
                captcha=captcha,
            )

        result = await self._post_with_captcha_retry(
            "cause_list/submitCauseList",
            build_form,
            state_code=state_code,
            dist_code=dist_code,
            court_complex_code=court_complex_code,
            est_code=est_code,
        )
        html = result.get("causelist_data", result.get("cause_list_data", ""))
        return parse_cause_list_html(html)
