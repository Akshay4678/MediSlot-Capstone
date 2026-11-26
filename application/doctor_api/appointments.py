"""Doctor side of Milestones 5 and 6: the doctor's own appointments, slot cancellation and what happened at the visit.

The logic lives in application/booking/service.py; these are thin adapters for the doctor router.
Every query is scoped to the calling doctor (identity comes from the authorizer context only).
"""

from application.booking import service
from application.booking.deps import default_deps


def list_(ctx):
    return service.list_for_doctor(ctx.conn, default_deps(), ctx.user_id, ctx.query)


def get(ctx):
    return service.get_for_doctor(ctx.conn, default_deps(), ctx.user_id, (ctx.params.get("appointment_id") or "").strip())


def cancel_slot(ctx):
    return service.cancel_slot_by_doctor(ctx.conn, default_deps(), ctx.user_id, ctx.body)


def no_show(ctx):
    """POST /doctors/me/appointments/{appointment_id}/no-show : the patient did not come (the fee is kept)."""
    return service.record_attendance(ctx.conn, default_deps(), ctx.user_id,
                                     (ctx.params.get("appointment_id") or "").strip(), "no_show")


def complete(ctx):
    """POST /doctors/me/appointments/{appointment_id}/complete : the patient was seen."""
    return service.record_attendance(ctx.conn, default_deps(), ctx.user_id,
                                     (ctx.params.get("appointment_id") or "").strip(), "completed")
