from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from macro_positioning.core.models import PipelineRunRequest, PipelineRunResult, PositioningMemo, SourceOnboardingRequest, Thesis
from macro_positioning.core.settings import settings
from macro_positioning.api.funnel import router as funnel_router
from macro_positioning.api.insiders_routes import router as insiders_router
from macro_positioning.api.journal_routes import router as journal_router
from macro_positioning.api.manual_input import router as manual_input_router
from macro_positioning.api.paper_cohort_routes import router as paper_cohort_router
from macro_positioning.api.paper_routes import router as paper_router
from macro_positioning.api.stock_unlocked_routes import router as stock_unlocked_router
from macro_positioning.api.rules_routes import router as rules_router
from macro_positioning.api.signal_routes import router as signal_router
from macro_positioning.api.trade_plan_routes import router as trade_plan_router
from macro_positioning.dashboard.desk_routes import router as desk_router
from macro_positioning.dashboard.router import router as dashboard_router
from macro_positioning.integration.endpoints import router as integration_router
from macro_positioning.db.repository import SQLiteRepository
from macro_positioning.db.schema import initialize_database
from macro_positioning.ingestion.source_registry import load_source_registry
from macro_positioning.pipelines.run_pipeline import build_pipeline
from macro_positioning.services.framework import default_credential_requirements, onboarding_template

import logging

_log = logging.getLogger(__name__)

app = FastAPI(title="Macro Positioning Analyzer", version="0.1.0")

# CORS — allow the local review tooling (verify.html) and the IDE preview
# panel to call the API cross-origin during dev. No credentials are sent, so
# a wildcard origin is safe here; the deployed instance is gated by the
# bearer-auth middleware below regardless.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Bearer auth — only enforced when MPA_AUTH_TOKEN is set (i.e. on deployed
# instances). Local dev leaves the env var unset and the middleware no-ops.
# Static SPA assets and the health endpoint stay public so the browser can
# fetch index.html and the API can be probed by Render's health check.
# ---------------------------------------------------------------------------

_PUBLIC_PREFIXES = ("/web/", "/health", "/login")


@app.middleware("http")
async def _bearer_auth(request: Request, call_next):
    token = settings.auth_token
    if not token:
        return await call_next(request)

    path = request.url.path
    if path == "/" or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    cookie = request.cookies.get("mpa_token", "")
    expected = f"Bearer {token}"
    if header != expected and cookie != token:
        return JSONResponse(
            {"detail": "unauthorized"},
            status_code=401,
        )
    return await call_next(request)


# Disable browser caching of SPA source files so reloads always pick up
# fresh JSX/JS during development. Babel-standalone transforms .jsx in
# the browser; without these headers, the cached transform survives even
# `Cmd+Shift+R` and edits silently don't appear.
@app.middleware("http")
async def _no_cache_spa_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/web/") and (
        path.endswith(".jsx")
        or path.endswith(".js")
        or path.endswith(".html")
        or path.endswith(".css")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Desk routes (dynamic /web/data.js + /api/desk/data) MUST register
# BEFORE the StaticFiles mount so they take precedence over the static
# data.mock.js fallback.
app.include_router(desk_router)
app.include_router(dashboard_router)
app.include_router(integration_router)
app.include_router(manual_input_router)
app.include_router(funnel_router)
app.include_router(journal_router)
app.include_router(rules_router)
app.include_router(trade_plan_router)
app.include_router(insiders_router)
app.include_router(signal_router)
app.include_router(paper_router)
app.include_router(paper_cohort_router)
app.include_router(stock_unlocked_router)

def _init_schema_resiliently(attempts: int = 3, wait_seconds: float = 5.0) -> None:
    """Apply schema migrations at boot without letting a writer lock kill the API.

    The DB has three other writers, and `alert_watch.py` holds a write
    transaction through a long price pass every hour. `initialize_database`
    takes a write lock (it re-runs the documents dedupe), so a boot that
    lands inside that window used to raise `database is locked` at import
    time and crash-loop the launchd job — the SPA went down because an
    unrelated cron was busy.

    WAL keeps readers working throughout, so the right behaviour is to
    retry a few times and then serve anyway. A migration that could not be
    applied is logged loudly; the next boot (or the next tick script) will
    apply it.
    """
    import sqlite3
    import time

    for attempt in range(1, attempts + 1):
        try:
            initialize_database(settings.sqlite_path)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts:
                _log.error(
                    "schema init failed (%s). Serving reads anyway — re-run "
                    "initialize_database() once the writer clears.", exc,
                )
                return
            _log.warning(
                "schema init blocked by another writer (attempt %d/%d); retrying in %.0fs",
                attempt, attempts, wait_seconds,
            )
            time.sleep(wait_seconds)


_init_schema_resiliently()
repository = SQLiteRepository(settings.sqlite_path)


# ---------------------------------------------------------------------------
# Static SPA mount (Claude Design output)
# ---------------------------------------------------------------------------
# Serves web/index.html, *.jsx, styles.css, etc. Dynamic /web/data.js is
# handled by desk_router above and shadows the static fallback at
# web/data.mock.js. SPA reads `window.MA_DATA` on first paint.
_WEB_DIR = settings.base_dir / "web"


