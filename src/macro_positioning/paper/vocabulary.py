"""The words the paper book speaks.

Every fill, every refusal, and every do-nothing the engine produces is
tagged with terms from this module. That is the whole point: a machine
that trades an account and cannot say *why* is indistinguishable from a
random number generator with a good month.

Four axes:

    Action   — what happened to the position (OPEN / TRIM / EXIT / ...)
    Intent   — why it happened (STOP_HIT / MAKE_ROOM / CONVICTION_DECAY / ...)
    Blocker  — why an intended trade did NOT happen (CASH_FLOOR / BUCKET_CAP / ...)
    Band     — the rank rung a size was drawn from (STARTER ... ANCHOR)

Each term carries a `describe()` so the SPA, the launchd log, and the
order blotter all render the same English rather than each inventing its
own phrasing for `"conviction_decay"`. When a term needs numbers in the
sentence, the caller passes them to `describe(**ctx)` — the templates
here own the wording, the caller owns the facts.
"""

from __future__ import annotations

from enum import Enum


class _Described(str, Enum):
    """A str-Enum whose members carry a human template.

    Members are declared as `NAME = ("value", "template")`. The value is
    what lands in SQLite; the template is what a person reads.
    """

    def __new__(cls, value: str, template: str = ""):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.template = template
        return obj

    def describe(self, **ctx) -> str:
        """Render this term's sentence. Missing keys degrade to the raw
        template rather than raising — a log line is never worth a
        crash mid-tick."""
        try:
            return self.template.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return self.template

    def __str__(self) -> str:  # so f-strings emit the value, not "Action.OPEN"
        return self._value_


class Action(_Described):
    """What the engine did to the position."""

    OPEN = ("OPEN", "Opened {ticker} {side} at {weight_pct:.1f}% of the book")
    ADD = ("ADD", "Added to {ticker} — {from_pct:.1f}% → {weight_pct:.1f}% of the book")
    TRIM = ("TRIM", "Trimmed {ticker} — {from_pct:.1f}% → {weight_pct:.1f}% of the book")
    EXIT = ("EXIT", "Closed {ticker} {side} at {price}")
    HOLD = ("HOLD", "Held {ticker} unchanged")
    REJECT = ("REJECT", "Passed on {ticker}")

    @property
    def is_fill(self) -> bool:
        """Does this action move cash? HOLD and REJECT do not."""
        return self in (Action.OPEN, Action.ADD, Action.TRIM, Action.EXIT)

    @property
    def is_entry(self) -> bool:
        return self in (Action.OPEN, Action.ADD)


