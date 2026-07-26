"""Phase 0: hardcoded scraper for one D.R. Horton community (Blue Ridge Ranch, San Antonio).

No config abstraction yet — this exists to prove the schema and the
change-only write path against real payload shapes before Phase 1
generalizes it.

Data sources (discovered 2026-07-26, robots.txt reviewed — no rules cover
these paths):
  1. GET /api/comms/direct/texas/san-antonio      -> JSON, all SA communities
  2. GET <community page>                          -> HTML with three embedded
     `var model = {...}` JSON blobs: QMI homes, floorplans, nearby communities

Usage:
  python -m pricing.phase0_drhorton                       # live fetch
  python -m pricing.phase0_drhorton --from-fixtures DIR   # replay saved payloads
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from pricing import db
from pricing.config import RATE_LIMIT_MS, USER_AGENT
from pricing.normalize import identity_hash, parse_decimal, parse_int, parse_price

BUILDER_SLUG = "drhorton"
BUILDER_NAME = "D.R. Horton"
ROOT_URL = "https://www.drhorton.com"
BUILDER_NOTES = (
    "robots.txt checked 2026-07-26: 95 Disallow rules, none covering /api/comms, "
    "/api/qmis, or /texas/* paths; no Crawl-delay. Prices do not include lot premium "
    "on plan base prices; QMI prices are all-in. Community list via /api/comms/direct, "
    "plans+QMI parsed from JSON embedded in the community page (no Playwright needed)."
)

COMMS_PATH = "/api/comms/direct/texas/san-antonio"
COMMUNITY_PATH = "/texas/san-antonio/san-antonio/blue-ridge-ranch"

# fixture filenames (also the shapes captured in tests/fixtures/drhorton/)
FIXTURE_COMMS = "comms_san_antonio.json"
FIXTURE_COMMUNITY = "community_blue_ridge_ranch.html"


# ---------- fetch ----------

def fetch_live() -> tuple[dict[str, Any], str]:
    import httpx

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"}
    with httpx.Client(base_url=ROOT_URL, headers=headers, timeout=60, follow_redirects=True) as client:
        comms = client.get(COMMS_PATH)
        comms.raise_for_status()
        time.sleep(RATE_LIMIT_MS / 1000)
        page = client.get(COMMUNITY_PATH)
        page.raise_for_status()
    return comms.json(), page.text


def fetch_fixtures(fixture_dir: Path) -> tuple[dict[str, Any], str]:
    comms = json.loads((fixture_dir / FIXTURE_COMMS).read_text())
    html = (fixture_dir / FIXTURE_COMMUNITY).read_text()
    return comms, html


# ---------- extract ----------

def _balanced_json(s: str, start: int) -> str | None:
    """Return the balanced {...} object starting at s[start] ('{')."""
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(s)):
        ch = s[j]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if in_str:
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : j + 1]
    return None


def extract_models(html: str) -> list[dict[str, Any]]:
    """Pull every `var model = {...}` JSON blob out of the page."""
    models = []
    for m in re.finditer(r"var model = ", html):
        brace = html.find("{", m.end())
        if brace == -1:
            continue
        blob = _balanced_json(html, brace)
        if blob:
            models.append(json.loads(blob))
    return models


def classify_models(
    models: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """Return (plan_items, qmi_items); None means that blob was not found.

    QMI items carry an Address; floorplan items carry a PlanName but no
    Address; the nearby-communities blob (CommunityName) is ignored.
    """
    plan_items = None
    qmi_items = None
    for model in models:
        items = model.get("Items")
        if not items:
            continue
        first = items[0]
        if "CommunityName" in first:
            continue
        if "Address" in first:
            qmi_items = items
        elif "PlanName" in first:
            plan_items = items
    return plan_items, qmi_items


_CITY_ZIP_RE = re.compile(r"^(.*?),?\s*([A-Z]{2})[,\s]+(\d{5})")


def extract_community(comms_payload: dict[str, Any], page_link: str) -> dict[str, Any] | None:
    for c in comms_payload.get("CommunityData", []):
        if c.get("commPageLink") != page_link:
            continue
        city = zip_code = None
        m = _CITY_ZIP_RE.match(c.get("commAddress") or "")
        if m:
            city, _, zip_code = m.groups()
        return {
            "source_key": page_link,
            "name": c["commName"],
            "url": ROOT_URL + page_link,
            "city": city,
            "zip": zip_code,
            "raw": c,
        }
    return None


def extract_plan(item: dict[str, Any]) -> dict[str, Any]:
    sq_ft = parse_int(item.get("SquareFootage"))
    beds = parse_decimal(item.get("NumberOfBedrooms"))
    baths = parse_decimal(item.get("NumberOfBathrooms"))
    stories = parse_int(item.get("NumberOfStories"))
    return {
        "source_key": (item.get("PlanCode") or item["ItemId"]).lower(),
        "name": item["PlanName"],
        "sq_ft": sq_ft,
        "stories": stories,
        "beds": beds,
        "baths": baths,
        "garage": parse_int(item.get("NumberOfGarages")),
        "base_price": parse_price(item.get("Price")),
        "identity_hash": identity_hash(sq_ft, beds, baths, stories),
        "raw": item,
    }


def extract_qmi(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_key": item["ItemId"],
        "address": item.get("Address"),
        "sq_ft": parse_int(item.get("SquareFootage")),
        "list_price": parse_price(item.get("Price")),
        "was_price": parse_price(item.get("OriginalPrice")),  # 0 -> None
        "status": item.get("Status"),
        "plan_code": (item.get("PlanCode") or "").lower() or None,
        "est_completion": None,  # not exposed by this builder's payload
        "raw": item,
    }


# ---------- pipeline ----------

def run(conn, comms_payload: dict[str, Any], community_html: str) -> dict[str, int]:
    """Upsert entities, write change-only events, detect delistings.

    Returns counters. Raises on payload shapes it cannot interpret; the
    caller decides how to record the failure.
    """
    counters = {"entities": 0, "events": 0, "delisted": 0}

    builder_id = db.ensure_builder(conn, BUILDER_SLUG, BUILDER_NAME, ROOT_URL, BUILDER_NOTES)

    community = extract_community(comms_payload, COMMUNITY_PATH)
    if community is None:
        raise RuntimeError(f"community {COMMUNITY_PATH} not present in comms payload")
    community_id = db.upsert_community(conn, builder_id, community)
    counters["entities"] += 1

    plan_items, qmi_items = classify_models(extract_models(community_html))
    if plan_items is None:
        raise RuntimeError("no floorplan model found in community page")

    # plans first so QMIs can link plan_id via PlanCode
    plan_ids: dict[str, int] = {}
    for item in plan_items:
        plan = extract_plan(item)
        plan_id, prev_price = db.upsert_plan(conn, community_id, plan)
        plan_ids[plan["source_key"]] = plan_id
        counters["entities"] += 1
        if plan["base_price"] != prev_price:
            db.insert_plan_price_event(
                conn, plan_id, plan["base_price"], prev_price, None, plan["raw"]
            )
            counters["events"] += 1

    # QMI homes. qmi_items is None when the page had no QMI blob — treat as
    # "unknown", never as "everything sold".
    seen_keys: set[str] = set()
    for item in qmi_items or []:
        qmi = extract_qmi(item)
        qmi["plan_id"] = plan_ids.get(qmi["plan_code"])
        qmi_id, prev_price = db.upsert_qmi(conn, community_id, qmi)
        seen_keys.add(qmi["source_key"])
        counters["entities"] += 1
        if qmi["list_price"] != prev_price:
            db.insert_qmi_price_event(
                conn, qmi_id, qmi["list_price"], prev_price, qmi["was_price"],
                qmi["status"], qmi["raw"],
            )
            counters["events"] += 1

    # Delisting: only off a fetch that actually returned homes.
    if seen_keys:
        counters["delisted"] = db.mark_delisted(conn, community_id, seen_keys)

    return counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-fixtures",
        metavar="DIR",
        help="replay saved payloads from DIR instead of fetching the live site",
    )
    args = parser.parse_args(argv)

    conn = db.connect()
    run_id = db.open_run(conn, "phase0")
    conn.commit()
    try:
        if args.from_fixtures:
            comms_payload, community_html = fetch_fixtures(Path(args.from_fixtures))
        else:
            comms_payload, community_html = fetch_live()
        counters = run(conn, comms_payload, community_html)
    except Exception as exc:  # any failure: no partial writes for this builder
        conn.rollback()
        db.close_run(conn, run_id, "error", 0, 0, f"{type(exc).__name__}: {exc}")
        conn.commit()
        print(f"error: {exc}", file=sys.stderr)
        return 1

    db.close_run(conn, run_id, "ok", counters["entities"], counters["events"])
    conn.commit()
    conn.close()
    print(
        f"ok: entities={counters['entities']} events={counters['events']} "
        f"delisted={counters['delisted']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
