"""Weekly change report.

Reads the accumulated event history and renders a summary: price movements
with direction and %, median $/sf by submarket against 30 days prior, new
communities, and delisted inventory with days on site.

Because the database stores only changes, "what was the price 30 days ago"
is answered by the plan_price_on() helper rather than by a stored snapshot.

Usage:
  python -m pricing.digest                # last 7 days, print to stdout
  python -m pricing.digest --days 30      # wider window
  python -m pricing.digest --dry-run      # never post, even if a webhook is set

Posts to SLACK_WEBHOOK_URL when that variable is set (and --dry-run is not).
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row

from pricing import db

# ---------- queries ----------

_PRICE_CHANGES = """
select 'plan' as kind, b.name as builder,
       coalesce(c.submarket, c.city, 'Unassigned') as market,
       c.name as community, p.name as label, p.sq_ft,
       e.observed_on, e.prev_price, e.base_price as new_price
from plan_price_events e
join plans p on p.id = e.plan_id
join communities c on c.id = p.community_id
join builders b on b.id = c.builder_id
where e.observed_on >= current_date - %(days)s::int
  and e.prev_price is not null
  and e.base_price is not null
union all
select 'qmi' as kind, b.name as builder,
       coalesce(c.submarket, c.city, 'Unassigned') as market,
       c.name as community, h.address as label, h.sq_ft,
       e.observed_on, e.prev_price, e.list_price as new_price
from qmi_price_events e
join qmi_homes h on h.id = e.qmi_id
join communities c on c.id = h.community_id
join builders b on b.id = c.builder_id
where e.observed_on >= current_date - %(days)s::int
  and e.prev_price is not null
  and e.list_price is not null
order by 8 desc, 2, 4
"""

# Median $/sf per market, now vs. a prior date reconstructed from the events.
_PSF_TREND = """
with psf as (
  select coalesce(c.submarket, c.city, 'Unassigned') as market,
         p.current_base_price / nullif(p.sq_ft, 0) as psf_now,
         plan_price_on(p.id, current_date - %(back)s::int)
           / nullif(p.sq_ft, 0) as psf_then
  from plans p
  join communities c on c.id = p.community_id
  where p.sq_ft > 0
)
select market,
       percentile_cont(0.5) within group (order by psf_now)
         filter (where psf_now is not null) as median_now,
       percentile_cont(0.5) within group (order by psf_then)
         filter (where psf_then is not null) as median_then,
       count(psf_now) as n_now,
       count(psf_then) as n_then
from psf
group by market
order by market
"""

_DELISTINGS = """
select builder, coalesce(submarket, 'Unassigned') as market, community,
       address, sq_ft, first_seen, delisted_on, days_on_site, current_list_price
from v_qmi_absorption
where delisted_on >= current_date - %(days)s::int
order by days_on_site
"""

_NEW_COMMUNITIES = """
select b.name as builder, c.name as community,
       coalesce(c.submarket, c.city, 'Unassigned') as market,
       c.first_seen,
       (select count(*) from plans p where p.community_id = c.id) as plans,
       (select count(*) from qmi_homes h
         where h.community_id = c.id and h.delisted_on is null) as homes
from communities c
join builders b on b.id = c.builder_id
where c.first_seen >= current_date - %(days)s::int
order by c.first_seen desc, b.name
"""

_NEW_LISTINGS = """
select count(*) as n
from qmi_price_events
where observed_on >= current_date - %(days)s::int and prev_price is null
"""

_COVERAGE = """
select (select count(*) from builders) as builders,
       (select count(*) from communities where status = 'active') as communities,
       (select count(*) from plans) as plans,
       (select count(*) from qmi_homes where delisted_on is null) as active_homes,
       (select max(last_checked_at)::date from plans) as last_checked,
       (select min(observed_on) from plan_price_events) as tracking_since
