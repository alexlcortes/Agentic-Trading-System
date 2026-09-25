"""Human-in-the-loop override for trades the risk manager blocks solely for
hitting max_position_pct.

This is deliberately narrow in scope: it is called from exactly one place
(orchestration.graph.risk_final_check_node) and only when
risk_manager.check_trade rejected a BUY with
reason_code == "max_position_pct_exceeded". Every other rejection (kill
switch, daily loss halt, max_open_positions, sells) is a hard stop and is
never routed here — see the reason_code docstring in agents/risk_manager.py.

The actual "ask a human and wait" step lives in an external n8n workflow
(Webhook -> Discord message -> Wait-for-reply-or-timeout -> Respond to
Webhook), not in this process. This function makes ONE blocking HTTP POST
and treats anything other than a clean, well-formed "approved: true" as a
decline — a network error, a timeout, or a malformed response must never be
interpreted as approval, since that would silently defeat the position cap
it's meant to bypass only on purpose.
"""

from datetime import datetime, timezone

import httpx

from config import settings
from logs.audit_logger import log_human_override


def format_override_message(
    ticker: str,
    qty: int,
    notional: float,
    size_pct: float,
    existing_pct: float,
    max_position_pct: float,
) -> str:
    return (
        f"{ticker}: buy {qty} sh (~${notional:,.0f}, {size_pct:.2%} of equity)\n"
        f"current position: {existing_pct:.2%}, cap: {max_position_pct:.2%}"
    )


def request_override(
    run_id: str,
    ticker: str,
    requested_size_pct: float,
    requested_qty: int,
    requested_notional: float,
    equity: float,
    existing_pct: float,
    max_position_pct: float,
    reasoning: str,
) -> dict:
    """Ask a human whether to bypass max_position_pct for this one trade.

    Returns:
        {"approved": bool, "responder": str | None, "reason": str}
    """
    if not settings.ENABLE_HUMAN_OVERRIDE:
        return {
            "approved": False,
            "responder": None,
            "reason": "human override disabled (ENABLE_HUMAN_OVERRIDE is not set)",
        }

    if not settings.N8N_OVERRIDE_WEBHOOK_URL or not settings.N8N_OVERRIDE_SECRET:
        return {
            "approved": False,
            "responder": None,
            "reason": "human override enabled but N8N_OVERRIDE_WEBHOOK_URL/N8N_OVERRIDE_SECRET not configured",
        }

    payload = {
        "run_id": run_id,
        "ticker": ticker,
        "action": "buy",
        # Pre-rendered so the notification shows the real consequence
        # (shares and dollars) instead of a raw fraction a human has to
        # interpret — a bare "5.0" reads as 5% but means 500%.
        "message": format_override_message(
            ticker, requested_qty, requested_notional, requested_size_pct,
            existing_pct, max_position_pct,
        ),
        "requested_size_pct": requested_size_pct,
        "requested_qty": requested_qty,
        "requested_notional": round(requested_notional, 2),
        "equity": round(equity, 2),
        "existing_pct": existing_pct,
        "max_position_pct": max_position_pct,
        "reasoning": reasoning,
        "timeout_seconds": settings.OVERRIDE_TIMEOUT_SECONDS,
    }
    http_timeout = settings.OVERRIDE_TIMEOUT_SECONDS + settings.OVERRIDE_HTTP_TIMEOUT_BUFFER_SECONDS

    result: dict
    try:
        response = httpx.post(
            settings.N8N_OVERRIDE_WEBHOOK_URL,
            json=payload,
            headers={"X-Override-Secret": settings.N8N_OVERRIDE_SECRET},
            timeout=http_timeout,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or "approved" not in body:
            result = {
                "approved": False,
                "responder": None,
                "reason": f"malformed override response body: {body!r}",
            }
        else:
            result = {
                "approved": bool(body["approved"]),
                "responder": body.get("responder"),
                "reason": body.get("reason", "human responded via Discord"),
            }
    except httpx.TimeoutException:
        result = {
            "approved": False,
            "responder": None,
            "reason": f"no response within {settings.OVERRIDE_TIMEOUT_SECONDS}s — auto-declined",
        }
    except httpx.HTTPError as exc:
        result = {
            "approved": False,
            "responder": None,
            "reason": f"override request failed: {exc}",
        }

    log_human_override(
        run_id=run_id,
        ticker=ticker,
        requested_size_pct=requested_size_pct,
        requested_qty=requested_qty,
        requested_notional=requested_notional,
        timestamp=datetime.now(timezone.utc),
        result=result,
    )
    return result
