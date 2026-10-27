"""Optional direct RDS connection helper. Auth handlers use auth_utils.db_connection."""
from application.common.auth_utils import db_connection

__all__ = ["db_connection"]
