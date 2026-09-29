"""Market Traders Brief — multi-issue HTML generator.

Renders dated Big_Nuts-relay briefs from the manual:telegram-channel:ari_gold
docs. Each issue is a self-contained <div class="issue"> block with its own
tabs and pages. An issue picker at the top switches between them.

Output: briefs/market_traders_brief_<YYYY-MM-DD>.html (dated to the latest
batch included).

Usage: python3 scripts/build_brief.py
"""
from __future__ import annotations
import base64
import json
import sqlite3
from pathlib import Path
from html import escape

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "macro_positioning.db"
OUT_DIR = REPO / "briefs"
OUT = OUT_DIR / "market_traders_brief_2026-09-21.html"


# ---- Data ------------------------------------------------------------
def load_rows(day_from: str, day_to: str) -> list[dict]:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT document_id, published_at, author, content_type,
               attachment_paths_json, extracted_features_json, cleaned_text
        FROM documents
        WHERE source_id='manual:telegram-channel:ari_gold'
          AND published_at >= ? AND published_at < ?
        ORDER BY published_at, document_id
        """,
        (day_from, day_to),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        feat = json.loads(r["extracted_features_json"]) if r["extracted_features_json"] else {}
        attach = json.loads(r["attachment_paths_json"]) if r["attachment_paths_json"] else []
        out.append({
            "id": r["document_id"], "t": r["published_at"], "author": r["author"],
            "kind": r["content_type"], "attach": attach, "feat": feat,
            "text": (r["cleaned_text"] or "").strip(),
        })
    return out


def data_uri(path_rel: str, max_width: int = 900, quality: int = 78) -> str | None:
    """Return a base64 data URI, downscaled to keep the artifact under 16 MB."""
    p = REPO / path_rel
    if not p.exists():
        return None
    try:
        from PIL import Image
        import io
        img = Image.open(p)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
    except Exception:
        ext = p.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext, "jpeg")
        return f"data:image/{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


# ---- Card rendering --------------------------------------------------
def num(x) -> str:
    if x is None: return "—"
    if isinstance(x, (int, float)):
        if abs(x) >= 1000: return f"{x:,.0f}"
        if abs(x) >= 10:   return f"{x:,.2f}"
        return f"{x:g}"
    return escape(str(x))


def setup_row(setup: dict) -> str:
    dir_ = (setup.get("direction") or "").lower()
    dir_pill = f'<span class="dir dir-{dir_}">{escape(dir_.upper()) or "—"}</span>' if dir_ else ""
    cells = [
        f'<span class="lvl"><span class="k">entry</span><span class="v">{num(setup.get("entry"))}</span></span>',
        f'<span class="lvl"><span class="k">stop</span><span class="v">{num(setup.get("stop_loss"))}</span></span>',
    ]
    tps = [t for t in (setup.get("take_profits") or []) if t is not None]
    if tps:
        cells.append(f'<span class="lvl"><span class="k">tp</span><span class="v">{" · ".join(num(t) for t in tps)}</span></span>')
    if (ft := setup.get("final_target")) is not None:
        cells.append(f'<span class="lvl tgt"><span class="k">target</span><span class="v">{num(ft)}</span></span>')
    row = f'<div class="setup-row">{dir_pill}<div class="lvls">{"".join(cells)}</div></div>'
    if (inv := setup.get("invalidation")):
        row += f'<div class="inv">Invalidation · {escape(inv)}</div>'
    return row


def bias_of(feat: dict) -> str:
    ct = feat.get("call_type") or ""
    if ct in ("no_trade", "retrospective", "bidirectional", "not_a_chart"):
        return "neutral"
    return {"bullish": "bull", "bearish": "bear"}.get(feat.get("bias") or "neutral", "neutral")


def _infer_ticker_from_caption(caption: str) -> str | None:
    """Fallback ticker inference when vision leaves the ticker null.

    Big_Nuts's captions almost always lead with the ticker.
    ('BNB .. all look same', 'Hype holding long since $55', 'Twt testing...').
    """
    if not caption:
        return None
    first = caption.split()[0].strip(".,:;!?—-()[]")
    if not first or len(first) < 2 or len(first) > 6 or not first.isalpha():
        return None
    return first.upper()


def card(d: dict, size: str = "std") -> str:
    feat = d["feat"] or {}
    ticker = feat.get("ticker") or _infer_ticker_from_caption(d.get("text", "")) or "—"
    tf = feat.get("timeframe") or ""
    ct = feat.get("call_type") or ""
    stage = feat.get("trade_stage") or ""
    pattern = feat.get("pattern") or ""
    notes = feat.get("notes") or ""
    setups = feat.get("setups") or []
    caption = d["text"] or ""
    conf = feat.get("confluence_score")

    img_html = ""
    for path in d["attach"][:1]:
        uri = data_uri(path)
        if uri:
            img_html = f'<figure class="chart"><img src="{uri}" alt="{escape(ticker)} chart" loading="lazy"/></figure>'
            break

    meta = []
    if tf: meta.append(f'<span class="meta-tf">{escape(tf)}</span>')
    if ct: meta.append(f'<span class="meta-ct meta-{ct}">{escape(ct.replace("_"," "))}</span>')
    if stage: meta.append(f'<span class="meta-stage stage-{stage}">{escape(stage)}</span>')
    if conf is not None:
        dots = "●" * min(int(conf), 5) + "○" * max(0, 5 - int(conf))
        meta.append(f'<span class="meta-conf" title="confluence {conf}/5">{dots}</span>')

    setup_html = ""
    if setups:
        setup_html = '<div class="setups">' + "".join(setup_row(s) for s in setups) + "</div>"

    caption_html = f'<p class="caption">{escape(caption)}</p>' if caption else ""
    pattern_html = f'<p class="pattern">{escape(pattern)}</p>' if pattern else ""
    notes_html = f'<p class="notes">{escape(notes)}</p>' if notes else ""

    return f"""
    <article class="card card-{size} bias-{bias_of(feat)}">
      <header class="card-hd">
        <div class="tkr-block"><span class="tkr">{escape(ticker)}</span>{pattern_html}</div>
        <div class="meta">{"".join(meta)}</div>
      </header>
      {img_html}
      <div class="body">{caption_html}{notes_html}{setup_html}</div>
    </article>
    """


def split_by_direction(cards: list[dict]) -> dict[str, list[dict]]:
    b = {"long": [], "short": [], "neutral": []}
    for c in cards:
        ct = (c["feat"].get("call_type") or "").lower()
        if ct == "directional_long":    b["long"].append(c)
        elif ct == "directional_short": b["short"].append(c)
        else:                            b["neutral"].append(c)
    return b


def grid(cards: list[dict], size: str = "std") -> str:
    return f'<div class="grid grid-{size}">' + "".join(card(c, size) for c in cards) + "</div>"


def directional_grid(cards: list[dict], size: str = "std") -> str:
    if not cards:
        return ""
    b = split_by_direction(cards)
    blocks = []
    for key, label, tag in [
        ("long", "LONG", "Directional long"),
        ("short", "SHORT", "Directional short"),
        ("neutral", "NEUTRAL", "Bidirectional · watching · retrospective · no-trade"),
    ]:
        if not b[key]: continue
        blocks.append(f"""
        <div class="dir-block dir-block-{key}">
          <div class="dir-block-hd">
            <span class="dir-lbl dir-lbl-{key}">{label}</span>
            <span class="dir-tag">{tag}</span>
            <span class="dir-n">{len(b[key])}</span>
          </div>
          {grid(b[key], size)}
        </div>
        """)
    return "".join(blocks)


def std_section(sid: str, num_label: str, title: str, dek: str, cards: list[dict]) -> str:
    if not cards: return ""
    return f"""
    <section class="section" id="{sid}">
      <div class="section-hd">
        <div class="eyebrow">{num_label}</div>
        <h2>{title}</h2>
        <p class="dek">{dek}</p>
      </div>
      {directional_grid(cards)}
    </section>
    """


def counts(cards):
    b = split_by_direction(cards)
    return len(b["long"]), len(b["short"]), len(b["neutral"])


def ov_row(sid: str, num_label: str, title: str, cards: list[dict], issue_id: str) -> str:
    if not cards: return ""
    L, S, N = counts(cards)
    total = L + S + N
    return f"""
    <a class="ov-row" href="#{issue_id}/{sid}" data-jump="{sid}">
      <span class="ov-num">{num_label}</span>
      <span class="ov-title">{title}</span>
      <span class="ov-bar" aria-hidden="true">
        <span class="ov-seg ov-l" style="flex:{L}"></span>
        <span class="ov-seg ov-s" style="flex:{S}"></span>
        <span class="ov-seg ov-n" style="flex:{N}"></span>
      </span>
      <span class="ov-counts">
        <span class="ov-c ov-cl">{L}<em>L</em></span>
        <span class="ov-c ov-cs">{S}<em>S</em></span>
        <span class="ov-c ov-cn">{N}<em>N</em></span>
        <span class="ov-c ov-ct">{total}</span>
      </span>
    </a>
    """


# =====================================================================
# Issue: Sep 21, 2026 — Crypto majors sweep
# =====================================================================
def build_issue_sep21() -> dict:
    rows = load_rows("2026-09-21", "2026-09-22")
    charts = [r for r in rows if r["kind"] == "manual_chart" and (r["feat"] or r["text"].strip())]
    notes = [r for r in rows if r["kind"] == "manual_note"]

    by_ticker: dict[str, list[dict]] = {}
    for c in charts:
        t = (c["feat"].get("ticker") or _infer_ticker_from_caption(c["text"]) or "").upper()
        by_ticker.setdefault(t, []).append(c)

    def pick(*tickers: str) -> list[dict]:
        out, seen = [], set()
        for t in tickers:
            for c in by_ticker.get(t.upper(), []):
                if c["id"] not in seen:
                    out.append(c); seen.add(c["id"])
        return out

    # Sections: BTC + ratio anchors → ETH/majors → alts → RMBS retrospective
    btc_anchor = pick("BTC/USDT", "BTC/USD", "BTCUSD", "BTCUSD/XAUUSD", "BTC/XAU", "BTC/GOLD")
    eth_majors = pick("ETH/USDT", "ETH/USD", "SOL/USDT", "SOL/USD", "BNB/USDT", "BNB", "LTC")
    l1_alts = pick("TAO/USDT", "TAO", "SEIUSDT", "SEI", "RON/USDT", "RON",
                   "ZECUSDT", "ZEC/USDT", "ZEC", "DOGE/USDT", "DOGE", "LPT/USDT", "LPT")
    equity_retro = pick("RMBS")

    placed_ids = {c["id"] for g in [btc_anchor, eth_majors, l1_alts, equity_retro] for c in g}
    unused = [c for c in charts if c["id"] not in placed_ids]

    big_nuts_notes = [n for n in notes if n["author"] == "Big_Nuts"]

    n_docs = len(rows); n_charts = len(charts)
    bullish = sum(1 for c in charts if c["feat"].get("bias") == "bullish")
    bearish = sum(1 for c in charts if c["feat"].get("bias") == "bearish")
    active = sum(1 for c in charts if c["feat"].get("trade_stage") == "active")
    watching = sum(1 for c in charts if c["feat"].get("trade_stage") == "watching")

    tabs = [
        ("overview", "§", "Overview"),
        ("btc", "I", "BTC + BTC/Gold Ratio"),
        ("eth-majors", "II", "ETH + Majors"),
        ("l1-alts", "III", "L1s + Alts"),
        ("retro", "IV", "Equity Retrospective"),
        ("commentary", "V", "Commentary"),
    ]

    section_defs = [
        ("btc", "I", "BTC + BTC/Gold Ratio", btc_anchor),
        ("eth-majors", "II", "ETH + Majors", eth_majors),
        ("l1-alts", "III", "L1s + Alts", l1_alts),
        ("retro", "IV", "Equity Retrospective", equity_retro),
    ]
    ov_rows = "".join(ov_row(sid, nl, t, cs, "2026-09-21") for (sid, nl, t, cs) in section_defs)

    thesis_html = """
    <div class="thesis">
      <blockquote class="thesis-quote">
        BTC review keeping it simple — that red line we marked was important. Multiple views, same structures.
        <cite class="thesis-attrib">— Big_Nuts, 2026-09-21 · BTC review</cite>
      </blockquote>
      <div class="thesis-body">
        <p>A tight, crypto-only sweep — <b>16 chart docs (25 images with album-grouping)</b>, zero equities except one retrospective. Every fresh call is bullish or bidirectional-watching. Zero fresh shorts. The reflation basket from Aug 23–31 sits untouched; this batch is the follow-through on the crypto side of the book.</p>
        <p><b>The BTC/GOLD ratio thread continues.</b> Big_Nuts marks the ratio testing "the area we discussed 20 to 200 range" as a dome / rounding-top on the 3D — echoing the Aug 20 and Aug 31 calls that framed BTC exposure against gold rather than USD. This chart is now the third time the ratio has driven a session.</p>
        <p><b>Actives</b>: BTC (diamond re-break, W4→W5), ETH (bull-flag measured move to ~$3.3k), SOL (rising-flag break to $118), LTC (over 1.618), BNB (ascending triangle/megaphone), LPT (falling-channel reclaim), RON (wedge breakout), SEI (falling-wedge break, "nibbled"). <b>Watching</b>: TAO (two views, both bullish), DOGE (bull flag), ZEC (two views, W4→W5 target). <b>Retrospective</b>: RMBS (5-wave impulse peaked at W5; equity contrarian aside — "took a deep shit on memory").</p>
      </div>
    </div>
    """

    overview_page = f"""
    <section class="page overview" data-page="overview" role="tabpanel">
      {thesis_html}
      <div class="ov-matrix">
        <div class="ov-hd">
          <h3>Batch at a glance</h3>
          <div class="ov-legend">
            <span class="lg-l">■ <em>Long</em></span>
            <span class="lg-s">■ <em>Short</em></span>
            <span class="lg-n">■ <em>Neutral</em></span>
          </div>
        </div>
        {ov_rows}
        <div class="kb-hint"><span>Click a row · or use <kbd>←</kbd><kbd>→</kbd> to page</span></div>
      </div>
    </section>
    """

    btc_section = std_section("btc", "Section I", "BTC + BTC/Gold Ratio",
        "The anchor set. BTC on 1D/3D showing diamond re-break with Elliott W4→W5 count and a cup-and-handle rounding bottom. BTC/GOLD ratio testing the 20-to-200 range on 3D as a dome / rounding top — the third session in a row Big_Nuts frames crypto vs gold.",
        btc_anchor)
    eth_majors_section = std_section("eth-majors", "Section II", "ETH + Majors",
        "ETH bull-flag with measured move to ~$3.3k. SOL rising-flag break projecting $118. BNB ascending triangle/megaphone breakout ('we told you other day it wanted more'). LTC over 1.618.",
        eth_majors)
    l1_alts_section = std_section("l1-alts", "Section III", "L1s + Alts",
        "TAO ×2 (main + alternative view) both bullish/watching. SEI falling-wedge breakout, 'nibbled' — 200-EMA and 1.618 as next targets. RON out of wedge, active. LPT reclaiming liquidity zone with 200-EMA + 1.618 above. DOGE bull flag. ZEC ×2 (4h triangle watching + 3D W4→W5 target).",
        l1_alts)
    retro_section = std_section("retro", "Section IV", "Equity Retrospective",
        "One equity call — the only non-crypto in the batch. RMBS 5-wave impulse peaked at W5 and rolled over. Retrospective / played-out, but noted as a lesson for the memory-sector fade.",
        equity_retro)

    commentary_html = ""
    if big_nuts_notes:
        items = "".join(
            f'<li class="commentary-bn"><div class="cmt-hd"><time>{escape(n["t"][11:16])}</time><span class="cmt-src">Big_Nuts</span></div><div class="cmt-body">{escape(n["text"])}</div></li>'
            for n in big_nuts_notes
        )
        commentary_html = f"""
        <section class="section" id="commentary">
          <div class="section-hd">
            <div class="eyebrow">Section V</div>
            <h2>Big_Nuts — In His Own Words</h2>
            <p class="dek">Text-only asides that landed between the charts.</p>
          </div>
          <ul class="commentary-list">{items}</ul>
        </section>
        """

    unused_html = std_section("unused", "Appendix", "Other Charts",
        "Charts not placed in a named theme cluster.", unused) if unused else ""

    pages = f"""
      {overview_page}
      <section class="page" data-page="btc" role="tabpanel" hidden>{btc_section}</section>
      <section class="page" data-page="eth-majors" role="tabpanel" hidden>{eth_majors_section}</section>
      <section class="page" data-page="l1-alts" role="tabpanel" hidden>{l1_alts_section}{unused_html}</section>
      <section class="page" data-page="retro" role="tabpanel" hidden>{retro_section}</section>
      <section class="page" data-page="commentary" role="tabpanel" hidden>{commentary_html}</section>
    """

    return {
        "id": "2026-09-21",
        "label": "Sep 21 · Crypto sweep",
        "date_range": "Sep 21, 2026",
        "vol": "Vol. I · No. 09.21",
        "title": "BTC diamond re-break. ETH bull-flag. Ratio still driving.",
        "sub": f"Big_Nuts's Sep 21 drop — {n_charts} charts, essentially all crypto. BTC/GOLD ratio the through-line for a third session running. Every fresh call bullish or watching. One equity retrospective on RMBS.",
        "stats": {"n_docs": n_docs, "n_charts": n_charts, "bullish": bullish, "bearish": bearish,
                  "active": active, "watching": watching},
        "tabs": tabs,
        "pages": pages,
    }


# =====================================================================
# Issue: Aug 23–31, 2026 — Reflation + tonight's crypto refresh
# =====================================================================
def build_issue_aug23() -> dict:
    # Load through 2026-09-02 to capture tonight's UTC batch that lands at
    # 2026-09-01 01:30 (evening local of 2026-08-31).
    rows = load_rows("2026-08-23", "2026-09-02")
    charts = [r for r in rows if r["kind"] == "manual_chart" and (r["feat"] or r["text"].strip())]
    notes = [r for r in rows if r["kind"] == "manual_note"]

    # Split by batch date so we can section the tonight crypto refresh separately
    tonight_charts = [r for r in charts if r["t"] >= "2026-09-01"]
    prior_charts = [r for r in charts if r["t"] < "2026-09-01"]

    def by_ticker(chart_list):
        d = {}
        for c in chart_list:
            t = (c["feat"].get("ticker") or _infer_ticker_from_caption(c["text"]) or "").upper()
            d.setdefault(t, []).append(c)
        return d

    by_prior = by_ticker(prior_charts)
    by_tonight = by_ticker(tonight_charts)

    def pick_from(source, *tickers):
        out, seen = [], set()
        for t in tickers:
            for c in source.get(t.upper(), []):
                if c["id"] not in seen:
                    out.append(c); seen.add(c["id"])
        return out

    # Prior sections (Aug 23–25)
    metals_anchor = pick_from(by_prior, "XAUUSD", "GOLD/FDHBFIN", "GOLD")
    miners = pick_from(by_prior, "GDXJ", "PHYS", "CDE", "HL", "AG", "HYMC")
    copper = pick_from(by_prior, "COPX", "COPX/SPY", "FCX", "EMET", "XME", "SPSIMM", "REMX", "XLB")
    uranium = pick_from(by_prior, "SPUT", "U.UN", "CCJ", "URNM", "URNJ", "NNE", "OKLO")
    ag = pick_from(by_prior, "SB1!", "SUGAR11", "SUGAR", "WHEAT", "ZS1!", "ZCZ2026", "LE1!")
    spec = pick_from(by_prior, "CMPS", "BEAM", "INJ", "SOL")

    # Tonight — crypto majors
    crypto_tonight = tonight_charts[:]  # all of them; already crypto

    placed_ids = {c["id"] for g in [metals_anchor, miners, copper, uranium, ag, spec, crypto_tonight]
                  for c in g}
    unused = [c for c in charts if c["id"] not in placed_ids]

    big_nuts_notes = [n for n in notes if n["author"] == "Big_Nuts"]

    n_docs = len(rows)
    n_charts = len(charts)
    bullish = sum(1 for c in charts if c["feat"].get("bias") == "bullish")
    bearish = sum(1 for c in charts if c["feat"].get("bias") == "bearish")
    active = sum(1 for c in charts if c["feat"].get("trade_stage") == "active")
    watching = sum(1 for c in charts if c["feat"].get("trade_stage") == "watching")

    tabs = [
        ("overview", "§", "Overview"),
        ("metals", "I", "Precious Metals"),
        ("miners", "II", "Gold + Silver Miners"),
        ("copper", "III", "Copper + Base Metals"),
        ("uranium", "IV", "Uranium + Nuclear"),
        ("ag", "V", "Agriculture"),
        ("spec", "VI", "Speculative"),
        ("crypto", "VII", "Crypto Majors — Aug 31"),
        ("commentary", "VIII", "Commentary"),
    ]

    section_defs = [
        ("metals", "I", "Precious Metals", metals_anchor),
        ("miners", "II", "Gold + Silver Miners", miners),
        ("copper", "III", "Copper + Base Metals", copper),
        ("uranium", "IV", "Uranium + Nuclear", uranium),
        ("ag", "V", "Agriculture", ag),
        ("spec", "VI", "Speculative", spec),
        ("crypto", "VII", "Crypto Majors — Aug 31", crypto_tonight),
    ]
    ov_rows = "".join(ov_row(sid, nl, t, cs, "2026-08-23") for (sid, nl, t, cs) in section_defs)

    thesis_html = """
    <div class="thesis">
      <blockquote class="thesis-quote">
        A break out in sugar generally leads a break out in inflation. What many people fail to understand is that we are not guessing in here.
        <cite class="thesis-attrib">— Big_Nuts, 2026-08-23 · macro thesis fragments</cite>
      </blockquote>
      <div class="thesis-body">
        <p>The follow-through to the Aug 8 gold seed, executed at full breadth over Aug 23–25 — <b>44 charts across four commodity complexes at once</b> (precious metals, base metals, uranium, agriculture) — with essentially no diversification into anything else. Every chart bullish or neutral. Zero shorts.</p>
        <p>The through-line is <b>hard-asset reflation</b>: sugar leading inflation; wheat, corn, soybeans, cattle; gold measured against foreign US-debt holdings; copper vs SPY; GDXJ, PHYS, CDE, HL, AG; full uranium stack from physical (SPUT, U.UN) through majors (CCJ) to juniors (URNM, URNJ).</p>
        <p><b>Tonight's Aug 31 refresh</b> — a fresh crypto-majors sweep drops the first shorts into the book. <b>BTC 4h wedge broken down</b>, <b>ETH flat-top wedge breakdown</b>, and directional longs still active on <b>SOL, UNI, ZEC, PUMP</b> at 40-EMA support tests. Also on the tape: <b>HYPE</b> holding a long since $55 targeting $95–100 W5, <b>BNB</b> flagged as an executed win (sold $720 from $614 call), <b>TWT</b> testing a liquidity break.</p>
        <p>The reflation basket is the position; the tonight crypto refresh is the tactical adjust — first hedge signals appearing on the crypto side while the commodity book stays untouched.</p>
      </div>
    </div>
    """

    overview_page = f"""
    <section class="page overview" data-page="overview" role="tabpanel">
      {thesis_html}
      <div class="ov-matrix">
        <div class="ov-hd">
          <h3>Batch at a glance</h3>
          <div class="ov-legend">
            <span class="lg-l">■ <em>Long</em></span>
            <span class="lg-s">■ <em>Short</em></span>
            <span class="lg-n">■ <em>Neutral</em></span>
          </div>
        </div>
        {ov_rows}
        <div class="kb-hint"><span>Click a row · or use <kbd>←</kbd><kbd>→</kbd> to page</span></div>
      </div>
    </section>
    """

    metals_section = std_section("metals", "Section I", "Precious Metals — the Anchor",
        "XAUUSD's rising channel and the multi-decade gold-vs-foreign-US-debt ratio setup. Big_Nuts frames the debt-ratio break as the trigger for the entire hard-asset complex re-rating.",
        metals_anchor)
    miners_section = std_section("miners", "Section II", "Gold + Silver Miners",
        "The leveraged expression. GDXJ, PHYS anchor with three GDXJ charts across timeframes. CDE, HL, AG, HYMC round out the basket. All bullish.",
        miners)
    copper_section = std_section("copper", "Section III", "Copper + Base Metals",
        "COPX and FCX as the direct copper trade. COPX/SPY as the ratio confirmation. XME, EMET, REMX, SPSIMM, XLB as broader base-metals and materials exposure.",
        copper)
    uranium_section = std_section("uranium", "Section IV", "Uranium + Nuclear",
        "Big_Nuts's headline call: 'the 3rd cyclical uranium bull just starting.' Full stack from physical (SPUT, U.UN) through Cameco (CCJ — 'Wave 5 in play') to juniors (URNM, URNJ) to nuclear speculatives (NNE, OKLO).",
        uranium)
    ag_section = std_section("ag", "Section V", "Agriculture — Inflation Leaders",
        "Sugar as the tell — 'a break out in sugar generally leads a break out in inflation.' Two sugar charts on 1M. Wheat active, soybeans W5 pending, cattle retrospective, Dec corn breaking out to 1.618.",
        ag)
    spec_section = std_section("spec", "Section VI", "Speculative — Biotech + Crypto Flyers",
        "Small tail added Aug 24–25 outside the reflation basket. CMPS (COMPASS Pathways), BEAM (Beam Therapeutics). Crypto: INJ (two conflicting reads), SOL (needs a break). Nuclear speculatives NNE + OKLO are in the uranium section.",
        spec)
    crypto_section = std_section("crypto", "Section VII", "Crypto Majors — Aug 31 refresh",
        "Fresh sweep tonight — first shorts of the sequence appear here. BTC 4h broke its rising wedge; ETH broke down from a flat-top wedge holding 40-EMA support. Directional longs still active on SOL / UNI / ZEC / PUMP at wedge-break-with-support setups. HYPE noted as a running long since $55. BNB called as an executed win.",
        crypto_tonight)

    commentary_html = ""
    if big_nuts_notes:
        items = "".join(
            f'<li class="commentary-bn"><div class="cmt-hd"><time>{escape(n["t"][11:16])}</time><span class="cmt-src">Big_Nuts</span></div><div class="cmt-body">{escape(n["text"])}</div></li>'
            for n in big_nuts_notes
        )
        commentary_html = f"""
        <section class="section" id="commentary">
          <div class="section-hd">
            <div class="eyebrow">Section VIII</div>
            <h2>Big_Nuts — In His Own Words</h2>
            <p class="dek">The text posts alongside the charts. Macro asides, thesis fragments, and market-context notes.</p>
          </div>
          <ul class="commentary-list">{items}</ul>
        </section>
        """

    unused_html = std_section("unused", "Appendix", "Other Charts",
        "Charts not placed in a named theme cluster.", unused) if unused else ""

    pages = f"""
      {overview_page}
      <section class="page" data-page="metals" role="tabpanel" hidden>{metals_section}</section>
      <section class="page" data-page="miners" role="tabpanel" hidden>{miners_section}</section>
      <section class="page" data-page="copper" role="tabpanel" hidden>{copper_section}</section>
      <section class="page" data-page="uranium" role="tabpanel" hidden>{uranium_section}</section>
      <section class="page" data-page="ag" role="tabpanel" hidden>{ag_section}</section>
      <section class="page" data-page="spec" role="tabpanel" hidden>{spec_section}{unused_html}</section>
      <section class="page" data-page="crypto" role="tabpanel" hidden>{crypto_section}</section>
      <section class="page" data-page="commentary" role="tabpanel" hidden>{commentary_html}</section>
    """

    return {
        "id": "2026-08-23",
        "label": "Aug 23–31 · Reflation + crypto refresh",
        "date_range": "Aug 23 – 31, 2026",
        "vol": "Vol. I · No. 08.31",
        "title": "The reflation basket, plus a tactical crypto adjust.",
        "sub": f"Big_Nuts's Aug 23–31 sequence — {n_charts} charts. Aug 23–25 was the full commodity-complex reflation trade; Aug 31 added a crypto-majors refresh that introduces the first shorts (BTC, ETH) into an otherwise one-way book.",
        "stats": {"n_docs": n_docs, "n_charts": n_charts, "bullish": bullish, "bearish": bearish,
                  "active": active, "watching": watching},
        "tabs": tabs,
        "pages": pages,
    }


# =====================================================================
# Issue: Aug 20, 2026 — Crypto follow-through
# =====================================================================
def build_issue_aug20() -> dict:
    rows = load_rows("2026-08-20", "2026-08-21")
    charts = [r for r in rows if r["kind"] == "manual_chart" and (r["feat"] or r["text"].strip())]

    by_ticker: dict[str, list[dict]] = {}
    for c in charts:
        t = (c["feat"].get("ticker") or "").upper()
        by_ticker.setdefault(t, []).append(c)

    def pick(*tickers: str) -> list[dict]:
        out, seen = [], set()
        for t in tickers:
            for c in by_ticker.get(t.upper(), []):
                if c["id"] not in seen:
                    out.append(c); seen.add(c["id"])
        return out

    btc = pick("BTC/USDT", "BTC/USD")
    eth = pick("ETH/USD", "ETH/USDT")
    sol = pick("SOL/USD", "SOL/USDT", "SOL")
    ratios = pick("BTCUSD/XAUUSD", "BTC/XAU")

    n_docs = len(rows); n_charts = len(charts)
    bullish = sum(1 for c in charts if c["feat"].get("bias") == "bullish")
    bearish = sum(1 for c in charts if c["feat"].get("bias") == "bearish")
    active = sum(1 for c in charts if c["feat"].get("trade_stage") == "active")
    watching = sum(1 for c in charts if c["feat"].get("trade_stage") == "watching")

    tabs = [
        ("overview", "§", "Overview"),
        ("crypto", "I", "Crypto Majors"),
        ("ratios", "II", "BTC / Gold Ratio"),
    ]
    ov_rows = ov_row("crypto", "I", "Crypto Majors", btc + eth + sol, "2026-08-20") \
            + ov_row("ratios", "II", "BTC / Gold Ratio", ratios, "2026-08-20")

    thesis_html = """
    <div class="thesis">
      <blockquote class="thesis-quote">
        I don't consider BTC in a secular bull market until it takes out the highs against gold.
        <cite class="thesis-attrib">— Big_Nuts, 2026-08-20 · BTC / GOLD chart caption</cite>
      </blockquote>
      <div class="thesis-body">
        <p>A tight, focused batch — <b>six crypto-major charts</b>, no equities. The book has shifted to something more defensive on crypto: <b>BTC standalone short</b> to the 40-week EMA, <b>BTC/GOLD ratio bearish</b>, <b>ETH the only outright long</b>, <b>SOL a coin-flip</b>.</p>
        <p>The <b>BTC/GOLD ratio call is the through-line</b> to the Aug 8 gold book. Big_Nuts is now framing crypto exposure against gold, not USD.</p>
      </div>
    </div>
    """

    overview_page = f"""
    <section class="page overview" data-page="overview" role="tabpanel">
      {thesis_html}
      <div class="ov-matrix">
        <div class="ov-hd">
          <h3>Batch at a glance</h3>
          <div class="ov-legend">
            <span class="lg-l">■ <em>Long</em></span>
            <span class="lg-s">■ <em>Short</em></span>
            <span class="lg-n">■ <em>Neutral</em></span>
          </div>
        </div>
        {ov_rows}
        <div class="kb-hint"><span>Click a row · or use <kbd>←</kbd><kbd>→</kbd> to page</span></div>
      </div>
    </section>
    """

    crypto_section = std_section("crypto", "Section I", "Crypto Majors",
        "BTC standalone short into the 40-week EMA; ETH the sole reclaimed long on the 3D 40-EMA; SOL a two-sided wait at the dome-wall / cloud-resistance confluence.", btc + eth + sol)
    ratios_section = std_section("ratios", "Section II", "BTC / Gold Ratio",
        "The through-line to the Aug 8 gold thesis. Big_Nuts is framing BTC exposure against gold, not USD.", ratios)

    pages = f"""
      {overview_page}
      <section class="page" data-page="crypto" role="tabpanel" hidden>{crypto_section}</section>
      <section class="page" data-page="ratios" role="tabpanel" hidden>{ratios_section}</section>
    """

    return {
        "id": "2026-08-20",
        "label": "Aug 20 · Crypto follow-through",
        "date_range": "Aug 20, 2026",
        "vol": "Vol. I · No. 08.20",
        "title": "BTC/GOLD ratio call. ETH the last man standing.",
        "sub": f"Big_Nuts's Aug 20 drop — {n_charts} crypto-majors charts, no equities. The follow-through to the Aug 8 gold thesis, executed as defensive crypto sizing.",
        "stats": {"n_docs": n_docs, "n_charts": n_charts, "bullish": bullish, "bearish": bearish,
                  "active": active, "watching": watching},
        "tabs": tabs,
        "pages": pages,
    }


# =====================================================================
# Issue: Aug 8, 2026 — Melt-up book
# =====================================================================
def build_issue_aug8() -> dict:
    rows = load_rows("2026-08-08", "2026-08-09")
    charts = [r for r in rows if r["kind"] == "manual_chart" and (r["feat"] or r["text"].strip())]
    notes = [r for r in rows if r["kind"] == "manual_note"]

    by_ticker: dict[str, list[dict]] = {}
    for c in charts:
        t = (c["feat"].get("ticker") or "").upper()
        by_ticker.setdefault(t, []).append(c)

    def pick(*tickers: str) -> list[dict]:
        out, seen = [], set()
        for t in tickers:
            for c in by_ticker.get(t.upper(), []):
                if c["id"] not in seen:
                    out.append(c); seen.add(c["id"])
        return out

    pair_long = pick("NQ", "ES1!")
    pair_short = pick("SPX", "SPX500")
    pair_bond = pick("TLT")
    pair_btc = pick("BTC/USD")
    pair_ctx = pick("MOVE", "SOXL")
    gold_hero = pick("XAUUSD", "GOLD")
    gold_miners = pick("TMC", "HYMC", "NG", "CRML")
    ai_sw = pick("PLTR", "HIMS", "APPS", "PAYC", "VCTR", "NBIS", "LITE", "AMPL", "VAC")
    semis = pick("MU", "DELL", "ORCL", "AMD", "INTC", "MRVL", "CRWV")
    space = pick("SPCE", "RKLB")
    consumer = pick("ATKR", "BHVN", "USAR", "HCC", "CRS")
    fx_crypto = pick("USDJPY", "BNB", "ETH/USDT", "SOL/USDT")

    placed_ids = {c["id"] for g in [pair_long, pair_short, pair_bond, pair_btc, pair_ctx,
                                    gold_hero, gold_miners, ai_sw, semis, space, consumer,
                                    fx_crypto] for c in g}
    unused = [c for c in charts if c["id"] not in placed_ids]

    big_nuts_notes = [n for n in notes if n["author"] == "Big_Nuts"]

    n_docs = len(rows); n_charts = len(charts)
    bullish = sum(1 for c in charts if c["feat"].get("bias") == "bullish")
    bearish = sum(1 for c in charts if c["feat"].get("bias") == "bearish")
    active = sum(1 for c in charts if c["feat"].get("trade_stage") == "active")
    watching = sum(1 for c in charts if c["feat"].get("trade_stage") == "watching")

    tabs = [
        ("overview", "§", "Overview"),
        ("regime-pair", "I", "Regime Pair"),
        ("gold", "II", "Gold + Miners"),
        ("ai-sw", "III", "AI & SW"),
        ("semis", "IV", "Semis"),
        ("space", "V", "Space"),
        ("consumer", "VI", "Consumer"),
        ("fx-crypto", "VII", "FX / Crypto"),
        ("commentary", "VIII", "Commentary"),
    ]

    section_defs = [
        ("regime-pair", "I", "Regime Pair", pair_long + pair_short + pair_bond + pair_btc + pair_ctx),
        ("gold", "II", "Gold + Miners", gold_hero + gold_miners),
        ("ai-sw", "III", "AI & Software", ai_sw),
        ("semis", "IV", "Semis + Mega-cap", semis),
        ("space", "V", "Space", space),
        ("consumer", "VI", "Consumer + Materials", consumer),
        ("fx-crypto", "VII", "FX + Crypto", fx_crypto),
    ]
    ov_rows = "".join(ov_row(sid, nl, t, cs, "2026-08-08") for (sid, nl, t, cs) in section_defs)

    thesis_html = """
    <div class="thesis">
      <blockquote class="thesis-quote">
        The SPX now following the 1997 to 2000 last leg phase. Have to see by September where we are.
        <cite class="thesis-attrib">— Big_Nuts, 2026-08-08 16:20 UTC</cite>
      </blockquote>
      <div class="thesis-body">
        <p>The book is priced for a <b>melt-up finish with a wedge break already in view</b>. Long NQ and ES on the active rising channel with W1–W5 Elliott counts; short SPX cash on the wedge break; short TLT on the multi-year descending channel; short BTC 4h as the risk-off canary.</p>
        <p>The thematic overlay is <b>gold and its miners</b> — XAUUSD's falling-wedge break as the anchor, with TMC, HYMC (Sprott flagged as backer), NG and CRML as the leveraged beta expression. The highest-conviction directional cluster in the batch.</p>
      </div>
    </div>
    """

    overview_page = f"""
    <section class="page overview" data-page="overview" role="tabpanel">
      {thesis_html}
      <div class="ov-matrix">
        <div class="ov-hd">
          <h3>Week at a glance</h3>
          <div class="ov-legend">
            <span class="lg-l">■ <em>Long</em></span>
            <span class="lg-s">■ <em>Short</em></span>
            <span class="lg-n">■ <em>Neutral</em></span>
          </div>
        </div>
        {ov_rows}
        <div class="kb-hint"><span>Click a row · or use <kbd>←</kbd><kbd>→</kbd> to page</span></div>
      </div>
    </section>
    """

    pair_section = f"""
    <section class="section pair-section" id="regime-pair">
      <div class="section-hd">
        <div class="eyebrow">Section I</div>
        <h2>The Regime Pair</h2>
        <p class="dek">A melt-up finish, hedged. Long the rising channels; short the wedge and the long bond; a Bitcoin cross as the risk-off tell.</p>
      </div>
      <div class="pair-grid">
        <div class="pair-col pair-long">
          <div class="pair-label"><span class="pair-side long">LONG</span><span class="pair-tag">Melt-up expression</span></div>
          {grid(pair_long, "hero") if pair_long else ""}
        </div>
        <div class="pair-col pair-short">
          <div class="pair-label"><span class="pair-side short">SHORT</span><span class="pair-tag">Hedge / wedge break</span></div>
          {grid(pair_short + pair_bond + pair_btc, "hero") if (pair_short or pair_bond or pair_btc) else ""}
        </div>
      </div>
      {("<div class='section-sub'><h3>Context indicators</h3>" + grid(pair_ctx) + "</div>") if pair_ctx else ""}
    </section>
    """

    gold_section = f"""
    <section class="section" id="gold">
      <div class="section-hd">
        <div class="eyebrow">Section II · Highest-conviction theme</div>
        <h2>Gold, and the Miners Beneath It</h2>
        <p class="dek">XAUUSD's falling-wedge break anchoring a small basket of miners on W4→W5 Elliott counts — TMC, HYMC, NG, CRML.</p>
      </div>
      {("<div class='sub-lede'>The anchor</div>" + grid(gold_hero, "hero")) if gold_hero else ""}
      {("<div class='sub-lede'>The miner basket</div>" + directional_grid(gold_miners)) if gold_miners else ""}
    </section>
    """

    ai_section = std_section("ai-sw", "Section III", "AI &amp; Software Beta",
        "The cup-and-handle cohort. Continuation setups on the AI-adjacent software names.", ai_sw)
    semis_section = std_section("semis", "Section IV", "Semis &amp; Mega-cap Tech",
        "Mixed posture. MU / DELL / ORCL constructive. AMD bidirectional. INTC contrarian short. MRVL: weekly credit spread.", semis)
    space_section = std_section("space", "Section V", "Space &amp; Speculative",
        "SPCE active 4h wedge breakout with a bearish 1h retrospective. RKLB post-W5.", space)
    consumer_section = std_section("consumer", "Section VI", "Consumer &amp; Materials",
        "Individual setups outside the primary themes.", consumer)
    fx_section = std_section("fx-crypto", "Section VII", "FX &amp; Crypto Context",
        "USDJPY rising channel. BNB pennant. ETH and SOL range-bound.", fx_crypto)
    misc_section = std_section("misc", "Appendix", "Other Charts",
            "Not placed in a named theme cluster.", unused) if unused else ""

    commentary_html = ""
    if big_nuts_notes:
        items = "".join(
            f'<li class="commentary-bn"><div class="cmt-hd"><time>{escape(n["t"][11:16])}</time><span class="cmt-src">Big_Nuts</span></div><div class="cmt-body">{escape(n["text"])}</div></li>'
            for n in big_nuts_notes
        )
        commentary_html = f"""
        <section class="section" id="commentary">
          <div class="section-hd">
            <div class="eyebrow">Section VIII</div>
            <h2>Big_Nuts — In His Own Words</h2>
            <p class="dek">Text-only asides that landed between the charts.</p>
          </div>
          <ul class="commentary-list">{items}</ul>
        </section>
        """

    pages = f"""
      {overview_page}
      <section class="page" data-page="regime-pair" role="tabpanel" hidden>{pair_section}</section>
      <section class="page" data-page="gold" role="tabpanel" hidden>{gold_section}</section>
      <section class="page" data-page="ai-sw" role="tabpanel" hidden>{ai_section}</section>
      <section class="page" data-page="semis" role="tabpanel" hidden>{semis_section}</section>
      <section class="page" data-page="space" role="tabpanel" hidden>{space_section}</section>
      <section class="page" data-page="consumer" role="tabpanel" hidden>{consumer_section}</section>
      <section class="page" data-page="fx-crypto" role="tabpanel" hidden>{fx_section}</section>
      <section class="page" data-page="commentary" role="tabpanel" hidden>{commentary_html}{misc_section}</section>
    """

    return {
        "id": "2026-08-08",
        "label": "Aug 8 · Melt-up book",
        "date_range": "Week of Aug 3 – 9, 2026",
        "vol": "Vol. I · No. 08.08",
        "title": "A melt-up book, hedged for the finish.",
        "sub": f"Big_Nuts's Aug 8 drop — {n_charts} annotated charts across a gold-miner cluster, an AI-software cup-and-handle cohort, and a paired regime bet on the SPX endgame.",
        "stats": {"n_docs": n_docs, "n_charts": n_charts, "bullish": bullish, "bearish": bearish,
                  "active": active, "watching": watching},
        "tabs": tabs,
        "pages": pages,
    }


# ---- Render whole document -------------------------------------------
def render_issue_block(issue: dict, is_default: bool) -> str:
    tabs_html = "".join(
        f'<button class="tab" role="tab" data-page="{pid}"><span class="tab-num">{nl}</span>{escape(name)}</button>'
        for (pid, nl, name) in issue["tabs"]
    )
    s = issue["stats"]
    hidden_attr = "" if is_default else "hidden"
    return f"""
    <div class="issue" data-issue="{issue['id']}" {hidden_attr}>
      <header class="masthead">
        <div class="mast-meta">
          <span>Market Traders Brief <span class="dot">·</span> {escape(issue['vol'])}</span>
          <span>{escape(issue['date_range'])}</span>
        </div>
        <h1 class="mast-title">{escape(issue['title'])}</h1>
        <p class="mast-sub">{issue['sub']}</p>
        <div class="mast-stats">
          <div class="stat"><span class="n">{s['n_docs']}</span><span class="k">Messages</span></div>
          <div class="stat"><span class="n">{s['n_charts']}</span><span class="k">Chart calls</span></div>
          <div class="stat bull"><span class="n">{s['bullish']}</span><span class="k">Bullish</span></div>
          <div class="stat bear"><span class="n">{s['bearish']}</span><span class="k">Bearish</span></div>
          <div class="stat acc"><span class="n">{s['active']}</span><span class="k">Active</span></div>
          <div class="stat"><span class="n">{s['watching']}</span><span class="k">Watching</span></div>
        </div>
      </header>
      <nav class="tabs" role="tablist" aria-label="Sections">
        <div class="tabs-inner">{tabs_html}</div>
      </nav>
      <main class="pages">{issue['pages']}</main>
    </div>
    """


def build() -> str:
    issues = [build_issue_sep21(), build_issue_aug23(), build_issue_aug20(), build_issue_aug8()]

    issue_picker = "".join(
        f'<button class="iss-btn" data-issue="{i["id"]}" data-index="{n}">'
        f'<span class="iss-mark">{"LATEST" if n == 0 else "PRIOR"}</span>'
        f'<span class="iss-lbl">{escape(i["label"])}</span></button>'
        for n, i in enumerate(issues)
    )
    issue_blocks = "".join(render_issue_block(i, n == 0) for n, i in enumerate(issues))

    return f"""<title>Market Traders Brief · Big_Nuts / Ari's Relays</title>
