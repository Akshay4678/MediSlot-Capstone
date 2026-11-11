"""Patient portal (Milestone 4): profile, doctor search, doctor page and slot browsing."""

from datetime import date

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
    return render_template(
        "patient/home.html",
        doctors=found.get("items", []),
        total=found.get("total", 0),
        has_more=found.get("has_more", False),
        offset=offset,
        page_size=page_size,
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
