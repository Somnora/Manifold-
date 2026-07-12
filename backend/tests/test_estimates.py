"""Cost/utilization intelligence: the estimation math and the right-size
threshold. Pure functions, so the logic is checked directly; the endpoints
are exercised in test_estimate_routes.py against the mock app."""

from app.estimates import (
    MEASURED_MIN_RUNS,
    RIGHT_SIZE_VRAM_FRACTION,
    estimate_job,
    utilization_summary,
)


# -- pre-launch estimate ----------------------------------------------------------


def test_measured_estimate_from_history_median():
    # Five runs of 40, 42, 38, 41, 39 min on an A10 at $1.29/hr.
    durations = [40 * 60, 42 * 60, 38 * 60, 41 * 60, 39 * 60]
    e = estimate_job("whisper-batch", "gpu_1x_a10", durations, 129)
    assert e.confidence == "measured"
    assert e.minutes == 40.0                       # median of the five
    # 40 min at $1.29/hr = $0.86.
    assert round(e.cost_usd, 2) == 0.86
    assert e.sample_size == 5
    assert "median of 5" in e.basis


def test_few_runs_are_marked_rough():
    e = estimate_job("whisper-batch", "gpu_1x_a10", [30 * 60, 34 * 60], 129)
    assert e.confidence == "rough"                 # 2 < MEASURED_MIN_RUNS
    assert e.sample_size == 2
    assert "still learning" in e.basis
    assert MEASURED_MIN_RUNS == 3


def test_no_history_falls_back_to_default_marked_rough():
    e = estimate_job("whisper-batch", "gpu_1x_a10", [], 129)
    assert e.confidence == "rough"
    assert e.sample_size == 0
    assert e.minutes == 30                          # coarse default
    assert "no history" in e.basis
    assert e.cost_usd is not None                   # still priced


def test_server_template_has_no_fixed_cost():
    e = estimate_job("vllm-serve", "gpu_1x_a10", [], 129)
    assert e.confidence == "none"
    assert e.minutes is None and e.cost_usd is None
    assert "until you stop it" in e.basis


def test_estimate_without_rate_gives_time_but_no_cost():
    e = estimate_job("whisper-batch", "gpu_1x_a10", [40 * 60] * 4, None)
    assert e.minutes == 40.0
    assert e.cost_usd is None                       # rate unknown


# -- post-run utilization + right-size hint ---------------------------------------


def _util(peak_mib, total_mib=24564, samples=30, util=14.0, runtime=45 * 60):
    return utilization_summary(
        gpu_description="A10",
        runtime_seconds=runtime,
        peak_vram_used_mib=peak_mib,
        vram_total_mib=total_mib,
        avg_util_pct=util,
        sample_count=samples,
    )


def test_verdict_line_shape():
    u = _util(9 * 1024)   # 9 GB peak on a 24 GB card
    assert "A10" in u.verdict
    assert "45 min" in u.verdict
    assert "peak VRAM 9.0/24 GB" in u.verdict
    assert "avg util 14%" in u.verdict


def test_right_size_hint_fires_when_clearly_underused():
    u = _util(9 * 1024)   # 9/24 = 0.375 <= 0.45
    assert u.right_size_hint is True
    assert "smaller" in u.hint.lower()
    assert 9 * 1024 / 24564 <= RIGHT_SIZE_VRAM_FRACTION


def test_no_hint_when_vram_near_capacity():
    u = _util(20 * 1024)  # 20/24 = 0.83, card was well used
    assert u.right_size_hint is False
    assert "smaller" not in u.hint.lower()


def test_no_hint_in_the_gray_zone_but_headroom_noted():
    u = _util(14 * 1024)  # 14/24 = 0.58: between 0.45 and 0.65
    assert u.right_size_hint is False
    assert "headroom" in u.hint.lower()


def test_conservative_no_hint_on_thin_telemetry():
    # Even a low peak must NOT fire the hint without enough samples: a false
    # "downsize" that OOMs the next run destroys trust.
    u = _util(4 * 1024, samples=2)
    assert u.right_size_hint is False
    assert "limited telemetry" in u.hint.lower()


def test_no_telemetry_at_all():
    u = _util(0, samples=0)
    assert u.right_size_hint is False
    assert "no telemetry" in u.verdict.lower()
