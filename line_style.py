"""
Helpers for EdgeWizard line styles (Thin / Bold).

- THIN  -> original behaviour, no pre-smoothing
- BOLD  -> adaptive smoothing before edge detection
"""

from typing import Literal, Optional

import numpy as np
from PIL import Image
from skimage import filters


LINE_STYLE_THIN: str = "thin"
LINE_STYLE_BOLD: str = "bold"


def apply_line_style(
    img_color: Image.Image,
    style: Optional[str],
) -> Image.Image:
    """
    Apply the requested line style to the input image.

    - "thin":  return the image unchanged
    - "bold":  apply adaptive smoothing for thicker, smoother lines
    - other:   treated as "thin"
    """
    if style is None:
        style_normalized = LINE_STYLE_THIN
    else:
        style_normalized = style.strip().lower()

    if style_normalized == LINE_STYLE_BOLD:
        return _adaptive_smooth_rgb(img_color)

    # Default / THIN: no pre-processing
    return img_color


def _adaptive_smooth_rgb(img_color: Image.Image) -> Image.Image:
    """
    Light, adaptive Gaussian smoothing of the RGB image to reduce micro-noise
    without destroying contours. This is used for the 'Bold Lines' style.
    """
    arr = np.array(img_color).astype("float32") / 255.0
    h, w, _ = arr.shape

    base_sigma = 0.8
    scale = min(h, w) / 512.0
    sigma = base_sigma * scale
    sigma = float(np.clip(sigma, 0.6, 1.8))

    smoothed = np.empty_like(arr)
    for c in range(3):
        smoothed[:, :, c] = filters.gaussian(
            arr[:, :, c],
            sigma=sigma,
            preserve_range=True,
        )

    smoothed = np.clip(smoothed, 0.0, 1.0)
    return Image.fromarray((smoothed * 255).astype("uint8"), mode="RGB")
