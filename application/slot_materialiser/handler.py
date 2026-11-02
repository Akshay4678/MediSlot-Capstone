"""Daily slot materialiser (EventBridge Scheduler -> Lambda).

Safe to run any number of times: slot ids are deterministic and inserts use INSERT IGNORE.
Optional test input: {"as_of": "2026-10-30T00:00:00Z"} runs the job as if it were that instant
(used to demonstrate a daylight-saving crossing without waiting for the real date).
"""

import json
import logging
from datetime import datetime, timezone

from application.common import tracing
from application.database.connection import get_connection
from application.slot_materialiser import core

tracing.enable()
log = logging.getLogger()
log.setLevel(logging.INFO)


def _valid_local(local_dt, zone, fold=0):
    """Return a valid timezone-aware local datetime, or None for a DST gap.

    For ambiguous fall-back times, ``fold=0`` selects the first occurrence.
    """
    aware = local_dt.replace(tzinfo=zone, fold=fold)
    round_trip = aware.astimezone(timezone.utc).astimezone(zone)
    if round_trip.replace(tzinfo=None) != local_dt:
        return None
    return aware


def _slot_id(doctor_id, instant):
    """Build a deterministic slot identifier from a UTC instant."""
    utc_instant = instant.astimezone(timezone.utc)
    return f"{doctor_id}#{utc_instant:%Y%m%dT%H%M%SZ}"


def _parse_as_of(event):
    value = (event or {}).get("as_of")
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def lambda_handler(event, context):
    now_utc = _parse_as_of(event)
    conn = get_connection()
    summaries, errors = [], 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT d.doctor_id, d.timezone FROM doctors d "
                "JOIN availability_patterns ap ON ap.doctor_id = d.doctor_id AND ap.is_active = TRUE "
                "WHERE d.is_active = TRUE"
            )
            doctors = cur.fetchall()

        for doctor in doctors:
            try:
                summaries.append(core.materialise_doctor(conn, doctor["doctor_id"], doctor["timezone"], now_utc))
                conn.commit()
            except Exception:  # noqa: BLE001 - one doctor must not block the others
                conn.rollback()
                errors += 1
                log.exception("Materialisation failed for doctor %s", doctor["doctor_id"])

        purged = core.purge_old_slots(conn, now_utc)
        conn.commit()
    finally:
        conn.close()

    result = {
        "as_of": now_utc.isoformat(), "doctors": len(summaries) + errors, "errors": errors,
        "inserted": sum(s["inserted"] for s in summaries),
        "already_existing": sum(s["already_existing"] for s in summaries), "purged": purged,
    }
    log.info(json.dumps({"event": "slot_materialisation_complete", **result}))
    if errors:
        raise RuntimeError(f"Slot materialisation finished with {errors} failed doctor(s)")
    return result
