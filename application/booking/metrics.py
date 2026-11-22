"""CloudWatch custom metrics (namespace MediSlot).

    BookingConflictRetries   count of version-check conflicts that forced a booking to retry
    CancellationsByTier      count of cancellations, dimension Tier = FULL | TIER_75 | TIER_50 | NONE | DOCTOR_CANCELLED
    BookingsConfirmed, BookingsRejected (dimension Reason)  outcome counters

Best effort: a metrics failure is logged and never fails a request.
"""

import logging

log = logging.getLogger()

NAMESPACE = "MediSlot"


class Metrics:
    def __init__(self, client, namespace=NAMESPACE, environment="dev"):
        self.client, self.namespace, self.environment = client, namespace, environment

    def put(self, name, value=1, dimensions=None, unit="Count"):
        dims = [{"Name": "Environment", "Value": self.environment}]
        dims += [{"Name": k, "Value": str(v)} for k, v in (dimensions or {}).items()]
        try:
            self.client.put_metric_data(Namespace=self.namespace, MetricData=[{
                "MetricName": name, "Value": float(value), "Unit": unit, "Dimensions": dims}])
            return True
        except Exception:  # noqa: BLE001
            log.exception("Could not publish metric %s", name)
            return False


class NullMetrics:
    def put(self, name, value=1, dimensions=None, unit="Count"):
        return False
