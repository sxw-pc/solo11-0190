"""Translator capacity decided by the capture engine's global clock and dedup."""
from .time_capture import booked_hours, resolve  # noqa: F401  (booked_hours re-exported)


def remaining(records, evidence=None, day_hours=8):
    """Capacity decision driven by time_capture's resolution, not the naive sum.

    Returns the single day report, or {'days': [...]} when the rows span
    several account/date scopes.  The legacy naive figure is preserved
    inside each report under 'legacy'.
    """
    reports = resolve(records, evidence=evidence, day_hours=day_hours)
    return reports[0] if len(reports) == 1 else {'days': reports}
