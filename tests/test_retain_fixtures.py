"""Runs the shared golden fixtures (tests/fixtures/retain/*.json) through
merge_into_inventory. These same JSON files are consumed by the
bsky-saves-gui test suite as the cross-implementation anti-drift contract.
See the v0.6.0 spec section 10.4."""
from __future__ import annotations

import json
import pathlib

import pytest

from bsky_saves.normalize import merge_into_inventory

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "retain"
_FIXTURES = sorted(_FIXTURE_DIR.glob("*.json"))


def test_fixture_directory_is_populated():
    assert _FIXTURES, "no golden fixtures found under tests/fixtures/retain/"


@pytest.mark.parametrize("fixture_path", _FIXTURES, ids=lambda p: p.stem)
def test_retain_golden_fixture(fixture_path):
    case = json.loads(fixture_path.read_text(encoding="utf-8"))
    result = merge_into_inventory(
        case["prior_inventory"],
        case["fetch_records"],
        mode=case["mode"],
        now=case["now"],
    )
    assert result == case["expected_output_inventory"], case["description"]
    # Idempotency (spec section 10.4): re-running the reconcile on its own
    # output with the same fetch must yield a stable result.
    second = merge_into_inventory(
        result,
        case["fetch_records"],
        mode=case["mode"],
        now=case["now"],
    )
    assert second == result, f"{case['description']} (not idempotent)"
