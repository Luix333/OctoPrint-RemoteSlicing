"""Headless thumbnail renderer — numpy + zlib only, mirrors Cura's Create Thumbnail."""

import math
import struct
import zlib

import numpy as np


def parse_color(text):
    """'#rrggbb' (any case) -> (r, g, b) floats 0..1; anything else -> (0.8, 0.8, 0.8)."""
    try:
        if isinstance(text, str) and text.startswith("#") and len(text) == 7:
            return (int(text[1:3], 16) / 255, int(text[3:5], 16) / 255, int(text[5:7], 16) / 255)
    except (ValueError, TypeError):
        pass
    return (0.8, 0.8, 0.8)


def prettier(rgb):
    """Cura's PreviewPass.prettier_color."""
    m = max(rgb)
    factor = min(1.0 / m, 2.5) if m > 0 else 1.0
    return tuple(c * factor for c in rgb)


def encode_png(rgba):
    """rgba: uint8 array (h, w, 4) -> PNG bytes. 8-bit RGBA, no interlace."""
    h, w = rgba.shape[:2]
    def chunk(ctype, data):
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    rows = b""
    for y in range(h):
        rows += b"\x00" + rgba[y, :].tobytes()
    idat = zlib.compress(rows, 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _build_weights(out_size, in_size):
    """Build weight matrix for area-averaging resize."""
    W = np.zeros((out_size, in_size))
    for o in range(out_size):
        s0 = o * in_size / out_size
        s1 = (o + 1) * in_size / out_size
        for s in range(int(np.floor(s0)), min(int(np.ceil(s1)), in_size)):
            W[o, s] = max(0, min(s + 1, s1) - max(s, s0))
    rs = W.sum(axis=1, keepdims=True)
    rs[rs == 0] = 1
    return W / rs


def _do_render(tris, colors, eye, center, fovy, size):
    """Z-buffered render of the plate as Cura's PreviewPass shades it; (size, size, 4) float image, rgb 0..1,
    alpha 0 or 255, or None when no triangle is in front of the camera."""
    # lookAt(eye, center, up=(0, 1, 0)) and gluPerspective(fovy, 1, 1, 500)
    f = center - eye
    f = f / np.linalg.norm(f)
    right = np.cross(f, [0.0, 1.0, 0.0])
    right = right / np.linalg.norm(right)
    up = np.cross(right, f)
    view = np.eye(4)
    view[0, :3], view[1, :3], view[2, :3] = right, up, -f
    view[:3, 3] = -view[:3, :3] @ eye
    t = 1.0 / math.tan(math.radians(fovy) / 2)
    proj = np.zeros((4, 4))
    proj[0, 0] = proj[1, 1] = t
    proj[2, 2], proj[2, 3], proj[3, 2] = -501.0 / 499.0, -1000.0 / 499.0, -1.0

    flat = np.concatenate([tris.reshape(-1, 3), np.ones((len(tris) * 3, 1))], axis=1)
    cam = flat @ view.T
    clip = cam @ proj.T
    ndc = clip[:, :3] / clip[:, 3:4]
    sx = ((ndc[:, 0] + 1) / 2 * size).reshape(-1, 3)
    sy = ((1 - ndc[:, 1]) / 2 * size).reshape(-1, 3)
    sz = ndc[:, 2].reshape(-1, 3)

    # flat shading at the centroid: ambient 0.1, two-sided diffuse, specular 0.6 with shininess 20
    v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
    normal = np.cross(v1 - v0, v2 - v0)
    length = np.linalg.norm(normal, axis=1)
    keep = (length > 1e-12) & (cam[:, 2].reshape(-1, 3) < -0.001).all(axis=1)
    normal = normal / np.where(length > 0, length, 1.0)[:, None]
    centroid = (v0 + v1 + v2) / 3
    to_light = _unit(eye + np.array([0.0, 50.0, 0.0]) - centroid)
    to_eye = _unit(eye - centroid)
    n_dot_l = np.sum(normal * to_light, axis=1)
    reflected = -to_light + 2 * n_dot_l[:, None] * normal
    spec = 0.6 * np.maximum(np.sum(to_eye * reflected, axis=1), 0) ** 20
    peak = colors.max(axis=1, keepdims=True)
    pretty = colors * np.where(peak > 0, np.minimum(1 / np.where(peak > 0, peak, 1), 2.5), 1.0)
    shade = np.clip(0.1 + np.abs(n_dot_l)[:, None] * pretty + spec[:, None], 0, 1)

    keep = np.nonzero(keep)[0]
    if not len(keep):
        return None
    pix, depth, owner = _fragments(sx[keep], sy[keep], sz[keep], size)
    zbuf = np.full(size * size, np.inf)
    rgb = np.zeros((size * size, 3))
    if len(pix):
        order = np.lexsort((depth, pix))
        first = order[np.unique(pix[order], return_index=True)[1]]  # nearest fragment of each pixel
        zbuf[pix[first]] = depth[first]
        rgb[pix[first]] = shade[keep[owner[first]]]
    result = np.zeros((size, size, 4))
    result[:, :, :3] = rgb.reshape(size, size, 3)
    result[:, :, 3] = np.where(zbuf < np.inf, 255, 0).reshape(size, size)
    return result


def _unit(v):
    length = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.where(length > 0, length, 1.0)


def _fragments(sx, sy, sz, size, budget=1_000_000, height=None):
    """Covered pixel centres of every triangle: (pixel index, depth, triangle index).

    Triangles are grouped by bounding-box size (1, 2, 4, ... pixels) and each group is tested over all
    candidate pixels of its box at once. Barycentric weights are allowed a hair below zero, so a pixel centre
    on an edge shared by two triangles is never missed by both.

    When *height* is ``None`` (the default) the image is *size* × *size*.  Otherwise the image is
    *size* (width) × *height* and pixel indices use row-major order with *width* as the stride.
    """
    width = size
    height = height or size
    x0 = np.clip(np.floor(sx.min(axis=1)), 0, width).astype(np.int64)
    x1 = np.clip(np.floor(sx.max(axis=1)) + 1, 0, width).astype(np.int64)
    y0 = np.clip(np.floor(sy.min(axis=1)), 0, height).astype(np.int64)
    y1 = np.clip(np.floor(sy.max(axis=1)) + 1, 0, height).astype(np.int64)
    area = (sx[:, 1] - sx[:, 0]) * (sy[:, 2] - sy[:, 0]) - (sy[:, 1] - sy[:, 0]) * (sx[:, 2] - sx[:, 0])
    extent = np.maximum(x1 - x0, y1 - y0)
    usable = (x1 > x0) & (y1 > y0) & (np.abs(area) > 1e-12)
    out_pix, out_depth, out_owner = [], [], []
    k, low = 1, 0
    while low < max(width, height):
        group = np.nonzero(usable & (extent > low) & (extent <= k))[0]
        oy, ox = np.divmod(np.arange(k * k), k)
        step = max(1, budget // (k * k))
        for start in range(0, len(group), step):
            t = group[start:start + step]
            px = x0[t, None] + ox[None, :]
            py = y0[t, None] + oy[None, :]
            ax, ay = sx[t, 0:1] - (px + 0.5), sy[t, 0:1] - (py + 0.5)
            bx, by = sx[t, 1:2] - (px + 0.5), sy[t, 1:2] - (py + 0.5)
            qx, qy = sx[t, 2:3] - (px + 0.5), sy[t, 2:3] - (py + 0.5)
            inv = 1.0 / area[t, None]
            w0 = (bx * qy - by * qx) * inv
            w1 = (qx * ay - qy * ax) * inv
            w2 = (ax * by - ay * bx) * inv
            hit = ((px < x1[t, None]) & (py < y1[t, None])
                   & (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7))
            rows, cols = np.nonzero(hit)
            tri = t[rows]
            out_pix.append(py[rows, cols] * width + px[rows, cols])
            out_depth.append(w0[rows, cols] * sz[tri, 0] + w1[rows, cols] * sz[tri, 1]
                             + w2[rows, cols] * sz[tri, 2])
            out_owner.append(tri)
        low, k = k, k * 2
    if not out_pix:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int64)
    return np.concatenate(out_pix), np.concatenate(out_depth), np.concatenate(out_owner)


def render_top(models, width, height, px_per_mm):
    """Orthographic top-down render of a plate; (height, width, 4) uint8 RGBA, or None.

    Orthographic, looking straight down. The bed centre is the image centre and the back of the bed
    is at the top.  Models are triangle arrays (N, 3, 3) in centre-origin printer coordinates
    (x right, y back, z up, millimetres) paired with colours as (r, g, b) floats 0..1.

    A point (x, y) lands at continuous screen coordinates
    ``sx = x * px_per_mm + width / 2`` and ``sy = -y * px_per_mm + height / 2``.
    Depth = -z (higher z is nearer the camera).

    Flat shading in Cura scene coordinates (x, z, -y): ambient 0.1, two-sided diffuse,
    specular 0.4 with shininess 20.  Light at (-1500, 3000, -1500), view straight down
    from (0, top+1000, 0).
    """
    all_tris, all_colors = [], []
    for tris, rgb in models:
        if len(tris) == 0:
            continue
        all_tris.append(tris)
        all_colors.append(np.full((len(tris), 3), rgb))
    if not all_tris:
        return None
    tris = np.concatenate(all_tris, axis=0)
    colors = np.concatenate(all_colors, axis=0)

    # Scene coordinates: (x, z, -y) — X right, Y up, Z front
    scene = np.empty_like(tris)
    scene[:, :, 0] = tris[:, :, 0]
    scene[:, :, 1] = tris[:, :, 2]
    scene[:, :, 2] = -tris[:, :, 1]

    # Screen coordinates
    sx = tris[:, :, 0] * px_per_mm + width / 2.0
    sy = -tris[:, :, 1] * px_per_mm + height / 2.0
    depth = -tris[:, :, 2]

    # Flat shading at the centroid
    v0, v1, v2 = scene[:, 0], scene[:, 1], scene[:, 2]
    normal = np.cross(v1 - v0, v2 - v0)
    length = np.linalg.norm(normal, axis=1)
    normal = normal / np.where(length > 0, length, 1.0)[:, None]
    centroid = (v0 + v1 + v2) / 3

    top = tris[:, :, 2].max()
    LIGHT = np.array([-1500.0, 3000.0, -1500.0])
    VIEW = np.array([0.0, top + 1000.0, 0.0])

    l = _unit(LIGHT - centroid)
    v = _unit(VIEW - centroid)
    n_dot_l = np.sum(normal * l, axis=1)
    reflected = -l + 2 * n_dot_l[:, None] * normal
    spec = 0.4 * np.maximum(np.sum(v * reflected, axis=1), 0) ** 20

    peak = colors.max(axis=1, keepdims=True)
    pretty = colors * np.where(peak > 0, np.minimum(1 / np.where(peak > 0, peak, 1), 2.5), 1.0)
    shade = np.clip(0.1 + np.abs(n_dot_l)[:, None] * pretty + spec[:, None], 0, 1)

    # Filter degenerate triangles
    keep = np.nonzero(length > 1e-12)[0]
    if not len(keep):
        return None

    pix, depth_out, owner = _fragments(sx[keep], sy[keep], depth[keep], width, height=height)

    zbuf = np.full(width * height, np.inf)
    rgb_out = np.zeros((width * height, 3))
    if len(pix):
        order = np.lexsort((depth_out, pix))
        first = order[np.unique(pix[order], return_index=True)[1]]
        zbuf[pix[first]] = depth_out[first]
        rgb_out[pix[first]] = shade[keep[owner[first]]]

    result = np.zeros((height, width, 4), dtype=np.uint8)
    result[:, :, :3] = np.clip(np.rint(rgb_out.reshape(height, width, 3) * 255), 0, 255).astype(np.uint8)
    result[:, :, 3] = np.where(zbuf < np.inf, 255, 0).reshape(height, width)
    return result


def _crop_and_scale(result, width, height):
    """Crop opaque region, make square, scale to width x height, return PNG bytes."""
    opaque = result[:, :, 3] > 0
    if not opaque.any():
        return None
    ys, xs = np.nonzero(opaque)
    min_x, max_x = int(xs.min()), int(xs.max())
    min_y, max_y = int(ys.min()), int(ys.max())
    if max_x - min_x <= 0 or max_y - min_y <= 0:
        return None
    # Make square
    # one tuple assignment each, like Cura: both bounds come from the old values
    if max_x - min_x >= max_y - min_y:
        min_y, max_y = int((max_y + min_y) / 2 - (max_x - min_x) / 2), int((max_y + min_y) / 2 + (max_x - min_x) / 2)
    else:
        min_x, max_x = int((max_x + min_x) / 2 - (max_y - min_y) / 2), int((max_x + min_x) / 2 + (max_y - min_y) / 2)
    if max_x - min_x <= 0 or max_y - min_y <= 0:
        return None
    Rsize = result.shape[0]
    # Crop (clamp to image bounds)
    clamped = [[max(0, min_y), min(Rsize, max_y)], [max(0, min_x), min(Rsize, max_x)]]
    crop = np.zeros((max_y - min_y, max_x - min_x, 4), dtype=np.float64)
    sy, ey = clamped[0]
    sx, ex = clamped[1]
    if ey > sy and ex > sx:
        crop[sy - min_y:ey - min_y, sx - min_x:ex - min_x] = result[sy:ey, sx:ex]
    if crop.shape[0] == 0 or crop.shape[1] == 0:
        return None
    crop_h, crop_w = crop.shape[:2]
    # Premultiply
    pm_rgb = crop[:, :, :3] * (crop[:, :, 3:4] / 255.0)
    pm_a = crop[:, :, 3] / 255.0
    # Weight matrices for area averaging
    wy = _build_weights(height, crop_h)
    wx = _build_weights(width, crop_w)
    out = np.zeros((height, width, 4), dtype=np.float64)
    for c in range(3):
        out[:, :, c] = wy @ pm_rgb[:, :, c] @ wx.T
    out[:, :, 3] = wy @ pm_a @ wx.T
    mask = out[:, :, 3] > 0
    if mask.any():
        out[mask, :3] /= out[mask, 3:4]
    result_uint8 = np.clip(np.rint(out * 255), 0, 255).astype(np.uint8)
    return encode_png(result_uint8)


def render_png(models, width, height):
    """Render a plate of triangle meshes to PNG bytes."""
    all_tris, all_colors = [], []
    for tris, rgb in models:
        if len(tris) == 0:
            continue
        ogl = np.empty_like(tris)
        ogl[:, :, 0] = tris[:, :, 0]
        ogl[:, :, 1] = tris[:, :, 2]
        ogl[:, :, 2] = -tris[:, :, 1]
        all_tris.append(ogl)
        all_colors.append(np.full((len(tris), 3), rgb))
    if not all_tris:
        return None
    tris = np.concatenate(all_tris, axis=0)
    colors = np.concatenate(all_colors, axis=0)
    if len(tris) == 0:
        return None
    # Bounding box
    mins = tris.min(axis=(0, 1))
    maxs = tris.max(axis=(0, 1))
    center = (mins + maxs) / 2
    extents = maxs - mins
    size = max(extents[0], extents[1], extents[2] * 0.5)
    offset = np.array([-1, 1, 2]) * size * 1.75 if size > 0 else np.array([-1, 1, 2])
    eye = center + offset
    Rsize = max(512, 4 * max(width, height))
    fovy = 30.0
    result = _do_render(tris, colors, eye, center, fovy, Rsize)
    if result is None:
        return None
    # Check if need halved fovy
    opaque = result[:, :, 3] > 0
    if opaque.any():
        ys, xs = np.nonzero(opaque)
        frac = max((xs.max() - xs.min()) / Rsize, (ys.max() - ys.min()) / Rsize)
        if frac <= 0.5:
            result = _do_render(tris, colors, eye, center, fovy / 2, Rsize)
            if result is None:
                return None
    return _crop_and_scale(result, width, height)
