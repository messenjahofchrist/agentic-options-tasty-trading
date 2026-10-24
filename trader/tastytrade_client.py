from __future__ import annotations

import json
import gzip
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any


class BrokerError(RuntimeError):
    pass


class UncertainOrderSubmission(BrokerError):
    pass


class TransportError(BrokerError):
    pass


@dataclass
class OAuthToken:
    access_token: str
    expires_at: float


def _decode_content(body: bytes, content_encoding: str) -> bytes:
    """Decode HTTP content encodings before parsing JSON or surfacing API errors."""
    for encoding in reversed([part.strip().lower() for part in content_encoding.split(",") if part.strip()]):
        if encoding in {"gzip", "x-gzip"}:
            body = gzip.decompress(body)
        elif encoding == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
    return body


class _RequestPacer:
    """Process-wide spacing between REST request starts. tastytrade answers HTTP 429 when ~30 requests arrive
    at once (measured 2026-10-16 with 47 concurrent quote snapshots). Off unless set_request_rate_limit() is
    called, so the original engine's request timing is unchanged."""

    def __init__(self, per_second: float, *, get_retries: int = 3, backoff_seconds: float = 1.0):
        self.interval = 1.0 / per_second
        self.get_retries, self.backoff_seconds = get_retries, backoff_seconds
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


_PACER: _RequestPacer | None = None


def set_request_rate_limit(per_second: float | None) -> None:
    """Limit this process's tastytrade REST request rate (None disables)."""
    global _PACER
    _PACER = _RequestPacer(per_second) if per_second else None


# Optional observer(method, path, status, seconds, attempt) for request logging (V2 API accounting). None = off.
_OBSERVER = None


def set_request_observer(observer) -> None:
    global _OBSERVER
    _OBSERVER = observer


def _observe(method: str, path: str, status, started: float, attempt: int) -> None:
    if _OBSERVER is not None:
        try:
            _OBSERVER(method, path, status, time.monotonic() - started, attempt)
        except Exception:
            pass  # logging must never affect a request


