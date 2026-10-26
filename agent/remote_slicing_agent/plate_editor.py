""""More options" for parts sliced from Explorer: lay them out on the bed the way the Remote Slicing tab does.

The plate shows the bed with the back at the top. Each part is drawn as a shaded top-down render inside its
footprint outline. You can:
- drag a part to move it;
- turn it with the mouse wheel (15 degrees a notch, 1 with Shift) or with the buttons and the degree box;
- lay it on another face with Tilt X / Tilt Y (90 degrees, about X then Y, as in the tab);
- pick its extruder, duplicate it or remove it;
- arrange all parts, or move them all together to one of the nine spots on the bed.

Parts outside the bed, overlapping or too tall are outlined in red and listed under the plate, and outside or too
tall parts can't be sliced.
"""

import base64
import queue
import threading

import numpy as np

from . import plate_layout as pl

SNAP_ROTATION = 15.0
OK_OUTLINE = "#4c5a66"
SELECTED_OUTLINE = "#1f6fd1"
PROBLEM_OUTLINE = "#d93025"
ARROWS = {"top-left": "↖", "top-mid": "↑", "top-right": "↗", "mid-left": "←", "center": "•", "mid-right": "→",
          "btm-left": "↙", "btm-mid": "↓", "btm-right": "↘"}


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb)


def _inside(poly, x, y):
    """Point in a counter-clockwise convex polygon."""
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (bx - ax) * (y - ay) - (by - ay) * (x - ax) < 0:
            return False
    return True


