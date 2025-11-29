"""
EdgeWizard V1.0.0 - backend compatible pipeline.
This version works fully in memory (no input/output folders)
and is designed to be used from a FastAPI/Railway backend.
"""

import numpy as np
from PIL import Image, ImageDraw
from skimage import filters

# --------------------------------------------------------
# CONFIG
# --------------------------------------------------------

EDGE_GAIN = 2.0          # 1.0 = soft, >1.0 = darker edges
EDGE_GAMMA = 0.8         # <1.0 = more contrast in lines

LINE_SOFT_NORMALIZE = True
LINE_MASK_THRESH = 0.985

LINE_BRIGHTEN_STRONG = 0.8
LINE_BRIGHTEN_SOFT = 0.4

ENABLE_BRIGHT_EQUALIZER = True
BRIGHT_EQ_LOW_PCT = 5.0
BRIGHT_EQ_HIGH_PCT = 95.0
BRIGHT_EQ_BASE = 0.28
BRIGHT_EQ_RANGE = 0.06

ENABLE_GLOBAL_LIFT = True
GLOBAL_LIFT = 0.01
MIN_LINE_BRIGHT = 0.33

ENABLE_ULTRA_UNIFORMIZER = True
ULTRA_LOW_PCT = 10.0
ULTRA_BAND_MIN = 0.33
ULTRA_BAND_MAX = 0.36

ADD_BORDER = False
BORDER_WIDTH_PX = 2

RG_MIN_COVERAGE = 0.25
RG_MIN_MARGIN_FRAC = 0.02
RG_LINE_WIDTH = 1


# --------------------------------------------------------
# HELPERS
# --------------------------------------------------------

def float_to_pil(arr: np.ndarray) -> Image.Image:
    """Clamp float array to [0,1] and convert to 8-bit L image."""
    arr = np.clip(arr, 0.0, 1.0)
    arr_uint8 = (arr * 255).astype("uint8")
    return Image.fromarray(arr_uint8, mode="L")


def add_border(img: Image.Image, width_px: int) -> Image.Image:
    """Optional border around the image."""
    img = img.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size
    offset = width_px // 2

    draw.rectangle(
        [offset, offset, w - 1 - offset, h - 1 - offset],
        outline=0,
        width=width_px,
    )
    return img


# --------------------------------------------------------
# RGB -> Hue/Saturation (simplified HSV)
# --------------------------------------------------------

def rgb_to_hs(arr_rgb: np.ndarray):
    r = arr_rgb[:, :, 0]
    g = arr_rgb[:, :, 1]
    b = arr_rgb[:, :, 2]

    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    delta = maxc - minc

    s = np.zeros_like(maxc, dtype="float32")
    mask_max = maxc > 1e-6
    s[mask_max] = delta[mask_max] / maxc[mask_max]

    h = np.zeros_like(maxc, dtype="float32")
    mask_delta = delta > 1e-6

    r_m = r[mask_delta]
    g_m = g[mask_delta]
    b_m = b[mask_delta]
    delta_m = delta[mask_delta]
    maxc_m = maxc[mask_delta]

    h_raw = np.zeros_like(delta_m, dtype="float32")

    mask_r = maxc_m == r_m
    mask_g = maxc_m == g_m
    mask_b = ~(mask_r | mask_g)

    h_raw[mask_r] = ((g_m[mask_r] - b_m[mask_r]) / delta_m[mask_r]) % 6.0
    h_raw[mask_g] = ((b_m[mask_g] - r_m[mask_g]) / delta_m[mask_g]) + 2.0
    h_raw[mask_b] = ((r_m[mask_b] - g_m[mask_b]) / delta_m[mask_b]) + 4.0

    h_val = h_raw / 6.0
    h[mask_delta] = h_val

    return h, s


def _diff_linear(channel: np.ndarray) -> np.ndarray:
    dx = np.abs(np.diff(channel, axis=1))
    dx = np.pad(dx, ((0, 0), (0, 1)), mode="edge")

    dy = np.abs(np.diff(channel, axis=0))
    dy = np.pad(dy, ((0, 1), (0, 0)), mode="edge")

    return np.maximum(dx, dy)


def _diff_circular_hue(hue: np.ndarray) -> np.ndarray:
    dx = np.diff(hue, axis=1)
    dx = np.abs(dx)
    dx = np.minimum(dx, 1.0 - dx)
    dx = np.pad(dx, ((0, 0), (0, 1)), mode="edge")

    dy = np.diff(hue, axis=0)
    dy = np.abs(dy)
    dy = np.minimum(dy, 1.0 - dy)
    dy = np.pad(dy, ((0, 1), (0, 0)), mode="edge")

    return np.maximum(dx, dy)


