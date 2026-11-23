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


def is_missing_table(exc):
    """True when DynamoDB says a table does not exist (wrong or deleted table name in the Lambda settings)."""
    response = getattr(exc, "response", None)
    return isinstance(response, dict) and (response.get("Error") or {}).get("Code") == "ResourceNotFoundException"
