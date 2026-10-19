"""STL loading, placement, footprint, and binary STL writing."""

import numpy as np


def load_stl(path):
    """Read a binary or ASCII STL file and return a (N, 3, 3) float64 array."""
    with open(path, "rb") as f:
        data = f.read()

    file_size = len(data)

    # Try binary detection: file size == 84 + 50 * triangle_count
    if file_size >= 84:
        tri_count = int(np.frombuffer(data[80:84], dtype="<u4")[0])
        expected = 84 + 50 * tri_count
        if file_size == expected and tri_count > 0:
            return _read_binary(data, tri_count)

    # Fall back to ASCII
    return _read_ascii(data)


def _read_binary(data, tri_count):
    """Parse binary STL bytes using numpy structured dtype."""
    dtype = np.dtype([
        ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4"),
        ("v1x", "<f4"), ("v1y", "<f4"), ("v1z", "<f4"),
        ("v2x", "<f4"), ("v2y", "<f4"), ("v2z", "<f4"),
        ("v3x", "<f4"), ("v3y", "<f4"), ("v3z", "<f4"),
        ("attr", "<u2"),
    ])
    body = np.frombuffer(data[84:], dtype=dtype, count=tri_count)
    tris = np.zeros((tri_count, 3, 3), dtype=np.float64)
    for i in range(3):
        x = body[f"v{i + 1}x"]
        y = body[f"v{i + 1}y"]
        z = body[f"v{i + 1}z"]
        tris[:, i, 0] = x
        tris[:, i, 1] = y
        tris[:, i, 2] = z
    return tris


def _read_ascii(data):
    """Parse ASCII STL text."""
    text = data.decode("ascii", errors="replace")
    lines = text.lower().splitlines()
    vertices = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "vertex":
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                continue
    if not vertices:
        raise ValueError("not an STL file: no triangles found")
    n = len(vertices) // 3
    tris = np.zeros((n, 3, 3), dtype=np.float64)
    for i in range(n):
        for j in range(3):
            tris[i, j] = vertices[i * 3 + j]
    return tris


def place(tris, x, y, rotation_deg, bed_width, bed_depth, center_is_zero):
    """Place a mesh on the bed: rotate, translate to (x,y), lift to z=0, optional centre offset."""
    result = tris.copy()

    # 1. Rotate about Z axis (counter-clockwise from above)
    if rotation_deg != 0:
        rad = np.deg2rad(rotation_deg)
        cos_r = np.cos(rad)
        sin_r = np.sin(rad)
        rot = np.array([[cos_r, -sin_r, 0],
                        [sin_r, cos_r, 0],
                        [0, 0, 1]], dtype=np.float64)
        result = result @ rot.T  # vertices are row vectors: v' = R v  <=>  v'^T = v^T R^T

    # 2. Translate in X/Y so AABB centre lands on (x, y)
    mins = result.min(axis=(0, 1))
    maxs = result.max(axis=(0, 1))
    cx = (mins[0] + maxs[0]) / 2
    cy = (mins[1] + maxs[1]) / 2
    result[:, :, 0] += x - cx
    result[:, :, 1] += y - cy

    # 3. Lift so lowest vertex is at z = 0
    result[:, :, 2] -= mins[2]

    # 4. Optional CuraEngine centre offset
    if not center_is_zero:
        result[:, :, 0] -= bed_width / 2
        result[:, :, 1] -= bed_depth / 2

    return result


def footprint(tris):
    """Return axis-aligned bounding box as a dict of six floats."""
    mins = tris.min(axis=(0, 1))
    maxs = tris.max(axis=(0, 1))
    return {
        "min_x": float(mins[0]),
        "max_x": float(maxs[0]),
        "min_y": float(mins[1]),
        "max_y": float(maxs[1]),
        "min_z": float(mins[2]),
        "max_z": float(maxs[2]),
    }


def write_binary_stl(path, tris):
    """Write a binary STL file with computed normals."""
    n = tris.shape[0]
    dtype = np.dtype([
        ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4"),
        ("v1x", "<f4"), ("v1y", "<f4"), ("v1z", "<f4"),
        ("v2x", "<f4"), ("v2y", "<f4"), ("v2z", "<f4"),
        ("v3x", "<f4"), ("v3y", "<f4"), ("v3z", "<f4"),
        ("attr", "<u2"),
    ])
    body = np.zeros(n, dtype=dtype)

    # Compute normals via cross product
    a = tris[:, 0]  # (n, 3)
    b = tris[:, 1]
    c = tris[:, 2]
    cross = np.cross(b - a, c - a)  # (n, 3)
    lengths = np.linalg.norm(cross, axis=1, keepdims=True)
    normals = np.where(lengths > 0, cross / lengths, 0.0)
    body["nx"] = normals[:, 0]
    body["ny"] = normals[:, 1]
    body["nz"] = normals[:, 2]

    # Coordinates
    for i in range(3):
        for j, ch in enumerate("xyz"):
            body[f"v{i + 1}{ch}"] = tris[:, i, j]

    body["attr"] = 0

    header = b"remote_slicing" + b"\x00" * (80 - len(b"remote_slicing"))
    count = np.array([n], dtype="<u4")

    with open(path, "wb") as f:
        f.write(header)
        f.write(count.tobytes())
        f.write(body.tobytes())