def compute_hue_boost(hue: np.ndarray, lum_norm: np.ndarray, sat: np.ndarray) -> np.ndarray:
    dH = _diff_circular_hue(hue)
    dL = _diff_linear(lum_norm)

    dH_norm = np.clip(dH / 0.20, 0.0, 1.0)
    dL_norm = np.clip(dL / 0.25, 0.0, 1.0)

    boost = (dH_norm ** 1.4) * ((1.0 - dL_norm) ** 2.0)
    boost *= (sat ** 1.2)
    boost = np.clip(boost, 0.0, 1.0)
    return boost


# --------------------------------------------------------
# EDGE CORE
# --------------------------------------------------------

def compute_edge_map(img_color: Image.Image) -> Image.Image:
    """Compute raw edge map from RGB image."""
    rgb = np.array(img_color).astype("float32") / 255.0
    r = rgb[:, :, 0]
    g = rgb[:, :, 1]
    b = rgb[:, :, 2]

    # luminance
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    max_l = float(lum.max()) if lum.size > 0 else 1.0
    if max_l < 1e-6:
        max_l = 1e-6
    lum_norm = lum / max_l

    # gradients
    grad_lum = np.abs(filters.scharr(lum_norm))

    # hue-based boost
    hue, sat = rgb_to_hs(rgb)
    boost = compute_hue_boost(hue, lum_norm, sat)

    grad = grad_lum + 1.5 * boost

    max_g = float(grad.max()) if grad.size > 0 else 1.0
    if max_g < 1e-6:
        max_g = 1e-6

    grad_norm = grad / max_g

    edge_strength = np.clip(grad_norm * EDGE_GAIN, 0.0, 1.0)
    inv = 1.0 - edge_strength

    inv = inv ** EDGE_GAMMA

    low = float(np.percentile(inv, 0.5))
    high = float(np.percentile(inv, 99.5))
    if high > low:
        inv = np.clip((inv - low) / (high - low), 0.0, 1.0)

    return float_to_pil(inv)


# --------------------------------------------------------
# LINE NORMALIZATION / EQUALIZER / ULTRA-UNIFORMIZER
# --------------------------------------------------------

def soft_normalize_lines(edge_img: Image.Image) -> Image.Image:
    """Normalize and unify line brightness."""
    arr = np.array(edge_img).astype("float32") / 255.0

    mask_lines = arr < LINE_MASK_THRESH
    if not np.any(mask_lines):
        return edge_img

    line_vals = arr[mask_lines]

    # base normalization
    q25 = float(np.percentile(line_vals, 25))
    median = float(np.median(line_vals))

    if median - q25 >= 1e-4:
        new_vals = line_vals.copy()

        very_dark = line_vals < q25
        mid_dark = (line_vals >= q25) & (line_vals < median)

        new_vals[very_dark] = line_vals[very_dark] + LINE_BRIGHTEN_STRONG * (median - line_vals[very_dark])
        new_vals[mid_dark] = line_vals[mid_dark] + LINE_BRIGHTEN_SOFT * (median - line_vals[mid_dark])

        arr[mask_lines] = np.clip(new_vals, 0.0, 1.0)

    # brightness equalizer
    if ENABLE_BRIGHT_EQUALIZER:
        line_vals2 = arr[mask_lines]

        p_low = float(np.percentile(line_vals2, BRIGHT_EQ_LOW_PCT))
        p_high = float(np.percentile(line_vals2, BRIGHT_EQ_HIGH_PCT))

        if p_high - p_low >= 1e-4:
            norm = (line_vals2 - p_low) / (p_high - p_low)
            target_min = BRIGHT_EQ_BASE
            target_max = BRIGHT_EQ_BASE + BRIGHT_EQ_RANGE
            target_max = max(target_max, target_min + 1e-3)

            mapped = target_min + norm * (target_max - target_min)
            mapped = np.clip(mapped, 0.0, 1.0)

            brighter = mapped > line_vals2
            line_vals2[brighter] = mapped[brighter]

            arr[mask_lines] = line_vals2

    # global lift
    if ENABLE_GLOBAL_LIFT:
        line_vals3 = arr[mask_lines]
        line_vals3 = line_vals3 + GLOBAL_LIFT
        line_vals3 = np.maximum(line_vals3, MIN_LINE_BRIGHT)
        arr[mask_lines] = np.clip(line_vals3, 0.0, 1.0)

    # ultra uniformizer
    if ENABLE_ULTRA_UNIFORMIZER:
        line_vals4 = arr[mask_lines]

        p_ultra = float(np.percentile(line_vals4, ULTRA_LOW_PCT))
        low_mask = line_vals4 <= p_ultra

        if np.any(low_mask):
            low_vals = line_vals4[low_mask]
            low_min = float(low_vals.min())
            low_max = float(low_vals.max())

            if low_max - low_min < 1e-4:
                mapped_low = np.full_like(low_vals, (ULTRA_BAND_MIN + ULTRA_BAND_MAX) / 2.0)
            else:
                norm = (low_vals - low_min) / (low_max - low_min)
                mapped_low = ULTRA_BAND_MIN + norm * (ULTRA_BAND_MAX - ULTRA_BAND_MIN)

            mapped_low = np.maximum(mapped_low, low_vals)
            line_vals4[low_mask] = mapped_low
            arr[mask_lines] = line_vals4

    return float_to_pil(arr)


