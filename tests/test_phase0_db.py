"""End-to-end pipeline tests against a throwaway Postgres.

Set TEST_DATABASE_URL to run these (they are skipped otherwise so the suite
stays green in environments without a database). They never touch the network.
"""

import copy
import json
import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from pricing import phase0_drhorton  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "drhorton"
TEST_DSN = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not set")

TABLES = [
    "qmi_price_events", "plan_price_events", "incentive_events", "scrape_runs",
    "qmi_homes", "plans", "communities", "extractors", "builders",
]


@pytest.fixture()
def conn():
    with psycopg.connect(TEST_DSN) as c:
        c.execute("truncate " + ", ".join(TABLES) + " restart identity cascade")
        c.commit()
        yield c


@pytest.fixture(scope="module")
def payloads():
    comms = json.loads((FIXTURES / "comms_san_antonio.json").read_text())
    html = (FIXTURES / "community_blue_ridge_ranch.html").read_text()
    return comms, html


def counts(conn):
    def one(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "communities": one("select count(*) from communities"),
        "plans": one("select count(*) from plans"),
        "qmis": one("select count(*) from qmi_homes"),
        "plan_events": one("select count(*) from plan_price_events"),
        "qmi_events": one("select count(*) from qmi_price_events"),
        "delisted": one("select count(*) from qmi_homes where delisted_on is not null"),
    }


def test_first_run_populates(conn, payloads):
    result = phase0_drhorton.run(conn, *payloads)
    conn.commit()
    c = counts(conn)
    assert c["communities"] == 1
    assert c["plans"] == 15
    assert c["qmis"] == 30
    # first sight of every entity is an event with prev_price null
    assert c["plan_events"] == 15
    assert c["qmi_events"] == 30
    assert result["events"] == 45
    nulls = conn.execute("select count(*) from plan_price_events where prev_price is null").fetchone()[0]
    assert nulls == 15


def test_second_run_writes_zero_events(conn, payloads):
    phase0_drhorton.run(conn, *payloads)
    conn.commit()
    before = counts(conn)
    result = phase0_drhorton.run(conn, *payloads)
    conn.commit()
    after = counts(conn)
    assert result["events"] == 0
    assert after["plan_events"] == before["plan_events"]
    assert after["qmi_events"] == before["qmi_events"]
    assert after["delisted"] == 0
    # but the entities were still touched
    stale = conn.execute("select count(*) from plans where last_checked_at is null").fetchone()[0]
    assert stale == 0


def test_price_change_writes_one_event_with_prev(conn, payloads):
    comms, html = payloads
    phase0_drhorton.run(conn, comms, html)
    conn.commit()
    # simulate a $5k cut on The Alamo QMI at 5926 Celestite Bend
    html2 = html.replace('"Price":228999', '"Price":223999')
    result = phase0_drhorton.run(conn, comms, html2)
    conn.commit()
    assert result["events"] == 1
    row = conn.execute(
        """select e.list_price, e.prev_price from qmi_price_events e
           join qmi_homes h on h.id = e.qmi_id
           where h.address = '5926 Celestite Bend'
           order by e.id desc limit 1"""
    ).fetchone()
    assert float(row[0]) == 223999
    assert float(row[1]) == 228999
    cur = conn.execute(
        "select current_list_price from qmi_homes where address = '5926 Celestite Bend'"
    ).fetchone()
    assert float(cur[0]) == 223999


def test_delisting_detected_when_home_disappears(conn, payloads):
    comms, html = payloads
    phase0_drhorton.run(conn, comms, html)
    conn.commit()
    # drop one home from the QMI model only (the plans/nearby blobs are untouched)
    models = phase0_drhorton.extract_models(html)
    plan_items, qmi_items = phase0_drhorton.classify_models(models)
    victim = qmi_items[0]["ItemId"]
    trimmed = copy.deepcopy(qmi_items[1:])
    html2 = _rebuild_html_with_qmis(html, trimmed)
    result = phase0_drhorton.run(conn, comms, html2)
    conn.commit()
    assert result["delisted"] == 1
    row = conn.execute(
        "select delisted_on from qmi_homes where source_key = %s", (victim,)
    ).fetchone()
    assert row[0] is not None


def test_empty_qmi_feed_never_delists(conn, payloads):
    comms, html = payloads
    phase0_drhorton.run(conn, comms, html)
    conn.commit()
    html2 = _rebuild_html_with_qmis(html, [])
    result = phase0_drhorton.run(conn, comms, html2)
    conn.commit()
    assert result["delisted"] == 0
    delisted = conn.execute(
        "select count(*) from qmi_homes where delisted_on is not null"
    ).fetchone()[0]
    assert delisted == 0


def _rebuild_html_with_qmis(html: str, qmi_items: list) -> str:
    """Replace the QMI model's Items in the raw HTML with the given list."""
    import re

    for m in re.finditer(r"var model = ", html):
        brace = html.find("{", m.end())
        blob = phase0_drhorton._balanced_json(html, brace)
        if not blob:
            continue
        model = json.loads(blob)
        items = model.get("Items")
        if items and "Address" in items[0]:
            model["Items"] = qmi_items
            model["TotalItems"] = len(qmi_items)
            return html[:brace] + json.dumps(model) + html[brace + len(blob):]
    raise AssertionError("QMI model not found in html")