class PlateEditor:
    """A Toplevel over ``master``. ``on_slice(parts, start_print)`` or ``on_cancel()`` is called once, on the Tk
    thread, when the user decides."""

    def __init__(self, master, parts, snapshot, on_slice, on_cancel):
        import tkinter as tk
        from tkinter import ttk
        from . import thumbnail
        self.tk, self.ttk, self.thumbnail = tk, ttk, thumbnail
        self.parts = list(parts)
        self.snapshot = snapshot
        self.bed = snapshot["bed"]
        self.on_slice, self.on_cancel = on_slice, on_cancel
        self.decided = False
        self.selected = 0 if self.parts else None
        self.drag = None
        self._updating = False
        self._sprites = {}       # sprite key -> PhotoImage
        self._pending = set()
        self._renders = queue.Queue()
        self._finished = queue.Queue()
        threading.Thread(target=self._render_loop, name="plate-sprites", daemon=True).start()

        self.extruders = [e for e in snapshot["summary"]["extruders"] if e.get("enabled", True)]
        self.colors = {int(e.get("position", i)): thumbnail.parse_color(e.get("color"))
                       for i, e in enumerate(snapshot["summary"]["extruders"])}

        self.win = win = tk.Toplevel(master)
        win.title("Remote Slicing: more options")
        win.protocol("WM_DELETE_WINDOW", self._cancel)
        ppi = win.winfo_fpixels("1i")
        width, depth = float(self.bed["width"]), float(self.bed["depth"])
        self.scale = 4.6 * ppi / max(width, depth)   # canvas pixels per mm
        self.pad = 0.22 * ppi
        body = ttk.Frame(win, padding=(12, 12, 12, 8))
        body.grid(row=0, column=0, sticky="nsew")

        self.canvas = tk.Canvas(body, width=self.pad * 2 + width * self.scale, height=self.pad * 2 + depth * self.scale,
                                background="#f7f7f7", highlightthickness=0, takefocus=1)
        self.canvas.grid(row=0, column=0, rowspan=2, sticky="nw")
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self._wheel)
        for key, delta in (("<Left>", (-1, 0)), ("<Right>", (1, 0)), ("<Up>", (0, 1)), ("<Down>", (0, -1))):
            self.canvas.bind(key, lambda e, d=delta: self._nudge(d, 1))
            self.canvas.bind("<Shift-%s" % key[1:], lambda e, d=delta: self._nudge(d, 10))
        self.canvas.bind("<Delete>", lambda e: self._remove())

        side = ttk.Frame(body, padding=(14, 0, 0, 0))
        side.grid(row=0, column=1, sticky="nsew")
        from tkinter import font
        line = font.nametofont("TkDefaultFont").metrics("linespace")
        self.label_font = font.nametofont("TkDefaultFont").copy()
        self.label_font.configure(size=max(7, self.label_font.cget("size") - 2))
        ttk.Style(win).configure("Treeview", rowheight=int(line * 1.35))  # its default doesn't follow the scaling
        self.tree = ttk.Treeview(side, columns=("t", "x", "y", "r"), height=6, selectmode="browse")
        self.tree.heading("#0", text="Part")
        for col, title in (("t", "Ext."), ("x", "X"), ("y", "Y"), ("r", "Turn")):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=int(0.55 * ppi), anchor="e", stretch=False)
        self.tree.column("#0", width=int(1.5 * ppi))
        self.tree.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)

        box = ttk.LabelFrame(side, text="Selected part", padding=(10, 6))
        box.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.var_ext, self.var_x, self.var_y, self.var_rot = (tk.StringVar() for _ in range(4))
        ttk.Label(box, text="Extruder").grid(row=0, column=0, sticky="w")
        names = ["T%d  %s" % (int(e.get("position", i)), e.get("material") or "") for i, e in enumerate(self.extruders)]
        self.ext_box = ttk.Combobox(box, textvariable=self.var_ext, values=names, state="readonly", width=16)
        self.ext_box.grid(row=0, column=1, columnspan=3, sticky="w", pady=2)
        self.ext_box.bind("<<ComboboxSelected>>", self._extruder_changed)
        ttk.Label(box, text="X / Y (mm)").grid(row=1, column=0, sticky="w")
        self.entry_x = ttk.Entry(box, textvariable=self.var_x, width=8)
        self.entry_y = ttk.Entry(box, textvariable=self.var_y, width=8)
        self.entry_x.grid(row=1, column=1, sticky="w", pady=2)
        self.entry_y.grid(row=1, column=2, sticky="w", padx=(4, 0), pady=2)
        ttk.Label(box, text="Turn (°)").grid(row=2, column=0, sticky="w")
        self.entry_rot = ttk.Entry(box, textvariable=self.var_rot, width=8)
        self.entry_rot.grid(row=2, column=1, sticky="w", pady=2)
        turns = ttk.Frame(box)
        turns.grid(row=2, column=2, columnspan=2, sticky="w", padx=(4, 0))
        ttk.Button(turns, text="⟲ 90°", width=6, command=lambda: self._turn(90)).grid(row=0, column=0)
        ttk.Button(turns, text="⟳ 90°", width=6, command=lambda: self._turn(-90)).grid(row=0, column=1, padx=(4, 0))
        for entry in (self.entry_x, self.entry_y, self.entry_rot):
            entry.bind("<Return>", self._fields_changed)
            entry.bind("<FocusOut>", self._fields_changed)
        ttk.Label(box, text="Lay on side").grid(row=3, column=0, sticky="w")
        tilts = ttk.Frame(box)
        tilts.grid(row=3, column=1, columnspan=3, sticky="w", pady=2)
        ttk.Button(tilts, text="Tilt X 90°", command=lambda: self._tilt("x")).grid(row=0, column=0)
        ttk.Button(tilts, text="Tilt Y 90°", command=lambda: self._tilt("y")).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(tilts, text="Flat", command=lambda: self._tilt(None)).grid(row=0, column=2, padx=(4, 0))
        edits = ttk.Frame(box)
        edits.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(edits, text="Duplicate", command=self._duplicate).grid(row=0, column=0)
        ttk.Button(edits, text="Remove", command=self._remove).grid(row=0, column=1, padx=(4, 0))
        self.selected_widgets = [self.ext_box, self.entry_x, self.entry_y, self.entry_rot] + \
            list(turns.winfo_children()) + list(tilts.winfo_children()) + list(edits.winfo_children())

        group = ttk.LabelFrame(side, text="All parts", padding=(10, 6))
        group.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(group, text="Arrange", command=self._arrange).grid(row=0, column=0, sticky="nw")
        ttk.Label(group, text="Move all to").grid(row=0, column=1, sticky="ne", padx=(16, 6), pady=(4, 0))
        grid = ttk.Frame(group)
        grid.grid(row=0, column=2, sticky="ne")
        for index, position in enumerate(pl.POSITIONS):
            ttk.Button(grid, text=ARROWS[position], width=3,
                       command=lambda p=position: self._move_all(p)).grid(row=index // 3, column=index % 3)
        group.columnconfigure(0, weight=1)

        self.problem_text = ttk.Label(body, text="", foreground=PROBLEM_OUTLINE, justify="left")
        self.problem_text.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.hint = ttk.Label(body, text="Drag to move · wheel to turn (Shift: 1°) · arrows to nudge (Shift: 10 mm)",
                              foreground="#666")
        self.hint.grid(row=3, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(body, padding=(0, 10, 0, 0))
        bar.grid(row=4, column=0, columnspan=2, sticky="ew")
        bar.columnconfigure(0, weight=1)
        ttk.Button(bar, text="Cancel", command=self._cancel).grid(row=0, column=0, sticky="w")
        self.slice_buttons = [ttk.Button(bar, text="Slice and upload", command=lambda: self._slice(False)),
                              ttk.Button(bar, text="Slice, upload & print", command=lambda: self._slice(True))]
        self.slice_buttons[0].grid(row=0, column=1, padx=(0, 6))
        self.slice_buttons[1].grid(row=0, column=2)

        self._redraw()
        self._show_selected()
        win.update_idletasks()
        x = max(0, (win.winfo_screenwidth() - win.winfo_reqwidth()) // 2)
        y = max(0, (win.winfo_screenheight() - win.winfo_reqheight()) // 3)
        win.geometry("+%d+%d" % (x, y))
        win.lift()
        win.focus_force()
        self.canvas.focus_set()
        self._timer = win.after(100, self._collect_sprites)

    # --------------------------------------------------------------------------------------- coordinates

    def to_canvas(self, x, y):
        return self.pad + x * self.scale, self.pad + (float(self.bed["depth"]) - y) * self.scale

    def to_bed(self, cx, cy):
        return (cx - self.pad) / self.scale, float(self.bed["depth"]) - (cy - self.pad) / self.scale

    # --------------------------------------------------------------------------------------- drawing

    def _sprite_key(self, part):
        return (id(part.tris), part.tilt_x % 360, part.tilt_y % 360, round(part.rotation % 360, 2),
                self.colors.get(part.extruder))

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        width, depth = float(self.bed["width"]), float(self.bed["depth"])
        border = float(self.bed.get("edge_disallowed_size") or 0)
        x0, y0 = self.to_canvas(0, depth)
        x1, y1 = self.to_canvas(width, 0)
        c.create_rectangle(x0, y0, x1, y1, fill="#e7ecf0", outline="#9aa5ad")
        step = 50.0
        for gx in np.arange(step, width, step):
            c.create_line(*self.to_canvas(gx, 0), *self.to_canvas(gx, depth), fill="#d3dbe1")
        for gy in np.arange(step, depth, step):
            c.create_line(*self.to_canvas(0, gy), *self.to_canvas(width, gy), fill="#d3dbe1")
        if border > 0:
            c.create_rectangle(*self.to_canvas(border, depth - border), *self.to_canvas(width - border, border),
                               outline="#b0bac2", dash=(4, 3))
        c.create_text((x0 + x1) / 2, y0 - self.pad / 2, text="Back", fill="#8a959e")
        c.create_text((x0 + x1) / 2, y1 + self.pad / 2, text="Front", fill="#8a959e")

        found = pl.problems(self.parts, self.bed)
        bad = set(found["outside"]) | set(found["too_tall"]) | {i for pair in found["overlapping"] for i in pair}
        for i, part in enumerate(self.parts):
            self._draw_part(i, part, i in bad)
        self._draw_labels()
        self._show_problems(found)
        self._fill_tree()

    def _draw_labels(self):
        """Name under each part, or above it when that spot is taken; a label that fits nowhere is left out,
        except the selected part's (drawn first, so it always gets its place)."""
        c = self.canvas
        order = sorted(range(len(self.parts)), key=lambda i: i != self.selected)
        taken = []
        for i in order:
            part = self.parts[i]
            x0, y0, x1, y1 = pl.bbox(part.outline())
            cx, below = self.to_canvas((x0 + x1) / 2, y0)
            _cx, above = self.to_canvas((x0 + x1) / 2, y1)
            text = "%s · T%d" % (part.name, part.extruder)
            for y, anchor in ((below + 3, "n"), (above - 3, "s")):
                item = c.create_text(cx, y, anchor=anchor, text=text, fill="#24313b", font=self.label_font,
                                     tags=("part%d" % i,))
                box = c.bbox(item)
                shift = max(0, 2 - box[0]) - max(0, box[2] - (int(c.cget("width")) - 2))  # keep it on the canvas
                if shift:
                    c.move(item, shift, 0)
                    box = c.bbox(item)
                if not any(box[0] < t[2] and t[0] < box[2] and box[1] < t[3] and t[1] < box[3] for t in taken):
                    taken.append(box)
                    break
                if i == self.selected and anchor == "s":
                    break  # keep it anyway
                c.delete(item)

    def _draw_part(self, i, part, bad):
        c = self.canvas
        tag = "part%d" % i
        outline = part.outline()
        flat = [v for x, y in outline for v in self.to_canvas(x, y)]
        key = self._sprite_key(part)
        sprite = self._sprites.get(key)
        color = _hex(self.colors.get(part.extruder, (0.8, 0.8, 0.8)))
        if sprite is None:
            c.create_polygon(*flat, fill=color, stipple="gray50", outline="", tags=(tag,))
            self._request_sprite(part, key)
        else:
            c.create_image(*self.to_canvas(part.x, part.y), image=sprite, tags=(tag,))
        line = PROBLEM_OUTLINE if bad else (SELECTED_OUTLINE if i == self.selected else OK_OUTLINE)
        c.create_polygon(*flat, fill="", outline=line, width=3 if i == self.selected else 1.5, tags=(tag,))

    def _show_problems(self, found):
        lines = []
        names = lambda idx: ", ".join(self.parts[i].name for i in idx)  # noqa: E731
        if found["outside"]:
            lines.append("Outside the bed: %s" % names(found["outside"]))
        if found["too_tall"]:
            lines.append("Too tall for the printer: %s" % names(found["too_tall"]))
        if found["overlapping"]:
            lines.append("Overlapping: %s" % "; ".join("%s and %s" % (self.parts[i].name, self.parts[j].name)
                                                       for i, j in found["overlapping"]))
        if not self.parts:
            lines.append("No parts left on the plate.")
        self.problem_text.configure(text="\n".join(lines))
        blocked = bool(found["outside"] or found["too_tall"] or not self.parts)
        for button in self.slice_buttons:
            button.state(["disabled"] if blocked else ["!disabled"])

    def _fill_tree(self):
        rows = [(part.name, ("T%d" % part.extruder, "%.1f" % part.x, "%.1f" % part.y,
                             "%g°" % round(part.rotation % 360, 1))) for part in self.parts]
        existing = self.tree.get_children()
        for iid in existing[len(rows):]:
            self.tree.delete(iid)
        for i, (name, values) in enumerate(rows):
            if str(i) in existing:
                self.tree.item(str(i), text=name, values=values)
            else:
                self.tree.insert("", "end", iid=str(i), text=name, values=values)
        # Tk reports a selection change later, as an event: only touch it when it really differs, or that event
        # would redraw, refill and select again, forever
        wanted = (str(self.selected),) if self.selected is not None and self.selected < len(self.parts) else ()
        if tuple(self.tree.selection()) != wanted:
            self.tree.selection_set(wanted)

    # --------------------------------------------------------------------------------------- sprites

    def _request_sprite(self, part, key):
        if key in self._pending:
            return
        self._pending.add(key)
        self._renders.put((key, part.tris, part.tilt_x, part.tilt_y, part.rotation, self.colors.get(part.extruder)))

    def _render_loop(self):
        from . import mesh
        while True:
            key, tris, tilt_x, tilt_y, rotation, rgb = self._renders.get()
            try:
                placed = mesh.place(tris, 0.0, 0.0, rotation, 0.0, 0.0, True, tilt_x=tilt_x, tilt_y=tilt_y)
                pts = placed.reshape(-1, 3)
                w = int(np.ptp(pts[:, 0]) * self.scale) + 4
                h = int(np.ptp(pts[:, 1]) * self.scale) + 4
                rgba = self.thumbnail.render_top([(placed, rgb or (0.8, 0.8, 0.8))], max(w, 2), max(h, 2), self.scale)
                png = self.thumbnail.encode_png(rgba) if rgba is not None else None
            except Exception:
                png = None
            self._finished.put((key, png))

    def _collect_sprites(self):
        changed = False
        try:
            while True:
                key, png = self._finished.get_nowait()
                self._pending.discard(key)
                if png:
                    self._sprites[key] = self.tk.PhotoImage(master=self.win, data=base64.b64encode(png))
                    changed = True
        except queue.Empty:
            pass
        if changed and self.drag is None:
            self._redraw()
        if not self.decided:
            self._timer = self.win.after(120, self._collect_sprites)

    # --------------------------------------------------------------------------------------- selection

    def _select(self, index):
        self.selected = index
        self._show_selected()

    def _show_selected(self):
        part = self.parts[self.selected] if self.selected is not None and self.selected < len(self.parts) else None
        for widget in self.selected_widgets:
            widget.state(["!disabled"] if part else ["disabled"])
        if part is None:
            return
        self.ext_box.state(["readonly"])
        self._updating = True
        self.var_x.set("%.1f" % part.x)
        self.var_y.set("%.1f" % part.y)
        self.var_rot.set("%g" % round(part.rotation % 360, 2))
        for i, e in enumerate(self.extruders):
            if int(e.get("position", i)) == part.extruder:
                self.ext_box.current(i)
        self._updating = False

    def _tree_selected(self, _event=None):
        if self._updating:
            return
        chosen = self.tree.selection()
        if chosen and int(chosen[0]) != self.selected:
            self._select(int(chosen[0]))
            self._redraw()

    # --------------------------------------------------------------------------------------- mouse and keys

    def _part_at(self, x, y):
        for i in range(len(self.parts) - 1, -1, -1):
            if _inside(self.parts[i].outline(), x, y):
                return i
        return None

    def _press(self, event):
        self.canvas.focus_set()
        x, y = self.to_bed(event.x, event.y)
        index = self._part_at(x, y)
        if index is None:
            self.drag = None
            return
        part = self.parts[index]
        self._select(index)
        self.drag = (index, part.x - x, part.y - y, event.x, event.y)
        self._redraw()

    def _motion(self, event):
        if self.drag is None:
            return
        index, ox, oy, last_x, last_y = self.drag
        part = self.parts[index]
        x, y = self.to_bed(event.x, event.y)
        part.x, part.y = x + ox, y + oy
        self.canvas.move("part%d" % index, event.x - last_x, event.y - last_y)
        self.drag = (index, ox, oy, event.x, event.y)
        self._updating = True
        self.var_x.set("%.1f" % part.x)
        self.var_y.set("%.1f" % part.y)
        self._updating = False

    def _release(self, _event):
        if self.drag is not None:
            self.drag = None
            self._redraw()

    def _wheel(self, event):
        x, y = self.to_bed(event.x, event.y)
        index = self._part_at(x, y)
        if index is None:
            index = self.selected
        if index is None:
            return
        self._select(index)
        step = 1.0 if event.state & 0x0001 else SNAP_ROTATION  # Shift held
        self._turn(step if event.delta > 0 else -step)

    def _nudge(self, direction, step):
        part = self._current()
        if part is None:
            return "break"
        part.x += direction[0] * step
        part.y += direction[1] * step
        self._changed()
        return "break"

    # --------------------------------------------------------------------------------------- edits

    def _current(self):
        if self.selected is None or self.selected >= len(self.parts):
            return None
        return self.parts[self.selected]

    def _changed(self):
        self._show_selected()
        self._redraw()

    def _fields_changed(self, _event=None):
        part = self._current()
        if part is None or self._updating:
            return
        try:
            x, y, rot = float(self.var_x.get()), float(self.var_y.get()), float(self.var_rot.get())
        except ValueError:
            self._show_selected()  # put the last good values back
            return
        if (x, y, rot) != (round(part.x, 1), round(part.y, 1), round(part.rotation % 360, 2)):
            part.x, part.y, part.rotation = x, y, rot % 360
            self._changed()

    def _extruder_changed(self, _event=None):
        part = self._current()
        if part is None or self._updating:
            return
        index = self.ext_box.current()
        if index >= 0:
            part.extruder = int(self.extruders[index].get("position", index))
            self._changed()

    def _turn(self, degrees):
        part = self._current()
        if part is not None:
            part.rotation = (part.rotation + degrees) % 360
            self._changed()

    def _tilt(self, axis):
        part = self._current()
        if part is None:
            return
        if axis == "x":
            part.tilt_x = (part.tilt_x + 90) % 360
        elif axis == "y":
            part.tilt_y = (part.tilt_y + 90) % 360
        else:
            part.tilt_x = part.tilt_y = 0
        self._changed()

    def _duplicate(self):
        part = self._current()
        if part is None:
            return
        twin = part.copy()
        twin.x += part.size()[0] + pl.GAP
        self.parts.insert(self.selected + 1, twin)
        self.selected += 1
        self._changed()

    def _remove(self):
        if self._current() is None:
            return
        del self.parts[self.selected]
        self.selected = min(self.selected, len(self.parts) - 1) if self.parts else None
        self._changed()

    def _arrange(self):
        try:
            pl.place_group(self.parts, self.bed, "center")
        except pl.LayoutError as e:
            self.problem_text.configure(text=str(e))
            return
        self._changed()

    def _move_all(self, position):
        try:
            pl.move_group(self.parts, self.bed, position)
        except pl.LayoutError as e:
            self.problem_text.configure(text=str(e))
            return
        self._changed()

    # --------------------------------------------------------------------------------------- decisions

    def _slice(self, start_print):
        self._fields_changed()
        found = pl.problems(self.parts, self.bed)
        if found["outside"] or found["too_tall"] or not self.parts:
            return
        if found["overlapping"]:
            from tkinter import messagebox
            if not messagebox.askyesno("Remote Slicing", "Some parts overlap. Slice the plate anyway?", parent=self.win):
                return
        self._finish()
        self.on_slice(self.parts, start_print)

    def _cancel(self):
        self._finish()
        self.on_cancel()

    def _finish(self):
        self.close()

    def close(self):
        """Close the window without deciding anything (the callers' callbacks are not called)."""
        self.decided = True
        try:
            self.win.after_cancel(self._timer)
            self.win.destroy()
        except Exception:
            pass  # already gone with its master
