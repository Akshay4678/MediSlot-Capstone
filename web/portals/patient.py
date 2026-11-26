"""Patient portal (Milestones 4-5): profile, doctor search, slots, booking, appointments and refunds."""

import uuid
from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from web import api_client
from web.api_client import ApiError
from web.portals.common import add_auth_routes, role_required

CONSULTATION_TYPES = ("Quick Consult", "Standard Consult", "Detailed Consult", "Health Camp Session")
SLOT_WINDOW_DAYS = 30  # the booking window: patients can book this many days ahead

bp = Blueprint("patient", __name__, url_prefix="/patient")
add_auth_routes(bp, "patient")


@bp.errorhandler(ApiError)
def api_error(exc):
    flash(exc.message, "error")
    # Never redirect back to the page that just failed: that is an endless redirect loop.
    if request.endpoint == "patient.home":
        return redirect(url_for("landing"))
    return redirect(url_for("patient.home"))


def _items(path, params=None):
    """Load a list from the API; on failure show the real reason and keep the page usable."""
    try:
        return api_client.call("GET", path, params=params)["items"]
    except ApiError as exc:
        flash(f"{path}: {exc.message} (HTTP {exc.status})", "error")
        return []


@bp.route("/", endpoint="home")
@role_required("patient")
def home():
    filters = {
        "name": request.args.get("name", "").strip(),
        "hospital": request.args.get("hospital", "").strip(),
        "city": request.args.get("city", "").strip(),
        "specialisation": request.args.get("specialisation", "").strip(),
        "consultation_type": request.args.get("consultation_type", "").strip(),
    }
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    page_size = 10
    try:
        found = api_client.call(
            "GET",
            "/doctors/search",
            params=dict(filters, limit=page_size, offset=offset),
        )
    except ApiError as exc:
        flash(f"/doctors/search: {exc.message} (HTTP {exc.status})", "error")
        found = {"items": [], "total": 0, "has_more": False}
    return render_template(
        "patient/home.html",
        doctors=found.get("items", []),
        total=found.get("total", 0),
        has_more=found.get("has_more", False),
        offset=offset,
        page_size=page_size,
        filters=filters,
        today=date.today().isoformat(),
        consultation_types=CONSULTATION_TYPES,
    )


@bp.route("/slots/<doctor_id>", endpoint="slots")
@role_required("patient")
def slots(doctor_id):
    chosen = request.args.get("date", "").strip() or date.today().isoformat()
    until = request.args.get("to", "").strip()
    if not until:
        # Show the whole 30-day booking window by default, not a single day.
        try:
            until = (date.fromisoformat(chosen) + timedelta(days=SLOT_WINDOW_DAYS - 1)).isoformat()
        except ValueError:
            until = ""
    params = {"from": chosen, "to": until} if until else {"date": chosen}
    try:
        data = api_client.call("GET", f"/doctors/{doctor_id}/slots", params=params)
    except ApiError as exc:
        if exc.status != 400:
            raise
        # e.g. a date beyond the booking window: say so, and show the default window instead
        flash(exc.message, "error")
        chosen = date.today().isoformat()
        until = (date.today() + timedelta(days=SLOT_WINDOW_DAYS - 1)).isoformat()
        data = api_client.call("GET", f"/doctors/{doctor_id}/slots", params={"from": chosen, "to": until})

    # The waitlist is a separate domain from appointments. Keep the slot page
    # aware of the patient's active WAITING/HELD entries so the same patient
    # cannot keep clicking "Join waitlist" for an already-active entry.
    try:
        waitlist = api_client.call("GET", "/waitlist")
        waitlist_slot_ids = {
            str(item.get("slot_id"))
            for item in (waitlist.get("items") or [])
            if isinstance(item, dict) and item.get("slot_id")
        }
    except ApiError:
        waitlist_slot_ids = set()

    # A patient who already has a confirmed appointment for a slot must not
    # be offered the waitlist for that same slot. Keep this separate from the
    # waitlist lookup because a booked slot is an appointment, not a waitlist
    # entry.
    try:
        appointments = api_client.call("GET", "/appointments", params={"scope": "upcoming", "limit": 100, "offset": 0})
        booked_slot_ids = {
            str(item.get("slot_id"))
            for item in (appointments.get("items") or [])
            if isinstance(item, dict)
            and item.get("slot_id")
            and item.get("status") == "CONFIRMED"
        }
    except ApiError:
        booked_slot_ids = set()

    return render_template(
        "patient/slots.html",
        doctor_id=doctor_id,
        data=data,
        date=chosen,
        until=until,
        waitlist_slot_ids=waitlist_slot_ids,
        booked_slot_ids=booked_slot_ids,
        joined_slot_id=request.args.get("joined_slot_id", "").strip(),
        booking_key=uuid.uuid4().hex,  # one key per page view: a double click or a resubmit books only once
    )


@bp.route("/book", methods=["POST"], endpoint="book")
@role_required("patient")
def book():
    slot_id = request.form.get("slot_id", "").strip()
    doctor_id = request.form.get("doctor_id", "").strip()
    key = request.form.get("idempotency_key", "").strip() or uuid.uuid4().hex
    body = {"slot_id": slot_id}
    notes = request.form.get("notes", "").strip()
    if notes:
        body["notes"] = notes
    try:
        api_client.call("POST", "/appointments", body, headers={"Idempotency-Key": key})
    except ApiError as exc:
        flash(exc.message, "error")
        if doctor_id:
            return redirect(url_for("patient.slots", doctor_id=doctor_id, date=request.form.get("date", ""),
                                    to=request.form.get("to", "")))
        return redirect(url_for("patient.home"))
    # Back to the list: cancelling is done from "My appointments", not straight after booking.
    flash("Your appointment is confirmed.", "success")
    return redirect(url_for("patient.appointments"))