# Dynamic index.html with mtime-based cache-bust on every <script src=*.jsx>.
# This MUST register before the StaticFiles mount so it shadows the file at
# web/index.html. Each JSX URL gets `?v={file_mtime}` appended, so the
# browser is forced to re-fetch as soon as the source file changes — no
# manual `?v=N` bumping in the HTML, and no Cmd+Shift+R needed.
import re as _re
from fastapi.responses import HTMLResponse as _HTMLResponse

# Cache-bust both <script src="*.jsx"> AND <link href="*.css"> with the
# file's mtime. Without the CSS rule the browser will happily serve a
# stale stylesheet for hours after a rule change.
_SCRIPT_SRC_RE = _re.compile(r'(<script[^>]*\bsrc=")([^"?]+\.jsx)(\?[^"]*)?(")')
_LINK_HREF_RE = _re.compile(r'(<link[^>]*\bhref=")([^"?]+\.css)(\?[^"]*)?(")')


@app.get("/web/index.html", include_in_schema=False)
@app.get("/web/", include_in_schema=False)
def _spa_index() -> _HTMLResponse:
    index_path = _WEB_DIR / "index.html"
    raw = index_path.read_text(encoding="utf-8")

    def _bust(match: "_re.Match[str]") -> str:
        prefix, src, _existing_q, suffix = match.groups()
        asset_path = _WEB_DIR / src
        try:
            mtime = int(asset_path.stat().st_mtime)
        except OSError:
            mtime = 0
        return f"{prefix}{src}?v={mtime}{suffix}"

    rewritten = _SCRIPT_SRC_RE.sub(_bust, raw)
    rewritten = _LINK_HREF_RE.sub(_bust, rewritten)
    return _HTMLResponse(
        rewritten,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if _WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=_WEB_DIR, html=True), name="web")

# Manual-input chart attachments live under uploads/charts/YYYY-MM/.
# The SPA's I3 ticker drill-down renders thumbnails from these URLs.
_UPLOADS_DIR = settings.base_dir / "uploads"
if _UPLOADS_DIR.is_dir():
    app.mount("/uploads", StaticFiles(directory=_UPLOADS_DIR), name="uploads")

# The baseline_seed (trading_agent archive) is served under a different
# path. Surfaces the 223 historical charts in the SPA when their rows
# are drilled into.
_MANUAL_ENTRY_DIR = settings.base_dir / "manual_entry"
if _MANUAL_ENTRY_DIR.is_dir():
    app.mount("/manual_entry", StaticFiles(directory=_MANUAL_ENTRY_DIR), name="manual_entry")


# Convenience root → SPA. Old per-view routes (/positioning, /dev, etc)
# are 307-redirected here too via dashboard/router.py.
@app.get("/desk")
def desk_root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/web/index.html", status_code=307)


# One-shot cache nuke. Hit /reset to clear all browser caches, cookies,
# storage, and any service workers for this origin — then redirect to /
# so the SPA reloads cleanly. Useful when stale JSX gets stuck in the
# browser despite no-cache headers (Chrome's disk cache can be sticky).
@app.get("/reset", include_in_schema=False)
def reset_browser_cache() -> RedirectResponse:
    response = RedirectResponse(url="/web/index.html", status_code=303)
    # Clear-Site-Data is a W3C header Chrome / Edge / Firefox honor —
    # asks the browser to wipe everything for this origin in one shot.
    response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pipeline/run", response_model=PipelineRunResult)
def run_pipeline(request: PipelineRunRequest) -> PipelineRunResult:
    pipeline = build_pipeline()
    return pipeline.run(request.documents, context=request.context)


@app.get("/theses", response_model=list[Thesis])
def list_theses() -> list[Thesis]:
    return repository.list_theses()


@app.get("/memos/latest", response_model=PositioningMemo)
def latest_memo() -> PositioningMemo:
    memo = repository.latest_memo()
    if memo is None:
        raise HTTPException(status_code=404, detail="No memo has been generated yet.")
    return memo


@app.get("/framework/credentials")
def framework_credentials() -> list[dict]:
    return [item.model_dump() for item in default_credential_requirements()]


@app.get("/framework/onboarding-template", response_model=list[SourceOnboardingRequest])
def framework_onboarding_template() -> list[SourceOnboardingRequest]:
    return onboarding_template()


@app.get("/sources/example")
def example_sources() -> list[dict]:
    path = Path("config/sources.example.json")
    return [item.model_dump() for item in load_source_registry(path)]


# ---------------------------------------------------------------------------
# Chart analysis
# ---------------------------------------------------------------------------

class ChartAnalysisRequest(BaseModel):
    image_url: str
    asset_context: str = ""
    additional_context: str = ""


class BatchChartRequest(BaseModel):
    charts: list[dict] = Field(..., description="List of {url, asset_context} dicts")


@app.post("/charts/analyze")
def analyze_chart(request: ChartAnalysisRequest) -> dict:
    from macro_positioning.brain import build_brain_client
    brain = build_brain_client()
    try:
        return brain.analyze_chart(
            image_url=request.image_url,
            asset_context=request.asset_context,
            additional_context=request.additional_context,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/charts/analyze/batch")
def analyze_charts_batch(request: BatchChartRequest) -> list[dict]:
    from macro_positioning.brain.vision import analyze_multiple_charts
    try:
        return analyze_multiple_charts(request.charts)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
