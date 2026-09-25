"""RBI Monetary Policy Committee meeting-calendar acquisition -- provider
BOUNDARY ONLY in this iteration; no parser is implemented.

During discovery, RBI's MPC schedule was confirmed to be published only as
prose PDF press releases (e.g. under rbidocs.rbi.org.in/rdocs/PressRelease/
PDFs/..., indexed from https://www.rbi.org.in/scripts/BS_PressReleaseDisplay
.aspx), not as a structured page or API. This sandbox has no network
reachability to rbi.org.in and no way to inspect the actual text/layout of
any specific year's schedule PDF, so there is no verified content to build
a text-pattern parser against -- doing so would mean guessing the exact
wording and layout of a document this environment has never actually seen,
which is exactly the fabrication this iteration must avoid.

Per instruction, this ships as a provider boundary only: `fetch()` always
raises MacroProviderConfigurationError, and no RBI meeting is ever
persisted by this class. A future iteration should implement
`parse_rbi_mpc_schedule_text(text)` (mirroring
app.fomc_calendar_provider.parse_fomc_calendar_text) once a real fixture --
the verbatim text of an actual RBI MPC-schedule press release -- is
available to build and test it against.
"""
from __future__ import annotations

from datetime import datetime

import httpx

from app.macro_observation import MacroProviderConfigurationError


class RbiMpcCalendarProvider:
    provider_name = "RBI_MPC_CALENDAR_RBI"
    source_name = "Reserve Bank of India (official MPC meeting schedule press release)"

    def __init__(self, *, endpoint: str | None = None, client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 10.0) -> None:
        # `endpoint` is accepted (not hardcoded) for a future iteration to
        # supply a verified press-release URL; it is unused here because no
        # parser exists yet to apply to whatever it points at.
        self.endpoint = endpoint
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=4.0))

    async def fetch(self, now: datetime | None = None) -> list:
        raise MacroProviderConfigurationError(
            "MACRO_PROVIDER_NOT_CONFIGURED:rbi_mpc_calendar_parsing_not_implemented"
        )
