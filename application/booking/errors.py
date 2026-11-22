"""Domain exceptions of the booking engine (the service turns them into ApiError responses)."""


class SlotFull(Exception):
    """No free seat is left on the slot."""


class ContentionExceeded(Exception):
    """The bounded retry ran out: too many other bookings kept winning the same slot."""

    def __init__(self, attempts):
        super().__init__(f"seat update lost {attempts} times")
        self.attempts = attempts


def is_conditional_failure(exc):
    """True when a DynamoDB call failed only because its ConditionExpression was false."""
    error = (getattr(exc, "response", None) or {}).get("Error", {})
    return error.get("Code") == "ConditionalCheckFailedException"
