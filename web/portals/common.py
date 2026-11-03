"""Pieces shared by both portals: role guards, login / register / logout views."""

from functools import wraps

from flask import flash, redirect, render_template, request, url_for

from web import api_client, sessions


def role_required(role):
    """Route guard. No session -> that portal's login. Wrong role -> back to the landing page."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            session = sessions.current()
            if not session:
                return redirect(url_for(f"{role}.login"))
            if session.get("role") != role:
                flash("That area is not available for your account type.", "error")
                return redirect(url_for("landing"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def add_auth_routes(bp, role):
    other = "patient" if role == "doctor" else "doctor"

    @bp.route("/login", methods=["GET", "POST"], endpoint="login")
    def login():
        if request.method == "POST":
            try:
                tokens = api_client.public("POST", f"/auth/{role}s/login", {
                    "email": request.form.get("email", "").strip(), "password": request.form.get("password", "")})
            except api_client.ApiError as exc:
                flash(exc.message, "error")
                return render_template("login.html", role=role), 401 if exc.status == 401 else 200
            data = sessions.create(tokens)
            if data["role"] != role:  # defensive: the API only issues a matching role
                api_client.logout()
                flash("That account is not a " + role + " account.", "error")
                return redirect(url_for("landing"))
            return redirect(url_for(f"{role}.home"))
        current = sessions.current()
        if current and current.get("role") == role:
            return redirect(url_for(f"{role}.home"))
        return render_template("login.html", role=role)

    @bp.route("/register", methods=["GET", "POST"], endpoint="register")
    def register():
        if request.method == "POST":
            payload = {"email": request.form.get("email", "").strip(), "password": request.form.get("password", "")}
            if role == "patient":
                payload["first_name"] = request.form.get("first_name", "").strip()
                payload["last_name"] = request.form.get("last_name", "").strip()
                payload["phone"] = request.form.get("phone", "").strip()
            elif request.form.get("registration_code"):
                payload["registration_code"] = request.form["registration_code"].strip()
            try:
                api_client.public("POST", f"/auth/{role}s/register", payload)
            except api_client.ApiError as exc:
                flash(exc.message, "error")
                return render_template("register.html", role=role)
            flash("Account created. Please sign in.", "success")
            return redirect(url_for(f"{role}.login"))
        return render_template("register.html", role=role)

    @bp.route("/logout", methods=["POST"], endpoint="logout")
    @role_required(role)
    def logout():
        api_client.logout()
        flash("You have been signed out.", "success")
        return redirect(url_for("landing"))

    bp.other_role = other
