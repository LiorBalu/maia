"""Downloads an image URL and returns it as base64 (for vision prompts)."""

import base64
import requests
from typing import Optional
import logging
from io import BytesIO
from PIL import Image

logger = logging.getLogger(__name__)

# Maximum size in bytes (10MB)
MAX_SIZE_BYTES = 10 * 1024 * 1024


def convert_image_url_to_base64(url: str, timeout: int = 10) -> Optional[str]:
    """
    Convert an image URL to a base64-encoded data URL.

    Args:
        url: The HTTP/HTTPS URL of the image
        timeout: Request timeout in seconds (default: 10)

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
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()

        # Get the content type from the response headers
        content_type = response.headers.get("Content-Type", "image/jpeg")

        # If content type is not an image, try to infer from URL
        if not content_type.startswith("image/"):
            if url.lower().endswith(".png"):
                content_type = "image/png"
            elif url.lower().endswith(".jpg") or url.lower().endswith(".jpeg"):
                content_type = "image/jpeg"
            elif url.lower().endswith(".gif"):
                content_type = "image/gif"
            elif url.lower().endswith(".webp"):
                content_type = "image/webp"
            else:
                content_type = "image/jpeg"  # default fallback

        image_content = response.content

        # Check if image size exceeds 10MB
        if len(image_content) > MAX_SIZE_BYTES:
            logger.info(
                f"Image size {len(image_content)} bytes exceeds {MAX_SIZE_BYTES} bytes, resizing..."
            )

            # Open image with PIL
            img = Image.open(BytesIO(image_content))

            # Convert RGBA to RGB if necessary (for JPEG)
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
                )
                img = background

            # Start with 85% quality and reduce dimensions if needed
            quality = 85
            scale_factor = 1.0

            while True:
                # Resize image if scale factor is less than 1
                if scale_factor < 1.0:
                    new_size = (
                        int(img.size[0] * scale_factor),
                        int(img.size[1] * scale_factor),
                    )
                    resized_img = img.resize(new_size, Image.Resampling.LANCZOS)
                else:
                    resized_img = img

                # Save to BytesIO with current quality
                output = BytesIO()
                resized_img.save(output, format="JPEG", quality=quality, optimize=True)
                image_content = output.getvalue()

                # Check if size is acceptable
                if len(image_content) <= MAX_SIZE_BYTES:
                    content_type = "image/jpeg"
                    logger.info(
                        f"Resized image to {len(image_content)} bytes (quality={quality}, scale={scale_factor:.2f})"
                    )
                    break

                # Reduce quality or scale
                if quality > 60:
                    quality -= 5
                else:
                    scale_factor -= 0.1
                    quality = 85  # Reset quality when scaling down

                # Safety check to avoid infinite loop
                if scale_factor < 0.1:
                    logger.warning(
                        "Could not reduce image size below 10MB, using best effort"
                    )
                    break

        # Encode the image content to base64
        base64_image = base64.b64encode(image_content).decode("utf-8")

        # Return the data URL
        return f"data:{content_type};base64,{base64_image}"

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download image from {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to convert image URL to base64: {e}")
        return None
