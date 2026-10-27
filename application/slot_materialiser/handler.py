"""Scheduled internal job: materialises schedule windows for inspection.
This does not write to DynamoDB because the deployed SlotCapacityTable key schema
and slot ID convention must be confirmed before capacity items are created.
"""
from datetime import date, timedelta
from application.database.connection import get_connection

def lambda_handler(event, context):
    days=int(event.get("days",14)) if isinstance(event,dict) else 14
    days=max(1,min(days,31))
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ap.doctor_id,ap.consultation_type_id,ap.weekday,
                                  ap.start_local_time,ap.end_local_time,ap.timezone_name,
                                  ap.effective_from,ap.effective_until,ct.duration_minutes,ct.capacity
                           FROM availability_patterns ap JOIN consultation_types ct
                           ON ct.consultation_type_id=ap.consultation_type_id
                           WHERE ap.active=TRUE AND ct.is_active=TRUE""")
            patterns=cur.fetchall()
            cur.execute("SELECT doctor_id,local_date,start_local_time,end_local_time,reason FROM blackout_dates")
            blackouts=cur.fetchall()
        today=date.today()
        windows=[]
        for offset in range(days):
            day=today+timedelta(days=offset)
            for p in patterns:
                if day.isoweekday()!=int(p["weekday"]) or day<p["effective_from"] or (p["effective_until"] and day>p["effective_until"]): continue
                blocked=[b for b in blackouts if str(b["doctor_id"])==str(p["doctor_id"]) and b["local_date"]==day]
                if any(b["start_local_time"] is None or not (p["end_local_time"]<=b["start_local_time"] or p["start_local_time"]>=b["end_local_time"]) for b in blocked): continue
                windows.append({"doctor_id":p["doctor_id"],"consultation_type_id":p["consultation_type_id"],
                    "date":str(day),"start_local_time":str(p["start_local_time"]),"end_local_time":str(p["end_local_time"]),
                    "timezone_name":p["timezone_name"],"duration_minutes":p["duration_minutes"],"capacity":p["capacity"]})
        return {"statusCode":200,"materialised_schedule_windows":len(windows),"windows":windows,
                "note":"Schedule windows calculated only; DynamoDB slot-capacity writes require confirmed key schema."}
    finally: conn.close()
