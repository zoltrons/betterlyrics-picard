"""Small, dependency-free HTTP client used by optional lyric providers.

The client intentionally has no logging and never puts credentials in a URL.  It
is suitable for Picard's worker threads, but is also useful in tests because a
transport callable can be injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
import copy
import hashlib
import inspect
import json
import math
import os
import ssl
import sys
import threading
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


_STATUSES = frozenset(
    ("hit", "not_found", "auth_required", "rate_limited", "transport_error",
     "invalid_response", "cancelled")
)


@dataclass
class HttpResult:
    """A deliberately small, stable result object for provider adapters."""

    status: str
    data: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    status_code: int | None = None
    # Safe machine-readable diagnostics only; exception text may contain URLs
    # or implementation details and is intentionally never retained.
    error_code: str | None = None


def create_ssl_context() -> ssl.SSLContext:
    """Create a verified TLS context, with the common macOS CA bundle added.

    Python bundled inside a macOS application can lack the system CA path even
    though the host has one.  Loading it is additive; certificate and hostname
    verification remain enabled and callers may inject a Qt/application context
    explicitly through ``HttpClient(ssl_context=...)``.
    """
    context = ssl.create_default_context()
    if sys.platform == "darwin":
        cafile = "/etc/ssl/cert.pem"
        if os.path.isfile(cafile):
            try:
                context.load_verify_locations(cafile=cafile)
            except (OSError, ssl.SSLError):
                pass
    return context


class HttpClient:
    """Thread-safe HTTPS JSON/text GET client with bounded retries and pacing."""

    _cooldown_lock = threading.RLock()
    _cooldowns: dict[str, float] = {}
    _last_request: dict[str, float] = {}
    _cooldown_status: dict[str, tuple[str, int | None]] = {}

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        retries: int = 2,
        max_retries: int | None = None,
        min_interval: float = 0.15,
        max_response_bytes: int = 4 * 1024 * 1024,
        cancel_event: threading.Event | None = None,
        transport: Callable[..., Any] | Any | None = None,
        user_agent: str = "betterlyrics-picard/lyrics-provider",
        cache: bool = True,
        success_cache_ttl: float = 300.0,
        not_found_cache_ttl: float = 30.0,
        max_retry_wait: float = 15.0,
        max_cache_entries: int = 256,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.timeout = max(0.1, float(timeout))
        count = retries if max_retries is None else max_retries
        self.retries = max(0, min(int(count), 5))
        self.min_interval = max(0.0, float(min_interval))
        self.max_response_bytes = max(1, int(max_response_bytes))
        self.cancel_event = cancel_event or threading.Event()
        self.transport = transport
        self.user_agent = user_agent
        self.cache_enabled = bool(cache)
        self.success_cache_ttl = max(0.0, float(success_cache_ttl))
        self.not_found_cache_ttl = max(0.0, float(not_found_cache_ttl))
        self.max_retry_wait = max(0.0, float(max_retry_wait))
        self.max_cache_entries = max(0, int(max_cache_entries))
        self.ssl_context = ssl_context
        self._cache_lock = threading.RLock()
        self._cache: dict[str, tuple[float, HttpResult]] = {}
        self._closed = False

    def close(self) -> None:
        """Cancel current/future calls made through this client."""
        self._closed = True
        self.cancel_event.set()

    def request_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        result = self._request(url, params=params, headers=headers, want_json=True)
        return result

    def request_text(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        return self._request(url, params=params, headers=headers, want_json=False)

    def _request(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        want_json: bool,
    ) -> HttpResult:
        if self._is_cancelled():
            return HttpResult("cancelled")
        try:
            parsed = urlsplit(str(url))
            if (parsed.scheme.lower() != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or any(c.isspace() for c in str(url))):
                return HttpResult("invalid_response")
            host = f"{parsed.hostname.lower()}:{parsed.port or 443}"
            query_url = self._with_params(url, params)
            clean_headers = {str(k): str(v) for k, v in (headers or {}).items()}
            if any("\r" in k + v or "\n" in k + v for k, v in clean_headers.items()):
                return HttpResult("invalid_response")
        except (TypeError, ValueError, AttributeError):
            return HttpResult("invalid_response")
        if not any(k.lower() == "user-agent" for k in clean_headers):
            clean_headers["User-Agent"] = self.user_agent
        key = self._cache_key(query_url, clean_headers, want_json)
        cached = self._cached(key)
        if cached is not None:
            return cached

        attempts = self.retries + 1
        for attempt in range(attempts):
            if self._is_cancelled():
                return HttpResult("cancelled")
            with self._cooldown_lock:
                remaining = self._cooldowns.get(host, 0.0) - time.monotonic()
                status, code = self._cooldown_status.get(host, ("rate_limited", 429))
            if remaining > self.max_retry_wait:
                return HttpResult(status, headers={"Retry-After": str(math.ceil(remaining))}, status_code=code)
            if not self._wait_for_host(host):
                return HttpResult("cancelled")
            try:
                result = self._one_request(query_url, clean_headers, want_json)
            except _Cancelled:
                return HttpResult("cancelled")
            except ssl.SSLCertVerificationError:
                result = HttpResult("transport_error", error_code="TLS_VERIFY_FAILED")
            except ssl.SSLError:
                result = HttpResult("transport_error", error_code="TLS_ERROR")
            except (ValueError, UnicodeError, TypeError):
                result = HttpResult("invalid_response")
            except Exception:
                result = HttpResult("transport_error")
            if self._is_cancelled():
                return HttpResult("cancelled")
            if result.status not in _STATUSES:
                return HttpResult("invalid_response")
            if result.status == "cancelled":
                return result
            if result.error_code:
                # Certificate failures are deterministic for this process;
                # retrying only makes Picard's log and latency noisier.
                return result
            if result.status in ("hit", "not_found", "auth_required", "invalid_response"):
                if result.status in ("hit", "not_found"):
                    self._store(key, result)
                return result
            if result.status == "rate_limited" or (result.status == "transport_error" and _header(result.headers, "retry-after")):
                delay = _retry_after(result.headers)
                self._set_cooldown(host, delay, result.status, result.status_code)
                if delay > self.max_retry_wait:
                    return result
                if attempt + 1 < attempts and self._sleep(delay):
                    continue
                if self._is_cancelled():
                    return HttpResult("cancelled")
                return result
            # A server/transport error is retried a finite number of times.
            if attempt + 1 < attempts:
                if not self._sleep(min(8.0, 0.25 * (2**attempt))):
                    return HttpResult("cancelled")
                continue
            return result
        return HttpResult("transport_error")

    def _one_request(self, url: str, headers: Mapping[str, str], want_json: bool) -> HttpResult:
        if self.transport is not None:
            raw = self._call_transport(url, headers)
            if isinstance(raw, HttpResult):
                if raw.status == "hit":
                    size = len(json.dumps(raw.data, ensure_ascii=False).encode("utf-8")) if want_json else len(str(raw.data).encode("utf-8"))
                    if size > self.max_response_bytes:
                        return HttpResult("invalid_response", headers=dict(raw.headers), status_code=raw.status_code)
                return raw
            if isinstance(raw, tuple) and len(raw) >= 2:
                code, body = raw[0], raw[1]
                response_headers = dict(raw[2]) if len(raw) > 2 and raw[2] else {}
                return self._decode(int(code), body, response_headers, want_json)
            if isinstance(raw, (bytes, bytearray)):
                return self._decode(200, bytes(raw), {}, want_json)
            if isinstance(raw, str):
                return self._decode(200, raw, {}, want_json)
            if hasattr(raw, "read"):
                try:
                    code = getattr(raw, "status", None)
                    if code is None:
                        code = raw.getcode()
                    response_headers = dict(raw.headers.items()) if getattr(raw, "headers", None) else {}
                    return self._decode(int(code), self._read_bounded(raw), response_headers, want_json)
                finally:
                    close = getattr(raw, "close", None)
                    if close:
                        close()
            return self._decode(200, raw, {}, want_json)

        request = Request(url, headers=dict(headers), method="GET")
        try:
            # Canonical endpoints require no redirects.  Rejecting them also
            # prevents HTTPS downgrade or credential forwarding to other hosts.
            context = self.ssl_context or create_ssl_context()
            with build_opener(_NoRedirect(), HTTPSHandler(context=context)).open(request, timeout=self.timeout) as response:
                code = getattr(response, "status", None)
                code = int(code if code is not None else response.getcode())
                response_headers = dict(response.headers.items())
                body = self._read_bounded(response)
                return self._decode(code, body, response_headers, want_json)
        except HTTPError as exc:
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            exc.close()
            # Reading an error body is unnecessary and risks retaining secrets.
            if exc.code == 404:
                return HttpResult("not_found", headers=response_headers, status_code=404)
            if exc.code in (401, 403):
                return HttpResult("auth_required", headers=response_headers, status_code=exc.code)
            if exc.code == 429:
                return HttpResult("rate_limited", headers=response_headers, status_code=429)
            if exc.code in (408, 425) or exc.code >= 500:
                return HttpResult("transport_error", headers=response_headers, status_code=exc.code)
            return HttpResult("invalid_response", headers=response_headers, status_code=exc.code)
        except ssl.SSLCertVerificationError:
            return HttpResult("transport_error", error_code="TLS_VERIFY_FAILED")
        except ssl.SSLError:
            return HttpResult("transport_error", error_code="TLS_ERROR")
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                return HttpResult("transport_error", error_code="TLS_VERIFY_FAILED")
            if isinstance(reason, ssl.SSLError):
                return HttpResult("transport_error", error_code="TLS_ERROR")
            return HttpResult("transport_error")
        except (TimeoutError, OSError):
            return HttpResult("transport_error")

    def _call_transport(self, url: str, headers: Mapping[str, str]) -> Any:
        transport = self.transport
        callback = transport if callable(transport) else getattr(transport, "request", None)
        if not callable(callback):
            raise TypeError("transport must be callable or provide request()")
        kwargs = {"params": None, "headers": dict(headers), "timeout": self.timeout,
                  "cancel_event": self.cancel_event}
        # Bind first instead of retrying a callback on TypeError: a callback can
        # raise after it has sent a request, and must never be repeated blindly.
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return callback(url, headers=dict(headers), timeout=self.timeout)
        accepts_all = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        filtered = kwargs if accepts_all else {k: v for k, v in kwargs.items() if k in signature.parameters}
        if not callable(transport) and "method" in signature.parameters:
            filtered["method"] = "GET"
        signature.bind(url=url, **filtered) if "url" in signature.parameters else signature.bind(url, **filtered)
        return callback(url=url, **filtered) if "url" in signature.parameters else callback(url, **filtered)

    def _read_bounded(self, response: Any) -> bytes:
        remaining = self.max_response_bytes + 1
        chunks: list[bytes] = []
        while remaining > 0:
            if self._is_cancelled():
                raise _Cancelled()
            chunk = response.read(min(65536, remaining))
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            chunks.append(bytes(chunk))
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > self.max_response_bytes:
            raise ValueError("response exceeds configured limit")
        return data

    def _decode(self, code: int, body: Any, headers: Mapping[str, str], want_json: bool) -> HttpResult:
        response_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if code == 404:
            return HttpResult("not_found", headers=response_headers, status_code=code)
        if code in (401, 403):
            return HttpResult("auth_required", headers=response_headers, status_code=code)
        if code == 429:
            return HttpResult("rate_limited", headers=response_headers, status_code=code)
        if code < 200 or code >= 300:
            status = "transport_error" if code in (408, 425) or code >= 500 else "invalid_response"
            return HttpResult(status, headers=response_headers, status_code=code)
        try:
            if isinstance(body, (bytes, bytearray)):
                if len(body) > self.max_response_bytes:
                    raise ValueError("response exceeds configured limit")
                body = bytes(body).decode("utf-8-sig")
            if want_json:
                if not isinstance(body, str):
                    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > self.max_response_bytes:
                        raise ValueError("response exceeds configured limit")
                    data = copy.deepcopy(body)
                else:
                    if len(body.encode("utf-8")) > self.max_response_bytes:
                        raise ValueError("response exceeds configured limit")
                    data = json.loads(body)
                return HttpResult("hit", data=data, headers=response_headers, status_code=code)
            if not isinstance(body, str) or len(body.encode("utf-8")) > self.max_response_bytes:
                raise ValueError("invalid text response")
            return HttpResult("hit", data=str(body), headers=response_headers, status_code=code)
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return HttpResult("invalid_response", headers=response_headers, status_code=code)

    def _wait_for_host(self, host: str) -> bool:
        while True:
            if self._is_cancelled():
                return False
            now = time.monotonic()
            with self._cooldown_lock:
                wait = max(
                    self._cooldowns.get(host, 0.0) - now,
                    self._last_request.get(host, 0.0) + self.min_interval - now,
                )
                if wait <= 0:
                    self._last_request[host] = now
                    return True
            if not self._sleep(wait):
                return False

    def _set_cooldown(self, host: str, seconds: float, status: str, status_code: int | None) -> None:
        if seconds <= 0:
            return
        with self._cooldown_lock:
            expires = time.monotonic() + seconds
            if expires > self._cooldowns.get(host, 0.0):
                self._cooldowns[host] = expires
                self._cooldown_status[host] = (status, status_code)

    def _sleep(self, seconds: float) -> bool:
        return not self.cancel_event.wait(max(0.0, seconds)) and not self._closed

    def _is_cancelled(self) -> bool:
        return self._closed or self.cancel_event.is_set()

    def _with_params(self, url: str, params: Mapping[str, Any] | None) -> str:
        if not params:
            return url
        pairs = [(str(k), str(v)) for k, v in params.items() if v is not None]
        if not pairs:
            return url
        parts = urlsplit(url)
        query = parts.query
        encoded = urlencode(pairs, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, f"{query}&{encoded}" if query else encoded, parts.fragment))

    def _cache_key(self, url: str, headers: Mapping[str, str], want_json: bool) -> str:
        # Fingerprint every header, including all auth/cookie headers together.
        # Neither the stored cache key nor any logged value contains secrets.
        header_value = json.dumps(sorted((k.lower(), v) for k, v in headers.items()))
        auth_fp = hashlib.sha256(header_value.encode("utf-8", "replace")).hexdigest()
        value = f"{url}\n{auth_fp}\n{int(want_json)}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _cached(self, key: str) -> HttpResult | None:
        if not self.cache_enabled:
            return None
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, result = entry
            if expires <= time.monotonic():
                self._cache.pop(key, None)
                return None
            return HttpResult(result.status, copy.deepcopy(result.data), dict(result.headers), result.status_code, result.error_code)

    def _store(self, key: str, result: HttpResult) -> None:
        ttl = self.success_cache_ttl if result.status == "hit" else self.not_found_cache_ttl
        if not self.cache_enabled or ttl <= 0 or self.max_cache_entries <= 0:
            return
        with self._cache_lock:
            now = time.monotonic()
            self._cache = {k: item for k, item in self._cache.items() if item[0] > now}
            if len(self._cache) >= self.max_cache_entries and key not in self._cache:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic() + ttl,
                                HttpResult(result.status, copy.deepcopy(result.data), dict(result.headers), result.status_code, result.error_code))


class _Cancelled(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    return next((str(v).strip() for k, v in (headers or {}).items() if str(k).lower() == name.lower()), "")


def _retry_after(headers: Mapping[str, str] | None) -> float:
    value = _header(headers, "retry-after")
    if not value:
        return 1.0
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else 1.0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 1.0
