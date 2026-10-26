"""GCS helpers for the public bucket (media the platforms fetch by URL)."""

# Google Cloud Storage bucket (GCS_BUCKET in .env; the service account in serviceKey.json must write to it)
import logging
import os
from urllib.parse import quote
from google.cloud import storage

logger = logging.getLogger(__name__)


os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "serviceKey.json"
BUCKET_NAME = os.getenv("GCS_BUCKET", "")
if not BUCKET_NAME:
    logger.warning("GCS_BUCKET is not set: uploads to Google Cloud Storage will fail")


def upload_to_gcs(file_obj, folder: str, destination_blob_name: str, content_type: str) -> str:
    """Uploads a file to Google Cloud Storage"""
    # Ensure the folder ends with a slash
    if not folder.endswith("/"):
        folder += "/"
    # Create the full destination blob name
    destination_blob_name = f"{folder}{destination_blob_name}"
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_file(file_obj, content_type=content_type)
    # Percent-encode the path: non-ASCII blob names (e.g. Hebrew filenames) otherwise
    # produce URLs that external fetchers such as Twilio reject as invalid media URLs.
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{quote(destination_blob_name)}"


def delete_file_from_gcs(folder: str, filename: str) -> None:
    """Deletes a specific file in a folder"""
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob_name = f"{folder}/{filename}"
    blob = bucket.blob(blob_name)
    blob.delete()
    return None
