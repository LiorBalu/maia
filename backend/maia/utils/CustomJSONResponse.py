"""JSON response that serializes ObjectId, datetime and mongoengine documents."""

from fastapi.responses import JSONResponse
from bson import ObjectId
import json


from datetime import datetime


def default_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class CustomJSONResponse(JSONResponse):
    def render(self, content: any) -> bytes:
        return json.dumps(content, default=default_serializer).encode("utf-8")