class TastytradeClient:
    """Small OAuth REST client. It never submits an order except via submit_order()."""
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, base_url: str,
                 user_agent: str = "optio-bot/0.1.0", timeout: float = 15):
        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("OAuth client credentials required")
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.base_url, self.user_agent, self.timeout = base_url.rstrip("/"), user_agent, timeout
        self._token: OAuthToken | None = None

    def _request(self, method: str, path: str, *, body: dict | None = None, auth: bool = True) -> Any:
        if auth:
            token = self._access_token()
        else:
            token = None
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if token: headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=payload, headers=headers, method=method)
        # With pacing on, a read-only GET refused with 429 is retried after a short backoff. Never a POST,
        # PUT or DELETE: order submission and cancellation are not repeated by this layer.
        attempts = _PACER.get_retries + 1 if _PACER is not None and method == "GET" else 1
        for attempt in range(attempts):
            if _PACER is not None:
                _PACER.wait()
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    content = _decode_content(response.read(), response.headers.get("Content-Encoding", ""))
                    _observe(method, path, getattr(response, "status", None), started, attempt + 1)
                    return json.loads(content.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                _observe(method, path, exc.code, started, attempt + 1)
                content = _decode_content(exc.read(), exc.headers.get("Content-Encoding", ""))
                detail = content.decode("utf-8", errors="replace")[:1000]
                if exc.code == 429 and attempt + 1 < attempts:
                    time.sleep(_PACER.backoff_seconds * (attempt + 1))
                    continue
                raise BrokerError(f"tastytrade HTTP {exc.code} for {method} {path}: {detail} (status={exc.code})") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                _observe(method, path, "transport_error", started, attempt + 1)
                raise TransportError(f"tastytrade request failed: {exc}") from exc
        raise AssertionError("unreachable")

    def clock_offset_seconds(self, samples: int = 3) -> float:
        """Estimate (tastytrade server clock - local clock) from the HTTP Date header.

        The header has one-second resolution, so the estimate is good to about +/-0.5s plus latency:
        enough to catch an unsynchronized PC clock, which otherwise makes fresh quotes look future-dated.
        """
        from email.utils import parsedate_to_datetime
        offsets = []
        for _ in range(samples):
            req = urllib.request.Request(self.base_url + "/", headers={"User-Agent": self.user_agent}, method="HEAD")
            sent = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    stamp = response.headers.get("Date")
            except urllib.error.HTTPError as exc:  # any status still carries the server's Date header
                stamp = exc.headers.get("Date")
            except (urllib.error.URLError, TimeoutError) as exc:
                raise TransportError(f"tastytrade clock check failed: {exc}") from exc
            received = time.time()
            if stamp:
                # Mid-second of the server's truncated timestamp vs. the local request midpoint.
                offsets.append(parsedate_to_datetime(stamp).timestamp() + 0.5 - (sent + received) / 2)
        if not offsets:
            raise TransportError("tastytrade responses did not include a Date header")
        return sorted(offsets)[len(offsets) // 2]

    def _access_token(self) -> str:
        if self._token and time.time() < self._token.expires_at - 30:
            return self._token.access_token
        body = {"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                "client_id": self.client_id, "client_secret": self.client_secret}
        result = self._request("POST", "/oauth/token", body=body, auth=False)
        try:
            token = result["access_token"]
            lifetime = float(result.get("expires_in", 900))
        except (TypeError, KeyError, ValueError) as exc:
            raise BrokerError("OAuth token response missing access_token/expires_in") from exc
        self._token = OAuthToken(token, time.time() + lifetime)
        return token

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def accounts(self) -> Any:
        return self.get("/customers/me/accounts")

    def balances(self, account: str) -> Any:
        return self.get(f"/accounts/{urllib.parse.quote(account, safe='')}/balances")

    def positions(self, account: str) -> Any:
        return self.get(f"/accounts/{urllib.parse.quote(account, safe='')}/positions")

    def orders(self, account: str) -> Any:
        return self.get(f"/accounts/{urllib.parse.quote(account, safe='')}/orders")

    def trading_status(self, account: str) -> Any:
        return self.get(f"/accounts/{urllib.parse.quote(account, safe='')}/trading-status")

    def live_orders(self, account: str) -> Any:
        """Orders still working at the broker plus those that reached a terminal state today."""
        return self.get(f"/accounts/{urllib.parse.quote(account, safe='')}/orders/live")

    def order(self, account: str, order_id: str | int) -> Any:
        return self.get(f"/accounts/{urllib.parse.quote(account, safe='')}/orders/{urllib.parse.quote(str(order_id), safe='')}")

    def cancel_order(self, account: str, order_id: str | int) -> Any:
        """Request cancellation. The response is a request only; poll order() until it reports Cancelled."""
        path = f"/accounts/{urllib.parse.quote(account, safe='')}/orders/{urllib.parse.quote(str(order_id), safe='')}"
        return self._request("DELETE", path)

    def quotes(self, query: str) -> Any:
        return self.get("/market-data/by-type?" + query)

    def option_chain(self, underlying: str) -> Any:
        return self.get("/option-chains/" + urllib.parse.quote(underlying, safe=""))

    def market_metrics(self, symbols: list[str]) -> Any:
        if not symbols: raise ValueError("At least one market-metrics symbol is required")
        query = urllib.parse.urlencode({"symbols": ",".join(symbols)})
        return self.get("/market-metrics?" + query)

    def equity_session(self, which: str = "current") -> Any:
        if which not in {"current", "next", "previous"}: raise ValueError("Invalid equity session selector")
        return self.get(f"/market-time/equities/sessions/{which}")

    def quote_token(self) -> Any:
        return self.get("/api-quote-tokens")

    def dry_run_order(self, account: str, order: dict) -> Any:
        path = f"/accounts/{urllib.parse.quote(account, safe='')}/orders/dry-run"
        return self._request("POST", path, body=order)

    def submit_order(self, account: str, order: dict, *, dry_run_result: dict | None = None) -> Any:
        if dry_run_result is None:
            raise BrokerError("Order submission blocked: a successful dry-run result is required")
        data = dry_run_result.get("data")
        if not isinstance(data, dict):
            raise BrokerError("Order submission blocked: malformed dry-run response")
        if data.get("errors") or dry_run_result.get("errors"):
            raise BrokerError("Order submission blocked: dry-run returned errors")
        if data.get("warnings") or dry_run_result.get("warnings"):
            raise BrokerError("Order submission blocked: dry-run returned warnings requiring human review")
        if not data.get("order") and not data.get("complex-order"):
            raise BrokerError("Order submission blocked: dry-run response has no validated order")
        if not order.get("external-identifier"):
            raise BrokerError("Order submission blocked: unique external-identifier required")
        path = f"/accounts/{urllib.parse.quote(account, safe='')}/orders"
        try:
            return self._request("POST", path, body=order)
        except TransportError as exc:
            # The caller must query orders for this external identifier before any retry.
            raise UncertainOrderSubmission(f"Submission outcome uncertain for {order['external-identifier']}; reconcile before retry") from exc
        except BrokerError as exc:
            if "status=5" in str(exc):
                raise UncertainOrderSubmission(f"Submission outcome uncertain for {order['external-identifier']}; reconcile before retry") from exc
            raise

    def find_order_by_external_id(self, account: str, external_id: str) -> dict | None:
        # /orders/live covers every order touched today; /orders is the paginated history fallback.
        for response in (self.live_orders(account), self.orders(account)):
            items = response.get("data", {}).get("items", [])
            found = next((item for item in items if item.get("external-identifier") == external_id), None)
            if found is not None:
                return found
        return None
