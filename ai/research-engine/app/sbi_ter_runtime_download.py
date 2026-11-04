"""Runtime acquisition of the current official SBI TER workbook.

Each explicit invocation downloads fresh workbook bytes from the reviewed,
fixed official endpoint. Values and reporting dates are NEVER embedded in
application code or loaded from a checked-in fixture.

All workbook rows remain subject to BoundedSbiTerAdapter's strict schema,
scheme classification, identity resolver, and persistence gates.
No background scheduler is enabled by importing this module.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

import httpx

from app.research_fetching import HttpResearchFetcher
from app.sbi_ter_workbook_adapter import BoundedSbiTerAdapter, SbiTerAdapterResult
from app.settings import Settings


# Approved acquisition endpoint, not a hardcoded TER value. A change to the
# source endpoint requires explicit review; changes to workbook content do not.
SBI_TER_SOURCE_URL = (
    "https://www.sbimf.com/docs/default-source/ter_allschemes/current-year-ter.xlsx"
)
MAX_SBI_TER_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_SBI_TER_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_ALLOWED_XLSX_CONTENT_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "application/zip",
})


@dataclass(frozen=True)
class SbiTerRuntimeAcquisition:
    source_url: str
    sha256: str
    retrieved_at: datetime
    bytes_downloaded: int
    results: tuple[SbiTerAdapterResult, ...]


def _validate_xlsx_bytes(payload: bytes) -> None:
    """Reject HTML/login pages, malformed archives, and oversized ZIP expansion."""
    if not payload.startswith(b"PK\x03\x04"):
        raise ValueError("SBI_TER_WORKBOOK_SIGNATURE_INVALID")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if not entries or sum(item.file_size for item in entries) > MAX_SBI_TER_UNCOMPRESSED_BYTES:
                raise ValueError("SBI_TER_WORKBOOK_UNCOMPRESSED_LIMIT_EXCEEDED")
            names = {item.filename for item in entries}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError("SBI_TER_WORKBOOK_STRUCTURE_INVALID")
    except BadZipFile as exc:
        raise ValueError("SBI_TER_WORKBOOK_ARCHIVE_INVALID") from exc


class SbiTerRuntimeDownloader:
    """Fetch one fresh workbook, then pass it to the existing fail-closed adapter.

    Requires an explicit `acquire()` call. The default adapter has no trusted
    SBI scheme-code mapping and consequently stages rows without persisting.
    Callers may inject a separately verified identity resolver through the
    adapter; this downloader never derives identity from names or symbols.
    """

    def __init__(self, settings: Settings, *,
                 client: httpx.AsyncClient | None = None,
                 adapter: BoundedSbiTerAdapter | None = None,
                 clock=None):
        self._fetcher = HttpResearchFetcher(settings, client=client)
        self._adapter = adapter if adapter is not None else BoundedSbiTerAdapter()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def acquire(self, persistence) -> SbiTerRuntimeAcquisition:
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("SBI_TER_RETRIEVAL_TIMESTAMP_NOT_AWARE")
        response = await self._fetcher.fetch_network(
            SBI_TER_SOURCE_URL,
            headers={"Accept": ", ".join(sorted(_ALLOWED_XLSX_CONTENT_TYPES))},
            max_bytes=MAX_SBI_TER_DOWNLOAD_BYTES,
        )
        # Public URL validation runs at every redirect inside fetch_network,
        # but final source ownership must also be pinned to the reviewed host.
        if (urlsplit(response.final_url).scheme != "https"
                or urlsplit(response.final_url).hostname != urlsplit(SBI_TER_SOURCE_URL).hostname):
            raise ValueError("SBI_TER_CROSS_HOST_REDIRECT_REJECTED")
        if response.status_code != 200:
            raise ValueError(f"SBI_TER_HTTP_STATUS:{response.status_code}")
        content_type = (response.content_type or "").split(";")[0].strip().lower()
        if content_type not in _ALLOWED_XLSX_CONTENT_TYPES:
            raise ValueError(f"SBI_TER_UNEXPECTED_CONTENT_TYPE:{content_type or 'missing'}")
        payload = response.content
        if len(payload) > MAX_SBI_TER_DOWNLOAD_BYTES:
            raise ValueError("SBI_TER_DOWNLOAD_LIMIT_EXCEEDED")
        _validate_xlsx_bytes(payload)

        digest = sha256(payload).hexdigest()
        # The existing schema parser accepts a file path. Use a short-lived
        # private temp directory; no workbook bytes are committed or cached.
        with TemporaryDirectory(prefix="sbi-ter-") as directory:
            path = Path(directory) / "current-year-ter.xlsx"
            path.write_bytes(payload)
            results = self._adapter.acquire(
                persistence, path, source_identity=f"SBI_TER_WORKBOOK_SHA256:{digest}",
                retrieved_at=retrieved_at,
            )
        return SbiTerRuntimeAcquisition(
            source_url=SBI_TER_SOURCE_URL, sha256=digest,
            retrieved_at=retrieved_at, bytes_downloaded=len(payload),
            results=tuple(results),
        )
