# SPDX-License-Identifier: AGPL-3.0-or-later
"""ThumbHash placeholder generation, matching Fluxer's media proxy.

Fluxer stores an attachment ``placeholder`` as the base64 (standard, padded)
encoding of a ThumbHash computed from the image resized so its largest
dimension is <= 100px (see fluxer_media_proxy/src/thumbhash.rs, MAX_DIM = 100).

This is a faithful port of Evan Wallace's reference ``rgbaToThumbHash``
(https://github.com/evanw/thumbhash). The blur it produces is visually
identical regardless of the exact resampling used to downscale, so it does not
need to be byte-for-byte equal to the vips-based pipeline.
"""

import base64
import math

MAX_DIM = 100


def rgba_to_thumbhash(w: int, h: int, rgba: bytes) -> bytes:
    """Encode an RGBA image (<=100x100) into ThumbHash bytes."""
    if w > 100 or h > 100:
        raise ValueError("ThumbHash input must be <= 100x100")

    # Average color (premultiplied by alpha).
    avg_r = avg_g = avg_b = avg_a = 0.0
    n = w * h
    for i in range(n):
        j = i * 4
        alpha = rgba[j + 3] / 255.0
        avg_r += alpha / 255.0 * rgba[j]
        avg_g += alpha / 255.0 * rgba[j + 1]
        avg_b += alpha / 255.0 * rgba[j + 2]
        avg_a += alpha
    if avg_a > 0:
        avg_r /= avg_a
        avg_g /= avg_a
        avg_b /= avg_a

    has_alpha = avg_a < n
    l_limit = 5 if has_alpha else 7
    max_wh = max(w, h)
    lx = max(1, round(l_limit * w / max_wh))
    ly = max(1, round(l_limit * h / max_wh))

    l = [0.0] * n  # luminance
    p = [0.0] * n  # yellow - blue
    q = [0.0] * n  # red - green
    a = [0.0] * n  # alpha

    for i in range(n):
        j = i * 4
        alpha = rgba[j + 3] / 255.0
        r = avg_r * (1 - alpha) + alpha / 255.0 * rgba[j]
        g = avg_g * (1 - alpha) + alpha / 255.0 * rgba[j + 1]
        b = avg_b * (1 - alpha) + alpha / 255.0 * rgba[j + 2]
        l[i] = (r + g + b) / 3.0
        p[i] = (r + g) / 2.0 - b
        q[i] = r - g
        a[i] = alpha

    def encode_channel(channel, nx, ny):
        dc = 0.0
        ac = []
        scale = 0.0
        fx = [0.0] * w
        for cy in range(ny):
            cx = 0
            while cx * ny < nx * (ny - cy):
                f = 0.0
                for x in range(w):
                    fx[x] = math.cos(math.pi / w * cx * (x + 0.5))
                for y in range(h):
                    fy = math.cos(math.pi / h * cy * (y + 0.5))
                    row = y * w
                    for x in range(w):
                        f += channel[x + row] * fx[x] * fy
                f /= w * h
                if cx or cy:
                    ac.append(f)
                    scale = max(scale, abs(f))
                else:
                    dc = f
                cx += 1
        if scale > 0:
            ac = [0.5 + 0.5 / scale * v for v in ac]
        return dc, ac, scale

    l_dc, l_ac, l_scale = encode_channel(l, max(3, lx), max(3, ly))
    p_dc, p_ac, p_scale = encode_channel(p, 3, 3)
    q_dc, q_ac, q_scale = encode_channel(q, 3, 3)
    if has_alpha:
        a_dc, a_ac, a_scale = encode_channel(a, 5, 5)
    else:
        a_dc, a_ac, a_scale = 1.0, [], 1.0

    is_landscape = w > h
    header24 = (
        round(63 * l_dc)
        | (round(31.5 + 31.5 * p_dc) << 6)
        | (round(31.5 + 31.5 * q_dc) << 12)
        | (round(31 * l_scale) << 18)
        | ((1 if has_alpha else 0) << 23)
    )
    header16 = (
        (ly if is_landscape else lx)
        | (round(63 * p_scale) << 3)
        | (round(63 * q_scale) << 9)
        | ((1 if is_landscape else 0) << 15)
    )
    hash_bytes = [
        header24 & 255,
        (header24 >> 8) & 255,
        header24 >> 16,
        header16 & 255,
        header16 >> 8,
    ]
    if has_alpha:
        hash_bytes.append(round(15 * a_dc) | (round(15 * a_scale) << 4))

    ac_groups = [l_ac, p_ac, q_ac, a_ac] if has_alpha else [l_ac, p_ac, q_ac]
    ac_start = len(hash_bytes)
    ac_index = 0
    for group in ac_groups:
        for f in group:
            idx = ac_start + (ac_index >> 1)
            while idx >= len(hash_bytes):
                hash_bytes.append(0)
            hash_bytes[idx] |= round(15 * f) << ((ac_index & 1) << 2)
            ac_index += 1

    return bytes(hash_bytes)


def image_placeholder(pil_image) -> str:
    """Return the base64 ThumbHash placeholder for a PIL image.

    The image is downscaled so its largest dimension is <= MAX_DIM, matching
    Fluxer's pipeline, then encoded as RGBA.
    """
    im = pil_image
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    w, h = im.size
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    w, h = im.size
    rgba = im.tobytes()
    return base64.b64encode(rgba_to_thumbhash(w, h, rgba)).decode("ascii")
