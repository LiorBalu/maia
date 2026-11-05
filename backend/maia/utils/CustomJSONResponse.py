"""JSON response that serializes ObjectId, datetime and mongoengine documents."""

from fastapi.responses import JSONResponse
from bson import ObjectId
import json


from datetime import datetime


def serialize_object_id_or_datetime(value):
    """json fallback: datetimes as ISO strings, ObjectId as str."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class CustomJSONResponse(JSONResponse):
    """JSONResponse that can render Mongo documents (ObjectId, datetime)."""
    def render(self, content: any) -> bytes:
        """Serialize the content with serialize_object_id_or_datetime."""
        return json.dumps(content, default=serialize_object_id_or_datetime).encode("utf-8")
