"""Execution-speed profile shared by web, desktop and mobile runners.

The dashboard slider sends a *factor*: ``1.0`` is the historical behaviour,
``2.0`` should finish roughly twice as fast and ``0.5`` roughly half as fast.
Speed is never implemented as an unbounded wait.  A faster run mainly gets
smaller (still bounded) locator timeouts; a slower run gets larger timeouts, a
small typing delay and a bounded inter-step pace so an operator can watch it.
That pace is at most one second per already-bounded step and is opt-in through
the slider (below ``1.0``), so the default free-tier budget is unchanged.

This module is dependency-free so it can be copied verbatim into portable
exports as ``testforge_speed.py``.
"""

MIN_SPEED = 0.25
MAX_SPEED = 3.0


def normalize_speed(value, default=1.0):
    """Return a validated speed factor in ``[0.25, 3.0]``.

    Accepts a number, a numeric string, or ``{"factor": n}`` /
    ``{"execution_speed": n}`` so API, plan and UI callers share one contract.
    Booleans are rejected even though ``bool`` is an ``int`` subclass.
    """
    if value is None:
        value = default
    if isinstance(value, dict):
        if "factor" in value:
            value = value["factor"]
        elif "execution_speed" in value:
            value = value["execution_speed"]
        else:
            raise ValueError("execution_speed object must carry a factor")
    if isinstance(value, bool):
        raise ValueError("execution_speed must be a number from 0.25 to 3.0")
    try:
        factor = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution_speed must be a number from 0.25 to 3.0") from exc
    if factor != factor or factor in (float("inf"), float("-inf")):
        raise ValueError("execution_speed must be a number from 0.25 to 3.0")
    if not MIN_SPEED <= factor <= MAX_SPEED:
        raise ValueError("execution_speed must be between 0.25 and 3.0")
    # Two decimals are plenty for a slider and keep exported plans stable.
    return round(factor, 2)


def speed_profile(value=None):
    """Derive the concrete pacing numbers for a speed factor."""
    factor = normalize_speed(value)
    timeout_scale = min(2.5, max(0.34, 1.0 / factor))
    if factor >= 1.0:
        pace_ms = 0
        type_delay_ms = 0
    else:
        pace_ms = int(round((1.0 - factor) * 1000))
        type_delay_ms = int(round((1.0 - factor) * 80))
    if factor >= 2.0:
        label = "fast"
    elif factor > 1.0:
        label = "faster"
    elif factor == 1.0:
        label = "normal"
    else:
        label = "slow (watchable)"
    return {
        "factor": factor,
        "timeout_scale": round(timeout_scale, 3),
        "pace_ms": pace_ms,
        "type_delay_ms": type_delay_ms,
        "label": label,
    }


def scaled_timeout(timeout_ms, profile=None, *, minimum=1, maximum=30000):
    """Scale one locator timeout by the profile, keeping hard bounds."""
    prof = profile if isinstance(profile, dict) and "timeout_scale" in profile else speed_profile(profile)
    try:
        base = int(timeout_ms)
    except (TypeError, ValueError):
        base = 3000
    scaled = int(round(base * float(prof["timeout_scale"])))
    return max(minimum, min(maximum, scaled))
