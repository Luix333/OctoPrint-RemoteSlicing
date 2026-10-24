"""Tests for remote_slicing_agent.thumbnail (headless stand-in for Cura's Create Thumbnail snapshot)."""

import struct
import time
import unittest
import zlib

import numpy as np

from remote_slicing_agent.thumbnail import encode_png, parse_color, prettier, render_png, render_top


def decode_png(data):
    """Minimal decoder for what encode_png writes: 8-bit RGBA, no interlace, filter 0 on every row."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, kind
        chunks.append((kind, body))
        pos += 12 + length
    assert chunks[0][0] == b"IHDR" and chunks[-1][0] == b"IEND"
    w, h, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, ctype, comp, filt, interlace) == (8, 6, 0, 0, 0)
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(h, 1 + w * 4)
    assert (rows[:, 0] == 0).all()
    return rows[:, 1:].reshape(h, w, 4)


def box(x0, x1, y0, y1, z0, z1):
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=np.float64)
    idx = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 4, 7], [0, 7, 3],
           [1, 2, 6], [1, 6, 5], [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2]]
    return v[np.array(idx)]


def sphere(radius, n_lat, n_lon, center=(0.0, 0.0, 0.0)):
    th = np.linspace(0, np.pi, n_lat + 1)
    ph = np.linspace(0, 2 * np.pi, n_lon + 1)
    t, p = np.meshgrid(th, ph, indexing="ij")
    pts = np.stack([np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)], axis=-1) * radius + center
    a, b = pts[:-1, :-1], pts[1:, :-1]
    c, d = pts[1:, 1:], pts[:-1, 1:]
    tris = np.concatenate([np.stack([a, b, c], axis=-2).reshape(-1, 3, 3),
                           np.stack([a, c, d], axis=-2).reshape(-1, 3, 3)])
    return tris


def opaque_extent(img):
    ys, xs = np.nonzero(img[:, :, 3] > 0)
    return xs.min(), xs.max(), ys.min(), ys.max()


class PngTests(unittest.TestCase):
    def test_round_trip(self):
        rng = np.random.default_rng(1)
        img = rng.integers(0, 256, size=(7, 5, 4), dtype=np.uint8)
        np.testing.assert_array_equal(decode_png(encode_png(img)), img)


class ColorTests(unittest.TestCase):
    def test_parse_color(self):
        self.assertEqual(parse_color("#ff8000"), (1.0, 128 / 255, 0.0))
        self.assertEqual(parse_color("#FF8000"), (1.0, 128 / 255, 0.0))
        self.assertEqual(parse_color("nonsense"), (0.8, 0.8, 0.8))
        self.assertEqual(parse_color(None), (0.8, 0.8, 0.8))

    def test_prettier_like_cura(self):
        # PreviewPass.prettier_color: scale so the brightest channel is 1, by at most 2.5x
        np.testing.assert_allclose(prettier((0.2, 0.1, 0.1)), (0.5, 0.25, 0.25))
        np.testing.assert_allclose(prettier((0.8, 0.4, 0.0)), (1.0, 0.5, 0.0))
        np.testing.assert_allclose(prettier((0.0, 0.0, 0.0)), (0.0, 0.0, 0.0))


class RenderTests(unittest.TestCase):
    def test_empty_plate_gives_none(self):
        self.assertIsNone(render_png([], 128, 128))
        self.assertIsNone(render_png([(np.zeros((0, 3, 3)), (1.0, 0.0, 0.0))], 128, 128))

    def test_box_fills_the_square_and_background_is_transparent(self):
        img = decode_png(render_png([(box(-5, 5, -10, 10, 0, 5), (0.6, 0.8, 0.1))], 128, 128))
        self.assertEqual(img.shape, (128, 128, 4))
        x0, x1, y0, y1 = opaque_extent(img)
        # cropped to the content and made square, so it spans (almost) the full width or height
        self.assertTrue(x1 - x0 >= 124 or y1 - y0 >= 124, (x0, x1, y0, y1))
        for corner in (img[0, 0], img[0, -1], img[-1, 0], img[-1, -1]):
            self.assertEqual(corner[3], 0)
        self.assertGreater((img[:, :, 3] == 255).sum(), 128 * 128 // 5)

    def test_top_face_is_brighter_than_the_sides(self):
        img = decode_png(render_png([(box(-5, 5, -10, 10, 0, 5), (0.6, 0.8, 0.1))], 128, 128)).astype(float)
        mask = img[:, :, 3] == 255
        rows = np.nonzero(mask.any(axis=1))[0]
        third = (rows.max() - rows.min()) // 3
        top = img[rows.min():rows.min() + third][mask[rows.min():rows.min() + third]][:, :3].mean()
        bottom = img[rows.max() - third:rows.max() + 1][mask[rows.max() - third:rows.max() + 1]][:, :3].mean()
        self.assertGreater(top, bottom + 20)

    def test_view_is_from_the_front_left(self):
        red, blue = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        # -X is nearer the camera and to the left
        img = decode_png(render_png([(box(-60, -40, -10, 10, 0, 20), red), (box(40, 60, -10, 10, 0, 20), blue)], 96, 96))
        reds = np.nonzero((img[:, :, 0] > 100) & (img[:, :, 2] < 60) & (img[:, :, 3] == 255))
        blues = np.nonzero((img[:, :, 2] > 100) & (img[:, :, 0] < 60) & (img[:, :, 3] == 255))
        self.assertLess(reds[1].mean(), blues[1].mean())
        # the back (+Y) is further away, so higher up in the picture
        img = decode_png(render_png([(box(-10, 10, 40, 60, 0, 20), red), (box(-10, 10, -60, -40, 0, 20), blue)], 96, 96))
        reds = np.nonzero((img[:, :, 0] > 100) & (img[:, :, 2] < 60) & (img[:, :, 3] == 255))
        blues = np.nonzero((img[:, :, 2] > 100) & (img[:, :, 0] < 60) & (img[:, :, 3] == 255))
        self.assertLess(reds[0].mean(), blues[0].mean())

    def test_nearer_object_hides_the_one_behind(self):
        red, blue = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        # a tall red wall in front (-Y) of a small blue box: blue must be (almost) entirely hidden
        img = decode_png(render_png([(box(-40, 40, -22, -20, 0, 60), red), (box(-5, 5, 0, 10, 0, 5), blue)], 128, 128))
        blues = ((img[:, :, 2] > 100) & (img[:, :, 0] < 60) & (img[:, :, 3] == 255)).sum()
        self.assertLess(blues, 10)

    def test_deterministic(self):
        tris = sphere(20, 30, 40)
        self.assertEqual(render_png([(tris, (0.5, 0.5, 0.9))], 64, 64), render_png([(tris, (0.5, 0.5, 0.9))], 64, 64))

    def test_dense_mesh_is_fast_and_has_no_holes(self):
        tris = sphere(30, 300, 400)  # 240k triangles, mostly far smaller than a pixel
        start = time.perf_counter()
        img = decode_png(render_png([(tris, (0.9, 0.9, 0.9))], 128, 128))
        self.assertLess(time.perf_counter() - start, 4.0)
        # a sphere is a disc in the picture: no transparent pixels inside it
        alpha = img[:, :, 3]
        self.assertEqual(alpha[64, 64], 255)
        inside = alpha[40:88, 40:88]
        self.assertTrue((inside == 255).all())


class RenderTopTests(unittest.TestCase):
    def test_box_alpha_extent(self):
        # 10 mm box at origin, px_per_mm=2, 100x80 canvas
        # screen: sx = x*2 + 50, sy = -y*2 + 40
        # box x=[0,10] → sx=[50,70], y=[0,10] → sy=[40,20] (the back of the bed is at the top)
        img = render_top([(box(0, 10, 0, 10, 0, 10), (1.0, 0.0, 0.0))], 100, 80, 2)
        self.assertEqual(img.shape, (80, 100, 4))
        expected = np.zeros((80, 100), dtype=np.uint8)
        expected[20:40, 50:70] = 255
        np.testing.assert_array_equal(img[:, :, 3], expected)

    def test_back_is_at_top(self):
        # Red box at back (+Y), blue box at front (-Y); red should be higher (smaller row index)
        red = (1.0, 0.0, 0.0)
        blue = (0.0, 0.0, 1.0)
        img = render_top(
            [(box(-10, -5, 3, 12, 0, 5), red), (box(-10, -5, -12, -3, 0, 5), blue)],
            100, 80, 2,
        )
        # Red box: x=[-10,-5] → sx=[30,40], y=[3,12] → sy=[34,22]
        # Blue box: x=[-10,-5] → sx=[30,40], y=[-12,-3] → sy=[64,46]
        # Check red-dominant pixels in red box region
        red_region = img[22:39, 31:40]
        blue_region = img[41:59, 31:40]
        red_dominant = (red_region[:, :, 0] > 80) & (red_region[:, :, 2] < 80) & (red_region[:, :, 3] == 255)
        blue_dominant = (blue_region[:, :, 2] > 80) & (blue_region[:, :, 0] < 80) & (blue_region[:, :, 3] == 255)
        self.assertGreater(red_dominant.sum(), 5)
        self.assertGreater(blue_dominant.sum(), 5)

    def test_depth_testing_near_wins(self):
        # Blue box (z=0-20) occludes red box (z=0-5) where they overlap
        red = (1.0, 0.0, 0.0)
        blue = (0.0, 0.0, 1.0)
        img = render_top(
            [(box(-10, 10, -10, 10, 0, 5), red), (box(-2, 2, -2, 2, 0, 20), blue)],
            100, 80, 2,
        )
        # Center (0,0) → screen (50,40): inside both boxes, blue wins (higher)
        self.assertGreater(img[40, 50, 2], 100)  # blue-dominant
        self.assertLess(img[40, 50, 0], 100)
        # (x=-7.5, y=0) → screen (35,40): inside red box only, red wins
        self.assertGreater(img[40, 35, 0], 100)  # red-dominant
        self.assertLess(img[40, 35, 2], 100)

    def test_flat_top_color(self):
        # Top face of a centered red box: well-lit, so bright red
        img = render_top([(box(-5, 5, -5, 5, 0, 10), (1.0, 0.0, 0.0))], 40, 40, 2)
        # Center pixel should be close to [235, 27, 27, 255]
        np.testing.assert_array_less(np.abs(img[20, 20] - np.array([235, 27, 27, 255])), 3)

    def test_empty_or_none(self):
        self.assertIsNone(render_top([], 100, 80, 2))
        self.assertIsNone(render_top([(np.zeros((0, 3, 3)), (1.0, 0.0, 0.0))], 100, 80, 2))

    def test_non_square_shape(self):
        img = render_top([(box(-5, 5, -5, 5, 0, 10), (0.5, 0.5, 0.5))], 200, 50, 1)
        self.assertEqual(img.shape, (50, 200, 4))

    def test_degenerate_only_is_none(self):
        degenerate = np.array([[[0, 0, 0], [1, 1, 1], [2, 2, 2]]], dtype=np.float64)
        self.assertIsNone(render_top([(degenerate, (1.0, 0.0, 0.0))], 100, 80, 2))


if __name__ == "__main__":
    unittest.main()