"""

_FAILED_RUNS = """
select job, status, started_at::date as day, error
from scrape_runs
where status <> 'ok' and started_at >= current_date - %(days)s::int
order by started_at desc
limit 10
"""


def build(conn, days: int = 7, psf_lookback: int = 30) -> dict[str, Any]:
    """Collect every section of the digest as plain data."""
    cur = conn.cursor(row_factory=dict_row)
    params = {"days": days, "back": psf_lookback}
    return {
        "days": days,
        "psf_lookback": psf_lookback,
        "generated_on": date.today(),
        "changes": cur.execute(_PRICE_CHANGES, params).fetchall(),
        "psf": cur.execute(_PSF_TREND, params).fetchall(),
        "delistings": cur.execute(_DELISTINGS, params).fetchall(),
        "new_communities": cur.execute(_NEW_COMMUNITIES, params).fetchall(),
        "new_listings": cur.execute(_NEW_LISTINGS, params).fetchone()["n"],
        "coverage": cur.execute(_COVERAGE).fetchone(),
        "failures": cur.execute(_FAILED_RUNS, params).fetchall(),
    }


# ---------- rendering ----------

def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.0f}"


def _psf(value: Decimal | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _pct(new: Decimal, prev: Decimal) -> str:
    if not prev:
        return "—"
    return f"{(new - prev) / prev * 100:+.1f}%"


def _headline(data: dict[str, Any]) -> str:
    cuts = sum(1 for c in data["changes"] if c["new_price"] < c["prev_price"])
    raises = sum(1 for c in data["changes"] if c["new_price"] > c["prev_price"])
    sold = len(data["delistings"])
    bits = []
    if cuts:
        bits.append(f"{cuts} price cut{'s' if cuts != 1 else ''}")
    if raises:
        bits.append(f"{raises} increase{'s' if raises != 1 else ''}")
    if sold:
        bits.append(f"{sold} home{'s' if sold != 1 else ''} off the market")
    if data["new_listings"]:
        bits.append(f"{data['new_listings']} new listing{'s' if data['new_listings'] != 1 else ''}")
    if data["new_communities"]:
        n = len(data["new_communities"])
        bits.append(f"{n} new communit{'ies' if n != 1 else 'y'}")
    return ", ".join(bits) if bits else "No pricing movement"


def render(data: dict[str, Any]) -> str:
    days = data["days"]
    out: list[str] = []
    out.append(f"San Antonio builder pricing — {days}-day digest")
    out.append(f"{data['generated_on']:%b %d, %Y}  ·  {_headline(data)}")
    out.append("")

    # --- price movement ---
    if data["changes"]:
        out.append(f"PRICE CHANGES ({len(data['changes'])})")
        for c in data["changes"]:
            arrow = "▼" if c["new_price"] < c["prev_price"] else "▲"
            delta = c["new_price"] - c["prev_price"]
            psf = ""
            if c["sq_ft"]:
                psf = f"  ({_psf(c['new_price'] / c['sq_ft'])}/sf)"
            out.append(
                f"  {arrow} {c['community']} · {c['label']} [{c['kind']}]"
            )
            out.append(
                f"      {_money(c['prev_price'])} → {_money(c['new_price'])}  "
                f"{_money(abs(delta))} {_pct(c['new_price'], c['prev_price'])}"
                f"{psf}   {c['observed_on']:%b %d}"
            )
        out.append("")

    # --- $/sf trend ---
    if data["psf"]:
        out.append(f"MEDIAN $/SF BY SUBMARKET (vs. {data['psf_lookback']} days prior)")
        for r in data["psf"]:
            if r["median_then"] is not None and r["median_now"] is not None:
                trend = _pct(r["median_now"], r["median_then"])
                was = f"was {_psf(r['median_then'])}, {trend}"
            else:
                was = "no prior baseline"
            out.append(
                f"  {r['market']}: {_psf(r['median_now'])}/sf  "
                f"({r['n_now']} plans, {was})"
            )
        out.append("")

    # --- absorption ---
    if data["delistings"]:
        out.append(f"OFF THE MARKET ({len(data['delistings'])})")
        for d in data["delistings"]:
            out.append(
                f"  {d['community']} · {d['address']}  "
                f"{_money(d['current_list_price'])}  "
                f"{d['sq_ft'] or '—'} sf  ·  {d['days_on_site']} days on site"
            )
        avg = sum(d["days_on_site"] for d in data["delistings"]) / len(data["delistings"])
        out.append(f"  Average days on site: {avg:.0f}")
        out.append("")

    # --- new communities ---
    if data["new_communities"]:
        out.append(f"NEW COMMUNITIES ({len(data['new_communities'])})")
        for n in data["new_communities"]:
            out.append(
                f"  {n['builder']} · {n['community']} ({n['market']})  "
                f"{n['plans']} plans, {n['homes']} homes  ·  first seen {n['first_seen']}"
            )
        out.append("")

    # --- scrape health ---
    if data["failures"]:
        out.append(f"SCRAPE FAILURES ({len(data['failures'])})")
        for f in data["failures"]:
            out.append(f"  {f['day']} {f['job']} [{f['status']}] {f['error'] or ''}".rstrip())
        out.append("")

    cov = data["coverage"]
    if cov["tracking_since"]:
        span = (data["generated_on"] - cov["tracking_since"]).days
        since = f"tracking since {cov['tracking_since']} ({span}d)"
    else:
        since = "no history yet"
    communities = f"{cov['communities']} communit{'y' if cov['communities'] == 1 else 'ies'}"
    builders = f"{cov['builders']} builder{'' if cov['builders'] == 1 else 's'}"
    out.append(
        f"Tracking {cov['plans']} plans and {cov['active_homes']} homes across "
        f"{communities} / {builders} · "
        f"last checked {cov['last_checked'] or '—'} · {since}"
    )
    return "\n".join(out)


# ---------- delivery ----------

def post_to_slack(webhook_url: str, text: str) -> None:
    import httpx

    resp = httpx.post(webhook_url, json={"text": f"```\n{text}\n```"}, timeout=30)
    resp.raise_for_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly pricing digest")
    parser.add_argument("--days", type=int, default=7, help="reporting window (default 7)")
    parser.add_argument(
        "--psf-lookback", type=int, default=30, help="$/sf comparison window (default 30)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print only; never post to Slack"
    )
    args = parser.parse_args(argv)

    with db.connect() as conn:
        text = render(build(conn, args.days, args.psf_lookback))

    print(text)

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook and not args.dry_run:
        post_to_slack(webhook, text)
        print("\n(posted to Slack)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
