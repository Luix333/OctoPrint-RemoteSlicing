"""Where parts go on the bed when they are sliced from Explorer, and the checks the Remote Slicing tab makes.

Coordinates are the tab's: millimetres from the front-left corner of the bed, X to the right, Y to the back. An
item's (x, y) is the centre of the part's footprint after its tilt and turn, which is what ``mesh.place`` puts
there. The geometry follows ``remoteslicing_bed.js`` (convex hull of the footprint, separating-axis overlap test,
inside-the-bed test) so a plate laid out here gets the same warnings as in the tab.
"""

import math

import numpy as np

POSITIONS = ("top-left", "top-mid", "top-right", "mid-left", "center", "mid-right", "btm-left", "btm-mid", "btm-right")
MARGIN = 10.0  # mm kept clear of the bed edge (on top of Cura's own border) when parts go to a side or corner
GAP = 10.0     # mm between parts placed together


class LayoutError(Exception):
    """The parts can't be placed as asked; the message says why."""


# ------------------------------------------------------------------------------------------------ geometry

def _axis_rotation(axis, deg):
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    if axis == 0:
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def tilt(points, tilt_x=0, tilt_y=0):
    """Tilt about X, then Y (mesh.place's order). ``points`` is (N, 3)."""
    if not (tilt_x % 360 or tilt_y % 360):
        return points
    return points @ (_axis_rotation(1, tilt_y) @ _axis_rotation(0, tilt_x)).T


def rotate2d(points, deg):
    """Counter-clockwise turn about Z (seen from above) of (N, 2) points."""
    if not deg % 360:
        return points
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return points @ np.array([[c, -s], [s, c]]).T


def convex_hull(points):
    """Counter-clockwise convex hull of (N, 2) points (Andrew's monotone chain), as a (K, 2) array."""
    pts = [tuple(p) for p in np.unique(np.round(np.asarray(points, dtype=np.float64).reshape(-1, 2), 4), axis=0)]
    if len(pts) < 3:
        return np.array(pts, dtype=np.float64).reshape(-1, 2)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], dtype=np.float64)


OCTAGON = np.array([(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)], dtype=np.float64)


def footprint_hull(points):
    """Convex hull of the XY projection of (N, 3) points. Points strictly inside the polygon of the extreme points
    in eight directions can't be hull vertices, so they are dropped first (exact; most of a dense mesh goes)."""
    xy = np.unique(np.round(np.asarray(points, dtype=np.float64)[:, :2], 4), axis=0)
    if len(xy) > 64:
        ring = convex_hull(xy[np.argmax(xy @ OCTAGON.T, axis=0)])
        if len(ring) >= 3:
            inside = np.ones(len(xy), dtype=bool)
            for a, b in zip(ring, np.roll(ring, -1, axis=0)):
                inside &= (b[0] - a[0]) * (xy[:, 1] - a[1]) - (b[1] - a[1]) * (xy[:, 0] - a[0]) > 1e-9
            xy = xy[~inside]
    return convex_hull(xy)


def bbox(poly):
    poly = np.asarray(poly)
    return poly[:, 0].min(), poly[:, 1].min(), poly[:, 0].max(), poly[:, 1].max()


def inside_bed(poly, width, depth, border=0.0):
    x0, y0, x1, y1 = bbox(poly)
    eps = 1e-6
    return x0 >= border - eps and y0 >= border - eps and x1 <= width - border + eps and y1 <= depth - border + eps


def overlaps(a, b):
    """Separating-axis test for two convex polygons; touching is not overlapping (as in the tab)."""
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            ex, ey = poly[(i + 1) % n][0] - poly[i][0], poly[(i + 1) % n][1] - poly[i][1]
            length = math.hypot(ex, ey)
            if not length:
                continue
            ax, ay = -ey / length, ex / length
            pa = [p[0] * ax + p[1] * ay for p in a]
            pb = [p[0] * ax + p[1] * ay for p in b]
            if max(pa) <= min(pb) + 1e-9 or max(pb) <= min(pa) + 1e-9:
                return False
    return True


