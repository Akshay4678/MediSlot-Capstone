"""Doctor portal (/doctor/*): profile + photo, consultation types, weekly availability,
blackout dates and the day / week schedule. Every action goes through the REST API."""

from datetime import date, datetime, timedelta

import requests
from flask import Blueprint, flash, redirect, render_template, request, url_for

from web import api_client
from web.api_client import ApiError
from web.portals.common import add_auth_routes, role_required


bp = Blueprint(
    "doctor",
    __name__,
    url_prefix="/doctor",
)

add_auth_routes(bp, "doctor")


DAYS = [
    (1, "Monday"),
    (2, "Tuesday"),
    (3, "Wednesday"),
    (4, "Thursday"),
    (5, "Friday"),
    (6, "Saturday"),
    (7, "Sunday"),
]


TIMEZONES = [
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Singapore",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Australia/Sydney",
    "UTC",
]


PHOTO_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _profile_or_none():
    try:
        return api_client.call(
            "GET",
            "/doctors/me",
        )["profile"]

    except ApiError as exc:
        if exc.status == 404:
            return None

        raise


def _need_profile():
    flash(
        "Please complete your profile first.",
        "error",
    )

    return redirect(
        url_for("doctor.profile")
    )


@bp.errorhandler(ApiError)
def _api_error(exc):

    if (
        exc.status == 409
        and exc.code == "PROFILE_REQUIRED"
    ):
        return _need_profile()

    flash(
        exc.message,
        "error",
    )

    return redirect(
        url_for("doctor.home")
    )


# ------------------------------------------------------------------ home

@bp.route("/", endpoint="home")
@role_required("doctor")
def home():

    profile = _profile_or_none()

    if profile is None:
        return _need_profile()

    today = api_client.call(
        "GET",
        "/doctors/me/schedule",
    )

    return render_template(
        "doctor/home.html",
        profile=profile,
        today=today,
    )


# ---------------------------------------------------------------- profile

@bp.route(
    "/profile",
    methods=["GET", "POST"],
    endpoint="profile",
)
@role_required("doctor")
def profile():

    if request.method == "POST":

        body = {
            k: request.form.get(k, "").strip()
            for k in (
                "first_name",
                "last_name",
                "licence_number",
                "hospital_id",
                "specialisation_id",
                "experience_years",
                "qualifications",
                "timezone",
            )
        }

        body = {
            k: v
            for k, v in body.items()
            if v != "" or k == "qualifications"
        }

        try:

            api_client.call(
                "PUT",
                "/doctors/me",
                body,
            )

            flash(
                "Profile saved.",
                "success",
            )

            return redirect(
                url_for("doctor.profile")
            )

        except ApiError as exc:
            flash(
                exc.message,
                "error",
            )

        current = body

    else:
        current = _profile_or_none() or {}

    return render_template(
        "doctor/profile.html",
        p=current,
        hospitals=api_client.call(
            "GET",
            "/hospitals",
        )["items"],
        specialisations=api_client.call(
            "GET",
            "/specialisations",
        )["items"],
        timezones=TIMEZONES,
    )


# -------------------------------------------------------------- profile photo

@bp.route(
    "/profile/photo",
    methods=["POST"],
    endpoint="photo",
)
@role_required("doctor")
def photo():

    upload = request.files.get("photo")

    if not upload or not upload.filename:
        flash(
            "Choose an image to upload.",
            "error",
        )

        return redirect(
            url_for("doctor.profile")
        )

    content_type = (
        upload.mimetype or ""
    ).lower()

    if content_type not in PHOTO_TYPES:
        flash(
            "Photos must be JPEG, PNG or WebP.",
            "error",
        )

        return redirect(
            url_for("doctor.profile")
        )

    data = upload.read()

    grant = api_client.call(
        "POST",
        "/doctors/me/photo",
        {
            "content_type": content_type,
        },
    )

    if len(data) > grant["max_bytes"]:
        flash(
            f"The photo is too large "
            f"(maximum {grant['max_bytes'] // (1024 * 1024)} MB).",
            "error",
        )

        return redirect(
            url_for("doctor.profile")
        )

    try:
        result = requests.post(
            grant["upload"]["url"],
            data=grant["upload"]["fields"],
            files={
                "file": (
                    upload.filename,
                    data,
                    content_type,
                )
            },
            timeout=(3.05, 30),
        )

    except requests.RequestException:
        flash(
            "The photo could not be uploaded. Please try again.",
            "error",
        )

        return redirect(
            url_for("doctor.profile")
        )

    success = result.status_code in (
        200,
        201,
        204,
    )

    flash(
        "Photo uploaded."
        if success
        else "The photo upload was rejected.",
        "success"
        if success
        else "error",
    )

    return redirect(
        url_for("doctor.profile")
    )


