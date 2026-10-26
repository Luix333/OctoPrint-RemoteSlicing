"""Tests for remote_slicing_agent.plate_editor, driving the real Tk window (hidden behind a withdrawn root)."""

import types
import unittest
from unittest import mock

import numpy as np

from remote_slicing_agent import plate_layout as pl

BED = {"width": 350.0, "depth": 340.0, "height": 330.0, "center_is_zero": False, "edge_disallowed_size": 0.0}
SNAPSHOT = {"bed": BED, "summary": {"extruders": [
    {"position": 0, "enabled": True, "material": "ABS", "color": "#8cb219"},
    {"position": 1, "enabled": False, "material": "PLA", "color": "#ff0000"},
    {"position": 2, "enabled": True, "material": "PETG", "color": "#2060ff"}]}}


def box(sx, sy, sz):
    v = np.array([[0, 0, 0], [sx, 0, 0], [sx, sy, 0], [0, sy, 0],
                  [0, 0, sz], [sx, 0, sz], [sx, sy, sz], [0, sy, sz]], dtype=float)
    idx = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 4, 7], [0, 7, 3],
           [1, 2, 6], [1, 6, 5], [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2]]
    return v[np.array(idx)]


def ev(**kw):
    return types.SimpleNamespace(**dict({"state": 0, "delta": 0}, **kw))


class EditorTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no Tk display: %s" % e)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        from remote_slicing_agent.plate_editor import PlateEditor
        self.parts = pl.place_group([pl.Part("A", box(40, 20, 10)), pl.Part("B", box(30, 30, 5))], BED, "center")
        self.result = {}
        self.ed = PlateEditor(self.root, self.parts, SNAPSHOT,
                              lambda parts, start_print: self.result.update(parts=parts, start_print=start_print),
                              lambda: self.result.update(cancelled=True))
        self.addCleanup(self.ed.close)  # runs before the root is destroyed
        self.root.update()

    def drag(self, index, to_x, to_y):
        part = self.ed.parts[index]
        sx, sy = self.ed.to_canvas(part.x, part.y)
        ex, ey = self.ed.to_canvas(to_x, to_y)
        self.ed._press(ev(x=sx, y=sy))
        for k in range(1, 6):
            self.ed._motion(ev(x=sx + (ex - sx) * k / 5, y=sy + (ey - sy) * k / 5))
        self.ed._release(ev(x=ex, y=ey))

    def test_drag_moves_the_part_under_the_mouse(self):
        self.drag(1, 300, 40)
        self.assertEqual(self.ed.selected, 1)
        self.assertAlmostEqual(self.parts[1].x, 300, places=6)
        self.assertAlmostEqual(self.parts[1].y, 40, places=6)
        self.assertEqual(self.ed.var_x.get(), "300.0")
        self.assertEqual(self.ed.tree.item("1")["values"][1], "300.0")

    def test_clicking_empty_bed_selects_nothing_and_moves_nothing(self):
        before = [(p.x, p.y) for p in self.parts]
        self.ed._press(ev(x=5, y=5))
        self.ed._motion(ev(x=50, y=50))
        self.ed._release(ev(x=50, y=50))
        self.assertEqual([(p.x, p.y) for p in self.parts], before)

    def test_wheel_turns_by_15_or_1_with_shift(self):
        part = self.parts[0]
        cx, cy = self.ed.to_canvas(part.x, part.y)
        self.ed._wheel(ev(x=cx, y=cy, delta=120))
        self.assertEqual(part.rotation, 15)
        self.ed._wheel(ev(x=cx, y=cy, delta=-120, state=0x0001))
        self.assertEqual(part.rotation, 14)

    def test_buttons_and_fields(self):
        self.ed._select(0)
        self.ed._turn(-90)
        self.assertEqual(self.parts[0].rotation, 270)
        self.ed._tilt("x")
        self.ed._tilt("y")
        self.assertEqual((self.parts[0].tilt_x, self.parts[0].tilt_y), (90, 90))
        self.ed._tilt(None)
        self.assertEqual((self.parts[0].tilt_x, self.parts[0].tilt_y), (0, 0))
        self.ed.var_x.set("100")
        self.ed.var_y.set("bad")
        self.ed._fields_changed()
        self.assertNotEqual(self.parts[0].x, 100)          # a bad value changes nothing and resets the fields
        self.assertEqual(self.ed.var_x.get(), "%.1f" % self.parts[0].x)
        self.ed.var_x.set("100")
        self.ed.var_y.set("60")
        self.ed.var_rot.set("400")
        self.ed._fields_changed()
        self.assertEqual((self.parts[0].x, self.parts[0].y, self.parts[0].rotation), (100, 60, 40))

    def test_extruder_list_has_only_enabled_ones(self):
        self.assertEqual(list(self.ed.ext_box.cget("values")), ["T0  ABS", "T2  PETG"])
        self.ed._select(1)
        self.ed.ext_box.current(1)
        self.ed._extruder_changed()
        self.assertEqual(self.parts[1].extruder, 2)

    def test_duplicate_remove_arrange_and_move_all(self):
        self.ed._select(0)
        self.ed._duplicate()
        self.assertEqual(len(self.ed.parts), 3)
        self.assertIs(self.ed.parts[1].tris, self.ed.parts[0].tris)
        self.ed._remove()
        self.assertEqual([p.name for p in self.ed.parts], ["A", "B"])
        self.ed._move_all("top-right")
        right = max(pl.bbox(p.outline())[2] for p in self.ed.parts)
        self.assertAlmostEqual(right, 350 - pl.MARGIN)
        self.ed._arrange()
        self.assertEqual(pl.problems(self.ed.parts, BED), {"outside": [], "overlapping": [], "too_tall": []})

    def test_outside_blocks_slicing_and_overlap_asks(self):
        self.drag(0, 5, 100)                                  # half off the left edge
        self.assertEqual([str(s) for s in (b.state() for b in self.ed.slice_buttons)], ["('disabled',)"] * 2)
        self.assertIn("Outside the bed: A", self.ed.problem_text.cget("text"))
        self.ed._slice(False)
        self.assertEqual(self.result, {})
        self.drag(0, self.parts[1].x + 5, self.parts[1].y)   # onto B
        self.assertIn("Overlapping: A and B", self.ed.problem_text.cget("text"))
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            self.ed._slice(True)
        ask.assert_called_once()
        self.assertEqual(self.result, {})
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            self.ed._slice(True)
        self.assertTrue(self.result["start_print"])
        self.assertEqual([p.name for p in self.result["parts"]], ["A", "B"])

    def test_cancel(self):
        self.ed._cancel()
        self.assertEqual(self.result, {"cancelled": True})

    def test_sprites_are_rendered_in_the_background(self):
        for _ in range(100):
            self.root.update()
            if len(self.ed._sprites) >= 2:
                break
            self.root.after(50)
        self.assertGreaterEqual(len(self.ed._sprites), 2)


if __name__ == "__main__":
    unittest.main()