# ------------------------------------------------------------------------------------------------ parts

class Part:
    """One part on the plate: its mesh, orientation, place and extruder. Copies share the mesh."""

    def __init__(self, name, tris, source=None):
        self.name = name
        self.tris = tris                 # (N, 3, 3) as in the file
        self.source = source             # what to upload for it (the STL path)
        self.points = np.unique(tris.reshape(-1, 3), axis=0) if len(tris) else np.zeros((0, 3))
        self.tilt_x = self.tilt_y = 0
        self.rotation = 0.0
        self.x = self.y = 0.0
        self.extruder = 0
        self._tilted = {}                # (tilt_x, tilt_y) -> (hull at rotation 0, height)

    def copy(self):
        other = Part.__new__(Part)
        other.__dict__.update(self.__dict__)
        return other

    def _tilted_hull(self):
        key = (self.tilt_x % 360, self.tilt_y % 360)
        if key not in self._tilted:
            pts = tilt(self.points, *key)
            self._tilted[key] = (footprint_hull(pts), float(np.ptp(pts[:, 2])) if len(pts) else 0.0)
        return self._tilted[key]

    def height(self):
        return self._tilted_hull()[1]

    def shape(self):
        """The footprint hull after tilt and turn, centred on its bounding-box centre."""
        hull = rotate2d(self._tilted_hull()[0], self.rotation)
        x0, y0, x1, y1 = bbox(hull)
        return hull - [(x0 + x1) / 2, (y0 + y1) / 2]

    def size(self):
        x0, y0, x1, y1 = bbox(self.shape())
        return x1 - x0, y1 - y0

    def outline(self):
        return self.shape() + [self.x, self.y]

    def item(self, model_id):
        return {"model_id": model_id, "x": round(self.x, 3), "y": round(self.y, 3),
                "rotation": round(self.rotation % 360, 3), "tilt_x": self.tilt_x % 360, "tilt_y": self.tilt_y % 360,
                "extruder": self.extruder}


def problems(parts, bed):
    """{"outside": [indices], "overlapping": [(i, j)], "too_tall": [indices]}, as the tab reports them."""
    width, depth = float(bed["width"]), float(bed["depth"])
    border = float(bed.get("edge_disallowed_size") or 0)
    outlines = [p.outline() for p in parts]
    result = {"outside": [i for i, o in enumerate(outlines) if not inside_bed(o, width, depth, border)],
              "overlapping": [], "too_tall": [i for i, p in enumerate(parts) if p.height() > float(bed["height"]) + 1e-6]}
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if overlaps(outlines[i], outlines[j]):
                result["overlapping"].append((i, j))
    return result


# ------------------------------------------------------------------------------------------------ layout

def _rows(sizes, room_width, gap):
    """Shelf packing in the given order: each row fills left to right until the next part doesn't fit."""
    rows, row, used = [], [], 0.0
    for i, (w, _d) in enumerate(sizes):
        if row and used + gap + w > room_width + 1e-6:
            rows.append(row)
            row, used = [], 0.0
        used = used + gap + w if row else w
        row.append(i)
    if row:
        rows.append(row)
    return rows


def pack(sizes, room_width, room_depth, gap=GAP):
    """Centres of parts of the given (width, depth), packed in rows from the back, each row centred, relative to
    the group's front-left corner; and the group's (width, depth). Raises LayoutError when they don't fit."""
    if not sizes:
        return [], (0.0, 0.0)
    for w, d in sizes:
        if w > room_width + 1e-6 or d > room_depth + 1e-6:
            raise LayoutError("A part is %.0f x %.0f mm and the space for it is %.0f x %.0f mm."
                              % (w, d, room_width, room_depth))
    rows = _rows(sizes, room_width, gap)
    widths = [sum(sizes[i][0] for i in row) + gap * (len(row) - 1) for row in rows]
    depths = [max(sizes[i][1] for i in row) for row in rows]
    group_w, group_d = max(widths), sum(depths) + gap * (len(rows) - 1)
    if group_d > room_depth + 1e-6:
        raise LayoutError("The parts need %.0f x %.0f mm together and the space for them is %.0f x %.0f mm."
                          % (group_w, group_d, room_width, room_depth))
    centres = [None] * len(sizes)
    top = group_d
    for row, row_w, row_d in zip(rows, widths, depths):
        x = (group_w - row_w) / 2
        for i in row:
            w = sizes[i][0]
            centres[i] = (x + w / 2, top - row_d / 2)
            x += w + gap
        top -= row_d + gap
    return centres, (group_w, group_d)


