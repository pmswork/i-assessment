from app import settings
from app.coverage import CoverageGauge, coverage_gauge, coverage_label, coverage_stats
from tests.factories import AMSTERDAM, ROTTERDAM, make_interpreter, make_job

GRONINGEN = (53.2194, 6.5665)


def test_onsite_job_counts_only_qualified_interpreters_within_radius():
    near = make_interpreter(interpreter_id="INT-NEAR", home=AMSTERDAM)
    # Groningen is outside the default 100km coverage radius.
    far = make_interpreter(interpreter_id="INT-FAR", home=GRONINGEN)
    job = make_job(modality="on-site", location=AMSTERDAM)

    stats = coverage_stats(job, [near, far])

    assert stats.distance_applicable is True
    assert stats.qualified_total == 2
    assert stats.within_radius == 1
    assert stats.is_scarce is False


def test_onsite_job_is_scarce_when_qualified_interpreters_all_live_too_far():
    far = make_interpreter(interpreter_id="INT-FAR", home=GRONINGEN)
    job = make_job(modality="on-site", location=AMSTERDAM)

    stats = coverage_stats(job, [far])

    assert stats.within_radius == 0
    assert stats.qualified_total == 1
    assert stats.is_scarce is True


def test_language_mismatch_is_excluded_regardless_of_distance():
    wrong_language = make_interpreter(language="Polish", home=AMSTERDAM)
    job = make_job(language="Arabic", modality="on-site", location=AMSTERDAM)

    stats = coverage_stats(job, [wrong_language])

    assert stats.qualified_total == 0
    assert stats.within_radius == 0
    # Not "scarce" in the distance sense — there's no coverage gap to flag,
    # this job simply has no qualified interpreter at all (a different,
    # already-reported problem).
    assert stats.is_scarce is False


def test_sworn_required_excludes_non_sworn_interpreters():
    non_sworn = make_interpreter(sworn=False, home=AMSTERDAM)
    job = make_job(sworn_required=True, modality="on-site", location=AMSTERDAM)

    stats = coverage_stats(job, [non_sworn])

    assert stats.qualified_total == 0


def test_remote_job_ignores_distance_and_counts_all_qualified_interpreters():
    near = make_interpreter(interpreter_id="INT-NEAR", home=AMSTERDAM)
    far = make_interpreter(interpreter_id="INT-FAR", home=ROTTERDAM)
    job = make_job(modality="remote", location=None)

    stats = coverage_stats(job, [near, far])

    assert stats.distance_applicable is False
    assert stats.qualified_total == 2
    assert stats.within_radius == 2  # distance not applicable -> not filtered out
    assert stats.is_scarce is False


def test_coverage_label_for_zero_qualified_interpreters_is_not_confusing():
    wrong_language = make_interpreter(language="Polish", home=AMSTERDAM)
    job = make_job(language="Arabic", modality="on-site", location=AMSTERDAM)

    label = coverage_label(coverage_stats(job, [wrong_language]))

    assert label == "0 qualified"


def test_coverage_label_formats_onsite_and_remote_differently():
    near = make_interpreter(interpreter_id="INT-NEAR", home=AMSTERDAM)
    onsite_job = make_job(modality="on-site", location=AMSTERDAM)
    remote_job = make_job(job_id="J-remote", modality="remote", location=None)

    onsite_label = coverage_label(coverage_stats(onsite_job, [near]))
    remote_label = coverage_label(coverage_stats(remote_job, [near]))

    assert f"{settings.get().coverage_radius_km:.0f}km" in onsite_label
    assert "1/1" in onsite_label
    assert "remote" in remote_label


def test_coverage_gauge_zero_qualified_is_the_none_level_with_no_bars_lit():
    wrong_language = make_interpreter(language="Polish", home=AMSTERDAM)
    job = make_job(language="Arabic", modality="on-site", location=AMSTERDAM)

    gauge = coverage_gauge(coverage_stats(job, [wrong_language]), cap=3)

    assert gauge.level == "none"
    assert gauge.segments_lit == 0
    assert gauge.segments_total == 3


def test_coverage_gauge_qualified_but_none_nearby_is_empty_level():
    far = make_interpreter(home=GRONINGEN)  # outside 100km radius of Amsterdam
    job = make_job(modality="on-site", location=AMSTERDAM)

    gauge = coverage_gauge(coverage_stats(job, [far]), cap=3)

    assert gauge.level == "empty"
    assert gauge.segments_lit == 0


def test_coverage_gauge_levels_scale_with_count_up_to_cap():
    job = make_job(modality="on-site", location=AMSTERDAM)

    one = [make_interpreter(interpreter_id="INT-1", home=AMSTERDAM)]
    two = [make_interpreter(interpreter_id=f"INT-{n}", home=AMSTERDAM) for n in range(2)]
    plenty = [make_interpreter(interpreter_id=f"INT-{n}", home=AMSTERDAM) for n in range(5)]

    assert coverage_gauge(coverage_stats(job, one), cap=3) == CoverageGauge(1, 3, "low")
    assert coverage_gauge(coverage_stats(job, two), cap=3) == CoverageGauge(2, 3, "mid")
    # 5 qualified interpreters, but the gauge caps the *display* at 3 bars —
    # it's a quick-scan indicator, not a precise count (the label text is).
    assert coverage_gauge(coverage_stats(job, plenty), cap=3) == CoverageGauge(3, 3, "good")


def test_coverage_gauge_remote_job_uses_qualified_count():
    qualified = [make_interpreter(interpreter_id="INT-1", home=ROTTERDAM)]
    remote_job = make_job(modality="remote", location=None)

    gauge = coverage_gauge(coverage_stats(remote_job, qualified), cap=3)

    assert gauge == CoverageGauge(1, 3, "low")
