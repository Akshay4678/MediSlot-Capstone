"""Refund tier calculation. Pure functions: no database, no AWS, no clock of their own.

The windows come from SSM (rules/refund-full-hours, rules/refund-75-hours, rules/refund-50-hours;
defaults 48 / 24 / 6). The time left is measured to the exact instant, not to whole hours:

    time left >= full window      -> FULL      100 %
    time left >= 75 % window      -> TIER_75    75 %
    time left >= 50 % window      -> TIER_50    50 %
    anything less                 -> NONE        0 %

A boundary instant belongs to the BETTER tier (exactly 48 h left refunds 100 %, one microsecond
less refunds 75 %). A doctor cancelling a slot always refunds 100 % (tier DOCTOR_CANCELLED).
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

FULL, TIER_75, TIER_50, NONE = "FULL", "TIER_75", "TIER_50", "NONE"
DOCTOR_CANCELLED = "DOCTOR_CANCELLED"
TIERS = (FULL, TIER_75, TIER_50, NONE, DOCTOR_CANCELLED)

PERCENTAGE = {FULL: 100, TIER_75: 75, TIER_50: 50, NONE: 0, DOCTOR_CANCELLED: 100}

_CENT = Decimal("0.01")


def _money(value):
    return Decimal(str(value if value is not None else 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def tier_for(time_left, bands):
    """Tier name for a timedelta (appointment start minus cancellation instant)."""
    full_h, h75, h50 = bands
    if time_left >= timedelta(hours=full_h):
        return FULL
    if time_left >= timedelta(hours=h75):
        return TIER_75
    if time_left >= timedelta(hours=h50):
        return TIER_50
    return NONE


def refund_for(fee, tier):
    """(amount, percentage) for a fee and a tier; amount is a Decimal rounded half-up to 2 places."""
    pct = PERCENTAGE[tier]
    return (_money(fee) * Decimal(pct) / Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP), pct


def calculate_refund(fee, start_utc, now_utc, bands):
    """Refund for a PATIENT cancellation. start_utc and now_utc must both be naive UTC (or both aware)."""
    time_left = start_utc - now_utc
    tier = tier_for(time_left, bands)
    amount, pct = refund_for(fee, tier)
    return {"tier": tier, "percentage": pct, "amount": amount, "hours_before_start": round(time_left.total_seconds() / 3600, 2)}


def doctor_cancellation_refund(fee):
    amount, pct = refund_for(fee, DOCTOR_CANCELLED)
    return {"tier": DOCTOR_CANCELLED, "percentage": pct, "amount": amount}


def no_show_refund(fee):
    """A patient who did not come keeps paying: 0 % refund (tier NONE). The ledger row records the decision."""
    amount, pct = refund_for(fee, NONE)
    return {"tier": NONE, "percentage": pct, "amount": amount}
