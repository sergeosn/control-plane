"""Task 15: `plane.yaml`, the model profiles, prices and defaults a run is built from (docs/SPEC.md
sections 5, 8.5 and 14)."""

from pathlib import Path

import pytest

from control_plane.store.home import Home, create_home
from control_plane.store.plane import PlaneConfigError, load_plane, price_table


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


def test_the_shipped_plane_yaml_loads_with_profiles_prices_and_defaults(home: Home) -> None:
    plane = load_plane(home)

    assert plane.profiles["default"].wire_format == "openai-compatible"
    assert plane.profiles["default"].model in price_table(plane)
    assert plane.profiles["strict"].temperature == 0.0
    assert plane.defaults.lane == "development"
    assert plane.defaults.step_budget.max_turns == 40
    assert plane.defaults.roles.reviewer == "strict"


def test_a_profile_without_a_price_is_rejected_naming_the_profile(home: Home) -> None:
    text = home.plane_yaml.read_text().replace("model: claude-opus-5-5", "model: unpriced-model")
    home.plane_yaml.write_text(text)

    with pytest.raises(PlaneConfigError, match=r"profiles\.strict.*unpriced-model"):
        load_plane(home)


def test_a_malformed_plane_yaml_names_the_field(home: Home) -> None:
    text = home.plane_yaml.read_text().replace("max_turns: 40", "max_turns: many")
    home.plane_yaml.write_text(text)

    with pytest.raises(PlaneConfigError, match=r"defaults\.step_budget\.max_turns"):
        load_plane(home)


def test_a_plane_yaml_that_is_not_a_mapping_is_rejected(home: Home) -> None:
    home.plane_yaml.write_text("- just\n- a list\n")

    with pytest.raises(PlaneConfigError, match="mapping"):
        load_plane(home)


def test_invalid_yaml_is_rejected(home: Home) -> None:
    home.plane_yaml.write_text("profiles: [unclosed\n")

    with pytest.raises(PlaneConfigError, match="not valid YAML"):
        load_plane(home)
