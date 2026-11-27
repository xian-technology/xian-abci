"""Runtime limits must remain usable finite durations and refill rates."""

from types import SimpleNamespace

import pytest

from xian.dashboard.app import DashboardRateLimiter
from xian.metrics import MetricsConfig

NONFINITE = [
    float("nan"),
    float("inf"),
    float("-inf"),
    pytest.param(10**400, id="overflowing_integer"),
]


@pytest.mark.parametrize("value", NONFINITE)
@pytest.mark.parametrize("from_settings", [False, True])
def test_metrics_refresh_rejects_nonfinite_interval(value, from_settings):
    with pytest.raises(ValueError, match="finite"):
        if from_settings:
            MetricsConfig.from_runtime_settings({"metrics_bds_refresh_seconds": value})
        else:
            MetricsConfig(bds_refresh_seconds=value)


@pytest.mark.parametrize("value", NONFINITE)
@pytest.mark.parametrize(
    "option", ["rest_rate_limit_per_second", "expensive_rest_rate_limit_per_second"]
)
def test_dashboard_rejects_nonfinite_refill_rate(value, option):
    with pytest.raises(ValueError, match="finite"):
        DashboardRateLimiter(**{option: value})


@pytest.mark.parametrize("rate,refill_after", [(0.0, 1000.0), (-1.0, 1000.0), (2.0, 0.5)])
def test_finite_dashboard_rate_keeps_minimum_and_refill_behavior(rate, refill_after):
    now = [0.0]
    limiter = DashboardRateLimiter(
        rest_rate_limit_per_second=rate, rest_rate_limit_burst=1, clock=lambda: now[0]
    )
    request = SimpleNamespace(path="/api/status", remote="local-client")
    assert limiter.check(request) is None
    assert limiter.check(request).status == 429
    now[0] = refill_after
    assert limiter.check(request) is None


@pytest.mark.parametrize("interval", [0.0, -1.0, 2.5])
def test_finite_metrics_refresh_keeps_existing_values(interval):
    assert MetricsConfig(bds_refresh_seconds=interval).bds_refresh_seconds == interval
    assert (
        MetricsConfig.from_runtime_settings(
            {"metrics_bds_refresh_seconds": interval}
        ).bds_refresh_seconds
        == interval
    )
