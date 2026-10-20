"""Tests for remote_slicing_agent.mesh."""

import os
import tempfile
import unittest

import numpy as np

from remote_slicing_agent.mesh import load_stl, place, footprint, write_binary_stl


def make_box():
    """Build a 10×20×5 box centred at the origin (12 triangles, float64)."""
    # Box corners: x∈[-5,5], y∈[-10,10], z∈[-2.5,2.5]
    v = np.array([
        [-5, -10, -2.5],  # 0
        [ 5, -10, -2.5],  # 1
        [ 5,  10, -2.5],  # 2
        [-5,  10, -2.5],  # 3
        [-5, -10,  2.5],  # 4
        [ 5, -10,  2.5],  # 5
        [ 5,  10,  2.5],  # 6
        [-5,  10,  2.5],  # 7
    ], dtype=np.float64)
    # Each face has 2 triangles; CCW winding
    idx = [
        [0, 1, 2], [0, 2, 3],  # bottom (z=-2.5)
        [4, 6, 5], [4, 7, 6],  # top (z=+2.5)
        [0, 4, 7], [0, 7, 3],  # left (x=-5)
        [1, 5, 6], [1, 6, 2],  # right (x=+5)
        [0, 1, 5], [0, 5, 4],  # front (y=-10)
        [3, 2, 6], [3, 6, 7],  # back (y=+10)
    ]
    return v[idx]


def write_ascii_stl(path, tris):
    """Write an ASCII STL for testing."""
    with open(path, "w") as f:
        f.write("solid box\n")
        for i in range(tris.shape[0]):
            f.write("  facet normal 0 0 0\n")
            f.write("    outer loop\n")
            for j in range(3):
                x, y, z = tris[i, j]
                f.write(f"      vertex {x} {y} {z}\n")
            f.write("    endloop\n")
            f.write("  endfacet\n")
        f.write("endsolid box\n")


class TestLoadStl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = make_box()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_binary(self):
        path = os.path.join(self.tmp.name, "box.stl")
        write_binary_stl(path, self.box)
        result = load_stl(path)
        self.assertEqual(result.shape, self.box.shape)
        np.testing.assert_allclose(result, self.box, atol=1e-5)

    def test_load_ascii(self):
        path = os.path.join(self.tmp.name, "box_ascii.stl")
        write_ascii_stl(path, self.box)
        result = load_stl(path)
        self.assertEqual(result.shape, self.box.shape)
        np.testing.assert_allclose(result, self.box, atol=1e-5)

    def test_binary_ascii_identical(self):
        bin_path = os.path.join(self.tmp.name, "box_bin.stl")
        asc_path = os.path.join(self.tmp.name, "box_asc.stl")
        write_binary_stl(bin_path, self.box)
        write_ascii_stl(asc_path, self.box)
        r1 = load_stl(bin_path)
        r2 = load_stl(asc_path)
        np.testing.assert_allclose(r1, r2, atol=1e-5)

    def test_garbage_raises(self):
        path = os.path.join(self.tmp.name, "garbage.stl")
        with open(path, "w") as f:
            f.write("this is not an stl file at all\n")
        with self.assertRaises(ValueError):
            load_stl(path)


