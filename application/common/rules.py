"""Business rules from the frozen rule set, read from SSM Parameter Store.

Parameter names (under $SSM_PREFIX): rules/quick-consult-minutes, rules/standard-consult-minutes,
rules/detailed-consult-minutes, rules/health-camp-minutes, rules/health-camp-min-capacity,
rules/health-camp-max-capacity, rules/booking-window-days, rules/photo-max-bytes.
The numbers below are only fallbacks used when a parameter is missing (a warning is logged).
"""

from application.common import config

# lower-case name -> (canonical name, SSM parameter, fallback minutes)
CONSULTATION_TYPES = {
    "quick consult": ("Quick Consult", "rules/quick-consult-minutes", 15),
    "standard consult": ("Standard Consult", "rules/standard-consult-minutes", 30),
    "detailed consult": ("Detailed Consult", "rules/detailed-consult-minutes", 60),
    "health camp session": ("Health Camp Session", "rules/health-camp-minutes", 60),
}
HEALTH_CAMP = "health camp session"


def canonical_type(name):
    """Return (canonical_name, duration_minutes) or None if the name is not an allowed type."""
    entry = CONSULTATION_TYPES.get(str(name or "").strip().lower())
    if not entry:
        return None
    canonical, parameter, fallback = entry
    return canonical, config.get_int(parameter, fallback)


def allowed_type_names():
    return [entry[0] for entry in CONSULTATION_TYPES.values()]


def is_health_camp(type_name):
    return str(type_name or "").strip().lower() == HEALTH_CAMP


def health_camp_capacity_bounds():
    return config.get_int("rules/health-camp-min-capacity", 2), config.get_int("rules/health-camp-max-capacity", 10)


def booking_window_days():
    return max(1, config.get_int("rules/booking-window-days", 30))


def photo_max_bytes():
    return config.get_int("rules/photo-max-bytes", 5 * 1024 * 1024)
