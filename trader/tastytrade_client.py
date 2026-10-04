from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
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
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise BrokerError(f"tastytrade HTTP {exc.code} for {method} {path}: {detail} (status={exc.code})") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TransportError(f"tastytrade request failed: {exc}") from exc

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
        response = self.orders(account)
        items = response.get("data", {}).get("items", [])
        return next((item for item in items if item.get("external-identifier") == external_id), None)
