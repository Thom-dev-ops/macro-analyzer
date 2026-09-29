"""Chief Analyst — Claude tool-use loop over the Macro Analyzer dataset.

Endpoint (mounted by main.py):
  POST /api/analyst/ask  → { answer, trace, model, cost_usd, latency_ms }

The endpoint runs a bounded Claude tool-use loop. Tools call into the
existing learning/scoring/prices helpers directly (no HTTP hop) so the
answer is grounded in the same data the dashboard renders. A read-only
SQL tool is included as an escape hatch for anything the wrappers don't
cover.

Model routing:
  request.model == "sonnet" (default) → settings.claude_model
  request.model == "opus"              → settings.analyst_opus_model or hard-coded fallback

If MPA_ANTHROPIC_API_KEY is unset the endpoint returns a helpful stub
answer rather than a 500 so the UI still works during setup.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from macro_positioning.core.settings import settings


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analyst", tags=["analyst"])


# ── Request / response models ──────────────────────────────────────────────


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    model: str = "sonnet"  # "sonnet" | "opus"
    history: list[ChatTurn] = Field(default_factory=list)


class TraceStep(BaseModel):
    tool: str
    args_summary: str = ""
    result_summary: str = ""
    error: bool = False


class AskResponse(BaseModel):
    answer: str
    trace: list[TraceStep]
    model: str
    cost_usd: Optional[float] = None
    latency_ms: Optional[float] = None


# ── Model IDs — override via env if you want the Claude 5 family ───────────
# Defaults are the project's known-good sonnet/opus. User can flip via
# MPA_ANALYST_SONNET_MODEL / MPA_ANALYST_OPUS_MODEL.

def _sonnet_model() -> str:
    return getattr(settings, "analyst_sonnet_model", "") or settings.claude_model or "claude-sonnet-4-5"


def _opus_model() -> str:
    return getattr(settings, "analyst_opus_model", "") or "claude-opus-4-6"


def _analyst_api_key() -> str:
    """Dedicated chat-agent key wins; falls back to the shared Anthropic key."""
    return (
        getattr(settings, "anthropic_chatagent_api_key", "")
        or settings.anthropic_api_key
        or ""
    )


# ── Tool wrappers ──────────────────────────────────────────────────────────
#
# Each wrapper returns a dict with a small, LLM-friendly shape.
# Errors are caught inside so the tool loop can recover instead of 500ing.


def _tool_get_source_accuracy(window_days: Optional[int] = 30) -> dict:
    from macro_positioning.learning.call_accuracy import source_accuracy
    rows = source_accuracy(window_days=window_days) or []
    trimmed = [
        {
            "author": r.get("display_name") or r.get("author_id"),
            "n_calls": r.get("n_calls"),
            "meaningful": r.get("meaningful"),
            "setup_win_rate": r.get("setup_win_rate"),
            "avg_r_planned": r.get("avg_r_planned"),
            "alpha_win_rate": r.get("alpha_win_rate"),
            "avg_alpha_pct": r.get("avg_alpha_pct"),
            "dir_win_rate": r.get("win_rate"),
            "avg_return_pct": r.get("avg_return_pct"),
        }
        for r in rows
    ]
    return {"window_days": window_days, "sources": trimmed}


def _tool_get_trusted_kol_calls(
    window_days: int = 14,
    ticker: Optional[str] = None,
    limit: int = 80,
) -> dict:
    """Recent trade calls from trusted KOL authors.

    Source of truth is `documents.extracted_features_json` — the vision
    layer's per-chart extraction (ticker, bias, call_type, entries[],
    setups[], trade_stage, thesis). Joins `input_authors` for the display
    name + trust_weight and filters to trusted authors only.

    `call_type` is honored: `no_trade` and `not_a_chart` drops carry no
    directional signal and are excluded.
    """
    sql = """
    SELECT d.document_id,
           d.published_at,
           d.author_id,
           COALESCE(ia.display_name, d.author_id) AS author,
           ia.trust_weight,
           d.source_id,
           d.extracted_features_json
    FROM documents d
    LEFT JOIN input_authors ia ON ia.author_id = d.author_id
    WHERE d.published_at >= datetime('now', ?)
      AND ia.trust_weight IS NOT NULL
      AND d.extracted_features_json IS NOT NULL
      AND d.extracted_features_json != ''
    ORDER BY d.published_at DESC
    LIMIT ?
    """
    params: list[Any] = [f"-{int(window_days)} days", int(limit) * 3]

    want_ticker = str(ticker).upper() if ticker else None
    calls: list[dict] = []
    with sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
        except sqlite3.OperationalError as e:
            return {"error": f"query failed: {e}", "calls": []}

    for r in rows:
        try:
            feats = json.loads(r["extracted_features_json"] or "null") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(feats, dict):
            continue
        call_type = (feats.get("call_type") or "").lower()
        if call_type in ("no_trade", "not_a_chart"):
            continue
        row_ticker = (feats.get("ticker") or "").upper() or None
        if want_ticker and row_ticker != want_ticker:
            continue
        calls.append({
            "document_id": r["document_id"],
            "published_at": r["published_at"],
            "author": r["author"],
            "author_id": r["author_id"],
            "trust_weight": r["trust_weight"],
            "source_id": r["source_id"],
            "ticker": row_ticker,
            "bias": feats.get("bias"),
            "call_type": feats.get("call_type"),
            "trade_stage": feats.get("trade_stage"),
            "direction": feats.get("direction"),
            "timeframe": feats.get("timeframe"),
            "entries": feats.get("entries"),
            "setups": feats.get("setups"),
            "targets": feats.get("targets"),
            "stops": feats.get("stops"),
            "thesis": feats.get("thesis") or feats.get("summary"),
        })
        if len(calls) >= limit:
            break

    return {
        "window_days": window_days,
        "ticker": ticker,
        "n": len(calls),
        "calls": calls,
    }


def _tool_get_trusted_source_themes(window_days: int = 90, min_trust: float = 1.15) -> dict:
    from macro_positioning.learning.source_themes import trusted_source_themes
    themes = trusted_source_themes(min_trust=min_trust, window_days=window_days) or []
    out = []
    for t in themes:
        d = t.to_dict() if hasattr(t, "to_dict") else t
        out.append({
            "author": d.get("author") or d.get("display_name") or d.get("author_id"),
            "top_tickers": d.get("top_tickers"),
            "bias_distribution": d.get("bias_distribution"),
            "top_setups": d.get("top_setups"),
            "high_conviction": d.get("high_conviction_tickers"),
        })
    return {"window_days": window_days, "min_trust": min_trust, "authors": out}


def _tool_get_author_themes(author_id: str, window_days: int = 90) -> dict:
    from macro_positioning.learning.source_themes import author_themes
    res = author_themes(author_id, window_days=window_days)
    if res is None:
        return {"author_id": author_id, "found": False}
    d = res.to_dict() if hasattr(res, "to_dict") else res
    return {"author_id": author_id, "found": True, "themes": d}


def _tool_get_spot_price(ticker: str) -> dict:
    from macro_positioning.prices.spot import spot_price
    px = spot_price(ticker)
    if not px:
        return {"ticker": ticker, "found": False}
    return {"ticker": ticker, "found": True, **px}


def _tool_get_technicals(ticker: str, days: int = 180) -> dict:
    from macro_positioning.prices.provider import default_provider
    from macro_positioning.prices.technicals import compute_technical_features
    try:
        bars = default_provider().fetch_history(ticker, days=days)
    except Exception as e:
        return {"ticker": ticker, "error": f"fetch_history failed: {e}"}
    if not bars:
        return {"ticker": ticker, "error": "no bars"}
    feats = compute_technical_features(bars) or {}
    return {"ticker": ticker, "days": days, "n_bars": len(bars), **feats}


_DESK_ALLOWED_SECTIONS = {
    "regime", "kpis", "heroSignals", "macroHome", "watchlist",
    "conceptSuggestions", "activeTrades", "closedTrades",
    "sourceLeaderboard", "streams", "reasoning", "sourceHealth",
}


def _tool_get_desk_snapshot(sections: Optional[list[str]] = None) -> dict:
    from macro_positioning.dashboard.desk_data import build_desk_snapshot
    snap = build_desk_snapshot() or {}
    if not sections:
        sections = ["regime", "kpis", "heroSignals", "watchlist", "activeTrades", "sourceLeaderboard"]
    keep = {}
    for k in sections:
        if k in _DESK_ALLOWED_SECTIONS and k in snap:
            keep[k] = snap[k]
    return {"sections": list(keep.keys()), "data": keep}


_SQL_FORBIDDEN = (
    "insert", "update", "delete", "drop", "alter", "create",
    "attach", "detach", "replace", "pragma", "vacuum",
)


def _tool_query_db(sql: str, limit: int = 200) -> dict:
    """Read-only SELECT escape hatch. One statement, LIMIT enforced."""
    s = (sql or "").strip().rstrip(";")
    if ";" in s:
        return {"error": "only a single statement is allowed"}
    lower = s.lower()
    if not lower.startswith(("select", "with")):
        return {"error": "only SELECT / WITH queries are allowed"}
    for w in _SQL_FORBIDDEN:
        if f" {w} " in f" {lower} ":
            return {"error": f"forbidden keyword: {w}"}
    if " limit " not in lower:
        s = f"{s} LIMIT {int(limit)}"

    try:
        with sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(s)
            rows = [dict(r) for r in cur.fetchall()]
            return {"sql": s, "n": len(rows), "rows": rows}
    except sqlite3.OperationalError as e:
        return {"error": f"query failed: {e}"}


def _tool_list_tables() -> dict:
    with sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
    return {"tables": tables}


def _tool_describe_table(table: str) -> dict:
    if not table.replace("_", "").isalnum():
        return {"error": "invalid table name"}
    with sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True) as conn:
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [{"name": r[1], "type": r[2], "notnull": bool(r[3]), "pk": bool(r[5])} for r in cur.fetchall()]
    return {"table": table, "columns": cols}


# ── Tool registry — Claude tool schema + Python callable ───────────────────


TOOLS: list[dict] = [
    {
        "name": "get_source_accuracy",
        "description": (
            "Per-source (KOL) accuracy scoreboard. Returns setup_win_rate "
            "(target-before-stop, PRIMARY), avg_r_planned (planned R multiple, "
            "winsorized), alpha_win_rate + avg_alpha_pct (BTC-relative), plus "
            "raw directional win_rate. Use setup_win_rate + avg_alpha_pct as "
            "the trustworthy accuracy signal, NOT dir_win_rate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "description": "Rolling window; omit for all-time.", "minimum": 7},
            },
        },
    },
    {
        "name": "get_trusted_kol_calls",
        "description": (
            "Recent vision-extracted chart calls from trusted KOL authors "
            "(trust_weight NOT NULL). Source: documents.extracted_features_json. "
            "Returns per-call ticker, bias, call_type, trade_stage, direction, "
            "entries[], setups[], targets[], stops[], thesis, author, timestamp. "
            "no_trade / not_a_chart drops are excluded (no directional signal). "
            "window_days defaults to 14."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "minimum": 1, "maximum": 180, "default": 14},
                "ticker": {"type": "string", "description": "Filter to one symbol (e.g. BTC)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80},
            },
        },
    },
    {
        "name": "get_trusted_source_themes",
        "description": (
            "Rollup of what every trusted author is calling: top tickers, bias "
            "distribution, top setups, high-conviction tickers. Good for a "
            "'where are the trusted voices leaning' overview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "default": 90, "minimum": 7},
                "min_trust": {"type": "number", "default": 1.15},
            },
        },
    },
    {
        "name": "get_author_themes",
        "description": "Deep dive into ONE author's themes: top tickers, biases, setups, high-conviction picks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "author_id": {"type": "string"},
                "window_days": {"type": "integer", "default": 90},
            },
            "required": ["author_id"],
        },
    },
    {
        "name": "get_spot_price",
        "description": "Current spot for a ticker + prior close + 1d change. Use to check where price is now vs the entry/target zones on a call.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_technicals",
        "description": (
            "Technical features for a ticker: MA20/50/200, EMAs, ATR14, RSI14, "
            "recent breakout flag, prior_high_20, swing_low_10, pct_change over "
            "1/3/5/20/60d, higher-highs/lows. Use to check current structure "
            "and levels vs mentioned support/resistance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days": {"type": "integer", "default": 180, "minimum": 30, "maximum": 720},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_desk_snapshot",
        "description": (
            "Current dashboard state — same shapes the SPA renders. Sections: "
            "regime, kpis, heroSignals, watchlist, activeTrades, sourceLeaderboard, "
            "closedTrades, streams, reasoning, sourceHealth, macroHome, conceptSuggestions. "
            "Ask for only what you need — the full snapshot is large."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sections to include; default = regime, kpis, heroSignals, watchlist, activeTrades, sourceLeaderboard.",
                },
            },
        },
    },
    {
        "name": "list_tables",
        "description": "List every table in the SQLite DB. Use before writing a SQL query if you're not sure what's available.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": "Return column names/types for one table. Use before a query_db call if you're not sure of the schema.",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "query_db",
        "description": (
            "Read-only SQL escape hatch. Single SELECT (or WITH) statement, "
            "LIMIT auto-applied at 200 if you don't include one. Use only when "
            "the higher-level tools above don't cover what you need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
            },
            "required": ["sql"],
        },
    },
]

_TOOL_IMPLS = {
    "get_source_accuracy": _tool_get_source_accuracy,
    "get_trusted_kol_calls": _tool_get_trusted_kol_calls,
    "get_trusted_source_themes": _tool_get_trusted_source_themes,
    "get_author_themes": _tool_get_author_themes,
    "get_spot_price": _tool_get_spot_price,
    "get_technicals": _tool_get_technicals,
    "get_desk_snapshot": _tool_get_desk_snapshot,
    "list_tables": _tool_list_tables,
    "describe_table": _tool_describe_table,
    "query_db": _tool_query_db,
}


# ── System prompt ──────────────────────────────────────────────────────────


SYSTEM_PROMPT = """You are the Chief Analyst inside the Macro Analyzer app — a personal
trading-intelligence dashboard the user runs to combine trusted-KOL calls,
per-source accuracy metrics, theme momentum, and price/technical data into
directional views.

