"""Task 5: cost from the price table in `plane.yaml`, cache tokens included (SPEC.md 8.2)."""

from pathlib import Path

import pytest

from control_plane.llm import ModelPrice, PriceTable, PriceTableError, UnknownModelError, Usage
from control_plane.store.home import TEMPLATE_DIR

SONNET = ModelPrice(input=3.00, output=15.00, cache_write=3.75, cache_read=0.30)
OPUS = ModelPrice(input=15.00, output=75.00, cache_write=18.75, cache_read=1.50)
TABLE = PriceTable({"claude-sonnet-5-5": SONNET, "claude-opus-5-5": OPUS})


@pytest.mark.parametrize(
    ("model", "usage", "expected"),
    [
        # Each token class on its own, one million at a time, so the rate is the cost.
        ("claude-sonnet-5-5", Usage(input_tokens=1_000_000, output_tokens=0), 3.00),
        ("claude-sonnet-5-5", Usage(input_tokens=0, output_tokens=1_000_000), 15.00),
        (
            "claude-sonnet-5-5",
            Usage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=1_000_000),
            3.75,
        ),
        (
            "claude-sonnet-5-5",
            Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000),
            0.30,
        ),
        # Nothing used costs nothing, for a model that is in the table.
        ("claude-sonnet-5-5", Usage(input_tokens=0, output_tokens=0), 0.0),
        # A mixed call: 1200 * 3 + 300 * 15 + 1000 * 0.30 + 0 * 3.75, per million.
        (
            "claude-sonnet-5-5",
            Usage(
                input_tokens=1200,
                output_tokens=300,
                cache_read_input_tokens=1000,
                cache_creation_input_tokens=0,
            ),
            0.0084,
        ),
        # The same call on the other model uses the other model's prices.
        (
            "claude-opus-5-5",
            Usage(
                input_tokens=1200,
                output_tokens=300,
                cache_read_input_tokens=1000,
                cache_creation_input_tokens=2000,
            ),
            0.0795,
        ),
        # Small counts stay exact enough: 7 input tokens on sonnet.
        ("claude-sonnet-5-5", Usage(input_tokens=7, output_tokens=0), 0.000021),
    ],
)
def test_cost_matches_the_expected_value(model: str, usage: Usage, expected: float) -> None:
    assert TABLE.cost_usd(model, usage) == pytest.approx(expected, abs=1e-12)


def test_a_model_missing_from_the_table_is_an_error_that_names_the_model() -> None:
    with pytest.raises(UnknownModelError, match="claude-haiku-5-5"):
        TABLE.cost_usd("claude-haiku-5-5", Usage(input_tokens=1, output_tokens=1))


def test_a_missing_model_never_silently_costs_zero_even_with_no_usage() -> None:
    with pytest.raises(UnknownModelError):
        TABLE.cost_usd("claude-haiku-5-5", Usage(input_tokens=0, output_tokens=0))


def test_an_empty_table_rejects_every_model() -> None:
    with pytest.raises(UnknownModelError, match="price table is empty"):
        PriceTable({}).cost_usd("claude-sonnet-5-5", Usage(input_tokens=1, output_tokens=1))


def test_the_error_lists_the_models_that_are_priced() -> None:
    with pytest.raises(UnknownModelError, match="claude-opus-5-5"):
        TABLE.cost_usd("other", Usage(input_tokens=1, output_tokens=1))


def test_price_table_knows_which_models_it_prices() -> None:
    assert "claude-sonnet-5-5" in TABLE
    assert "other" not in TABLE
    assert TABLE.price_for("claude-opus-5-5") == OPUS


def test_price_for_a_missing_model_raises_the_same_error() -> None:
    with pytest.raises(UnknownModelError, match="other"):
        TABLE.price_for("other")


def test_the_shipped_plane_yaml_loads_into_a_table_with_both_profiles_models() -> None:
    table = PriceTable.from_plane_yaml(TEMPLATE_DIR / "plane.yaml")

    assert table.price_for("claude-sonnet-5-5") == SONNET
    assert table.price_for("claude-opus-5-5") == OPUS


def test_from_plane_yaml_cost_uses_the_file_prices(tmp_path: Path) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text(
        "version: 1\nprices:\n  m:\n    input: 1\n    output: 2\n    cache_write: 4\n"
        "    cache_read: 8\n"
    )

    table = PriceTable.from_plane_yaml(plane)

    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert table.cost_usd("m", usage) == pytest.approx(15.0)


def test_a_plane_yaml_without_a_prices_key_is_rejected(tmp_path: Path) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text("version: 1\nprofiles: {}\n")

    with pytest.raises(PriceTableError, match="prices"):
        PriceTable.from_plane_yaml(plane)


def test_a_price_entry_missing_a_rate_is_rejected_with_the_model_and_field(
    tmp_path: Path,
) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text("prices:\n  m:\n    input: 1\n    output: 2\n    cache_write: 4\n")

    with pytest.raises(PriceTableError, match=r"m.*cache_read"):
        PriceTable.from_plane_yaml(plane)


def test_a_negative_rate_is_rejected(tmp_path: Path) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text(
        "prices:\n  m:\n    input: -1\n    output: 2\n    cache_write: 4\n    cache_read: 8\n"
    )

    with pytest.raises(PriceTableError, match=r"m.*input"):
        PriceTable.from_plane_yaml(plane)


def test_an_unknown_rate_key_is_rejected(tmp_path: Path) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text(
        "prices:\n  m:\n    input: 1\n    output: 2\n    cache_write: 4\n    cache_read: 8\n"
        "    batch: 0.5\n"
    )

    with pytest.raises(PriceTableError, match=r"m.*batch"):
        PriceTable.from_plane_yaml(plane)


def test_a_prices_value_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text("prices: [1, 2]\n")

    with pytest.raises(PriceTableError, match="prices"):
        PriceTable.from_plane_yaml(plane)


def test_invalid_yaml_is_rejected_with_the_file_name(tmp_path: Path) -> None:
    plane = tmp_path / "plane.yaml"
    plane.write_text("prices: {\n")

    with pytest.raises(PriceTableError, match="plane.yaml"):
        PriceTable.from_plane_yaml(plane)


def test_model_price_rejects_extra_keys() -> None:
    with pytest.raises(PriceTableError):
        ModelPrice.model_validate(
            {"input": 1, "output": 2, "cache_write": 3, "cache_read": 4, "x": 1}
        )
