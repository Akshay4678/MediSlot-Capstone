"""Reference lists the profile form needs: GET /hospitals and GET /specialisations (any signed-in user)."""


def hospitals(ctx):
    city = str(ctx.query.get("city", "")).strip()
    sql = "SELECT hospital_id, hospital_name, address, city, phone FROM hospitals WHERE is_active = TRUE"
    params = []
    if city:
        sql += " AND city LIKE %s"
        params.append(f"%{city}%")
    with ctx.conn.cursor() as cur:
        cur.execute(sql + " ORDER BY hospital_name LIMIT 200", params)
        return 200, {"items": cur.fetchall()}


def specialisations(ctx):
    with ctx.conn.cursor() as cur:
        cur.execute("SELECT specialisation_id, specialisation_name, description FROM specialisations ORDER BY specialisation_name")
        return 200, {"items": cur.fetchall()}
