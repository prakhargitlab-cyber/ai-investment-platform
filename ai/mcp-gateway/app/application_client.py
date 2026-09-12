"""Bounded client for existing provider-free application read contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from opentelemetry.propagate import inject

from app.contracts import McpAuthContext, McpErrorCode, McpGatewayError
from app.settings import McpGatewaySettings


class ApplicationResearchReader(Protocol):
    async def invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        auth: McpAuthContext,
        request_id: str,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class HttpApplicationResearchReader:
    """Maps MCP reads onto existing research-engine APIs without acquisition routes."""

    def __init__(
        self,
        settings: McpGatewaySettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.research_base_url,
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_seconds,
                read=settings.http_read_timeout_seconds,
                write=settings.http_read_timeout_seconds,
                pool=settings.http_connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=settings.http_max_connections,
                max_keepalive_connections=settings.http_max_keepalive_connections,
            ),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        auth: McpAuthContext,
        request_id: str,
    ) -> dict[str, Any]:
        instrument_id = arguments.get("globalInstrumentId")
        if tool == "get_research_readiness":
            return await self._json(
                "GET",
                f"/api/v1/research/readiness/{instrument_id}",
                auth,
                request_id,
                not_found_code=McpErrorCode.READINESS_NOT_AVAILABLE,
            )
        if tool == "get_company_analysis":
            return await self._json(
                "POST",
                f"/api/v1/research/analysis/{instrument_id}",
                auth,
                request_id,
                json={"allowPartial": bool(arguments.get("allowPartial", False))},
                not_found_code=McpErrorCode.ANALYSIS_NOT_AVAILABLE,
            )
        if tool in {"get_financial_facts", "get_quarterly_results", "get_shareholding", "get_recent_news"}:
            summary = await self._json(
                "GET", f"/api/v1/research/companies/{instrument_id}/summary", auth, request_id
            )
            return _summary_projection(tool, summary, arguments)
        if tool == "search_research_evidence":
            documents = await self._json(
                "GET", f"/api/v1/research/companies/{instrument_id}/documents", auth, request_id
            )
            return _evidence_search(documents, arguments)
        if tool == "get_sector_performance":
            return await self._json(
                "GET",
                "/api/v1/research/sector-performance",
                auth,
                request_id,
                params={
                    "region": arguments["region"],
                    "sector": arguments["sector"],
                    "period": arguments["period"],
                    "limit": arguments["limit"],
                },
            )
        if tool == "get_watchlist":
            return await self._json(
                "GET",
                f"/api/v1/research/watchlists/{arguments['watchlistId']}/research",
                auth,
                request_id,
                not_found_code=McpErrorCode.DOWNSTREAM_UNAVAILABLE,
            )
        raise McpGatewayError(McpErrorCode.MCP_TOOL_NOT_FOUND)

    async def _json(
        self,
        method: str,
        path: str,
        auth: McpAuthContext,
        request_id: str,
        not_found_code: McpErrorCode = McpErrorCode.COMPANY_NOT_RESOLVED,
        **kwargs: Any,
    ) -> Any:
        headers = {
            "X-Request-ID": request_id,
            "X-Correlation-Id": request_id,
            **auth.identity_headers(),
        }
        inject(headers)
        try:
            response = await self.client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise McpGatewayError(McpErrorCode.DOWNSTREAM_TIMEOUT) from exc
        except httpx.RequestError as exc:
            raise McpGatewayError(McpErrorCode.DOWNSTREAM_UNAVAILABLE) from exc
        if response.status_code in {401}:
            raise McpGatewayError(McpErrorCode.UNAUTHORIZED)
        if response.status_code in {403}:
            raise McpGatewayError(McpErrorCode.FORBIDDEN)
        if response.status_code == 404:
            try:
                detail = str(response.json().get("detail", "")).strip().upper()
            except (AttributeError, ValueError):
                detail = ""
            code = (
                McpErrorCode.COMPANY_NOT_RESOLVED
                if detail == McpErrorCode.COMPANY_NOT_RESOLVED.value
                else not_found_code
            )
            raise McpGatewayError(code)
        if response.status_code in {400, 422}:
            raise McpGatewayError(McpErrorCode.INVALID_ARGUMENT)
        if response.status_code >= 400:
            raise McpGatewayError(McpErrorCode.DOWNSTREAM_UNAVAILABLE)
        try:
            return response.json()
        except ValueError as exc:
            raise McpGatewayError(McpErrorCode.DOWNSTREAM_UNAVAILABLE) from exc


def _summary_projection(tool: str, summary: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    instrument_id = arguments["globalInstrumentId"]
    if tool == "get_financial_facts":
        return {
            "globalInstrumentId": instrument_id,
            "financialResultHistory": summary.get("financialResultHistory", []),
            "balanceSheetHistory": summary.get("balanceSheetHistory", []),
            "cashFlowHistory": summary.get("cashFlowHistory", []),
            "dataFreshness": summary.get("dataFreshness"),
        }
    if tool == "get_quarterly_results":
        return {
            "globalInstrumentId": instrument_id,
            "latestQuarterlyResult": summary.get("latestQuarterlyResult"),
            "financialResultHistory": summary.get("financialResultHistory", []),
            "dataFreshness": summary.get("dataFreshness"),
        }
    if tool == "get_shareholding":
        return {
            "globalInstrumentId": instrument_id,
            "shareholdingSnapshots": summary.get("shareholdingSnapshots", []),
            "shareholdingFreshness": summary.get("shareholdingFreshness", "UNAVAILABLE"),
        }
    if tool == "get_recent_news":
        days = int(arguments.get("days", 30))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        items: list[dict[str, Any]] = []
        for event in summary.get("recentEvents", []):
            publication_value = event.get("publishedAt") or event.get("eventDate")
            publication_date = _parse_datetime(publication_value)
            if publication_date is None or not cutoff <= publication_date <= now:
                continue
            items.append(
                {
                    "eventId": event.get("eventId"),
                    "eventType": event.get("eventType"),
                    "publicationDate": publication_date.isoformat(),
                    "dateBasis": "PUBLISHED_AT" if event.get("publishedAt") else "EVENT_DATE",
                    "title": event.get("title"),
                    "summary": event.get("summary"),
                    "impact": event.get("impact"),
                    "source": {
                        "url": event.get("sourceUrl"),
                        "type": event.get("sourceType"),
                        "classification": event.get("sourceClassification"),
                        "reliability": event.get("reliability"),
                    },
                }
            )
        return {"globalInstrumentId": instrument_id, "days": days, "news": items}
    raise McpGatewayError(McpErrorCode.MCP_TOOL_NOT_FOUND)


def _evidence_search(documents: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    values = documents if isinstance(documents, list) else []
    query = str(arguments["query"]).casefold()
    limit = int(arguments.get("limit", 10))
    matches = []
    for document in values:
        haystack = " ".join(
            str(document.get(key) or "")
            for key in ("title", "sourceName", "publisher", "documentType", "documentSubtype")
        ).casefold()
        if query not in haystack:
            continue
        matches.append(
            {
                "documentId": document.get("documentId"),
                "title": document.get("title"),
                "canonicalUrl": document.get("canonicalUrl"),
                "sourceName": document.get("sourceName"),
                "publisher": document.get("publisher"),
                "sourceClassification": document.get("sourceClassification"),
                "reliabilityLevel": document.get("reliabilityLevel"),
                "documentType": document.get("documentType"),
                "documentSubtype": document.get("documentSubtype"),
                "publishedAt": document.get("publishedAt"),
                "retrievedAt": document.get("retrievedAt"),
            }
        )
    matches.sort(key=lambda item: item.get("publishedAt") or item.get("retrievedAt") or "", reverse=True)
    return {
        "globalInstrumentId": arguments["globalInstrumentId"],
        "query": arguments["query"],
        "matches": matches[:limit],
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
