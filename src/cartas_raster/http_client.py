"""Polite HTTP client for the DHN hosts.

``assets.marinha.mil.br`` presents a certificate whose issuer (AC Defesa,
GlobalSign) is not sent in the handshake. Browsers fetch that intermediate
via AIA; Python's ``ssl`` does not, and it has no API to supply that
certificate only as untrusted chain glue. ``SSLContext.load_verify_locations``
inserts it as a trust anchor, so a leaf signed by AC Defesa is accepted even
if the chain never reaches GlobalSign Root R46.

That context is created once and used only after the host allowlist check.
Requests and redirects to any other host, scheme, port or userinfo are
rejected before a connection is opened. The final URL is checked again.
Responses are read in chunks and aborted above ``max_bytes`` (default 2 GiB,
the same ceiling as a GitHub release asset).
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

USER_AGENT = (
    "cartas-raster/1.0 (+https://github.com/linuzz/cartas-raster; "
    "OpenCPN Chart Downloader catalogue)"
)
_CERT = Path(__file__).with_name("certs") / "ac-defesa-gr46-ov-tls-ca-2025.pem"
_RETRY_STATUS = {403, 429, 500, 502, 503, 504}
_MAX_RETRY_WAIT = 60.0
_CHUNK = 64 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"assets.marinha.mil.br", "idem.marinha.mil.br"})


class RejectedUrl(urllib.error.URLError):
    """The URL is not https on a Marinha host this client may talk to."""


class DownloadTooLarge(urllib.error.URLError):
    """The body grew past the streaming cap. Nothing further is read."""


def require_marinha_https(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_HOSTS:
        raise RejectedUrl(f"URL recusada: {url}")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise RejectedUrl(f"URL recusada: {url}")
    return url


def read_bounded(response, limit: int, chunk_size: int = _CHUNK) -> bytes:
    """Read ``response`` in chunks. Abort as soon as ``limit`` is exceeded."""
    if limit < 0:
        raise DownloadTooLarge("limite de download negativo")
    chunks: list[bytes] = []
    total = 0
    while True:
        block = response.read(chunk_size)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise DownloadTooLarge(f"resposta passou de {limit} bytes")
        chunks.append(block)
    return b"".join(chunks)


class _CappedStream:
    """File-like wrapper so urllib cannot slurp a redirect body in one unbounded read."""

    def __init__(self, raw, limit: int):
        self.raw = raw
        self.limit = limit
        self.total = 0

    def read(self, n=-1):
        chunks: list[bytes] = []
        remaining = None if n is None or n < 0 else n
        while remaining is None or remaining > 0:
            if self.total > self.limit:
                raise DownloadTooLarge(f"corpo do redirect passou de {self.limit} bytes")
            block = self.raw.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
            if not block:
                break
            self.total += len(block)
            if self.total > self.limit:
                raise DownloadTooLarge(f"corpo do redirect passou de {self.limit} bytes")
            chunks.append(block)
            if remaining is not None:
                remaining -= len(block)
        return b"".join(chunks)

    def close(self):
        close = getattr(self.raw, "close", None)
        if close is not None:
            close()

    def __getattr__(self, name):
        return getattr(self.raw, name)


class MarinhaRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when the next URL is still an allowed Marinha host."""

    redirect_body_limit = 64 * 1024

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_marinha_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):
        return super().http_error_302(
            req, _CappedStream(fp, self.redirect_body_limit), code, msg, headers
        )


def _marinha_ssl_context() -> ssl.SSLContext:
    """System trust store plus the AC Defesa intermediate, for Marinha hosts only.

    See the module docstring: stdlib ``ssl`` cannot use this PEM solely to
    complete a chain up to GlobalSign Root R46. Callers must not attach the
    context to a connection whose host failed ``require_marinha_https``.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(_CERT))
    return context


@dataclass
class HeadResult:
    status: int
    headers: dict[str, str]
    url: str


@dataclass
class GetResult:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


@dataclass
class UrllibTransport:
    user_agent: str = USER_AGENT
    timeout: float = 60.0
    min_interval: float = 0.5
    retries: int = 4
    max_bytes: int = MAX_DOWNLOAD_BYTES
    _last: float = field(default=0.0, init=False)
    _ssl: ssl.SSLContext = field(init=False)
    _opener: urllib.request.OpenerDirector = field(init=False)

    def __post_init__(self) -> None:
        self._ssl = _marinha_ssl_context()
        opener = urllib.request.build_opener(
            MarinhaRedirectHandler(),
            urllib.request.HTTPSHandler(context=self._ssl),
        )
        blocked = (
            urllib.request.FileHandler,
            urllib.request.FTPHandler,
            urllib.request.DataHandler,
        )
        opener.handlers = [handler for handler in opener.handlers if not isinstance(handler, blocked)]
        self._opener = opener

    def head(self, url: str, *, timeout: float | None = None, retries: int | None = None) -> HeadResult:
        status, headers, _body, final = self._request(url, method="HEAD", timeout=timeout, retries=retries)
        return HeadResult(status, headers, final)

    def get(self, url: str, *, timeout: float | None = None, retries: int | None = None) -> GetResult:
        status, headers, body, final = self._request(
            url, method="GET", timeout=timeout, retries=retries
        )
        return GetResult(status, headers, body, final)

    def _request(
        self,
        url: str,
        method: str,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> tuple[int, dict[str, str], bytes, str]:
        require_marinha_https(url)
        delay = 1.0
        attempts = self.retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._wait()
            request = urllib.request.Request(
                url,
                method=method,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, application/xml, application/zip, */*;q=0.1",
                },
            )
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    final = response.geturl()
                    require_marinha_https(final)
                    if method == "HEAD":
                        body = b""
                    else:
                        declared = _content_length(getattr(response, "headers", {}))
                        if declared is not None and declared > self.max_bytes:
                            raise DownloadTooLarge(f"Content-Length {declared} passa de {self.max_bytes}")
                        body = read_bounded(response, self.max_bytes)
                    return response.status, _header_map(response.headers), body, final
            except urllib.error.HTTPError as exc:
                headers = _header_map(exc.headers) if exc.headers else {}
                if exc.code in _RETRY_STATUS and attempt < attempts:
                    delay = retry_wait(headers, delay)
                    time.sleep(delay)
                    delay = min(_MAX_RETRY_WAIT, max(delay, 1.0) * 2)
                    continue
                if method == "HEAD":
                    payload = b""
                else:
                    payload = read_bounded(exc, self.max_bytes)
                return exc.code, headers, payload, url
            except (RejectedUrl, DownloadTooLarge):
                raise
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                last_error = exc
                if attempt == attempts:
                    raise
                time.sleep(delay)
                delay = min(_MAX_RETRY_WAIT, delay * 2)
        raise RuntimeError(f"falha ao acessar {url}: {last_error}")

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def _content_length(headers) -> int | None:
    if headers is None:
        return None
    value = headers.get("content-length") or headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _header_map(headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def retry_wait(headers: dict | None, fallback: float) -> float:
    """Seconds to wait. ``Retry-After`` wins, capped so one file cannot stall the run."""
    raw = None
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is not None:
        text = str(raw).strip()
        if text.isdigit():
            return min(_MAX_RETRY_WAIT, float(text))
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
            if seconds > 0:
                return min(_MAX_RETRY_WAIT, seconds)
    return min(_MAX_RETRY_WAIT, max(fallback, 0.0))
