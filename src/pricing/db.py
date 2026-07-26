"""Connection, entity upserts, and immutable event writes.

Event tables (*_events) are append-only: nothing in this module issues
UPDATE or DELETE against them.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import psycopg

from pricing.config import database_url


def connect() -> psycopg.Connection:
    return psycopg.connect(database_url())


# ---------- operations ----------

def open_run(conn: psycopg.Connection, job: str) -> int:
    row = conn.execute(
        "insert into scrape_runs (job) values (%s) returning id", (job,)
    ).fetchone()
    return row[0]


def close_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    entities_seen: int,
    events_written: int,
    error: str | None = None,
) -> None:
    conn.execute(
        """update scrape_runs
           set finished_at = now(), status = %s,
               entities_seen = %s, events_written = %s, error = %s
           where id = %s""",
        (status, entities_seen, events_written, error, run_id),
    )


# ---------- entities ----------

def ensure_builder(
    conn: psycopg.Connection, slug: str, name: str, root_url: str, notes: str | None = None
) -> int:
    row = conn.execute("select id from builders where slug = %s", (slug,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "insert into builders (slug, name, root_url, notes) values (%s, %s, %s, %s) returning id",
        (slug, name, root_url, notes),
    ).fetchone()
    return row[0]


def upsert_community(conn: psycopg.Connection, builder_id: int, c: dict[str, Any]) -> int:
    """Insert or update a community; always bumps last_checked_at/last_seen_on."""
    row = conn.execute(
        "select id from communities where builder_id = %s and source_key = %s",
        (builder_id, c["source_key"]),
    ).fetchone()
    if row:
        conn.execute(
            """update communities
               set name = %s, url = %s, city = %s, zip = %s, status = 'active',
                   last_checked_at = now(), last_seen_on = current_date
               where id = %s""",
            (c["name"], c.get("url"), c.get("city"), c.get("zip"), row[0]),
        )
        return row[0]
    row = conn.execute(
        """insert into communities (builder_id, source_key, name, url, city, zip,
                                    last_checked_at, last_seen_on)
           values (%s, %s, %s, %s, %s, %s, now(), current_date)
           returning id""",
        (builder_id, c["source_key"], c["name"], c.get("url"), c.get("city"), c.get("zip")),
    ).fetchone()
    return row[0]


def upsert_plan(
    conn: psycopg.Connection, community_id: int, p: dict[str, Any]
) -> tuple[int, Decimal | None]:
    """Insert or update a plan. Returns (plan_id, previous current_base_price)."""
    row = conn.execute(
        "select id, current_base_price from plans where community_id = %s and source_key = %s",
        (community_id, p["source_key"]),
    ).fetchone()
    if row:
        conn.execute(
            """update plans
               set name = %s, sq_ft = %s, stories = %s, beds = %s, baths = %s,
                   garage = %s, identity_hash = %s,
                   last_checked_at = now(), last_seen_on = current_date
               where id = %s""",
            (
                p["name"], p.get("sq_ft"), p.get("stories"), p.get("beds"),
                p.get("baths"), p.get("garage"), p.get("identity_hash"), row[0],
            ),
        )
        return row[0], row[1]
    row = conn.execute(
        """insert into plans (community_id, source_key, name, sq_ft, stories, beds,
                              baths, garage, identity_hash,
                              last_checked_at, last_seen_on)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), current_date)
           returning id""",
        (
            community_id, p["source_key"], p["name"], p.get("sq_ft"), p.get("stories"),
            p.get("beds"), p.get("baths"), p.get("garage"), p.get("identity_hash"),
        ),
    ).fetchone()
    return row[0], None


def upsert_qmi(
    conn: psycopg.Connection, community_id: int, h: dict[str, Any]
) -> tuple[int, Decimal | None]:
    """Insert or update a QMI home. Returns (qmi_id, previous current_list_price).

    A home that reappears in the feed after being marked delisted gets its
    delisted_on cleared — the delisting was premature.
    """
    row = conn.execute(
        "select id, current_list_price from qmi_homes where community_id = %s and source_key = %s",
        (community_id, h["source_key"]),
    ).fetchone()
    if row:
        conn.execute(
            """update qmi_homes
               set address = %s, plan_id = %s, sq_ft = %s, est_completion = %s,
                   delisted_on = null,
                   last_checked_at = now(), last_seen_on = current_date
               where id = %s""",
            (
                h.get("address"), h.get("plan_id"), h.get("sq_ft"),
                h.get("est_completion"), row[0],
            ),
        )
        return row[0], row[1]
    row = conn.execute(
        """insert into qmi_homes (community_id, plan_id, source_key, address, sq_ft,
                                  est_completion, last_checked_at, last_seen_on)
           values (%s, %s, %s, %s, %s, %s, now(), current_date)
           returning id""",
        (
            community_id, h.get("plan_id"), h["source_key"], h.get("address"),
            h.get("sq_ft"), h.get("est_completion"),
        ),
    ).fetchone()
    return row[0], None


# ---------- immutable events ----------

def insert_plan_price_event(
    conn: psycopg.Connection,
    plan_id: int,
    base_price: Decimal | None,
    prev_price: Decimal | None,
    status: str | None,
    raw: dict[str, Any],
    extractor_id: int | None = None,
) -> None:
    conn.execute(
        """insert into plan_price_events
             (plan_id, extractor_id, observed_on, base_price, prev_price, status, raw)
           values (%s, %s, current_date, %s, %s, %s, %s)""",
        (plan_id, extractor_id, base_price, prev_price, status, json.dumps(raw)),
    )
    conn.execute(
        "update plans set current_base_price = %s where id = %s", (base_price, plan_id)
    )


def insert_qmi_price_event(
    conn: psycopg.Connection,
    qmi_id: int,
    list_price: Decimal | None,
    prev_price: Decimal | None,
    was_price: Decimal | None,
    status: str | None,
    raw: dict[str, Any],
    extractor_id: int | None = None,
) -> None:
    conn.execute(
        """insert into qmi_price_events
             (qmi_id, extractor_id, observed_on, list_price, prev_price, was_price, status, raw)
           values (%s, %s, current_date, %s, %s, %s, %s, %s)""",
        (qmi_id, extractor_id, list_price, prev_price, was_price, status, json.dumps(raw)),
    )
    conn.execute(
        "update qmi_homes set current_list_price = %s where id = %s", (list_price, qmi_id)
    )


# ---------- delisting ----------

def mark_delisted(
    conn: psycopg.Connection, community_id: int, seen_source_keys: set[str]
) -> int:
    """Mark DB rows absent from the fetched feed as delisted today.

    Callers must guarantee the fetch succeeded and returned a non-empty set —
    never call this off an empty or errored response.
    """
    if not seen_source_keys:
        raise ValueError("refusing to mark delistings from an empty fetch")
    cur = conn.execute(
        """update qmi_homes
           set delisted_on = current_date
           where community_id = %s
             and delisted_on is null
             and source_key != all(%s)""",
        (community_id, list(seen_source_keys)),
    )
    return cur.rowcount