class TestPlace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = make_box()

    def tearDown(self):
        self.tmp.cleanup()

    def test_place_rotation_zero(self):
        """Place at (50, 60) on 350×350 bed, center_is_zero=False."""
        placed = place(self.box, 50, 60, 0, 350, 350, center_is_zero=False)
        fp = footprint(placed)
        # Box is 10 wide (x), 20 deep (y). Centre at (50,60) -> x:45-55, y:50-70.
        # center_is_zero=False subtracts (175,175) -> x:-130--120, y:-125--115
        # Wait, spec says: footprint X = 45-175 .. 55-175, Y = 50-175 .. 70-175
        # That means after subtracting 175: 45-175=-130, 55-175=-120, 50-175=-125, 70-175=-115
        self.assertAlmostEqual(fp["min_x"], -130.0, places=1)
        self.assertAlmostEqual(fp["max_x"], -120.0, places=1)
        self.assertAlmostEqual(fp["min_y"], -125.0, places=1)
        self.assertAlmostEqual(fp["max_y"], -105.0, places=1)
        self.assertAlmostEqual(fp["min_z"], 0.0, places=1)

    def test_place_rotation_90(self):
        """Rotation 90° swaps x/y extents (width 20 along X, 10 along Y)."""
        placed = place(self.box, 50, 60, 90, 350, 350, center_is_zero=False)
        fp = footprint(placed)
        # After 90° CCW: x' = -y (half-extent 10), y' = x (half-extent 5)
        # Centre at (50,60): x∈[40,60], y∈[55,65]
        # After -175: x∈[-135,-115], y∈[-120,-110]
        self.assertAlmostEqual(fp["min_x"], -135.0, places=1)
        self.assertAlmostEqual(fp["max_x"], -115.0, places=1)
        self.assertAlmostEqual(fp["min_y"], -120.0, places=1)
        self.assertAlmostEqual(fp["max_y"], -110.0, places=1)

    def test_rotation_is_counter_clockwise(self):
        """+X turns towards +Y: a marker at the +X end ends up at the +Y end after 90 degrees."""
        tris = np.array([[[0, 0, 0], [10, 0, 0], [10, 1, 0]],     # long arm along +X
                         [[0, 0, 0], [0, 2, 0], [-1, 2, 0]]], dtype=np.float64)  # short arm along +Y
        placed = place(tris, 0, 0, 90, 0, 0, center_is_zero=True)
        marker = placed[0, 1]  # the old (10, 0) vertex
        self.assertGreater(marker[1], 3.0)          # now far along +Y
        self.assertAlmostEqual(marker[0], placed[0, 0, 0], places=6)  # and level in X with the old origin

    def test_tilt_x_and_y_lay_the_box_on_other_faces(self):
        """10 x 20 x 5 box: +90 about X stands it on its 10 x 5 face, +90 about Y on its 5 x 20 face."""
        for tx, ty, size in ((90, 0, (10, 5, 20)), (0, 90, (5, 20, 10)), (90, 90, (20, 5, 10)),
                             (180, 0, (10, 20, 5)), (270, 270, (20, 5, 10))):
            fp = footprint(place(self.box, 50, 60, 0, 350, 350, True, tilt_x=tx, tilt_y=ty))
            got = (fp["max_x"] - fp["min_x"], fp["max_y"] - fp["min_y"], fp["max_z"] - fp["min_z"])
            for g, want in zip(got, size):
                self.assertAlmostEqual(g, want, places=6, msg="tilt %s/%s" % (tx, ty))
            self.assertAlmostEqual(fp["min_z"], 0.0, places=6)
            self.assertAlmostEqual((fp["min_x"] + fp["max_x"]) / 2, 50.0, places=6)
            self.assertAlmostEqual((fp["min_y"] + fp["max_y"]) / 2, 60.0, places=6)

    def test_tilt_direction_is_right_handed(self):
        """+90 about X turns +Y into +Z; +90 about Y turns +Z into +X."""
        marker = np.array([[[0, 0, 0], [0, 10, 0], [0, 0, 1]]], dtype=np.float64)  # tip at +Y
        tipped = place(marker, 0, 0, 0, 0, 0, True, tilt_x=90)
        self.assertAlmostEqual(tipped[0, 1, 2] - tipped[0, 0, 2], 10.0, places=6)   # tip now 10 above origin
        up = np.array([[[0, 0, 0], [0, 0, 10], [1, 0, 0]]], dtype=np.float64)       # tip at +Z
        turned = place(up, 0, 0, 0, 0, 0, True, tilt_y=90)
        self.assertAlmostEqual(turned[0, 1, 0] - turned[0, 0, 0], 10.0, places=6)   # tip now along +X

    def test_center_is_zero_true(self):
        """center_is_zero=True leaves centre at (50, 60)."""
        placed = place(self.box, 50, 60, 0, 350, 350, center_is_zero=True)
        fp = footprint(placed)
        self.assertAlmostEqual(fp["min_x"], 45.0, places=1)
        self.assertAlmostEqual(fp["max_x"], 55.0, places=1)
        self.assertAlmostEqual(fp["min_y"], 50.0, places=1)
        self.assertAlmostEqual(fp["max_y"], 70.0, places=1)

    def test_z_lift(self):
        """Box starting at z=7 is dropped to z=0."""
        shifted = self.box.copy()
        shifted[:, :, 2] += 7
        placed = place(shifted, 0, 0, 0, 100, 100, center_is_zero=True)
        fp = footprint(placed)
        self.assertAlmostEqual(fp["min_z"], 0.0, places=1)


class TestWriteBinaryStl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = make_box()

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_size(self):
        path = os.path.join(self.tmp.name, "out.stl")
        write_binary_stl(path, self.box)
        size = os.path.getsize(path)
        self.assertEqual(size, 84 + 50 * 12)

    def test_round_trip(self):
        path = os.path.join(self.tmp.name, "out.stl")
        write_binary_stl(path, self.box)
        result = load_stl(path)
        np.testing.assert_allclose(result, self.box, atol=1e-3)


class TestFootprint(unittest.TestCase):
    def test_box_footprint(self):
        box = make_box()
        fp = footprint(box)
        self.assertAlmostEqual(fp["min_x"], -5.0)
        self.assertAlmostEqual(fp["max_x"], 5.0)
        self.assertAlmostEqual(fp["min_y"], -10.0)
        self.assertAlmostEqual(fp["max_y"], 10.0)
        self.assertAlmostEqual(fp["min_z"], -2.5)
        self.assertAlmostEqual(fp["max_z"], 2.5)


if __name__ == "__main__":
    unittest.main()