class Intent(_Described):
    """Why the action fired. This is the field a human actually reads."""

    # ── Entries ───────────────────────────────────────────────────────
    CLEARED_BAR = (
        "cleared_bar",
        "{ticker} cleared the entry bar at rank {rank:.0f} "
        "(score {score}, {band} size)",
    )
    RANK_UPGRADE = (
        "rank_upgrade",
        "{ticker} climbed to rank {rank:.0f} from {rank_prev:.0f} — "
        "topped up toward its {target_weight_pct:.1f}% target weight",
    )
    RECLAIMED_SETUP = (
        "reclaimed_setup",
        "{ticker} broke its stop at {stop} on {exit_date} but has reclaimed the "
        "{entry} level and trades at {price} — the setup is intact, back in at rank {rank:.0f}",
    )
    ROTATION_IN = (
        "rotation_in",
        "Rotated into {ticker} (rank {rank:.0f}) with capital freed from "
        "{funded_by}",
    )

    # ── Exits and trims ───────────────────────────────────────────────
    STOP_HIT = (
        "stop_hit",
        "{ticker} traded through its stop at {stop} — thesis invalidated, closed at {price}",
    )
    TARGET_REACHED = (
        "target_reached",
        "{ticker} reached its {source} target at {target} — the trade did what it was "
        "for, closed in full at {price}",
    )
    NEAR_MISS = (
        "near_miss",
        "{ticker} got {reach_pct:.0f}% of the way to {target} (high {high_water}) and turned "
        "— {giveback_r:.1f}R back off the high without crossing it. The target was the "
        "idea, not the tick; closed at {price}",
    )
    RUNG_TAKEN = (
        "rung_taken",
        "{ticker} at {r:.1f}R — took {take_pct:.0f}% off, stop to {stop_to}",
    )
    TRAIL_GIVEBACK = (
        "trail_giveback",
        "{ticker} gave back {giveback_pct:.0f}% of its open profit from {high_water} — "
        "runner closed",
    )
    TRAIL_ROUND_TRIP = (
        "trail_round_trip",
        "{ticker} gave back everything it had made from {high_water} and is now "
        "underwater — closed rather than held for a round trip",
    )
    RANK_DECAY = (
        "rank_decay",
        "{ticker} slipped to rank {rank:.0f} from {rank_prev:.0f} at entry — "
        "below the {exit_floor:.0f} exit bar",
    )
    SIDE_FLIP = (
        "side_flip",
        "{ticker} flipped {side_prev} → {side} — the book was on the wrong side, closed",
    )
    SUPPORT_COLLAPSED = (
        "support_collapsed",
        "{ticker}'s case fell to {support:.0f}/100 from {support_prev:.0f} — {why}",
    )
    STALE_THESIS = (
        "stale_thesis",
        "{ticker} has had no scoring pass in {age_days} days — closed rather than "
        "drifting on a dead read",
    )
    MAKE_ROOM = (
        "make_room",
        "Trimmed {ticker} (rank {rank:.0f}) to fund {rotating_into} (rank {rival_rank:.0f}) — "
        "a materially stronger read",
    )
    CASH_FLOOR_BREACH = (
        "cash_floor_breach",
        "Marks pushed the book to {deployed_pct:.1f}% deployed — trimmed {ticker} "
        "back under the {max_deployed_pct:.0f}% ceiling",
    )
    DRIFT_REBALANCE = (
        "drift_rebalance",
        "{ticker} drifted to {weight_pct:.1f}% against a {target_weight_pct:.1f}% target — "
        "sized back",
    )
    MANUAL_OVERRIDE = (
        "manual_override",
        "{ticker} closed by hand from the desk — engine decision overridden",
    )
    BOOK_RETIRED = (
        "book_retired",
        "{ticker} closed at {price} — its book was retired and would never tick "
        "again, so the position was flattened rather than left unmanaged",
    )

    # ── Non-events ────────────────────────────────────────────────────
    NO_CHANGE = (
        "no_change",
        "{ticker} still ranks {rank:.0f} and sits at its target weight — nothing to do",
    )
    MANDATE_BLOCK = (
        "mandate_block",
        "{ticker} wanted {target_weight_pct:.1f}% but the mandate said no",
    )