<style>
  /* Dark-first — this brief is designed for a trader's screen at night.
     Light theme is a fallback for viewers who explicitly opt in. */
  :root {{
    --paper: #0B0D0C; --paper-elev: #14171A; --ink: #E8EAE6; --ink-med: #B5B9B3;
    --ink-dim: #7A8078; --rule: #22271F; --rule-soft: #1A1E1B;
    --bull: #5DB57A; --bear: #E27266; --neutral: #C9B85A; --accent: #4FB8A5;
    --pair-long: #5DB57A; --pair-short: #E27266;
    --tint-warm: #1B1A15; --tint-cool: #142020;
    color-scheme: dark;
  }}
  :root[data-theme="light"] {{
    --paper: #F5F3EE; --paper-elev: #FBFAF6; --ink: #0F1211; --ink-med: #3A3F3C;
    --ink-dim: #5F6461; --rule: #D5D1C4; --rule-soft: #E6E2D6;
    --bull: #2E7D48; --bear: #B23A2F; --neutral: #8B7A2D; --accent: #0F5F52;
    --pair-long: #2E7D48; --pair-short: #B23A2F; --tint-warm: #EDE8D9; --tint-cool: #E5EAE7;
    color-scheme: light;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--paper); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
    font-size: 15px; line-height: 1.55; -webkit-font-smoothing: antialiased;
    font-variant-numeric: tabular-nums;
  }}
  .wrap {{ max-width: 1240px; margin: 0 auto; padding: 0 32px 96px; }}

  .iss-picker {{
    display: flex; gap: 0; align-items: stretch;
    padding: 20px 0 0; margin: 0 0 8px;
    font-family: "JetBrains Mono", monospace; flex-wrap: wrap;
  }}
  .iss-btn {{
    background: transparent; border: 1px solid var(--rule);
    padding: 10px 16px; cursor: pointer;
    display: flex; flex-direction: column; gap: 4px; align-items: flex-start;
    color: var(--ink-dim); font: inherit; text-align: left;
    transition: background .12s, color .12s, border-color .12s;
    margin-right: -1px;
  }}
  .iss-btn:first-child {{ border-top-left-radius: 3px; border-bottom-left-radius: 3px; }}
  .iss-btn:last-child {{ border-top-right-radius: 3px; border-bottom-right-radius: 3px; margin-right: 0; }}
  .iss-btn:hover {{ background: var(--tint-cool); color: var(--ink); }}
  .iss-btn.active {{
    background: var(--paper-elev); color: var(--ink);
    border-color: var(--accent); position: relative; z-index: 1;
  }}
  .iss-btn.active .iss-mark {{ color: var(--accent); }}
  .iss-mark {{ font-size: 9.5px; letter-spacing: .16em; color: var(--ink-dim); }}
  .iss-lbl {{ font-size: 12.5px; color: inherit; letter-spacing: .02em; }}

  .masthead {{ border-bottom: 3px double var(--ink); padding: 20px 0 20px; margin-bottom: 32px; }}
  .mast-meta {{
    display: flex; justify-content: space-between; align-items: baseline;
    font-family: "JetBrains Mono", monospace;
    font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
    color: var(--ink-dim); padding-bottom: 12px;
    border-bottom: 1px solid var(--rule); flex-wrap: wrap; gap: 10px;
  }}
  .mast-meta .dot {{ color: var(--accent); }}
  .mast-title {{
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    font-weight: 700; font-size: clamp(38px, 5vw, 62px);
    line-height: 0.98; letter-spacing: -0.018em;
    margin: 22px 0 8px; text-wrap: balance;
  }}
  .mast-sub {{
    font-family: "Iowan Old Style", Georgia, serif; font-style: italic; color: var(--ink-med);
    font-size: clamp(16px, 1.5vw, 20px); max-width: 780px; margin: 6px 0 22px;
  }}
  .mast-stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0; border-top: 1px solid var(--rule); padding-top: 16px;
  }}
  .stat {{ padding: 4px 20px 4px 0; border-right: 1px solid var(--rule-soft); }}
  .stat:last-child {{ border-right: none; }}
  .stat .n {{
    font-family: "Iowan Old Style", Georgia, serif;
    font-weight: 700; font-size: 28px; line-height: 1; letter-spacing: -0.01em;
  }}
  .stat.bull .n {{ color: var(--bull); }}
  .stat.bear .n {{ color: var(--bear); }}
  .stat.acc  .n {{ color: var(--accent); }}
  .stat .k {{
    font-family: "JetBrains Mono", monospace;
    font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--ink-dim); margin-top: 6px; display: block;
  }}

  .tabs {{
    position: sticky; top: 0; z-index: 30;
    background: color-mix(in oklab, var(--paper) 94%, transparent);
    backdrop-filter: saturate(160%) blur(14px);
    -webkit-backdrop-filter: saturate(160%) blur(14px);
    border-bottom: 1px solid var(--rule);
    margin: 0 -32px 32px; padding: 0 32px;
    overflow-x: auto; scrollbar-width: none;
  }}
  .tabs::-webkit-scrollbar {{ display: none; }}
  .tabs-inner {{
    display: flex; gap: 2px; align-items: stretch; min-height: 48px;
    font-family: "JetBrains Mono", monospace;
  }}
  .tab {{
    background: transparent; border: 0; padding: 0 14px;
    display: flex; align-items: center; gap: 8px;
    font: inherit; font-size: 12px; letter-spacing: .04em;
    color: var(--ink-dim); cursor: pointer; white-space: nowrap;
    border-bottom: 2px solid transparent; margin-bottom: -1px;
    transition: color .12s;
  }}
  .tab:hover {{ color: var(--ink); }}
  .tab .tab-num {{ color: var(--accent); font-size: 10.5px; letter-spacing: .1em; }}
  .tab.active {{ color: var(--ink); border-bottom-color: var(--accent); }}

  .page {{ display: block; animation: pageIn .18s ease-out; }}
  .page[hidden] {{ display: none; }}
  @keyframes pageIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: translateY(0); }} }}

  .thesis {{ padding: 0 0 12px; margin: 0; display: grid; grid-template-columns: 1fr; gap: 20px; }}
  @media (min-width: 900px) {{ .thesis {{ grid-template-columns: 1.15fr 1fr; gap: 48px; }} }}
  .thesis-quote {{
    font-family: "Iowan Old Style", Georgia, serif;
    font-size: clamp(22px, 2.6vw, 32px);
    line-height: 1.14; letter-spacing: -0.014em;
    color: var(--ink); text-wrap: balance;
    position: relative; margin: 0;
  }}
  .thesis-quote::before {{
    content: "\\201C"; position: absolute; left: -0.55em; top: -0.3em;
    font-size: 1.6em; color: var(--accent); line-height: 1;
  }}
  .thesis-attrib {{
    display: block; margin-top: 16px;
    font-family: "JetBrains Mono", monospace;
    font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
    color: var(--ink-dim);
  }}
  .thesis-body {{
    font-family: "Iowan Old Style", Georgia, serif;
    font-size: 16.5px; line-height: 1.6; color: var(--ink-med);
  }}
  .thesis-body p {{ margin: 0 0 14px; }}
  .thesis-body p:last-child {{ margin: 0; }}
  .thesis-body b {{ color: var(--ink); font-weight: 700; }}

  .overview {{ display: grid; grid-template-columns: 1fr; gap: 40px; }}
  @media (min-width: 900px) {{ .overview {{ grid-template-columns: 1.05fr 1fr; gap: 56px; align-items: start; }} }}
  .ov-matrix {{ background: var(--paper-elev); border: 1px solid var(--rule); padding: 20px 20px 12px; border-radius: 2px; }}
  .ov-hd {{ display: flex; align-items: baseline; gap: 12px; padding-bottom: 12px; margin-bottom: 8px; border-bottom: 1px solid var(--rule-soft); }}
  .ov-hd h3 {{ font-family: "Iowan Old Style", Georgia, serif; font-size: 20px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }}
  .ov-hd .ov-legend {{ margin-left: auto; font-family: "JetBrains Mono", monospace; font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-dim); display: flex; gap: 12px; }}
  .ov-legend em {{ font-style: normal; color: var(--ink-med); }}
  .ov-legend .lg-l {{ color: var(--bull); }}
  .ov-legend .lg-s {{ color: var(--bear); }}
  .ov-legend .lg-n {{ color: var(--neutral); }}
  .ov-row {{ display: grid; grid-template-columns: 28px 1fr 120px 130px; align-items: center; gap: 12px; padding: 12px 4px; border-bottom: 1px solid var(--rule-soft); text-decoration: none; color: var(--ink); transition: background .1s; }}
  .ov-row:last-child {{ border-bottom: 0; }}
  .ov-row:hover {{ background: var(--tint-cool); }}
  .ov-num {{ font-family: "JetBrains Mono", monospace; font-size: 11px; color: var(--accent); letter-spacing: .1em; }}
  .ov-title {{ font-family: "Iowan Old Style", Georgia, serif; font-size: 15.5px; color: var(--ink); }}
  .ov-bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; background: var(--rule-soft); }}
  .ov-seg {{ height: 100%; }}
  .ov-seg.ov-l {{ background: var(--bull); }}
  .ov-seg.ov-s {{ background: var(--bear); }}
  .ov-seg.ov-n {{ background: var(--neutral); }}
  .ov-counts {{ display: flex; gap: 6px; justify-content: flex-end; align-items: baseline; font-family: "JetBrains Mono", monospace; font-size: 12px; }}
  .ov-c {{ display: inline-flex; gap: 3px; align-items: baseline; padding: 2px 5px; border-radius: 2px; color: var(--ink); }}
  .ov-c em {{ font-size: 9px; letter-spacing: .08em; color: var(--ink-dim); font-style: normal; }}
  .ov-c.ov-cl {{ background: color-mix(in oklab, var(--bull) 12%, transparent); color: var(--bull); }}
  .ov-c.ov-cs {{ background: color-mix(in oklab, var(--bear) 12%, transparent); color: var(--bear); }}
  .ov-c.ov-cn {{ background: color-mix(in oklab, var(--neutral) 12%, transparent); color: var(--neutral); }}
  .ov-c.ov-ct {{ padding-left: 8px; border-left: 1px solid var(--rule); color: var(--ink-med); font-weight: 700; }}
  .kb-hint {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--rule-soft); font-family: "JetBrains Mono", monospace; font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-dim); display: flex; gap: 20px; flex-wrap: wrap; align-items: center; }}
  .kb-hint kbd {{ display: inline-block; padding: 2px 6px; border: 1px solid var(--rule); border-bottom-width: 2px; border-radius: 3px; background: var(--paper); font-family: inherit; font-size: 10px; color: var(--ink); margin: 0 2px; }}

  .section {{ margin: 0 0 72px; }}
  .section-hd {{ margin: 0 0 24px; max-width: 780px; }}
  .eyebrow {{ font-family: "JetBrains Mono", monospace; font-size: 10.5px; letter-spacing: .18em; text-transform: uppercase; color: var(--accent); margin-bottom: 10px; }}
  .section-hd h2 {{ font-family: "Iowan Old Style", Georgia, serif; font-weight: 700; font-size: clamp(24px, 2.6vw, 32px); line-height: 1.1; letter-spacing: -0.012em; margin: 0 0 10px; text-wrap: balance; }}
  .dek {{ font-family: "Iowan Old Style", Georgia, serif; font-style: italic; color: var(--ink-med); font-size: 15.5px; line-height: 1.55; margin: 0; }}
  .sub-lede {{ font-family: "JetBrains Mono", monospace; font-size: 10.5px; letter-spacing: .14em; text-transform: uppercase; color: var(--ink-dim); padding: 20px 0 12px; margin-top: 20px; border-top: 1px dashed var(--rule); }}

  .dir-block {{ margin: 0 0 32px; }}
  .dir-block:last-child {{ margin-bottom: 0; }}
  .dir-block-hd {{ display: flex; align-items: baseline; gap: 12px; margin: 8px 0 14px; padding-bottom: 8px; border-bottom: 1px solid var(--rule-soft); }}
  .dir-lbl {{ font-family: "JetBrains Mono", monospace; font-size: 13px; font-weight: 700; letter-spacing: .12em; }}
  .dir-lbl-long {{ color: var(--pair-long); }}
  .dir-lbl-short {{ color: var(--pair-short); }}
  .dir-lbl-neutral {{ color: var(--neutral); }}
  .dir-tag {{ font-family: "Iowan Old Style", Georgia, serif; font-style: italic; font-size: 13px; color: var(--ink-dim); }}
  .dir-n {{ margin-left: auto; font-family: "JetBrains Mono", monospace; font-size: 11px; color: var(--ink-dim); letter-spacing: .08em; }}

  .pair-grid {{ display: grid; grid-template-columns: 1fr; gap: 32px; padding: 20px 0 8px; }}
  @media (min-width: 900px) {{ .pair-grid {{ grid-template-columns: 1fr 1fr; gap: 40px; }} }}
  .pair-col {{ padding: 22px 0 0; border-top: 4px solid var(--rule-soft); }}
  .pair-col.pair-long {{ border-top-color: var(--pair-long); }}
  .pair-col.pair-short {{ border-top-color: var(--pair-short); }}
  .pair-label {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; font-family: "JetBrains Mono", monospace; }}
  .pair-side {{ font-size: 22px; font-weight: 700; letter-spacing: .05em; }}
  .pair-side.long {{ color: var(--pair-long); }}
  .pair-side.short {{ color: var(--pair-short); }}
  .pair-tag {{ font-size: 11px; letter-spacing: .14em; text-transform: uppercase; color: var(--ink-dim); }}
  .section-sub {{ margin-top: 32px; }}
  .section-sub h3 {{ font-family: "JetBrains Mono", monospace; font-size: 11px; letter-spacing: .16em; text-transform: uppercase; color: var(--ink-dim); margin: 24px 0 12px; }}

  .grid {{ display: grid; gap: 18px; }}
  .grid-std {{ grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
  .grid-hero {{ grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }}

  .card {{ background: var(--paper-elev); border: 1px solid var(--rule); border-radius: 2px; display: flex; flex-direction: column; overflow: hidden; position: relative; }}
  .card::before {{ content: ""; position: absolute; left: 0; top: 0; width: 3px; height: 40px; background: var(--rule); }}
  .card.bias-bull::before {{ background: var(--bull); }}
  .card.bias-bear::before {{ background: var(--bear); }}
  .card.bias-neutral::before {{ background: var(--neutral); }}
  .card-hd {{ display: flex; justify-content: space-between; align-items: flex-start; padding: 12px 16px 10px; gap: 12px; }}
  .tkr-block {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
  .tkr {{ font-family: "JetBrains Mono", monospace; font-weight: 700; font-size: 16px; letter-spacing: -0.01em; color: var(--ink); }}
  .pattern {{ font-family: "Iowan Old Style", Georgia, serif; font-style: italic; font-size: 12.5px; line-height: 1.35; color: var(--ink-dim); margin: 0; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; flex-shrink: 0; max-width: 55%; }}
  .meta span {{ font-family: "JetBrains Mono", monospace; font-size: 10px; letter-spacing: .06em; text-transform: uppercase; padding: 3px 6px; border-radius: 2px; background: var(--tint-cool); color: var(--ink-med); white-space: nowrap; }}
  .meta .meta-tf {{ background: transparent; color: var(--ink-dim); padding-left: 0; padding-right: 0; }}
  .meta-directional_long {{ background: color-mix(in oklab, var(--bull) 15%, transparent); color: var(--bull); }}
  .meta-directional_short {{ background: color-mix(in oklab, var(--bear) 15%, transparent); color: var(--bear); }}
  .meta-bidirectional, .meta-no_trade, .meta-retrospective, .meta-not_a_chart {{ background: color-mix(in oklab, var(--neutral) 15%, transparent); color: var(--neutral); }}
  .stage-active {{ background: color-mix(in oklab, var(--accent) 20%, transparent); color: var(--accent); }}
  .stage-watching {{ background: var(--tint-warm); color: var(--ink-med); }}
  .stage-completed {{ background: transparent; border: 1px solid var(--rule); color: var(--ink-dim); }}
  .meta-conf {{ letter-spacing: 0; font-size: 9px; color: var(--ink-dim); background: transparent !important; padding: 0 !important; align-self: center; }}
  .chart {{ margin: 0; padding: 0; background: #000; display: flex; align-items: center; justify-content: center; max-height: 300px; overflow: hidden; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }}
  .chart img {{ width: 100%; height: auto; max-height: 300px; object-fit: contain; display: block; }}
  .body {{ padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 8px; }}
  .caption {{ margin: 0; font-size: 13.5px; line-height: 1.5; color: var(--ink); white-space: pre-wrap; }}
  .notes {{ margin: 0; font-size: 12.5px; line-height: 1.55; color: var(--ink-dim); font-style: italic; }}
  .setups {{ margin-top: 8px; padding-top: 12px; border-top: 1px dashed var(--rule); display: flex; flex-direction: column; gap: 8px; }}
  .setup-row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .dir {{ font-family: "JetBrains Mono", monospace; font-size: 9.5px; font-weight: 700; letter-spacing: .1em; padding: 3px 6px; border-radius: 2px; color: #fff; flex-shrink: 0; }}
  .dir-long {{ background: var(--pair-long); }}
  .dir-short {{ background: var(--pair-short); }}
  .lvls {{ display: flex; flex-wrap: wrap; gap: 4px 14px; font-family: "JetBrains Mono", monospace; font-size: 12px; }}
  .lvl {{ display: flex; align-items: baseline; gap: 5px; }}
  .lvl .k {{ font-size: 9.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--ink-dim); }}
  .lvl .v {{ color: var(--ink); font-weight: 500; }}
  .lvl.tgt .v {{ color: var(--accent); font-weight: 700; }}
  .inv {{ font-size: 11.5px; color: var(--ink-dim); font-style: italic; line-height: 1.45; border-left: 2px solid var(--rule); margin-left: 2px; padding-left: 8px; }}

  .commentary-list {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }}
  .commentary-list li {{ padding: 14px 18px; background: var(--paper-elev); border: 1px solid var(--rule); border-left: 3px solid var(--accent); }}
  .cmt-hd {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; font-family: "JetBrains Mono", monospace; font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-dim); }}
  .cmt-src {{ color: var(--accent); }}
  .cmt-body {{ font-family: "Iowan Old Style", Georgia, serif; font-size: 15.5px; line-height: 1.55; color: var(--ink); white-space: pre-wrap; }}

  footer {{ margin-top: 80px; padding-top: 24px; border-top: 3px double var(--ink); display: flex; justify-content: space-between; align-items: baseline; gap: 20px; flex-wrap: wrap; font-family: "JetBrains Mono", monospace; font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-dim); }}

  a:focus-visible, button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>

<div class="wrap">
  <div class="iss-picker" role="tablist" aria-label="Issues">{issue_picker}</div>
  {issue_blocks}
  <footer>
    <span>Source · manual:telegram-channel:ari_gold</span>
    <span>Extraction · claude-sonnet-5 (new) / 4-6 (cached)</span>
    <span>Multi-issue brief · latest first</span>
  </footer>
</div>

<script>
  (function() {{
    const issues = Array.from(document.querySelectorAll('.issue'));
    const pickers = Array.from(document.querySelectorAll('.iss-btn'));
    function activeIssue() {{ return issues.find(i => !i.hidden) || issues[0]; }}
    function tabsOf(issue) {{ return Array.from(issue.querySelectorAll('.tab')); }}
    function pagesOf(issue) {{ return Array.from(issue.querySelectorAll('.page')); }}

    function showPage(issue, pid, opts) {{
      opts = opts || {{}};
      const tabs = tabsOf(issue);
      const pages = pagesOf(issue);
      const ids = tabs.map(t => t.dataset.page);
      if (!ids.includes(pid)) pid = 'overview';
      tabs.forEach(t => {{
        const on = t.dataset.page === pid;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.setAttribute('tabindex', on ? '0' : '-1');
      }});
      pages.forEach(p => {{ p.hidden = (p.dataset.page !== pid); }});
      if (opts.pushHash !== false) {{
        try {{ history.replaceState(null, '', '#' + issue.dataset.issue + '/' + pid); }} catch(e) {{}}
      }}
      if (opts.scroll !== false) window.scrollTo({{ top: 0, behavior: opts.instant ? 'auto' : 'smooth' }});
    }}

    function showIssue(iid, pid, opts) {{
      opts = opts || {{}};
      const target = issues.find(i => i.dataset.issue === iid) || issues[0];
      issues.forEach(i => {{ i.hidden = (i !== target); }});
      pickers.forEach(b => b.classList.toggle('active', b.dataset.issue === target.dataset.issue));
      const tabs = tabsOf(target);
      const ids = tabs.map(t => t.dataset.page);
      if (!pid || !ids.includes(pid)) pid = 'overview';
      showPage(target, pid, opts);
    }}

    pickers.forEach(b => {{
      b.addEventListener('click', () => showIssue(b.dataset.issue, 'overview'));
    }});

    issues.forEach(iss => {{
      tabsOf(iss).forEach(t => {{
        t.addEventListener('click', () => showPage(iss, t.dataset.page));
      }});
      iss.querySelectorAll('[data-jump]').forEach(a => {{
        a.addEventListener('click', (e) => {{ e.preventDefault(); showPage(iss, a.dataset.jump); }});
      }});
    }});

    document.addEventListener('keydown', (e) => {{
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const iss = activeIssue();
      const tabs = tabsOf(iss);
      const cur = tabs.findIndex(t => t.classList.contains('active'));
      if (e.key === 'ArrowRight' && cur < tabs.length - 1) {{
        e.preventDefault(); showPage(iss, tabs[cur + 1].dataset.page);
      }} else if (e.key === 'ArrowLeft' && cur > 0) {{
        e.preventDefault(); showPage(iss, tabs[cur - 1].dataset.page);
      }}
    }});

    window.addEventListener('hashchange', () => {{
      const [iid, pid] = (location.hash || '#').slice(1).split('/');
      if (iid) showIssue(iid, pid, {{ pushHash: false }});
    }});

    const [initIid, initPid] = (location.hash || '#').slice(1).split('/');
    showIssue(initIid || issues[0].dataset.issue, initPid, {{ scroll: false, instant: true, pushHash: false }});
  }})();
</script>
"""


def main():
    OUT_DIR.mkdir(exist_ok=True)
    OUT.write_text(build(), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT}  ({kb:.1f} KB)")


if __name__ == "__main__":
    main()
