"""Tests for remote_slicing_agent.plate_layout (bed positions and the tab's checks, in Python)."""

import unittest

import numpy as np

from remote_slicing_agent import mesh, plate_layout as pl

BED = {"width": 350.0, "depth": 340.0, "height": 330.0, "center_is_zero": False, "edge_disallowed_size": 3.0}


def box(sx, sy, sz, at=(0, 0, 0)):
    v = np.array([[0, 0, 0], [sx, 0, 0], [sx, sy, 0], [0, sy, 0],
                  [0, 0, sz], [sx, 0, sz], [sx, sy, sz], [0, sy, sz]], dtype=float) + at
    idx = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 4, 7], [0, 7, 3],
           [1, 2, 6], [1, 6, 5], [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2]]
    return v[np.array(idx)]


def bounds(part):
    return [round(float(v), 6) for v in pl.bbox(part.outline())]


class GeometryTests(unittest.TestCase):
    def test_hull_matches_a_brute_force_hull_and_is_counter_clockwise(self):
        rng = np.random.default_rng(3)
        pts = rng.normal(size=(50000, 3)) * [40, 15, 3]
        fast = pl.footprint_hull(pts)
        full = pl.convex_hull(pts[:, :2])
        self.assertEqual(sorted(map(tuple, fast)), sorted(map(tuple, full)))
        area = 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(fast, np.roll(fast, -1, axis=0)))
        self.assertGreater(area, 0)

    def test_overlap_like_the_tab_touching_is_fine(self):
        a = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        self.assertTrue(pl.overlaps(a, a + [5, 5]))
        self.assertFalse(pl.overlaps(a, a + [10, 0]))      # touching
        self.assertFalse(pl.overlaps(a, a + [10.5, 3]))
        # bounding boxes overlap, shapes don't: the triangle's long side (x + y = 21) passes the corner (10, 10)
        tri = np.array([[8.5, 12.5], [12.5, 8.5], [14, 14]], float)
        self.assertFalse(pl.overlaps(a, tri))
        self.assertTrue(pl.overlaps(a, tri - [1, 1]))

    def test_part_shape_follows_tilt_and_turn_like_mesh_place(self):
        tris = box(40, 20, 10, at=(100, 50, 7))
        part = pl.Part("p", tris)
        part.tilt_x, part.rotation, part.x, part.y = 90, 30, 120, 80
        placed = mesh.place(tris, part.x, part.y, part.rotation, BED["width"], BED["depth"], False, tilt_x=90)
        pts = placed.reshape(-1, 3)[:, :2] + [BED["width"] / 2, BED["depth"] / 2]
        np.testing.assert_allclose(pl.bbox(part.outline()), pl.bbox(pts), atol=1e-6)
        self.assertAlmostEqual(part.height(), 20)

    def test_problems(self):
        a, b, tall = pl.Part("a", box(40, 20, 10)), pl.Part("b", box(30, 30, 5)), pl.Part("t", box(5, 5, 400))
        a.x, a.y, b.x, b.y, tall.x, tall.y = 30, 30, 50, 30, 200, 200
        found = pl.problems([a, b, tall], BED)
        self.assertEqual(found, {"outside": [], "overlapping": [(0, 1)], "too_tall": [2]})
        a.x = 21  # 1 mm into Cura's 3 mm border
        self.assertEqual(pl.problems([a], BED)["outside"], [0])


class LayoutTests(unittest.TestCase):
    def parts(self, *sizes):
        return [pl.Part("p%d" % i, box(*s)) for i, s in enumerate(sizes)]

    def test_every_position(self):
        for position, expected in {
            "top-left": [13, 287, 53, 327], "top-mid": [155, 287, 195, 327], "top-right": [297, 287, 337, 327],
            "mid-left": [13, 150, 53, 190], "center": [155, 150, 195, 190], "mid-right": [297, 150, 337, 190],
            "btm-left": [13, 13, 53, 53], "btm-mid": [155, 13, 195, 53], "btm-right": [297, 13, 337, 53],
        }.items():
            part, = pl.place_group(self.parts((40, 40, 5)), BED, position)
            self.assertEqual(bounds(part), expected, position)

    def test_group_packs_in_rows_from_the_back(self):
        parts = pl.place_group(self.parts((100, 40, 5), (100, 30, 5), (100, 20, 5), (100, 50, 5)), BED, "top-left")
        # room is 350 - 2 x (3 + 10) = 324 wide: three 100 mm parts and two gaps fit in a row, the fourth wraps
        self.assertEqual([round(p.y, 6) for p in parts[:3]], [307.0] * 3)   # row 1 centred on its 40 mm depth
        self.assertEqual(bounds(parts[0])[0], 13.0)
        self.assertEqual(round(parts[3].y, 6), 327 - 40 - 10 - 25)         # row 2 below, gap 10
        self.assertEqual(pl.problems(parts, BED), {"outside": [], "overlapping": [], "too_tall": []})

    def test_does_not_fit(self):
        with self.assertRaisesRegex(pl.LayoutError, "doesn't fit on the bed.*More options"):
            pl.place_group(self.parts((345, 10, 5)), BED, "center")
        pl.place_group(self.parts((344, 10, 5)), BED, "center")            # the centre uses the full width
        with self.assertRaises(pl.LayoutError):
            pl.place_group(self.parts((330, 10, 5)), BED, "top-left")      # a corner keeps the 10 mm margin
        with self.assertRaisesRegex(pl.LayoutError, "tall"):
            pl.place_group(self.parts((10, 10, 331)), BED, "center")
        with self.assertRaisesRegex(pl.LayoutError, "together"):
            pl.place_group(self.parts(*[(150, 150, 5)] * 5), BED, "center")

    def test_move_group_keeps_the_layout(self):
        parts = pl.place_group(self.parts((40, 40, 5), (20, 20, 5)), BED, "center")
        offsets = parts[1].x - parts[0].x, parts[1].y - parts[0].y
        pl.move_group(parts, BED, "btm-right")
        self.assertEqual((parts[1].x - parts[0].x, parts[1].y - parts[0].y), offsets)
        right = max(bounds(p)[2] for p in parts)
        bottom = min(bounds(p)[1] for p in parts)
        self.assertEqual((round(right, 6), round(bottom, 6)), (337.0, 13.0))

    def test_copies_share_the_mesh(self):
        part = self.parts((40, 20, 5))[0]
        twin = part.copy()
        twin.rotation = 90
        self.assertIs(twin.tris, part.tris)
        self.assertEqual([round(v, 6) for v in twin.size()], [20, 40])
        self.assertEqual([round(v, 6) for v in part.size()], [40, 20])

    def test_items_and_centre_origin_beds(self):
        part = self.parts((40, 20, 5))[0]
        part.x, part.y, part.rotation, part.tilt_y, part.extruder = 100.123456, 50, 370, 450, 2
        self.assertEqual(part.item("m"), {"model_id": "m", "x": 100.123, "y": 50, "rotation": 10, "tilt_x": 0,
                                          "tilt_y": 90, "extruder": 2})
        self.assertEqual(pl.agent_xy(175, 170, dict(BED, center_is_zero=True)), (0, 0))
        self.assertEqual(pl.agent_xy(175, 170, BED), (175, 170))


if __name__ == "__main__":
    unittest.main()