# ------------------------------------------------------- consultation types

@bp.route(
    "/consultation-types",
    methods=["GET", "POST"],
    endpoint="types",
)
@role_required("doctor")
def types():

    if request.method == "POST":

        action = request.form.get("action")

        try:

            if action == "create":

                api_client.call(
                    "POST",
                    "/doctors/me/consultation-types",
                    {
                        "type_name": request.form.get(
                            "type_name",
                            "",
                        ),
                        "fee": request.form.get(
                            "fee",
                            "",
                        ),
                    },
                )

                flash(
                    "Consultation type added.",
                    "success",
                )

            elif action == "update":

                api_client.call(
                    "PUT",
                    f"/doctors/me/consultation-types/"
                    f"{request.form.get('id', '')}",
                    {
                        "fee": request.form.get(
                            "fee",
                            "",
                        ),
                    },
                )

                flash(
                    "Fee updated.",
                    "success",
                )

            elif action == "delete":

                api_client.call(
                    "DELETE",
                    f"/doctors/me/consultation-types/"
                    f"{request.form.get('id', '')}",
                )

                flash(
                    "Consultation type deleted.",
                    "success",
                )

        except ApiError as exc:
            flash(
                exc.message,
                "error",
            )

        return redirect(
            url_for("doctor.types")
        )

    data = api_client.call(
        "GET",
        "/doctors/me/consultation-types",
    )

    # Remove consultation types that this doctor already owns.
    # Comparison is case-insensitive.
    existing = {
        str(item["type_name"])
        .strip()
        .lower()
        for item in data["items"]
    }

    data["allowed_types"] = [
        item
        for item in data.get("allowed_types", [])
        if str(item["type_name"])
        .strip()
        .lower()
        not in existing
    ]

    return render_template(
        "doctor/types.html",
        **data,
    )


# -------------------------------------------------------------- availability

def _rows_from_form(form):

    rows = []

    for i in range(
        int(
            form.get(
                "row_count",
                "0",
            )
            or 0
        )
    ):

        rows.append(
            {
                "availability_id": form.get(
                    f"p{i}_id"
                )
                or None,

                "consultation_type_id": form.get(
                    f"p{i}_type",
                    "",
                ),

                "day_of_week": form.get(
                    f"p{i}_day",
                    "",
                ),

                "start_time": form.get(
                    f"p{i}_start",
                    "",
                ),

                "end_time": form.get(
                    f"p{i}_end",
                    "",
                ),

                "capacity": form.get(
                    f"p{i}_capacity",
                    "",
                )
                or "1",

                "remove": (
                    form.get(
                        f"p{i}_remove"
                    )
                    == "on"
                ),
            }
        )

    return rows


