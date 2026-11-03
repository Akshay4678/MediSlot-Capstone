"""MediSlot web application: one Flask app, two portals (/doctor/* and /patient/*)."""

import os
import secrets

from flask import Flask, flash, redirect, render_template, request, url_for

from web import api_client, sessions
from web.portals import doctor, patient


def _secret_key():
    explicit = os.environ.get("SECRET_KEY")
    if explicit:
        return explicit
    parameter = os.environ.get("SECRET_KEY_PARAMETER")
    if parameter:
        import boto3  # imported lazily so local tests do not need AWS
        return boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    if os.environ.get("ALLOW_EPHEMERAL_SECRET") == "1":  # local development only
        return secrets.token_hex(32)
    raise RuntimeError("Set SECRET_KEY or SECRET_KEY_PARAMETER (an SSM SecureString).")


def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
    )
    app.register_blueprint(doctor.bp)
    app.register_blueprint(patient.bp)

    @app.route("/", endpoint="landing")
    def landing():
        return render_template("landing.html")

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.before_request
    def csrf_protect():
        """Every POST (including login and registration) must carry this browser's CSRF token."""
        if request.method == "POST" and request.endpoint != "static":
            if not sessions.csrf_ok(request.form.get("csrf_token")):
                flash("The page was open too long. Please try again.", "error")
                return redirect(request.path if request.path.endswith(("/login", "/register")) else url_for("landing"))

    @app.context_processor
    def inject():
        session = sessions.current()
        return {"csrf_token": sessions.csrf_token, "session_role": session["role"] if session else None}

    @app.errorhandler(api_client.SessionExpired)
    def session_expired(_):
        flash("Your session has ended. Please sign in again.", "error")
        return redirect(url_for("landing"))

    @app.errorhandler(413)
    def too_large(_):
        flash("That upload is too large.", "error")
        return redirect(request.referrer or url_for("landing"))

    @app.after_request
    def headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app
