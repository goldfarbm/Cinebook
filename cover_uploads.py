"""Validate local cover files while retaining their original image bytes."""
import io

from PIL import Image, UnidentifiedImageError

MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_COVER_PIXELS = 20_000_000


def validate_cover_image(data):
    if not data or len(data) > MAX_COVER_BYTES:
        raise ValueError('Choose a cover image no larger than 5 MB.')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Choose a JPEG, PNG, or WebP image.')
            if image.width * image.height > MAX_COVER_PIXELS or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Choose a still cover image with no more than 20 million pixels.')
            image.verify()
        # Decode too: header checks alone can accept truncated or corrupt JPEGs.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError('This file is not a valid JPEG, PNG, or WebP image.') from None
    return data


def cover_mimetype(data):
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'
