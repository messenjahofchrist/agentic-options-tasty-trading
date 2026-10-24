"""Option pricing and volatility analysis for a selected contract.

Contract selection itself is the ORIGINAL methodology, unchanged: trader.option_selector.evaluate_options with the
strategy's DTE, delta, spread, open-interest and volume rules. V2 only (a) fetches the near-the-money quotes using a
cached chain listing (same contract set as trader.market_data.fetch_option_quotes) and (b) adds checks on the
contract the original rules picked. It never relaxes a rule or swaps in a cheaper or less liquid contract.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any

from ..models import OptionQuote, require_ticker


def select_near_money(chain: list[dict], *, spot: float, min_dte: int, max_dte: int, max_contracts: int = 100) -> list[dict]:
    """Identical filter and ordering to trader.market_data.fetch_option_quotes."""
    contracts = [item for item in chain if item.get("active") and
                 min_dte <= int(item.get("days-to-expiration", -1)) <= max_dte and
                 abs(float(item.get("strike-price", 0)) - spot) <= spot * 0.05 and item.get("symbol")]
    contracts.sort(key=lambda item: (abs(float(item["strike-price"]) - spot),
                                    int(item.get("days-to-expiration", 999)), item["symbol"]))
    return contracts[:max_contracts]


def quotes_for_contracts(client, underlying: str, contracts: list[dict]) -> list[OptionQuote]:
    """One REST request for up to 100 contracts; parsing identical to fetch_option_quotes."""
    if not contracts:
        return []
    by_symbol = {item["symbol"]: item for item in contracts}
    query = urllib.parse.urlencode({"equity-option[]": ",".join(by_symbol)})
    items = client.quotes(query).get("data", {}).get("items", [])
    out: list[OptionQuote] = []
    for item in items:
        contract = by_symbol.get(item.get("symbol"))
        if not contract:
            continue
        try:
            expiration_raw = contract.get("expiration-date") or contract.get("expires-at")
            expiration = datetime.fromisoformat(expiration_raw.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=timezone.utc)
            quote_time_raw = item.get("updated-at")
            quote_time = datetime.fromisoformat(quote_time_raw.replace("Z", "+00:00")) if quote_time_raw else None
            optional = lambda name: float(item[name]) if item.get(name) not in (None, "") else None
            out.append(OptionQuote(
                symbol=item["symbol"], underlying=underlying, expiration=expiration,
                strike=float(contract["strike-price"]), right=contract.get("option-type", "").upper(),
                bid=float(item.get("bid") or 0), ask=float(item.get("ask") or 0),
                bid_size=int(float(item.get("bid-size") or 0)), ask_size=int(float(item.get("ask-size") or 0)),
                delta=optional("delta"), gamma=optional("gamma"), theta=optional("theta"),
                vega=optional("vega"), iv=optional("volatility"), volume=int(float(item.get("volume") or 0)),
                open_interest=int(float(item.get("open-interest") or 0)), quote_timestamp=quote_time))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return out


def fetch_option_quotes_cached(client, hub, *, underlying: str, spot: float, now: datetime, min_dte: int,
                               max_dte: int, max_contracts: int = 100) -> list[OptionQuote]:
    require_ticker(underlying)
    if spot <= 0 or min_dte < 1 or max_dte < min_dte:
        raise ValueError("Invalid underlying, spot, or DTE range for option quote request")
    chain = hub.option_chain(underlying, now.astimezone(timezone.utc).date())
    return quotes_for_contracts(client, underlying, select_near_money(chain, spot=spot, min_dte=min_dte,
                                                                      max_dte=max_dte, max_contracts=max_contracts))


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def volatility_inputs(metrics: dict[str, Any] | None) -> dict[str, Any]:
    if not metrics:
        return {"available": False}
    hv30 = _f(metrics.get("historical-volatility-30-day"))
    return {"available": True,
            "iv_index": _f(metrics.get("implied-volatility-index")),
            "iv_rank": _f(metrics.get("tw-implied-volatility-index-rank")),
            "iv_rank_source": "tastytrade tw-implied-volatility-index-rank (52-week)",
            "iv_percentile": _f(metrics.get("implied-volatility-percentile")),
            "iv_index_5_day_change": _f(metrics.get("implied-volatility-index-5-day-change")),
            # tastytrade quotes HV in percent points (e.g. 16.41) and IV as a fraction (0.29).
            # A zero or missing HV is a data gap (seen for IBIT), not "no volatility": treated as missing.
            "hv30": hv30 / 100 if hv30 is not None and hv30 > 0 else None,
            "liquidity_rating": metrics.get("liquidity-rating"),
            "metrics_updated_at": metrics.get("implied-volatility-updated-at") or metrics.get("updated-at"),
            "retrieved_at_utc": metrics.get("_retrieved_at_utc")}


def evaluate_option(quote: OptionQuote, metrics: dict[str, Any] | None, now: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    """Pricing, volatility and liquidity checks on the contract the original rules selected."""
    reasons, flags = [], []
    vol = volatility_inputs(metrics)
    age = (now - quote.quote_timestamp).total_seconds() if quote.quote_timestamp else None
    pricing = {"bid": quote.bid, "ask": quote.ask, "mid": quote.mid, "spread_dollars": round(quote.spread, 4),
               "spread_fraction": round(quote.spread_fraction, 4), "iv": quote.iv, "delta": quote.delta,
               "gamma": quote.gamma, "theta": quote.theta, "vega": quote.vega, "open_interest": quote.open_interest,
               "volume": quote.volume, "bid_size": quote.bid_size, "ask_size": quote.ask_size,
               "quote_timestamp": quote.quote_timestamp.isoformat() if quote.quote_timestamp else None,
               "quote_age_seconds": round(age, 2) if age is not None else None,
               "extrinsic_estimate": None}
    if age is None or age > cfg["max_quote_age_seconds"]:
        reasons.append(f"option_quote_stale_or_untimed (age {age})")
    if quote.bid_size < cfg["min_bid_size"] or quote.ask_size < cfg["min_ask_size"]:
        reasons.append(f"displayed_size_below_minimum (bid {quote.bid_size} x ask {quote.ask_size})")
    if quote.vega is None or quote.iv is None:
        reasons.append("contract_iv_or_vega_missing")
    if not vol["available"]:
        status = "missing_volatility_data"
        (reasons if cfg["missing_volatility_data"] == "reject" else flags).append("underlying_volatility_metrics_unavailable")
    else:
        status = "evaluated"
        retrieved = vol.get("retrieved_at_utc")
        if retrieved and (now - datetime.fromisoformat(retrieved)).total_seconds() > cfg["max_metrics_age_minutes"] * 60:
            (reasons if cfg["missing_volatility_data"] == "reject" else flags).append("volatility_metrics_stale")
        if vol["iv_rank"] is None or vol["hv30"] is None or vol["iv_index"] is None:
            (reasons if cfg["missing_volatility_data"] == "reject" else flags).append("iv_rank_or_hv_missing")
        else:
            if vol["iv_rank"] > cfg["max_iv_rank"]:
                reasons.append(f"iv_rank_{vol['iv_rank']:.2f}_above_{cfg['max_iv_rank']}")
            if vol["hv30"] > 0 and vol["iv_index"] / vol["hv30"] > cfg["max_iv_to_hv30"]:
                reasons.append(f"iv_to_hv30_{vol['iv_index'] / vol['hv30']:.2f}_above_{cfg['max_iv_to_hv30']}")
            if quote.iv is not None and vol["iv_index"] > 0 and quote.iv / vol["iv_index"] > cfg["max_contract_iv_to_index_iv"]:
                reasons.append(f"contract_iv_{quote.iv / vol['iv_index']:.2f}x_underlying_iv_index")
            change = vol.get("iv_index_5_day_change")
            vol["iv_trend"] = None if change is None else "expanding" if change > 0 else "contracting" if change < 0 else "flat"
    return {"status": status, "ok": not reasons, "reasons": reasons, "flags": flags, "pricing": pricing, "volatility": vol}
