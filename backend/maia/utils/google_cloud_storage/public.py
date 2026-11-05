"""GCS helpers for the public bucket (media the platforms fetch by URL)."""

# Google Cloud Storage bucket (GCS_BUCKET in .env; the service account in serviceKey.json must write to it)
import logging
import os
from urllib.parse import quote
from google.cloud import storage

logger = logging.getLogger(__name__)


os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "serviceKey.json"
PUBLIC_BUCKET_NAME = os.getenv("GCS_BUCKET", "")
if not PUBLIC_BUCKET_NAME:
    logger.warning("GCS_BUCKET is not set: uploads to Google Cloud Storage will fail")


def _build_public_url(blob_path: str) -> str:
    """Public https URL of a blob of the public bucket, its path percent-encoded."""
    # Percent-encode the path: non-ASCII blob names (e.g. Hebrew filenames) otherwise
    # produce URLs that external fetchers such as Twilio reject as invalid media URLs.
    return f"https://storage.googleapis.com/{PUBLIC_BUCKET_NAME}/{quote(blob_path)}"


def upload_file_to_public_bucket(file_stream, bucket_folder: str, relative_blob_path: str, content_type: str) -> str:
    """Uploads a file to Google Cloud Storage"""
    # Ensure the folder ends with a slash
    if not bucket_folder.endswith("/"):
        bucket_folder += "/"
    # Create the full destination blob name
    blob_path = f"{bucket_folder}{relative_blob_path}"
    gcs_client = storage.Client()
    public_bucket = gcs_client.bucket(PUBLIC_BUCKET_NAME)
    destination_blob = public_bucket.blob(blob_path)
    destination_blob.upload_from_file(file_stream, content_type=content_type)
    return _build_public_url(blob_path)