def place_group(parts, bed, position="center", gap=GAP, margin=MARGIN):
    """Pack the parts together and put the group at one of the nine POSITIONS ("top" is the back of the bed, as on
    the tab's plate). Sets each part's x and y. Raises LayoutError when they don't fit on the bed."""
    room = _room(bed, position, margin)
    for p in parts:
        if p.height() > float(bed["height"]) + 1e-6:
            raise LayoutError("%s is %.0f mm tall and the printer takes %.0f mm." % (p.name, p.height(), bed["height"]))
    sizes = [p.size() for p in parts]
    try:
        centres, (gw, gd) = pack(sizes, room["hi_x"] - room["lo_x"], room["hi_y"] - room["lo_y"], gap)
    except LayoutError as e:
        what = "The part doesn't fit on the bed" if len(parts) == 1 else "The parts don't fit on the bed together"
        raise LayoutError("%s: %s Use More options to turn or arrange them." % (what, e))
    gx, gy = _corner(room, gw, gd)
    for p, (cx, cy) in zip(parts, centres):
        p.x, p.y = gx + cx, gy + cy
    return parts


def move_group(parts, bed, position, margin=MARGIN):
    """Move all parts together, keeping how they are laid out, to one of the nine POSITIONS."""
    if not parts:
        return parts
    room = _room(bed, position, margin)
    boxes = np.array([bbox(p.outline()) for p in parts])
    x0, y0, x1, y1 = boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max()
    if x1 - x0 > room["hi_x"] - room["lo_x"] + 1e-6 or y1 - y0 > room["hi_y"] - room["lo_y"] + 1e-6:
        raise LayoutError("The parts take %.0f x %.0f mm together, more than fits there (%.0f x %.0f mm)."
                          % (x1 - x0, y1 - y0, room["hi_x"] - room["lo_x"], room["hi_y"] - room["lo_y"]))
    gx, gy = _corner(room, x1 - x0, y1 - y0)
    for p in parts:
        p.x += gx - x0
        p.y += gy - y0
    return parts


def _room(bed, position, margin):
    """The space a group may use for a position: the bed less Cura's border, and less ``margin`` on the sides the
    group is pushed against."""
    if position not in POSITIONS:
        raise LayoutError("unknown position %r" % position)
    width, depth = float(bed["width"]), float(bed["depth"])
    border = float(bed.get("edge_disallowed_size") or 0)
    vertical, horizontal = ("mid", "mid") if position == "center" else position.split("-")
    side_x = margin if horizontal != "mid" else 0.0
    side_y = margin if vertical != "mid" else 0.0
    return {"width": width, "depth": depth, "horizontal": horizontal, "vertical": vertical,
            "lo_x": border + side_x, "hi_x": width - border - side_x,
            "lo_y": border + side_y, "hi_y": depth - border - side_y}


def _corner(room, group_w, group_d):
    """Front-left corner of a group of the given size at the room's position."""
    gx = {"left": room["lo_x"], "mid": (room["width"] - group_w) / 2, "right": room["hi_x"] - group_w}
    gy = {"btm": room["lo_y"], "mid": (room["depth"] - group_d) / 2, "top": room["hi_y"] - group_d}
    return gx[room["horizontal"]], gy[room["vertical"]]


def agent_xy(x, y, bed):
    """Tab coordinates to the item coordinates mesh.place expects (the same unless the bed's origin is its centre)."""
    if bed.get("center_is_zero"):
        return x - float(bed["width"]) / 2, y - float(bed["depth"]) / 2
    return x, y
