@bp.route("/login", methods=["GET", "POST"], endpoint="login")
def login():
    if request.method == "POST":
        try:
            tokens = api_client.public(
                "POST",
                f"/auth/{role}s/login",
                {
                    "email": request.form.get("email", "").strip(),
                    "password": request.form.get("password", ""),
                },
            )

        except api_client.ApiError as exc:
            flash(exc.message, "error")
            return (
                render_template("login.html", role=role),
                401 if exc.status == 401 else 200,
            )

        data = sessions.create(tokens)

        if data["role"] != role:
            api_client.logout()
            flash("That account is not a " + role + " account.", "error")
            return redirect(url_for("landing"))

        return redirect(url_for(f"{role}.home"))

    current = sessions.current()

    if current and current.get("role") == role:
        return redirect(url_for(f"{role}.home"))

    return render_template("login.html", role=role)