# --------------------------------------------------------
# RED-GREEN BORDERS
# --------------------------------------------------------

def detect_vertical_red_green_borders(rgb_img: Image.Image):
    """Detect vertical red/green borders (flag separators)."""
    arr = np.array(rgb_img).astype("float32") / 255.0
    h, w, _ = arr.shape

    R = arr[:, :, 0]
    G = arr[:, :, 1]
    B = arr[:, :, 2]

    is_red = (R > 0.4) & (R - np.maximum(G, B) > 0.12)
    is_green = (G > 0.4) & (G - np.maximum(R, B) > 0.12)

    positions = []
    x_min = int(w * RG_MIN_MARGIN_FRAC)
    x_max = int(w * (1.0 - RG_MIN_MARGIN_FRAC))

    if x_max <= x_min + 1:
        return []

    for x in range(x_min + 1, x_max):
        left_red = is_red[:, x - 1]
        right_green = is_green[:, x]

        left_green = is_green[:, x - 1]
        right_red = is_red[:, x]

        match = ((left_red & right_green) | (left_green & right_red))
        score = match.sum() / float(h)

        if score >= RG_MIN_COVERAGE:
            positions.append(x)

    if not positions:
        return []

    grouped = []
    start = positions[0]
    prev = positions[0]

    for x in positions[1:]:
        if x == prev + 1:
            prev = x
        else:
            grouped.append((start + prev) // 2)
            start = x
            prev = x

    grouped.append((start + prev) // 2)
    return grouped


def add_soft_red_green_lines(edge_img: Image.Image, rgb_img: Image.Image) -> Image.Image:
    """Add soft vertical lines at red/green borders."""
    positions = detect_vertical_red_green_borders(rgb_img)
    if not positions:
        return edge_img

    arr = np.array(edge_img).astype("float32") / 255.0
    h, w = arr.shape

    mask_lines = arr < LINE_MASK_THRESH
    if np.any(mask_lines):
        median_line = float(np.median(arr[mask_lines]))
    else:
        median_line = 0.34

    target_val = max(0.0, median_line - 0.005)

    half_w = RG_LINE_WIDTH // 2

    for x in positions:
        for dx in range(-half_w, half_w + 1):
            cx = int(np.clip(x + dx, 0, w - 1))
            col = arr[:, cx]
            col_new = np.minimum(col, target_val)
            arr[:, cx] = col_new

    return float_to_pil(arr)


# --------------------------------------------------------
# PUBLIC PIPELINE FUNCTION
# --------------------------------------------------------

def run_edge_pipeline(
    img_color: Image.Image,
    enable_border: bool | None = None,
) -> Image.Image:
    """
    Takes an RGB PIL image and returns the final EdgeWizard result
    as an L-mode (grayscale) PIL image.

    If enable_border is None, the global ADD_BORDER config is used.
    If enable_border is True/False, it overrides the global setting.
    """
    img_color = img_color.convert("RGB")

    edge_img = compute_edge_map(img_color)

    if LINE_SOFT_NORMALIZE:
        edge_img = soft_normalize_lines(edge_img)

    edge_img = add_soft_red_green_lines(edge_img, img_color)

    use_border = ADD_BORDER if enable_border is None else enable_border
    if use_border:
        edge_img = add_border(edge_img, BORDER_WIDTH_PX)

    return edge_img


