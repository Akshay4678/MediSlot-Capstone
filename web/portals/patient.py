"""Patient portal (/patient/*). Search, slot browser and booking arrive in Milestone 4."""

from flask import Blueprint, render_template

from web.portals.common import add_auth_routes, role_required

bp = Blueprint("patient", __name__, url_prefix="/patient")
add_auth_routes(bp, "patient")


@bp.route("/", endpoint="home")
@role_required("patient")
def home():
    return render_template("patient/home.html")
