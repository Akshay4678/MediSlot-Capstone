"""Shared authentication routes and role guards for doctor and patient portals."""

from functools import wraps

from flask import flash, redirect, render_template, request, url_for

from web import api_client, sessions


def role_required(role):
    """Allow access only to users with the required role."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            current = sessions.current()

            if not current:
                return redirect(url_for(f"{role}.login"))

            if current.get("role") != role:
                flash(
                    "That area is not available for your account type.",
                    "error",
                )
                return redirect(url_for("landing"))

            return view(*args, **kwargs)

        return wrapped

    return decorator


def add_auth_routes(bp, role):
    """Register login, registration and logout routes for a portal."""

    other = "patient" if role == "doctor" else "doctor"

    @bp.route("/login", methods=["GET", "POST"], endpoint="login")
    def login():
        """
        Handle login for the current portal.

        A user may switch directly between doctor and patient login.
        Therefore, an existing session must NOT block a POST to the
        other login endpoint.
        """

        # ---------------------------------------------------------
        # POST = attempt a new login
        # ---------------------------------------------------------
        if request.method == "POST":
            try:
                tokens = api_client.public(
                    "POST",
                    f"/auth/{role}s/login",
                    {
                        "email": request.form.get(
                            "email",
                            "",
                        ).strip(),
                        "password": request.form.get(
                            "password",
                            "",
                        ),
                    },
                )

            except api_client.ApiError as exc:
                flash(exc.message, "error")

                return (
                    render_template(
                        "login.html",
                        role=role,
                        csrf_value=sessions.csrf_token(),
                    ),
                    401 if exc.status == 401 else 200,
                )

            # -----------------------------------------------------
            # Create a NEW session from the newly authenticated user.
            #
            # This replaces the existing session if the browser was
            # previously logged in as the other role.
            # -----------------------------------------------------
            data = sessions.create(tokens)

            # -----------------------------------------------------
            # Make sure the authenticated account belongs to the
            # portal being used.
            # -----------------------------------------------------
            if data["role"] != role:
                api_client.logout()

                flash(
                    "That account is not a " + role + " account.",
                    "error",
                )

                return redirect(url_for("landing"))

            return redirect(url_for(f"{role}.home"))

        # ---------------------------------------------------------
        # GET = display login page.
        #
        # Only here do we check whether the browser already has
        # an authenticated session.
        # ---------------------------------------------------------
        current = sessions.current()

        if current:
            current_role = current.get("role")

            if current_role == role:
                return redirect(url_for(f"{role}.home"))

            if current_role in ("doctor", "patient"):
                return redirect(url_for(f"{current_role}.home"))

        return render_template(
            "login.html",
            role=role,
            csrf_value=sessions.csrf_token(),
        )

    @bp.route("/register", methods=["GET", "POST"], endpoint="register")
    def register():
        """
        Handle registration for the current portal.

        An already authenticated user should not be shown a
        registration page.
        """

        current = sessions.current()

        if current and current.get("role") in (
            "doctor",
            "patient",
        ):
            return redirect(
                url_for(f"{current['role']}.home")
            )

        if request.method == "POST":
            payload = {
                "email": request.form.get(
                    "email",
                    "",
                ).strip(),
                "password": request.form.get(
                    "password",
                    "",
                ),
            }

            # -----------------------------------------------------
            # Patient-specific registration fields
            # -----------------------------------------------------
            if role == "patient":
                payload["first_name"] = request.form.get(
                    "first_name",
                    "",
                ).strip()

                payload["last_name"] = request.form.get(
                    "last_name",
                    "",
                ).strip()

                payload["phone"] = request.form.get(
                    "phone",
                    "",
                ).strip()

            # -----------------------------------------------------
            # Doctor-specific registration code
            # -----------------------------------------------------
            elif request.form.get("registration_code"):
                payload["registration_code"] = request.form[
                    "registration_code"
                ].strip()

            try:
                api_client.public(
                    "POST",
                    f"/auth/{role}s/register",
                    payload,
                )

            except api_client.ApiError as exc:
                flash(exc.message, "error")

                return render_template(
                    "register.html",
                    role=role,
                    csrf_value=sessions.csrf_token(),
                )

            flash(
                "Account created. Please sign in.",
                "success",
            )

            return redirect(
                url_for(f"{role}.login")
            )

        return render_template(
            "register.html",
            role=role,
            csrf_value=sessions.csrf_token(),
        )

    @bp.route("/logout", methods=["POST"], endpoint="logout")
    @role_required(role)
    def logout():
        """Sign out the currently authenticated user."""

        api_client.logout()

        flash(
            "You have been signed out.",
            "success",
        )

        return redirect(url_for("landing"))

    bp.other_role = other
