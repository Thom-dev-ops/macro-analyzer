"""Multi-backend LLM adapters for the Brain.

Each backend implements the same `generate()` interface:
  input: system_prompt, user_prompt, [image data], model-specific kwargs
  output: raw text response

Routing is decided by the BrainClient based on settings. Adding a new
backend = add a new adapter class + register it in BACKENDS.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

from macro_positioning.core.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class BackendResult:
    def __init__(
        self,
        text: str,
        model: str,
        latency_ms: float,
        raw: Any = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ):
        self.text = text
        self.model = model
        self.latency_ms = latency_ms
        self.raw = raw
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd


# ─── Pricing table — USD per 1M tokens ──────────────────────────────────────
# Lightweight reference; update when contracts change. None means "unknown
# pricing" → cost_usd stays None rather than reporting wrong numbers.
# Format: {model_substring: (input_per_1m, output_per_1m)}
_PRICING: dict[str, tuple[float, float]] = {
    # Gemini 2.5 family — public pay-as-you-go pricing.
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    # Claude — Anthropic public pricing.
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-haiku-4": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
}


def _estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Best-effort cost estimate. Returns None if pricing unknown or
    token counts missing — callers shouldn't silently report $0 when
    the truth is "unknown."
    """
    if input_tokens is None and output_tokens is None:
        return None
    model_lc = (model or "").lower()
    pricing = None
    for key, p in _PRICING.items():
        if key in model_lc:
            pricing = p
            break
    if pricing is None:
        return None
    in_price, out_price = pricing
    cost = 0.0
    if input_tokens is not None:
        cost += (input_tokens / 1_000_000.0) * in_price
    if output_tokens is not None:
        cost += (output_tokens / 1_000_000.0) * out_price
    return round(cost, 6)


class BackendUnavailable(RuntimeError):
    """Raised when a backend's credentials are missing or API fails."""


# ---------------------------------------------------------------------------
# Gemini (Google GenAI SDK — direct, no N8N)
# ---------------------------------------------------------------------------