@bp.route(
    "/availability",
    methods=["GET", "POST"],
    endpoint="availability",
)
@role_required("doctor")
def availability():

    types_data = api_client.call(
        "GET",
        "/doctors/me/consultation-types",
    )["items"]

    rows = None

    if request.method == "POST":

        rows = _rows_from_form(
            request.form
        )

        patterns = []

        for r in rows:

            if r["remove"]:
                continue

            if not (
                r["consultation_type_id"]
                and r["start_time"]
                and r["end_time"]
                and r["day_of_week"]
            ):
                continue

            item = {
                "consultation_type_id": r[
                    "consultation_type_id"
                ],
                "day_of_week": r[
                    "day_of_week"
                ],
                "start_time": r[
                    "start_time"
                ],
                "end_time": r[
                    "end_time"
                ],
                "capacity": r[
                    "capacity"
                ],
            }

            if r["availability_id"]:
                item["availability_id"] = (
                    r["availability_id"]
                )

            patterns.append(item)

        try:

            result = api_client.call(
                "PUT",
                "/doctors/me/availability",
                {
                    "patterns": patterns
                },
            )

            flash(
                f"Availability saved. "
                f"{result['slots']['created']} slot(s) created "
                f"in the next "
                f"{result['slots']['window_days']} days.",
                "success",
            )

            return redirect(
                url_for("doctor.availability")
            )

        except ApiError as exc:
            flash(
                exc.message,
                "error",
            )

    saved = api_client.call(
        "GET",
        "/doctors/me/availability",
    )

    if rows is None:
        rows = [
            dict(
                p,
                remove=False,
                day_of_week=str(
                    p["day_of_week"]
                ),
            )
            for p in saved["patterns"]
        ]

    rows = rows + [
        {
            "availability_id": None,
            "consultation_type_id": "",
            "day_of_week": "",
            "start_time": "",
            "end_time": "",
            "capacity": "1",
            "remove": False,
        }
        for _ in range(3)
    ]

    return render_template(
        "doctor/availability.html",
        rows=rows,
        types=types_data,
        days=DAYS,
        timezone=saved["timezone"],
    )


# ---------------------------------------------------------------- blackouts

@bp.route(
    "/blackouts",
    methods=["GET", "POST"],
    endpoint="blackouts",
)
@role_required("doctor")
def blackouts():

    if request.method == "POST":

        try:

            if request.form.get("action") == "delete":

                api_client.call(
                    "DELETE",
                    f"/doctors/me/blackouts/"
                    f"{request.form.get('id', '')}",
                )

                flash(
                    "Blackout removed.",
                    "success",
                )

            else:

                result = api_client.call(
                    "POST",
                    "/doctors/me/blackouts",
                    {
                        "blackout_date": request.form.get(
                            "blackout_date",
                            "",
                        ),
                        "reason": request.form.get(
                            "reason",
                            "",
                        ),
                    },
                )

                note = (
                    f" {result['slots_with_bookings']} "
                    f"slot(s) already have bookings."
                    if result["slots_with_bookings"]
                    else ""
                )

                flash(
                    f"Blackout added; "
                    f"{result['slots_blocked']} slot(s) blocked."
                    f"{note}",
                    "success",
                )

        except ApiError as exc:
            flash(
                exc.message,
                "error",
            )

        return redirect(
            url_for("doctor.blackouts")
        )

    return render_template(
        "doctor/blackouts.html",
        **api_client.call(
            "GET",
            "/doctors/me/blackouts",
        ),
    )


# ---------------------------------------------------------------- schedule

@bp.route(
    "/schedule",
    endpoint="schedule",
)
@role_required("doctor")
def schedule():

    view = (
        "week"
        if request.args.get("view") == "week"
        else "day"
    )

    try:
        chosen = date.fromisoformat(
            request.args.get("date", "")
        )
    except ValueError:
        chosen = None

    if view == "week":

        anchor = (
            chosen
            or datetime.utcnow().date()
        )

        start = anchor - timedelta(
            days=anchor.weekday()
        )

        data = api_client.call(
            "GET",
            "/doctors/me/schedule",
            params={
                "from": start.isoformat(),
                "to": (
                    start + timedelta(days=6)
                ).isoformat(),
            },
        )

        calendar_data = data

        step = timedelta(days=7)

    else:

        data = api_client.call(
            "GET",
            "/doctors/me/schedule",
            params=(
                {
                    "date": chosen.isoformat()
                }
                if chosen
                else None
            ),
        )

        anchor = date.fromisoformat(
            data["date"]
        )

        week_start = (
            anchor
            - timedelta(days=anchor.weekday())
        )

        # Fetch the whole week as well so the day view
        # still has a useful calendar at the top.
        calendar_data = api_client.call(
            "GET",
            "/doctors/me/schedule",
            params={
                "from": week_start.isoformat(),
                "to": (
                    week_start
                    + timedelta(days=6)
                ).isoformat(),
            },
        )

        step = timedelta(days=1)

    return render_template(
        "doctor/schedule.html",
        data=data,
        calendar_data=calendar_data,
        view=view,
        anchor=anchor.isoformat(),
        prev_date=(
            anchor - step
        ).isoformat(),
        next_date=(
            anchor + step
        ).isoformat(),
    )