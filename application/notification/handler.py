import os, json
import boto3
from application.common.http import response, request_method, request_body

sns=boto3.client("sns")
def lambda_handler(event, context):
    # Intended for internal EventBridge/Lambda invocation. Do not expose as public route.
    if "Records" in event and any(r.get("EventSource")=="aws:sns" for r in event["Records"]):
        print("Received SNS notification event.")
        return {"statusCode":200,"body":"ok"}
    if event.get("source")=="medislot.appointments" or event.get("detail-type") in ("AppointmentCreated","AppointmentCancelled","AppointmentRescheduled"):
        topic=os.environ.get("NOTIFICATION_TOPIC_ARN")
        if not topic: raise RuntimeError("NOTIFICATION_TOPIC_ARN is not configured")
        detail=event.get("detail",{})
        message=json.dumps({"event":event.get("detail-type"),"detail":detail},default=str)
        sns.publish(TopicArn=topic,Subject="MediSlot appointment update",Message=message)
        return {"statusCode":200,"body":"notification published"}
    if request_method(event)=="POST":
        # For controlled administrative testing only; protect this function from public invocation.
        try: body=request_body(event)
        except ValueError as e: return response(400,{"error":"INVALID_JSON","message":str(e)})
        topic=os.environ.get("NOTIFICATION_TOPIC_ARN")
        if not topic: return response(500,{"error":"NOTIFICATION_TOPIC_NOT_CONFIGURED"})
        if not body.get("message"): return response(400,{"error":"MESSAGE_REQUIRED"})
        sns.publish(TopicArn=topic,Subject=str(body.get("subject","MediSlot notification"))[:100],Message=str(body["message"]))
        return response(202,{"message":"Notification submitted."})
    return response(405,{"error":"METHOD_NOT_ALLOWED"})
