"""Paper trading execution — a simulated book that trades the desk's own signals.

    vocabulary — Action / Intent / Blocker / Band: the words the book speaks
    models     — Mandate, Position, Order, Decision, Portfolio, BookState
    rank       — percentile-rank each scored name, 0-100, against the live
                 score distribution, adjusted by signals / R:R / momentum
    store      — SQLite persistence over the five `paper_*` tables
    engine     — the tick: mark → exit → rank → rotate → fill → snapshot

The book is deliberately separate from `trades` / `trade_plans`, which are
the hand-traded funnel. Nothing here writes to those tables.
"""

from macro_positioning.paper.models import Mandate, load_mandate
from macro_positioning.paper.vocabulary import Action, Band, Blocker, Intent

__all__ = ["Mandate", "load_mandate", "Action", "Band", "Blocker", "Intent"]
