import os, time, uuid
import boto3
from application.common.http import response, request_method, request_body
from application.common.auth_context import claims

ddb=boto3.resource("dynamodb")
def lambda_handler(event, context):
    identity=claims(event)
    if not identity["user_id"] or identity["role"]!="patient": return response(401,{"error":"UNAUTHORIZED"})
    name=os.environ.get("WAITLIST_TABLE_NAME")
    if not name: return response(500,{"error":"WAITLIST_TABLE_NOT_CONFIGURED"})
    table=ddb.Table(name); method=request_method(event)
    path=(event.get("rawPath") or event.get("path") or "")
    q=event.get("queryStringParameters") or {}
    if method=="POST":
        try: body=request_body(event)
        except ValueError as e: return response(400,{"error":"INVALID_JSON","message":str(e)})
        slot_id=str(body.get("slot_id","")).strip()
        doctor_id=str(body.get("doctor_id","")).strip()
        if not slot_id or not doctor_id: return response(400,{"error":"MISSING_FIELDS","fields":["slot_id","doctor_id"]})
        # Requires table partition key waitlist_id (String); update this if Data Stack uses a different key.
        item={"waitlist_id":str(uuid.uuid4()),"user_id":identity["user_id"],"slot_id":slot_id,
              "doctor_id":doctor_id,"status":"WAITING","created_at":int(time.time()),"ttl":int(time.time())+90*86400}
        try:
            table.put_item(Item=item,ConditionExpression="attribute_not_exists(waitlist_id)")
            return response(201,{"message":"Added to waitlist.","waitlist_id":item["waitlist_id"]})
        except Exception:
            return response(500,{"error":"WAITLIST_WRITE_FAILED","message":"Verify the table key schema and IAM permissions."})
    if method=="GET":
        # Scan is deliberately avoided; a queryable user_id GSI is required.
        index=os.environ.get("WAITLIST_USER_INDEX","user_id-index")
        try:
            items=table.query(IndexName=index,KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(identity["user_id"])).get("Items",[])
            return response(200,{"items":items,"count":len(items)})
        except Exception:
            return response(500,{"error":"WAITLIST_QUERY_FAILED","message":"Configure a GSI partitioned by user_id or adjust WAITLIST_USER_INDEX."})
    if method=="DELETE":
        waitlist_id=(event.get("pathParameters") or {}).get("waitlist_id") or q.get("waitlist_id")
        if not waitlist_id: return response(400,{"error":"WAITLIST_ID_REQUIRED"})
        try:
            table.delete_item(Key={"waitlist_id":waitlist_id},ConditionExpression="user_id = :u",ExpressionAttributeValues={":u":identity["user_id"]})
            return response(200,{"message":"Waitlist entry removed."})
        except Exception: return response(404,{"error":"WAITLIST_ENTRY_NOT_FOUND"})
    return response(405,{"error":"METHOD_NOT_ALLOWED"})
