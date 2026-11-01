"""Idempotent rolling 30-day slot materialiser with IANA timezone/DST handling."""
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import boto3
try:
    from aws_xray_sdk.core import patch_all
    patch_all()
except ImportError:
    pass
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

log=logging.getLogger(__name__); log.setLevel(logging.INFO)


def _as_time(value):
    if isinstance(value,time): return value
    return time.fromisoformat(str(value))


def _valid_local(naive, zone, fold=0):
    """Convert local wall time to aware time; reject DST gaps, select first fold."""
    aware=naive.replace(tzinfo=zone,fold=fold)
    roundtrip=aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip != naive: return None
    return aware


def _slot_id(availability_id, start_utc):
    return f"{availability_id}#{start_utc.strftime('%Y%m%dT%H%M%SZ')}"


def _blackout_dates(cursor, doctor_id, start_date, end_date):
    cursor.execute("SELECT blackout_date FROM blackout_dates WHERE doctor_id=%s AND blackout_date BETWEEN %s AND %s",(doctor_id,start_date,end_date))
    return {r["blackout_date"].isoformat() if hasattr(r["blackout_date"],"isoformat") else str(r["blackout_date"]) for r in cursor.fetchall()}


def materialise(start_date=None, days=30):
    if not 1 <= int(days) <= 90: raise ValueError("days must be between 1 and 90")
    start_date=start_date or datetime.now(timezone.utc).date()
    end_date=start_date+timedelta(days=days-1)
    from application.database.connection import get_connection
    table_name=os.environ["MATERIALISED_SLOTS_TABLE"]
    table=boto3.resource("dynamodb").Table(table_name)
    created=0; existing=0; skipped_dst=0; blackout_skips=0
    valid_slot_ids=set()
    conn=get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT ap.availability_id,ap.doctor_id,ap.consultation_type_id,ap.day_of_week,
                ap.start_time,ap.end_time,ap.capacity,ct.duration_minutes,d.timezone
                FROM availability_patterns ap JOIN consultation_types ct ON ct.consultation_type_id=ap.consultation_type_id
                JOIN doctors d ON d.doctor_id=ap.doctor_id
                WHERE ap.is_active=TRUE AND ct.is_active=TRUE AND d.is_active=TRUE
                AND ap.day_of_week BETWEEN 1 AND 7""")
            patterns=cur.fetchall()
            blackout_cache={}
            for p in patterns:
                doctor_id=int(p["doctor_id"])
                if doctor_id not in blackout_cache: blackout_cache[doctor_id]=_blackout_dates(cur,doctor_id,start_date,end_date)
                try: zone=ZoneInfo(p["timezone"] or "Asia/Kolkata")
                except ZoneInfoNotFoundError: log.error("Skipping doctor_id=%s: invalid timezone",doctor_id); continue
                local_start=_as_time(p["start_time"]); local_end=_as_time(p["end_time"])
                duration=int(p["duration_minutes"]); availability_id=int(p["availability_id"])
                day=start_date
                while day<=end_date:
                    if day.isoweekday()==int(p["day_of_week"]):
                        if day.isoformat() in blackout_cache[doctor_id]: blackout_skips+=1
                        else:
                            cursor=datetime.combine(day,local_start)
                            finish=datetime.combine(day,local_end)
                            while cursor+timedelta(minutes=duration)<=finish:
                                slot_end=cursor+timedelta(minutes=duration)
                                local_a=_valid_local(cursor,zone,0); local_b=_valid_local(slot_end,zone,0)
                                if local_a is None or local_b is None:
                                    skipped_dst+=1
                                else:
                                    utc_a=local_a.astimezone(timezone.utc); utc_b=local_b.astimezone(timezone.utc)
                                    actual_minutes=(utc_b-utc_a).total_seconds()/60
                                    if actual_minutes == duration:
                                        sid=_slot_id(availability_id,utc_a)
                                        item={"slot_id":sid,"availability_id":availability_id,"doctor_id":doctor_id,
                                            "consultation_type_id":int(p["consultation_type_id"]),"date":day.isoformat(),
                                            "start_time":cursor.strftime("%H:%M:%S"),"end_time":slot_end.strftime("%H:%M:%S"),
                                            "capacity":int(p["capacity"]),"booked_count":0,"status":"AVAILABLE",
                                            "created_at":datetime.now(timezone.utc).isoformat()}
                                        valid_slot_ids.add(sid)
                                        try:
                                            table.put_item(Item=item,ConditionExpression=Attr("slot_id").not_exists())
                                            created+=1
                                        except ClientError as exc:
                                            if exc.response.get("Error",{}).get("Code")=="ConditionalCheckFailedException":
                                                existing+=1
                                                # Reactivate a previously disabled slot only when it has no bookings.
                                                try:
                                                    table.update_item(Key={"slot_id":sid},
                                                        UpdateExpression="SET #st=:available, capacity=:capacity",
                                                        ConditionExpression="booked_count = :zero",
                                                        ExpressionAttributeNames={"#st":"status"},
                                                        ExpressionAttributeValues={":available":"AVAILABLE",":capacity":int(p["capacity"]),":zero":0})
                                                except ClientError as update_exc:
                                                    if update_exc.response.get("Error",{}).get("Code") != "ConditionalCheckFailedException": raise
                                            else: raise
                                    else: skipped_dst+=1
                                cursor+=timedelta(minutes=duration)
                    day+=timedelta(days=1)
    finally: conn.close()
    # Disable stale, not-yet-booked future slots when a pattern is deactivated,
    # changed, or covered by a newly-added blackout date. Never alter booked slots.
    scan_args={"FilterExpression":Attr("date").between(start_date.isoformat(),end_date.isoformat())}
    while True:
        page=table.scan(**scan_args)
        for old in page.get("Items",[]):
            if old.get("slot_id") not in valid_slot_ids and int(old.get("booked_count",0)) == 0:
                try:
                    table.update_item(Key={"slot_id":old["slot_id"]},
                        UpdateExpression="SET #st=:unavailable",
                        ConditionExpression="booked_count = :zero",
                        ExpressionAttributeNames={"#st":"status"},
                        ExpressionAttributeValues={":unavailable":"UNAVAILABLE",":zero":0})
                except ClientError as exc:
                    if exc.response.get("Error",{}).get("Code") != "ConditionalCheckFailedException":
                        raise
        last=page.get("LastEvaluatedKey")
        if not last: break
        scan_args["ExclusiveStartKey"]=last
    return {"start_date":start_date.isoformat(),"end_date":end_date.isoformat(),"created":created,
            "already_existed":existing,"skipped_dst_nonexistent_or_invalid":skipped_dst,"blackout_dates_skipped":blackout_skips}


def lambda_handler(event, context):
    try:
        requested=(event or {}).get("start_date")
        start=date.fromisoformat(requested) if requested else datetime.now(timezone.utc).date()
        days=int((event or {}).get("days",os.environ.get("MATERIALISATION_DAYS","30")))
        result=materialise(start,days)
        log.info("Slot materialisation complete: %s",result)
        return {"statusCode":200,"body":result}
    except Exception:
        log.exception("Slot materialisation failed")
        raise
