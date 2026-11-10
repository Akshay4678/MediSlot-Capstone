"""Patient portal: doctor search, slot browsing, booking and appointment actions."""

from datetime import date
import uuid

from flask import Blueprint, flash, redirect, render_template, request, url_for

from web import api_client
from web.api_client import ApiError
from web.portals.common import add_auth_routes, role_required

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
    appointments = _items("/appointments")
    return render_template(
        "patient/home.html",
        doctors=found.get("items", []),
        total=found.get("total", 0),
        has_more=found.get("has_more", False),
        offset=offset,
        page_size=page_size,
        appointments=appointments,
        filters=filters,
        today=date.today().isoformat(),
    )


@bp.route("/slots/<doctor_id>", endpoint="slots")
@role_required("patient")
def slots(doctor_id):
    chosen = request.args.get("date", "").strip() or date.today().isoformat()
    until = request.args.get("to", "").strip()
    params = {"from": chosen, "to": until} if until else {"date": chosen}
    data = api_client.call("GET", f"/doctors/{doctor_id}/slots", params=params)
    return render_template(
        "patient/slots.html",
        doctor_id=doctor_id,
        data=data,
        date=chosen,
        until=until,
    )


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


@bp.route("/book", methods=["POST"], endpoint="book")
@role_required("patient")
def book():
    body = {
        "doctor_id": request.form.get("doctor_id", "").strip(),
        "consultation_type_id": request.form.get("consultation_type_id", "").strip(),
        "slot_id": request.form.get("slot_id", "").strip(),
        "idempotency_key": request.form.get("idempotency_key", "").strip() or str(uuid.uuid4()),
        "notes": request.form.get("notes", "").strip(),
    }
    try:
        result = api_client.call("POST", "/bookings", body)
        flash(f"Appointment confirmed: {result['appointment_id']}", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.home"))


@bp.route("/appointments/<appointment_id>/cancel", methods=["POST"], endpoint="cancel")
@role_required("patient")
def cancel(appointment_id):
    try:
        result = api_client.call(
            "DELETE",
            f"/appointments/{appointment_id}",
            {"reason": request.form.get("reason", "Patient cancellation").strip()},
        )
        refund = result.get("refund") or {}
        flash(
            f"Appointment cancelled. Refund: {refund.get('percentage', 0)}% (₹{refund.get('amount', 0)}).",
            "success",
        )
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.home"))


@bp.route("/appointments/<appointment_id>/reschedule", methods=["POST"], endpoint="reschedule")
@role_required("patient")
def reschedule(appointment_id):
    try:
        api_client.call(
            "POST",
            f"/appointments/{appointment_id}/reschedule",
            {"slot_id": request.form.get("slot_id", "").strip()},
        )
        flash("Appointment rescheduled successfully.", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.home"))


@bp.route("/waitlist", methods=["POST"], endpoint="waitlist")
@role_required("patient")
def waitlist():
    try:
        api_client.call("POST", "/waitlist", {"slot_id": request.form.get("slot_id", "").strip()})
        flash("You have been added to the waitlist.", "success")
    except ApiError as exc:
        flash(exc.message, "error")
    return redirect(url_for("patient.home"))
