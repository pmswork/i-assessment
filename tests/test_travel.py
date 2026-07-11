from app.travel import estimate_travel_minutes, haversine_km
from tests.factories import AMSTERDAM, ROTTERDAM, UTRECHT


def test_same_point_is_just_the_fixed_overhead():
    assert estimate_travel_minutes(AMSTERDAM, AMSTERDAM) == 10.0


def test_distance_is_symmetric():
    assert haversine_km(AMSTERDAM, ROTTERDAM) == haversine_km(ROTTERDAM, AMSTERDAM)


def test_farther_cities_take_longer():
    close = estimate_travel_minutes(AMSTERDAM, UTRECHT)
    far = estimate_travel_minutes(AMSTERDAM, ROTTERDAM)
    assert haversine_km(AMSTERDAM, UTRECHT) < haversine_km(AMSTERDAM, ROTTERDAM)
    assert close < far