class Blocker(_Described):
    """Why a trade the engine *wanted* to make did not happen.

    These rows are the reason the book is legible. Without them a tick
    that skipped a 92-score name looks like a bug; with them it reads as
    "the cash floor said no, and nothing was weak enough to sell."
    """

    CASH_FLOOR = (
        "cash_floor",
        "Book was {deployed_pct:.1f}% deployed against a {max_deployed_pct:.0f}% ceiling — "
        "the 30% cash reserve is a hard floor",
    )
    NO_ROTATION_EDGE = (
        "no_rotation_edge",
        "No room, and the weakest holding ({weakest}, rank {weakest_rank:.0f}) is not "
        "{rotation_edge:.0f} points below {ticker} at rank {rank:.0f} — not worth the churn",
    )
    MAX_POSITIONS = (
        "max_positions",
        "Book already holds {open_positions} names against a {max_positions} cap",
    )
    BUCKET_CAP = (
        "bucket_cap",
        "{bucket_label} exposure is {bucket_pct:.1f}% against a {max_bucket_pct:.0f}% cap — "
        "{ticker} would be stacking the same bet",
    )
    BUCKET_COUNT = (
        "bucket_count",
        "Already holding {bucket_count} names in {bucket_label} against a "
        "{max_per_bucket} cap",
    )
    BELOW_ENTRY_FLOOR = (
        "below_entry_floor",
        "{ticker} ranks {rank:.0f}, under the {entry_floor:.0f} entry bar",
    )
    NO_PRICE = (
        "no_price",
        "No usable price for {ticker} from any provider — cannot fill what cannot be marked",
    )
    NO_LEVELS = (
        "no_levels",
        "{ticker} has no stop from the technical agent ({reason}) — the book does not "
        "size a position it cannot invalidate",
    )
    UNSUPPORTED_TARGET = (
        "unsupported_target",
        "{ticker}'s target scores {support:.0f}/100 on the composed view — {why}",
    )
    STOP_TOO_TIGHT = (
        "stop_too_tight",
        "{ticker}'s stop is {risk_pct:.2f}% from price — under the {floor_pct:.2f}% floor. "
        "On a twice-daily tick a stop that close is a coin flip that pays out in "
        "multiples of the planned risk, not a stop",
    )
    STOP_TOO_WIDE = (
        "stop_too_wide",
        "{ticker}'s stop is {risk_pct:.1f}% from price — over the {cap_pct:.0f}% hard cap "
        "on a spot position",
    )
    LEVELS_STALE = (
        "levels_stale",
        "{ticker} is trading at {price} but its {rail} sits at {level} — the tape has "
        "moved through the level set since it was drawn, so the trade is already over "
        "before it starts",
    )
    NOT_DIRECTIONAL = (
        "not_directional",
        "{ticker} reads {side}, not a tradeable direction",
    )
    PROFIT_TAKEN = (
        "profit_taken",
        "{ticker} has already taken {rungs} rung(s) of profit — a position the ladder "
        "reduced on purpose is not a gap to fill",
    )
    ALREADY_AT_TARGET = (
        "already_at_target",
        "{ticker} already sits at {weight_pct:.1f}% against a {target_weight_pct:.1f}% target",
    )
    MIN_TICKET = (
        "min_ticket",
        "The gap on {ticker} is ${notional:,.0f}, under the ${min_ticket:,.0f} minimum — "
        "not worth the fill",
    )
    COOLING_OFF = (
        "cooling_off",
        "{ticker} was stopped out at {exit_price} on {exit_date} and has not reclaimed "
        "the {entry_price} level it was bought at — trading at {price}, it would need "
        "{reclaim_at} to be the same setup again",
    )
    CHOPPING = (
        "chopping",
        "{ticker} has been stopped out {exit_count} times in {window_days} days — "
        "reclaim or not, the book stops paying the spread to find out",
    )
    JUST_ACTED = (
        "just_acted",
        "{ticker} was already acted on this tick ({acted_action}) — one decision per "
        "position per pass",
    )
    DRY_RUN = ("dry_run", "Dry run — decision computed but nothing was written")


class Band(_Described):
    """The rank rung a position size was drawn from.

    Purely descriptive — the actual weight is interpolated continuously
    across the 1–5% band. The rung is what makes a book scannable:
    "two anchors, four cores, the rest starters" reads faster than
    twelve decimals.
    """

    NONE = ("none", "below the entry bar — no position")
    STARTER = ("starter", "starter size — clears the bar, but only just")
    CORE = ("core", "core size — a normal, well-supported position")
    HIGH = ("high", "high rank — score and signals agree")
    ANCHOR = ("anchor", "anchor size — the strongest read the stack produces")

    @classmethod
    def for_rank(cls, rank: float, *, entry_floor: float) -> "Band":
        """Rungs are placed across the tradeable range, not across 0–100:
        the bar can move, and a fixed rung at 45 would put every position
        in the same band the moment the bar rose above it."""
        if rank < entry_floor:
            return cls.NONE
        span = max(1e-9, 100.0 - entry_floor)
        frac = (rank - entry_floor) / span
        if frac < 0.25:
            return cls.STARTER
        if frac < 0.55:
            return cls.CORE
        if frac < 0.80:
            return cls.HIGH
        return cls.ANCHOR


# Sides the book will actually take a position in. WATCH / AVOID are
# reads, not trades — desk_data emits them for tier_3 and avoid rows.
TRADEABLE_SIDES = frozenset({"LONG", "SHORT"})


def opposite(side: str) -> str:
    return "SHORT" if (side or "").upper() == "LONG" else "LONG"


__all__ = ["Action", "Intent", "Blocker", "Band", "TRADEABLE_SIDES", "opposite"]
