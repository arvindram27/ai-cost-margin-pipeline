"""Phase 0 smoke test: the skeleton imports, the pinned dependencies are the ones
installed, and money arithmetic is exact.

This test makes no network calls, calls no provider, and writes nothing outside
pytest's own cache directory.
"""

from decimal import Decimal
from importlib.metadata import version

import duckdb
import pytest

import ai_cost_margin

# The three direct dependencies, exactly as pinned in requirements.txt.
PINS = {
    "streamlit": "1.67.0",
    "duckdb": "2.0.0",
    "pytest": "9.2.0",
}


@pytest.mark.parametrize(("distribution", "expected"), sorted(PINS.items()))
def test_pinned_distribution_is_installed(distribution: str, expected: str) -> None:
    assert version(distribution) == expected


def test_package_imports_and_reports_version() -> None:
    assert ai_cost_margin.__version__ == "0.0.0"


def test_duckdb_decimal_money_is_exact() -> None:
    """Money uses DECIMAL, so 0.10 + 0.20 is exactly 0.30 and never a float."""
    connection = duckdb.connect(":memory:")
    try:
        value = connection.execute(
            "SELECT CAST('0.10' AS DECIMAL(18,2)) + CAST('0.20' AS DECIMAL(18,2))"
        ).fetchone()[0]
    finally:
        connection.close()

    assert isinstance(value, Decimal)
    assert value == Decimal("0.30")
