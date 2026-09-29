from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Guard against a footgun that once wiped the live DB: scripts intending
# to override settings with `MACRO_POSITIONING_SQLITE_PATH=...` were
# silently ignored (real prefix is `MPA_`), so the smoke DB fell back to
# the production path. Any env var with the wrong-but-plausible prefix
# now halts import — loudly — rather than silently doing the wrong thing.
_FORBIDDEN_ENV_PREFIXES = ("MACRO_POSITIONING_",)
_stray = [k for k in os.environ if k.startswith(_FORBIDDEN_ENV_PREFIXES)]
if _stray:
    raise RuntimeError(
        "Refusing to load settings: env vars "
        f"{sorted(_stray)!r} use a prefix that is silently ignored "
        "(real prefix is 'MPA_'). Rename them, e.g. "
        "MACRO_POSITIONING_SQLITE_PATH → MPA_DATABASE_URL='sqlite:///...'."
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MPA_", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///data/macro_positioning.db"
    default_horizon: str = "2-12 weeks"
    enable_heuristic_extractor: bool = True
    base_dir: Path = Field(default_factory=lambda: Path.cwd())

    fred_api_key: str = ""
    finnhub_api_key: str = ""

    # Bearer auth for the public deployed API. Empty in dev = auth disabled.
    # Set in Render dashboard before exposing the URL. SPA same-origin
    # requests pick it up from a cookie set at /login (separate scaffolding).
    auth_token: str = ""

    # Telegram user-account API (Telethon). Used to read PE Deal Flow
    # chat messages directly — no screenshotting, no OCR. api_id +
    # api_hash come from https://my.telegram.org → API development tools.
    # Session file is a credentials artefact (gitignored).
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_session_path: Path = Field(
        default_factory=lambda: Path.cwd() / "data" / "telegram.session"
    )

    # Telegram channels + DMs the poller ingests from. Keys are stable
    # slugs used on the CLI (--channel feather_hands_trading). chat_id
    # is the numeric identifier from Telegram. author_display matches
    # one of the seeded entries in SEED_AUTHORS — that's how messages
    # get attributed downstream.
    #
    # is_dm=True for direct-message sources (Ari Gold). The poller
    # filters DMs to messages FROM the other party (skips the user's
    # own replies so they don't end up looking like trade calls).
    #
    # Stored in code (not env) because chat_ids are stable identifiers,
    # not secrets. Add new channels here as needed.
    telegram_channels: dict[str, dict] = Field(default_factory=lambda: {
        "feather_hands_trading": {
            "chat_id": -1001309918571, "author_display": "Feather Hands Trading", "is_dm": False,
            # Known authors inside this group. Keyed by Telegram user_id (int).
            # Run scripts/telegram_smoke_test.py to discover user_ids.
            "known_senders": {
                # "123456789": "MadDog31",
                # "987654321": "Big_Nuts",
            },
        },
        "gem_hunters": {
            "chat_id": -1002332468588, "author_display": "Gem Hunters 💎", "is_dm": False,
            "known_senders": {
                # "123456789": "joejoe55",  # populate after smoke test
            },
        },
        "og_whales": {
            "chat_id": -1001548106275, "author_display": "🐳 OG Whales 🐳", "is_dm": False,
            "known_senders": {
                # "123456789": "Big_Nuts",
            },
        },
        "the_wolf_pack": {
            "chat_id": -1002616207282, "author_display": "The Wolf Pack", "is_dm": False,
            "known_senders": {
                # "123456789": "Mark Wood",
                # "987654321": "MixinCrypto",
            },
        },
        "ari_gold": {
            "chat_id": 1073329886, "author_display": "Ari Gold", "is_dm": True,
            "known_senders": {},
        },
        "trading_operation_desk": {
            "chat_id": -1003855403507, "author_display": "Trading Operation Desk", "is_dm": False,
            "known_senders": {},
        },
        # New 2026-08-25 — the user's Stock Unlocked *trades* channel
        # (equities, not crypto). author_display is "Stock Unlocked" and
        # NOT the channel title, so posts land on the already-seeded
        # `stock-unlocked:stock-unlocked` author (trust_weight 1.5) rather
        # than minting a fresh NULL-trust row the conviction allowlist
        # would ignore. Artur Unlocked is seeded separately — add his
        # user_id to known_senders once a smoke test surfaces it.
        "stock_unlocked": {
            "chat_id": -1002752726473, "author_display": "Stock Unlocked", "is_dm": False,
            "known_senders": {
                # "123456789": "Artur Unlocked",  # discover via telegram_smoke_test
            },
        },
    })

    # ─── Alerts ──────────────────────────────────────────────────────
    # Outbound notification for the scoring layer. The tracker knew ETH
    # was tier_1 on 2026-08-17 and BTC on the 20th; nothing told anyone.
    #
    # Delivery is a Telegram *bot* (not the Telethon user session): the
    # listener holds an exclusive lock on data/telegram.session, so a
    # second process cannot reuse it. Create the bot with @BotFather,
    # then message it once so it can reply to you, and set:
    #   MPA_TELEGRAM_BOT_TOKEN=123456:ABC-...
    #   MPA_TELEGRAM_ALERT_CHAT_ID=<your numeric chat id>
    # `scripts/alert_watch.py --whoami` resolves the chat id for you.
    #
    # Unset = alerts are still computed and recorded in the `alerts`
    # table, just undelivered; the next cycle retries them once the
    # token lands, so nothing is lost by configuring this late.
    telegram_bot_token: str = ""
    telegram_alert_chat_id: str = ""

    # Position-size tiers that never warrant an interrupt. "avoid" is the
    # composer's own verdict that a name is not tradeable right now, so a
    # zone touch on one is a fact about a chart you have already declined.
    # 2026-08-26 delivered two of these before breakfast (XLP 46, USO 48).
    # They are still scored, still on the watchlist digest, still in the
    # DB — just not worth a buzz.
    alert_suppress_tiers: list[str] = ["avoid"]

    # Cooldown per (ticker, rule): suppress a repeat of the same alert
    # inside this window. Grade crosses are transitions and can't repeat
    # without first dropping out of the band, but a score oscillating on
    # the A/B boundary would otherwise nag twice a day.
    alert_cooldown_hours: int = 48

    # score_jump rule: absolute change between consecutive scores that is
    # worth an interrupt on its own, before any grade band is crossed.
    alert_score_jump: int = 15

    # ...but only when it lands somewhere that could become a trade. An
    # August 2026 replay fired "QQQ jumped +18 to 54" and "TLT +20 to 58";
    # a big move into the D band is a statistic, not a setup, and it was
    # two thirds of all score_jump traffic. 75 = within striking distance
    # of the A band at 80.
    alert_score_jump_min_score: int = 75

    # Alerts derived in one cycle are delivered as a single message. A
    # regime modifier flip moves the whole board at once — 2026-08-20 saw
    # 14 alerts in one pass — and 14 pings for one cause is how a channel
    # gets muted. The digest lists every one of them; it only truncates
    # if the rendered message would exceed Telegram's 4096-char limit
    # (~70 alerts), so there is no line cap to configure.

    # A scoring pass that scored far fewer tickers than usual is a partial
    # or failed run (2026-08-21 had several: 57 rows, prices missing, ETH
    # briefly graded D). Comparing against one manufactures phantom
    # crosses, so passes below this fraction of the recent median are
    # ignored by the alert evaluator.
    alert_min_pass_completeness: float = 0.8

    # Re-attempt delivery for alerts fired inside this window that have no
    # successful channel yet. Covers transient network failures and the
    # first run after the bot token is configured.
    alert_redelivery_window_hours: int = 24

    # LLM Brain — direct APIs (multi-model)
    # Primary synthesis model
    gemini_api_key: str = ""           # Google Gemini direct API key
    gemini_model: str = "gemini-2.5-pro"

    # Escalation / alternative model
    anthropic_api_key: str = ""        # Claude API key
    claude_model: str = "claude-sonnet-4-5"

    # Manual-input chart vision. Default Sonnet 5 — 33% cheaper input/output
    # than Sonnet 4.6 ($2/$10 vs $3/$15 per 1M) at same-or-better vision quality.
    # Override per-call (e.g. MPA_VISION_MODEL=claude-opus-5 for high-conviction
    # reprocessing of past drops; Opus is now only ~2.5x Sonnet 5, not 5x).
    # Image preprocessing settings tame token cost on the multimodal call
    # without hurting chart legibility.
    vision_model: str = "claude-sonnet-5"
    vision_max_image_width: int = 1500       # downscale wider images before send
    vision_resize_target_width: int = 1024   # post-resize width
    vision_cache_enabled: bool = True        # hash-dedupe identical bytes
    # Backend: "cli" (default — uses Claude Code subscription via the
    # `claude -p` CLI, no API credits) or "api" (uses MPA_ANTHROPIC_API_KEY
    # with per-call billing). CLI is preferred when Claude Code is installed.
    vision_backend: str = "cli"
    vision_cli_path: str = "claude"          # falls back to PATH lookup
    vision_cli_max_turns: int = 4            # Read tool + answer typically needs 2
    vision_cli_timeout_s: int = 120

    # Routing defaults
    brain_primary_backend: str = "gemini"    # gemini | anthropic | ollama
    brain_vision_backend: str = "gemini"     # gemini | anthropic
    brain_escalation_backend: str = "anthropic"  # backup tier for high-stakes

    # Ollama local (optional, dev/testing)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b"

    # N8N webhooks → Vertex/Gemini (primary production path — unlimited via N8N)
    n8n_webhook_url: str = ""          # text synthesis: Webhook → Gemini text/message
    n8n_vision_webhook_url: str = ""   # vision: Webhook → Gemini image/analyze
    n8n_audio_webhook_url: str = ""    # audio transcription: Webhook → Gemini audio/transcribe

    # Tactical-executor integration
    tactical_webhook_url: str = ""      # PUSH direction: regime-change alerts → tactical
    tactical_executor_url: str = ""     # PULL direction: dashboard reads tactical state (events, decisions, lifecycle)

    # Personal Gmail (separate from any shared project Gmail credentials)
    personal_gmail_client_id: str = ""
    personal_gmail_client_secret: str = ""
    personal_gmail_refresh_token: str = ""
    personal_gmail_token_path: str = "data/personal_gmail_token.json"

    @property
    def sqlite_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Only sqlite URLs are supported in the current scaffold.")
        return self.base_dir / self.database_url.removeprefix(prefix)

    # ── Manual input layer ──────────────────────────────────────────────
    # Where chart screenshots and other manual attachments land. Created
    # lazily by the processor on first save.

    @property
    def upload_dir(self) -> Path:
        return self.base_dir / "uploads"

    @property
    def chart_upload_dir(self) -> Path:
        return self.upload_dir / "charts"


settings = Settings()
