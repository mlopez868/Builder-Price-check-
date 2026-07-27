"""Digest tests against a throwaway Postgres with seeded history.

Set TEST_DATABASE_URL to run. No network access.
"""

import os
from datetime import date, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

from pricing import digest  # noqa: E402

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


def seed(conn, *, today=None):
    """Build a small world with 45 days of history.

    One community, two plans (one repriced 10 days ago), two QMI homes
    (one discounted 3 days ago, one delisted 2 days ago after 40 days).
    """
    today = today or date.today()

    def d(days_ago):
        return today - timedelta(days=days_ago)

    builder_id = conn.execute(
        "insert into builders (slug, name) values ('acme', 'Acme Homes') returning id"
    ).fetchone()[0]
    community_id = conn.execute(
        """insert into communities (builder_id, source_key, name, submarket, city,
                                    first_seen, last_checked_at, last_seen_on)
           values (%s, 'c1', 'Cypress Run', 'Far West', 'San Antonio', %s, now(), current_date)
           returning id""",
        (builder_id, d(45)),
    ).fetchone()[0]

    # plan A: 2000 sf, $400k for 45 days, cut to $380k ten days ago -> $190/sf
    plan_a = conn.execute(
        """insert into plans (community_id, source_key, name, sq_ft, current_base_price,
                              first_seen, last_checked_at, last_seen_on)
           values (%s, 'pa', 'The Cypress', 2000, 380000, %s, now(), current_date)
           returning id""",
        (community_id, d(45)),
    ).fetchone()[0]
    conn.execute(
        """insert into plan_price_events (plan_id, observed_on, base_price, prev_price)
           values (%s, %s, 400000, null), (%s, %s, 380000, 400000)""",
        (plan_a, d(45), plan_a, d(10)),
    )

    # plan B: 1000 sf, flat $250k -> $250/sf, no change in window
    plan_b = conn.execute(
        """insert into plans (community_id, source_key, name, sq_ft, current_base_price,
                              first_seen, last_checked_at, last_seen_on)
           values (%s, 'pb', 'The Laurel', 1000, 250000, %s, now(), current_date)
           returning id""",
        (community_id, d(45)),
    ).fetchone()[0]
    conn.execute(
        """insert into plan_price_events (plan_id, observed_on, base_price, prev_price)
           values (%s, %s, 250000, null)""",
        (plan_b, d(45)),
    )

    # QMI 1: discounted 3 days ago, still listed
    qmi_1 = conn.execute(
        """insert into qmi_homes (community_id, source_key, address, sq_ft,
                                  current_list_price, first_seen, last_checked_at, last_seen_on)
           values (%s, 'h1', '123 Oak', 2000, 405000, %s, now(), current_date)
           returning id""",
        (community_id, d(20)),
    ).fetchone()[0]
    conn.execute(
        """insert into qmi_price_events (qmi_id, observed_on, list_price, prev_price)
           values (%s, %s, 420000, null), (%s, %s, 405000, 420000)""",
        (qmi_1, d(20), qmi_1, d(3)),
    )

    # QMI 2: listed 42 days ago, delisted 2 days ago -> 40 days on site
    qmi_2 = conn.execute(
        """insert into qmi_homes (community_id, source_key, address, sq_ft,
                                  current_list_price, first_seen, last_checked_at,
                                  last_seen_on, delisted_on)
           values (%s, 'h2', '456 Elm', 1800, 360000, %s, now(), current_date, %s)
           returning id""",
        (community_id, d(42), d(2)),
    ).fetchone()[0]
    conn.execute(
        """insert into qmi_price_events (qmi_id, observed_on, list_price, prev_price)
           values (%s, %s, 360000, null)""",
        (qmi_2, d(42)),
    )
    conn.commit()
    return {"community": community_id, "plan_a": plan_a, "qmi_1": qmi_1, "qmi_2": qmi_2}


def test_price_changes_within_window(conn):
    seed(conn)
    data = digest.build(conn, days=7)
    # only the QMI discount (3 days ago) falls inside a 7-day window;
    # the plan cut was 10 days ago
    assert len(data["changes"]) == 1
    change = data["changes"][0]
    assert change["kind"] == "qmi"
    assert change["label"] == "123 Oak"
    assert float(change["prev_price"]) == 420000
    assert float(change["new_price"]) == 405000


def test_wider_window_picks_up_plan_cut(conn):
    seed(conn)
    data = digest.build(conn, days=14)
    kinds = sorted(c["kind"] for c in data["changes"])
    assert kinds == ["plan", "qmi"]


def test_first_sight_is_not_a_price_change(conn):
    seed(conn)
    data = digest.build(conn, days=60)
    # six events exist, but only two have a prev_price
    total = conn.execute(
        "select (select count(*) from plan_price_events) + (select count(*) from qmi_price_events)"
    ).fetchone()[0]
    assert total == 6
    assert len(data["changes"]) == 2


def test_psf_trend_reconstructs_prior_price(conn):
    seed(conn)
    data = digest.build(conn, days=7, psf_lookback=30)
    row = next(r for r in data["psf"] if r["market"] == "Far West")
    # now: median of [190, 250] = 220; 30 days ago plan A was still 400k -> [200, 250] = 225
    assert float(row["median_now"]) == pytest.approx(220.0)
    assert float(row["median_then"]) == pytest.approx(225.0)
    assert row["n_now"] == 2


def test_psf_trend_without_baseline(conn):
    seed(conn)
    # 90 days back predates all history, so there is no prior median
    data = digest.build(conn, days=7, psf_lookback=90)
    row = next(r for r in data["psf"] if r["market"] == "Far West")
    assert row["median_then"] is None
    assert row["median_now"] is not None
    assert "no prior baseline" in digest.render(data)


def test_delisting_reports_days_on_site(conn):
    seed(conn)
    data = digest.build(conn, days=7)
    assert len(data["delistings"]) == 1
    assert data["delistings"][0]["address"] == "456 Elm"
    assert data["delistings"][0]["days_on_site"] == 40


def test_new_communities_only_when_recent(conn):
    seed(conn)
    assert digest.build(conn, days=7)["new_communities"] == []
    fresh = digest.build(conn, days=60)["new_communities"]
    assert len(fresh) == 1
    assert fresh[0]["community"] == "Cypress Run"
    assert fresh[0]["plans"] == 2


def test_render_headline_and_sections(conn):
    seed(conn)
    text = digest.render(digest.build(conn, days=14))
    assert "1 price cut" in text or "2 price cuts" in text
    assert "1 home off the market" in text
    assert "PRICE CHANGES" in text
    assert "OFF THE MARKET" in text
    assert "40 days on site" in text
    assert "-3.6%" in text  # 420k -> 405k
    assert "Far West" in text


def test_render_empty_period_is_still_useful(conn):
    seed(conn)
    # a window with no activity at all
    data = digest.build(conn, days=1)
    text = digest.render(data)
    assert "No pricing movement" in text
    # coverage footer still tells the operator what is being watched
    assert "Tracking 2 plans" in text
    assert "PRICE CHANGES" not in text


def test_failed_runs_surface(conn):
    seed(conn)
    conn.execute(
        """insert into scrape_runs (job, status, error, started_at)
           values ('phase0', 'error', 'HTTPStatusError: 503', now())"""
    )
    conn.commit()
    text = digest.render(digest.build(conn, days=7))
    assert "SCRAPE FAILURES" in text
    assert "503" in text