Your job: answer the user's question by CALLING TOOLS to pull the actual
data, then synthesizing a decision-grade answer. Do not answer from memory
or guess numbers — every quantitative claim must come from a tool result.

Ground rules:
- Weight KOL views by their accuracy. `setup_win_rate` and `avg_alpha_pct`
  are the trustworthy metrics; `dir_win_rate` = market beta and should be
  used only as a caveat, not a headline.
- When the user asks about a symbol, check current spot + technicals AND the
  recent trusted-KOL calls on that symbol. Note if we're already past a
  mentioned entry zone or target.
- Bull/bear mix: if trusted voices conflict, say so — don't average them into
  a fake middle. Call out who's on each side and by how much conviction.
- Themes matter: if a theme is in breakout momentum, note it even if the
  specific ticker question didn't ask about it.
- Be concise. Structured markdown is fine (headers, short bullet lists).
  Lead with the answer. Show the reasoning underneath. End with "what would
  invalidate this view" when giving a directional call.
- If a tool returns an error or empty result, say so — do not pretend the
  data supported your point.
- Never invent a KOL name, a ticker, or a number. If you can't find it, say so.

You can call up to 10 tools per turn. Be economical — most questions need
2–5 tool calls, not 10."""


# ── Endpoint ───────────────────────────────────────────────────────────────


_MAX_TOOL_ITERATIONS = 10


def _summarize_args(args: dict) -> str:
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "…"
        parts.append(f"{k}={s}")
    joined = ", ".join(parts)
    return joined[:200]


def _summarize_result(result: Any) -> str:
    try:
        s = json.dumps(result, default=str)
    except Exception:
        s = str(result)
    if len(s) > 220:
        return s[:217] + "…"
    return s


def _estimate_cost(model: str, in_toks: int, out_toks: int) -> float:
    # Rough $/1M tokens. Keep aligned with brain/backends.py._PRICING.
    if "opus" in model.lower():
        return (in_toks / 1_000_000) * 15.0 + (out_toks / 1_000_000) * 75.0
    if "haiku" in model.lower():
        return (in_toks / 1_000_000) * 0.80 + (out_toks / 1_000_000) * 4.0
    # sonnet
    return (in_toks / 1_000_000) * 3.0 + (out_toks / 1_000_000) * 15.0


@router.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    t0 = time.time()

    api_key = _analyst_api_key()
    if not api_key:
        return AskResponse(
            answer=(
                "Chief Analyst is wired but no Anthropic key is set. "
                "Set MPA_ANTHROPIC_CHATAGENT_API_KEY (preferred; isolates chat "
                "spend) or MPA_ANTHROPIC_API_KEY and restart the API server."
            ),
            trace=[],
            model="unconfigured",
            cost_usd=None,
            latency_ms=(time.time() - t0) * 1000,
        )

    try:
        import anthropic
    except ImportError:
        raise HTTPException(500, "anthropic SDK not installed (pip install anthropic)")

    client = anthropic.Anthropic(api_key=api_key)
    model_name = _opus_model() if req.model == "opus" else _sonnet_model()

    # Build message list from prior history + new question.
    messages: list[dict] = []
    for turn in req.history:
        if turn.role in ("user", "assistant"):
            messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": req.question})

    trace: list[TraceStep] = []
    total_in = 0
    total_out = 0

    for _ in range(_MAX_TOOL_ITERATIONS):
        try:
            resp = client.messages.create(
                model=model_name,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            logger.exception("analyst: anthropic call failed")
            raise HTTPException(502, f"anthropic call failed: {e}")

        usage = getattr(resp, "usage", None)
        if usage is not None:
            total_in += getattr(usage, "input_tokens", 0) or 0
            total_out += getattr(usage, "output_tokens", 0) or 0

        # Collect any tool_use blocks in this response.
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]

        # Append the assistant turn verbatim so tool_use IDs can be matched.
        messages.append({
            "role": "assistant",
            "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in resp.content],
        })

        if resp.stop_reason == "end_turn" or not tool_uses:
            # Extract final text.
            answer_parts = [
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
            ]
            answer = "\n".join(p for p in answer_parts if p).strip()
            if not answer:
                answer = "(no answer produced)"
            latency_ms = (time.time() - t0) * 1000
            return AskResponse(
                answer=answer,
                trace=trace,
                model=model_name,
                cost_usd=_estimate_cost(model_name, total_in, total_out),
                latency_ms=latency_ms,
            )

        # Execute each requested tool + append tool_result blocks.
        tool_results: list[dict] = []
        for tu in tool_uses:
            name = tu.name
            args = tu.input or {}
            impl = _TOOL_IMPLS.get(name)
            step = TraceStep(tool=name, args_summary=_summarize_args(args))
            if impl is None:
                step.error = True
                step.result_summary = "unknown tool"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps({"error": f"unknown tool: {name}"}),
                    "is_error": True,
                })
            else:
                try:
                    result = impl(**args)
                    step.result_summary = _summarize_result(result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, default=str),
                    })
                except TypeError as e:
                    step.error = True
                    step.result_summary = f"bad args: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps({"error": f"bad args: {e}"}),
                        "is_error": True,
                    })
                except Exception as e:
                    logger.exception("analyst tool %s crashed", name)
                    step.error = True
                    step.result_summary = f"crash: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps({"error": f"tool crashed: {e}"}),
                        "is_error": True,
                    })
            trace.append(step)

        messages.append({"role": "user", "content": tool_results})

    # Iteration budget exhausted.
    latency_ms = (time.time() - t0) * 1000
    return AskResponse(
        answer=(
            "Chief Analyst hit the 10-tool-call budget without finishing. "
            "The trace shows what it looked at — try asking a narrower question."
        ),
        trace=trace,
        model=model_name,
        cost_usd=_estimate_cost(model_name, total_in, total_out),
        latency_ms=latency_ms,
    )
