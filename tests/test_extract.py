"""Extraction tests against real captured payloads. No network access."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pricing.phase0_drhorton import (
    COMMUNITY_PATH,
    classify_models,
    extract_community,
    extract_models,
    extract_plan,
    extract_qmi,
)

FIXTURES = Path(__file__).parent / "fixtures" / "drhorton"


@pytest.fixture(scope="module")
def comms_payload():
    return json.loads((FIXTURES / "comms_san_antonio.json").read_text())


@pytest.fixture(scope="module")
def community_html():
    return (FIXTURES / "community_blue_ridge_ranch.html").read_text()


def test_extract_community(comms_payload):
    c = extract_community(comms_payload, COMMUNITY_PATH)
    assert c is not None
    assert c["name"] == "Blue Ridge Ranch"
    assert c["city"] == "San Antonio"
    assert c["zip"] == "78222"
    assert c["source_key"] == COMMUNITY_PATH


def test_extract_community_absent(comms_payload):
    assert extract_community(comms_payload, "/texas/nowhere") is None


def test_models_classify(community_html):
    models = extract_models(community_html)
    assert len(models) == 3
    plan_items, qmi_items = classify_models(models)
    assert plan_items is not None and len(plan_items) == 15
    assert qmi_items is not None and len(qmi_items) == 30


def test_extract_plan_fields(community_html):
    plan_items, _ = classify_models(extract_models(community_html))
    plans = [extract_plan(i) for i in plan_items]
    alamo = next(p for p in plans if p["name"] == "The Alamo")
    assert alamo["source_key"] == "t25a"
    assert alamo["sq_ft"] == 1190
    assert alamo["beds"] == Decimal("3")
    assert alamo["base_price"] == Decimal("242000")
    # every plan must have a usable key and price
    assert all(p["source_key"] for p in plans)
    assert len({p["source_key"] for p in plans}) == len(plans)


def test_extract_qmi_fields(community_html):
    _, qmi_items = classify_models(extract_models(community_html))
    qmis = [extract_qmi(i) for i in qmi_items]
    first = next(q for q in qmis if q["address"] == "5926 Celestite Bend")
    assert first["list_price"] == Decimal("228999")
    assert first["was_price"] == Decimal("245000")
    assert first["status"] == "Available"
    assert first["plan_code"] == "t25a"
    assert first["sq_ft"] == 1190
    assert len({q["source_key"] for q in qmis}) == len(qmis)
    # OriginalPrice=0 must not surface as a was-price of 0
    zeroes = [q for q in qmis if q["raw"].get("OriginalPrice") == 0]
    assert zeroes and all(q["was_price"] is None for q in zeroes)