@bp.route("/waitlist/join", methods=["POST"], endpoint="waitlist_join")
@role_required("patient")
def waitlist_join():
    slot_id = request.form.get("slot_id", "").strip()
    doctor_id = request.form.get("doctor_id", "").strip()
    try:
        result = api_client.call("POST", "/waitlist", {"slot_id": slot_id})
        if result.get("already_joined"):
            flash("You are already on the waitlist for this slot.", "success")
        else:
            flash("You are now on the waitlist for this slot.", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for(
        "patient.slots",
        doctor_id=doctor_id,
        date=request.form.get("date", ""),
        to=request.form.get("to", ""),
        joined_slot_id=slot_id,
    ))

@bp.route("/waitlist", endpoint="waitlist")
@role_required("patient")
def waitlist():
    data = api_client.call("GET", "/waitlist")
    return render_template("patient/waitlist.html", data=data)

@bp.route("/waitlist/<slot_id>/leave", methods=["POST"], endpoint="waitlist_leave")
@role_required("patient")
def waitlist_leave(slot_id):
    try:
        api_client.call("DELETE", f"/waitlist/{slot_id}")
        flash("You left the waitlist.", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.waitlist"))

@bp.route("/waitlist/offers/<offer_id>/accept", methods=["POST"], endpoint="waitlist_offer_accept")
@role_required("patient")
def waitlist_offer_accept(offer_id):
    try:
        api_client.call("POST", f"/waitlist/offers/{offer_id}/accept", {})
        flash("Your waitlist offer was accepted and the appointment is confirmed.", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.waitlist"))

@bp.route("/appointments", endpoint="appointments")
@role_required("patient")
def appointments():
    scope = request.args.get("scope", "upcoming")
    if scope not in ("upcoming", "past", "all"):
        scope = "upcoming"
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    page_size = 10
    data = api_client.call("GET", "/appointments", params={"scope": scope, "limit": page_size, "offset": offset})
    return render_template("patient/appointments.html", data=data, scope=scope, offset=offset, page_size=page_size)


@bp.route("/appointments/<appointment_id>", endpoint="appointment")
@role_required("patient")
def appointment(appointment_id):
    data = api_client.call("GET", f"/appointments/{appointment_id}")
    return render_template("patient/appointment.html", a=data["appointment"])


@bp.route("/appointments/<appointment_id>/reschedule", methods=["GET", "POST"], endpoint="reschedule")
@role_required("patient")
def reschedule(appointment_id):
    if request.method == "POST":
        target_slot_id = request.form.get("target_slot_id", "").strip()
        try:
            api_client.call("POST", f"/appointments/{appointment_id}/reschedule", {"target_slot_id": target_slot_id})
            flash("Reschedule requested. The workflow is processing your new slot.", "success")
            return redirect(url_for("patient.appointment", appointment_id=appointment_id))
        except ApiError as exc:
            flash(exc.message, "error")
    a = api_client.call("GET", f"/appointments/{appointment_id}")["appointment"]
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=SLOT_WINDOW_DAYS - 1)).isoformat()
    try:
        slots_data = api_client.call("GET", f"/doctors/{a['doctor']['doctor_id']}/slots", params={"from": start, "to": end})
    except ApiError:
        slots_data = {"days": []}
    return render_template("patient/reschedule.html", a=a, data=slots_data)

@bp.route("/appointments/<appointment_id>/cancel", methods=["POST"], endpoint="cancel")
@role_required("patient")
def cancel(appointment_id):
    body = {}
    reason = request.form.get("reason", "").strip()
    if reason:
        body["reason"] = reason
    try:
        data = api_client.call("POST", f"/appointments/{appointment_id}/cancel", body)
    except ApiError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.appointment", appointment_id=appointment_id))
    refund = data["appointment"].get("refund") or {}
    flash(f"Appointment cancelled. Refund: \u20b9{refund.get('amount', 0):.2f} ({refund.get('percentage', 0):g}%).", "success")
    return redirect(url_for("patient.appointment", appointment_id=appointment_id))


@bp.route("/refunds", endpoint="refunds")
@role_required("patient")
def refunds():
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0
    page_size = 10
    data = api_client.call("GET", "/refunds", params={"limit": page_size, "offset": offset})
    return render_template("patient/refunds.html", data=data, offset=offset, page_size=page_size)


@bp.route("/doctor/<doctor_id>", endpoint="doctor")
@role_required("patient")
def doctor(doctor_id):
    data = api_client.call("GET", f"/doctors/{doctor_id}")
    return render_template("patient/doctor.html", doctor_id=doctor_id, today=date.today().isoformat(), **data)


PROFILE_FIELDS = ("first_name", "last_name", "date_of_birth", "gender", "phone", "address", "city", "emergency_contact")


@bp.route("/profile", methods=["GET", "POST"], endpoint="profile")
@role_required("patient")
def profile():
    if request.method == "POST":
        body = {f: request.form.get(f, "").strip() for f in PROFILE_FIELDS}
        try:
            api_client.call("PUT", "/patient/me", body)
            flash("Profile saved.", "success")
            return redirect(url_for("patient.profile"))
        except ApiError as exc:
            flash(exc.message, "error")
            return render_template("patient/profile.html", p=body)
    return render_template("patient/profile.html", p=api_client.call("GET", "/patient/me")["patient"])


@bp.route("/profile/delete", methods=["POST"], endpoint="profile_delete")
@role_required("patient")
def profile_delete():
    try:
        api_client.call("DELETE", "/patient/me")
    except ApiError as exc:
        flash(exc.message, "error")
        return redirect(url_for("patient.profile"))
    api_client.logout()
    flash("Your profile was deleted and your account is closed.", "success")
    return redirect(url_for("landing"))
