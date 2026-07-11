import pytest

from app import settings
from app.travel import estimate_travel_minutes
from tests.factories import AMSTERDAM, ROTTERDAM


def test_defaults_are_returned_without_any_update():
    current = settings.get()
    assert current == settings.Settings()
    assert current.coverage_radius_km == 100.0
    assert current.auto_assign_risk_level == 2
    assert current.urgent_unassigned_days == 3


def test_update_changes_only_the_given_fields():
    settings.update(travel_buffer_min=30.0, auto_assign_risk_level=3, urgent_unassigned_days=5)
    current = settings.get()
    assert current.travel_buffer_min == 30.0
    assert current.auto_assign_risk_level == 3
    assert current.urgent_unassigned_days == 5
    assert current.long_distance_km == settings.Settings().long_distance_km  # untouched


def test_update_rejects_values_below_the_field_minimum():
    with pytest.raises(ValueError):
        settings.update(average_speed_kmh=-5.0)
    # rejected update must not partially apply
    assert settings.get().average_speed_kmh == settings.Settings().average_speed_kmh


def test_update_rejects_unknown_field():
    with pytest.raises(ValueError):
        settings.update(not_a_real_field=1.0)


def test_update_rejects_unknown_auto_assign_risk_level():
    with pytest.raises(ValueError):
        settings.update(auto_assign_risk_level=9)


def test_reset_restores_defaults():
    settings.update(coverage_radius_km=100.0)
    settings.reset()
    assert settings.get() == settings.Settings()


def test_field_names_matches_all_settings_fields():
    names = settings.field_names()
    assert "travel_buffer_min" in names
    assert "coverage_bar_cap" in names
    assert len(names) == len(settings.FIELD_INFO)


def test_updating_average_speed_changes_travel_estimates_immediately():
    """The whole point of centralizing thresholds in settings.py: a change
    takes effect on the very next call, with nothing cached from import
    time. This is what makes the Settings tab actually work."""
    before = estimate_travel_minutes(AMSTERDAM, ROTTERDAM)

    settings.update(average_speed_kmh=settings.get().average_speed_kmh * 2)
    after = estimate_travel_minutes(AMSTERDAM, ROTTERDAM)

    assert after < before
