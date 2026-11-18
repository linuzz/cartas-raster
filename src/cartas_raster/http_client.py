"""Polite HTTP client for the DHN hosts.

``assets.marinha.mil.br`` presents a certificate whose issuer (AC Defesa,
GlobalSign) is not sent in the handshake. Browsers fetch that intermediate
via AIA; Python does not. The intermediate is bundled and added to the trust
store. Verification against the public GlobalSign root stays enabled.
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = (
    "cartas-raster/1.0 (+https://github.com/linuzz/cartas-raster; "
    "OpenCPN Chart Downloader catalogue)"
)
_CERT = Path(__file__).with_name("certs") / "ac-defesa-gr46-ov-tls-ca-2025.pem"
_RETRY_STATUS = {429, 500, 502, 503, 504}


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
    _last: float = field(default=0.0, init=False)
    _ssl: ssl.SSLContext = field(init=False)

    def __post_init__(self) -> None:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=str(_CERT))
        self._ssl = context

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
                with urllib.request.urlopen(request, timeout=timeout, context=self._ssl) as response:
                    body = b"" if method == "HEAD" else response.read()
                    return response.status, _header_map(response.headers), body, response.geturl()
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRY_STATUS or attempt == attempts:
                    headers = _header_map(exc.headers) if exc.headers else {}
                    return exc.code, headers, exc.read() if method != "HEAD" else b"", url
                last_error = exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                last_error = exc
                if attempt == attempts:
                    raise
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"falha ao acessar {url}: {last_error}")

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def _header_map(headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}
