"""The committed fixtures must be exactly what the seeded generator produces."""

from __future__ import annotations

from pathlib import Path

from ai_cost_margin.fixtures import FIXTURE_SEED, build_fixture_set

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase1"


def test_generator_reproduces_the_committed_fixtures():
    generated = build_fixture_set(FIXTURE_SEED)

    assert generated, "the generator produced no files"
    for name, text in generated.items():
        assert (FIXTURES / name).read_bytes() == text.encode("utf-8"), name


def test_generator_is_deterministic_within_a_process():
    assert build_fixture_set(FIXTURE_SEED) == build_fixture_set(FIXTURE_SEED)


def test_committed_fixture_set_has_no_stray_batch_files():
    committed = {path.name for path in FIXTURES.glob("*.json")}
    assert committed == set(build_fixture_set(FIXTURE_SEED))
