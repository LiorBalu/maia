"""Downloads an image URL and returns it as base64 (for vision prompts)."""

import base64
import requests
from typing import Optional
import logging
from io import BytesIO
from PIL import Image

logger = logging.getLogger(__name__)

# Maximum size in bytes (10MB)
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024


def _resolve_image_mime_type(header_mime_type: str, image_url: str) -> str:
    """Return the Content-Type when it is an image type, else infer the type from the URL's extension.

    Falls back to image/jpeg when no known extension matches.
    """
    mime_type = header_mime_type

    # If content type is not an image, try to infer from URL
    if not mime_type.startswith("image/"):
        if image_url.lower().endswith(".png"):
            mime_type = "image/png"
        elif image_url.lower().endswith(".jpg") or image_url.lower().endswith(".jpeg"):
            mime_type = "image/jpeg"
        elif image_url.lower().endswith(".gif"):
            mime_type = "image/gif"
        elif image_url.lower().endswith(".webp"):
            mime_type = "image/webp"
        else:
            mime_type = "image/jpeg"  # default fallback

    return mime_type


def _flatten_transparency_to_rgb(image: Image.Image) -> Image.Image:
    """Paste an RGBA, LA or palette image onto a white RGB canvas, since JPEG has no alpha channel.

    An image in any other mode is returned unchanged.
    """
    if image.mode not in ("RGBA", "LA", "P"):
        return image

    white_canvas = Image.new("RGB", image.size, (255, 255, 255))
    if image.mode == "P":
        image = image.convert("RGBA")
    white_canvas.paste(
        image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None
    )
    return white_canvas


def _encode_scaled_jpeg(image: Image.Image, shrink_ratio: float, jpeg_quality: int) -> bytes:
    """Encode one pass of the shrink loop: scale the image by shrink_ratio when it is below 1.0, then save as JPEG."""
    # Resize image if scale factor is less than 1
    if shrink_ratio < 1.0:
        scaled_size = (
            int(image.size[0] * shrink_ratio),
            int(image.size[1] * shrink_ratio),
        )
        candidate_image = image.resize(scaled_size, Image.Resampling.LANCZOS)
    else:
        candidate_image = image

    # Save to BytesIO with current quality
    jpeg_buffer = BytesIO()
    candidate_image.save(jpeg_buffer, format="JPEG", quality=jpeg_quality, optimize=True)
    return jpeg_buffer.getvalue()


def _shrink_image_below_limit(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Re-encode the image as JPEG, lowering the quality and then the size, until it fits MAX_IMAGE_SIZE_BYTES.

    Best effort: may return bytes still above the limit, labelled with the original MIME type.
    Returns (image_bytes, mime_type); mime_type becomes image/jpeg only when a pass fits the limit.
    """
    # Open image with PIL
    decoded_image = Image.open(BytesIO(image_bytes))

    # Convert RGBA to RGB if necessary (for JPEG)
    flattened_image = _flatten_transparency_to_rgb(decoded_image)

    # Start with 85% quality and reduce dimensions if needed
    jpeg_quality = 85
    shrink_ratio = 1.0

    while True:
        image_bytes = _encode_scaled_jpeg(flattened_image, shrink_ratio, jpeg_quality)

        # Check if size is acceptable
        if len(image_bytes) <= MAX_IMAGE_SIZE_BYTES:
            mime_type = "image/jpeg"
            logger.info(
                f"Resized image to {len(image_bytes)} bytes (quality={jpeg_quality}, scale={shrink_ratio:.2f})"
            )
            break

        # Reduce quality or scale
        if jpeg_quality > 60:
            jpeg_quality -= 5
        else:
            shrink_ratio -= 0.1
            jpeg_quality = 85  # Reset quality when scaling down

        # Safety check to avoid infinite loop
        if shrink_ratio < 0.1:
            logger.warning(
                "Could not reduce image size below 10MB, using best effort"
            )
            break

    return image_bytes, mime_type


def fetch_image_as_data_url(image_url: str, timeout_seconds: int = 10) -> Optional[str]:
    """
    Convert an image URL to a base64-encoded data URL.

    Args:
        image_url: The HTTP/HTTPS URL of the image
        timeout_seconds: Request timeout in seconds (default: 10)

    Returns:
        A base64-encoded data URL string (e.g., 'data:image/jpeg;base64,/9j/4AAQ...')
        or None if the conversion fails

    Note:
        This is useful for converting Google Cloud Storage URLs to a format
        that OpenAI's API accepts, especially when the GCS bucket is not publicly accessible.
        Images larger than 10MB will be automatically resized and compressed.
    """
    try:
        # Download the image
        download_response = requests.get(image_url, timeout=timeout_seconds)
        download_response.raise_for_status()

        # Get the content type from the response headers
        header_mime_type = download_response.headers.get("Content-Type", "image/jpeg")
        mime_type = _resolve_image_mime_type(header_mime_type, image_url)

        image_bytes = download_response.content

        # Check if image size exceeds 10MB
        if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
            logger.info(
                f"Image size {len(image_bytes)} bytes exceeds {MAX_IMAGE_SIZE_BYTES} bytes, resizing..."
            )
            image_bytes, mime_type = _shrink_image_below_limit(image_bytes, mime_type)

        # Encode the image content to base64
        base64_payload = base64.b64encode(image_bytes).decode("utf-8")

        # Return the data URL
        return f"data:{mime_type};base64,{base64_payload}"

    except requests.exceptions.RequestException as download_error:
        logger.error(f"Failed to download image from {image_url}: {download_error}")
        return None
    except Exception as error:
        logger.error(f"Failed to convert image URL to base64: {error}")
        return None