def generate_gemini(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    image_data: bytes | None = None,
    image_mime: str = "image/png",
    image_url: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    json_mode: bool = True,
) -> BackendResult:
    """Call Gemini directly via google-genai SDK."""
    if not settings.gemini_api_key:
        raise BackendUnavailable("Gemini API key not configured (MPA_GEMINI_API_KEY)")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    model_name = model or settings.gemini_model

    # Build content parts — text + optional image
    parts: list[Any] = [user_prompt]
    if image_data:
        parts.insert(0, types.Part.from_bytes(data=image_data, mime_type=image_mime))
    elif image_url:
        # Gemini can accept a URL in Part.from_uri, but easiest is to fetch
        # and inline for portability; small image scenarios
        parts.insert(0, types.Part.from_uri(file_uri=image_url, mime_type=image_mime))

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
        response_mime_type="application/json" if json_mode else "text/plain",
    )

    t0 = time.time()
    response = client.models.generate_content(
        model=model_name,
        contents=parts,
        config=config,
    )
    latency = (time.time() - t0) * 1000

    text = response.text or ""
    # Extract token usage if available. google-genai exposes it on
    # response.usage_metadata (prompt_token_count / candidates_token_count).
    in_toks = out_toks = None
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        in_toks = getattr(usage, "prompt_token_count", None)
        out_toks = getattr(usage, "candidates_token_count", None)
    cost = _estimate_cost(model_name, in_toks, out_toks)
    logger.info("Gemini %s responded in %.0fms (%d chars, %s in, %s out)",
                model_name, latency, len(text), in_toks, out_toks)
    return BackendResult(
        text=text, model=model_name, latency_ms=latency, raw=response,
        input_tokens=in_toks, output_tokens=out_toks, cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# Anthropic Claude (direct SDK)
# ---------------------------------------------------------------------------

def generate_anthropic(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    image_data: bytes | None = None,
    image_mime: str = "image/png",
    image_url: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    json_mode: bool = True,  # Claude doesn't have a JSON mode, handled via prompt
) -> BackendResult:
    """Call Claude directly via anthropic SDK."""
    if not settings.anthropic_api_key:
        raise BackendUnavailable("Anthropic API key not configured (MPA_ANTHROPIC_API_KEY)")

    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    model_name = model or settings.claude_model

    content: list[dict] = []
    if image_data:
        b64 = base64.standard_b64encode(image_data).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_mime, "data": b64},
        })
    elif image_url:
        content.append({"type": "image", "source": {"type": "url", "url": image_url}})

    content.append({"type": "text", "text": user_prompt})

    # Prompt caching for system prompt (high reuse across calls)
    system_blocks = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]

    # `temperature` (and other sampling params) are rejected on the 4.6+ family
    # and every 5.x model — they return 400 "temperature is deprecated". Only
    # send it for older models that still accept it.
    _sampling_removed = (
        "claude-sonnet-5" in model_name
        or "claude-opus-5" in model_name
        or "claude-fable-5" in model_name
        or "claude-mythos-5" in model_name
        or "claude-opus-4-6" in model_name
        or "claude-opus-4-7" in model_name
        or "claude-opus-4-8" in model_name
        or "claude-sonnet-4-6" in model_name
    )
    kwargs: dict = {
        "model": model_name,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": content}],
    }
    if not _sampling_removed:
        kwargs["temperature"] = temperature

    t0 = time.time()
    response = client.messages.create(**kwargs)
    latency = (time.time() - t0) * 1000

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    # Anthropic SDK exposes usage on response.usage (input_tokens / output_tokens).
    in_toks = out_toks = None
    usage = getattr(response, "usage", None)
    if usage is not None:
        in_toks = getattr(usage, "input_tokens", None)
        out_toks = getattr(usage, "output_tokens", None)
    cost = _estimate_cost(model_name, in_toks, out_toks)
    logger.info("Claude %s responded in %.0fms (%d chars, %s in, %s out)",
                model_name, latency, len(text), in_toks, out_toks)
    return BackendResult(
        text=text, model=model_name, latency_ms=latency, raw=response,
        input_tokens=in_toks, output_tokens=out_toks, cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# Ollama (local fallback / dev)
# ---------------------------------------------------------------------------

def generate_ollama(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    image_data: bytes | None = None,
    image_mime: str = "image/png",
    image_url: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    json_mode: bool = True,
) -> BackendResult:
    """Call a local Ollama instance. Used for dev/testing."""
    import httpx

    model_name = model or settings.ollama_model
    url = f"{settings.ollama_base_url}/api/chat"

    payload = {
        "model": model_name,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if json_mode:
        payload["format"] = "json"

    if image_data:
        payload["messages"][-1]["images"] = [base64.b64encode(image_data).decode("utf-8")]

    t0 = time.time()
    try:
        with httpx.Client(timeout=300.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as e:
        raise BackendUnavailable(f"Ollama unreachable at {url}: {e}")

    latency = (time.time() - t0) * 1000
    data = response.json()
    text = data.get("message", {}).get("content", "")

    # Ollama returns eval_count + prompt_eval_count when streaming=false.
    in_toks = data.get("prompt_eval_count")
    out_toks = data.get("eval_count")
    logger.info("Ollama %s responded in %.0fms (%d chars, %s in, %s out)",
                model_name, latency, len(text), in_toks, out_toks)
    return BackendResult(
        text=text, model=model_name, latency_ms=latency, raw=data,
        input_tokens=in_toks, output_tokens=out_toks,
        cost_usd=0.0,  # local model — actual cost is zero
    )


# ---------------------------------------------------------------------------
# Legacy N8N (kept for completeness, not recommended as primary)
# ---------------------------------------------------------------------------

def generate_n8n(
    system_prompt: str,
    user_prompt: str,
    *,
    webhook_url: str = "",
    image_url: str | None = None,
    **_: Any,
) -> BackendResult:
    """Call an N8N webhook that proxies to whatever model it wraps.

    Kept for advanced use cases. Not the default path.
    """
    import httpx

    url = webhook_url or settings.n8n_webhook_url
    if not url:
        raise BackendUnavailable("N8N webhook URL not configured")

    payload = {"system_prompt": system_prompt, "prompt": user_prompt}
    if image_url:
        payload["image_url"] = image_url

    t0 = time.time()
    with httpx.Client(timeout=180.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
    latency = (time.time() - t0) * 1000

    data = response.json()
    if isinstance(data, dict):
        text = data.get("text") or data.get("output") or data.get("content") or str(data)
    else:
        text = str(data)
    return BackendResult(text=text, model="n8n-proxy", latency_ms=latency, raw=data)


# ---------------------------------------------------------------------------
# Registry + dispatch
# ---------------------------------------------------------------------------

BACKENDS = {
    "gemini": generate_gemini,
    "anthropic": generate_anthropic,
    "claude": generate_anthropic,  # alias
    "ollama": generate_ollama,
    "n8n": generate_n8n,
}


def generate(backend: str, system_prompt: str, user_prompt: str, **kwargs) -> BackendResult:
    """Dispatch to the requested backend with graceful fallback."""
    fn = BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"Unknown backend: {backend}. Available: {list(BACKENDS)}")

    try:
        return fn(system_prompt, user_prompt, **kwargs)
    except BackendUnavailable:
        # If primary unavailable, try alternative configured backends
        for alt in ("gemini", "anthropic", "ollama"):
            if alt == backend:
                continue
            if alt == "gemini" and not settings.gemini_api_key:
                continue
            if alt == "anthropic" and not settings.anthropic_api_key:
                continue
            logger.warning("Backend %s unavailable, falling back to %s", backend, alt)
            return BACKENDS[alt](system_prompt, user_prompt, **kwargs)
        raise


def load_image_bytes(file_path: str | Path) -> tuple[bytes, str]:
    """Helper: read an image file, return (bytes, mime_type)."""
    path = Path(file_path)
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    mime = mime_map.get(path.suffix.lower(), "image/png")
    return path.read_bytes(), mime
