/*
 * Remote Slicing - Bed module: STL parsing, convex hull, bed visualization widget.
 *
 * ES5-compatible (concatenated into OctoPrint's bundle). No external dependencies.
 */
(function () {
    "use strict";

    var api = {};

    /* ------------------------------------------------------------------ */
    /*  STL parsing                                                        */
    /* ------------------------------------------------------------------ */

    api.parseStl = function (arrayBuffer) {
        var bytes = new Uint8Array(arrayBuffer);
        var result = { points: null, count: 0, minZ: Infinity, maxZ: -Infinity };

        // Try binary STL first
        if (bytes.length >= 84) {
            var numTriangles = new Uint32Array(arrayBuffer, 80, 1)[0];
            var expectedLen = 84 + numTriangles * 50;
            if (bytes.length === expectedLen) {
                return parseBinaryStl(arrayBuffer, result);
            }
        }

        // Fall back to ASCII STL
        return parseAsciiStl(arrayBuffer, result);
    };

    function parseBinaryStl(buf, result) {
        var view = new DataView(buf);
        var dv = view;
        var count = 0;
        var minZ = Infinity;
        var maxZ = -Infinity;
        var verts = [];
        var xyz = [];

        for (var i = 0; i < numTrianglesFromBuf(buf); i++) {
            var offset = 84 + i * 50;
            for (var v = 0; v < 3; v++) {
                var vx = dv.getFloat32(offset + 12 + v * 12, true);
                var vy = dv.getFloat32(offset + 12 + v * 12 + 4, true);
                var vz = dv.getFloat32(offset + 12 + v * 12 + 8, true);
                verts.push(vx, vy);
            xyz.push(vx, vy, vz);
                count++;
                if (vz < minZ) minZ = vz;
                if (vz > maxZ) maxZ = vz;
            }
        }

        if (count === 0) throw new Error("not an STL file");
        result.points = new Float64Array(verts);
        result.xyz = new Float32Array(xyz);
        result.count = count;
        result.minZ = minZ;
        result.maxZ = maxZ;
        return result;
    }

    function numTrianglesFromBuf(buf) {
        return new Uint32Array(buf, 80, 1)[0];
    }

    function parseAsciiStl(buf, result) {
        var text = new TextDecoder().decode(buf);
        var re = /vertex\s+([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s+([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s+([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)/g;
        var count = 0;
        var minZ = Infinity;
        var maxZ = -Infinity;
        var verts = [];
        var xyz = [];
        var m;

        while ((m = re.exec(text)) !== null) {
            var vx = parseFloat(m[1]);
            var vy = parseFloat(m[2]);
            var vz = parseFloat(m[3]);
            verts.push(vx, vy);
            xyz.push(vx, vy, vz);
            count++;
            if (vz < minZ) minZ = vz;
            if (vz > maxZ) maxZ = vz;
        }

        if (count === 0) throw new Error("not an STL file");
        result.points = new Float64Array(verts);
        result.xyz = new Float32Array(xyz);
        result.count = count;
        result.minZ = minZ;
        result.maxZ = maxZ;
        return result;
    }

    /* ------------------------------------------------------------------ */
    /*  Tilt: lay the model on another face                                */
    /* ------------------------------------------------------------------ */

    /*
     * X/Y of every vertex after rotating the model tiltX degrees about X and then tiltY degrees about Y
     * (right-handed), the same transform the PC agent applies before the Z rotation. Returns an
     * interleaved [x0, y0, x1, y1, ...] Float64Array ready for convexHull().
     */
    api.tiltPoints = function (xyz, count, tiltX, tiltY) {
        var ax = (tiltX || 0) * Math.PI / 180, ay = (tiltY || 0) * Math.PI / 180;
        var cx = Math.cos(ax), sx = Math.sin(ax), cy = Math.cos(ay), sy = Math.sin(ay);
        var out = new Float64Array(count * 2);
        for (var i = 0; i < count; i++) {
            var x = xyz[i * 3], y = xyz[i * 3 + 1], z = xyz[i * 3 + 2];
            var y1 = y * cx - z * sx, z1 = y * sx + z * cx;   // about X
            out[i * 2] = x * cy + z1 * sy;                      // about Y
            out[i * 2 + 1] = y1;
        }
        return out;
    };

    /* ------------------------------------------------------------------ */
    /*  Top-down render: what the model looks like from above, as it lies  */
    /* ------------------------------------------------------------------ */

    /*
     * Z-buffered, flat-shaded raster of the mesh seen from above after the tilt (same transform as
     * tiltPoints). Pixel (u, v): u along +X, v DOWN the image = towards -Y, so row 0 is the back edge (maxY);
     * one pixel is 1/ppm mm. shade[i] is 0 for empty pixels, else 1..255 brightness: faces lit from the
     * back-left-above, higher surfaces a little brighter. depth[i] is the top surface's Z (for tests).
     */
    api.renderTop = function (xyz, count, tiltX, tiltY, maxPx) {
        maxPx = maxPx || 512;
        var ax = (tiltX || 0) * Math.PI / 180, ay = (tiltY || 0) * Math.PI / 180;
        var cx = Math.cos(ax), sx = Math.sin(ax), cy = Math.cos(ay), sy = Math.sin(ay);
        var v = new Float64Array(count * 3);
        var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, minZ = Infinity, maxZ = -Infinity;
        for (var i = 0; i < count; i++) {
            var x = xyz[i * 3], y = xyz[i * 3 + 1], z = xyz[i * 3 + 2];
            var y1 = y * cx - z * sx, z1 = y * sx + z * cx;            // about X
            var X = x * cy + z1 * sy, Y = y1, Z = -x * sy + z1 * cy;    // about Y
            v[i * 3] = X; v[i * 3 + 1] = Y; v[i * 3 + 2] = Z;
            if (X < minX) minX = X; if (X > maxX) maxX = X;
            if (Y < minY) minY = Y; if (Y > maxY) maxY = Y;
            if (Z < minZ) minZ = Z; if (Z > maxZ) maxZ = Z;
        }
        var span = Math.max(maxX - minX, maxY - minY, 1e-6);
        var ppm = Math.min(8, maxPx / span);
        var W = Math.max(1, Math.ceil((maxX - minX) * ppm - 1e-6)), H = Math.max(1, Math.ceil((maxY - minY) * ppm - 1e-6));  // tilt leaves 5.0000001 mm
        var depth = new Float32Array(W * H), shade = new Uint8ClampedArray(W * H);
        for (var k = 0; k < depth.length; k++) depth[k] = -Infinity;
        var ln = Math.sqrt(0.35 * 0.35 + 0.45 * 0.45 + 1);
        var lx = -0.35 / ln, ly = 0.45 / ln, lz = 1 / ln;               // light from back-left, above
        var zr = (maxZ - minZ) || 1;

        for (var t = 0; t + 2 < count; t += 3) {
            var a = t * 3, b = a + 3, c = a + 6;
            var ux = v[b] - v[a], uy = v[b + 1] - v[a + 1], uz = v[b + 2] - v[a + 2];
            var wx = v[c] - v[a], wy = v[c + 1] - v[a + 1], wz = v[c + 2] - v[a + 2];
            var nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
            if (nz <= 0) continue;                                      // facing down or edge-on: hidden from above
            var nl = Math.sqrt(nx * nx + ny * ny + nz * nz);
            var diffuse = Math.max(0, (nx * lx + ny * ly + nz * lz) / nl);
            var x0 = (v[a] - minX) * ppm, y0 = (maxY - v[a + 1]) * ppm, z0 = v[a + 2];
            var x1 = (v[b] - minX) * ppm, y1s = (maxY - v[b + 1]) * ppm, z1s = v[b + 2];
            var x2 = (v[c] - minX) * ppm, y2 = (maxY - v[c + 1]) * ppm, z2 = v[c + 2];
            var area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1s - y0);
            if (area === 0) continue;
            var u0 = Math.max(0, Math.floor(Math.min(x0, x1, x2))), u1 = Math.min(W - 1, Math.ceil(Math.max(x0, x1, x2)));
            var v0 = Math.max(0, Math.floor(Math.min(y0, y1s, y2))), v1 = Math.min(H - 1, Math.ceil(Math.max(y0, y1s, y2)));
            for (var py = v0; py <= v1; py++) {
                var cyp = py + 0.5;
                for (var px = u0; px <= u1; px++) {
                    var cxp = px + 0.5;
                    var e0 = ((x2 - x1) * (cyp - y1s) - (y2 - y1s) * (cxp - x1)) / area;
                    var e1 = ((x0 - x2) * (cyp - y2) - (y0 - y2) * (cxp - x2)) / area;
                    var e2 = 1 - e0 - e1;
                    if (e0 < 0 || e1 < 0 || e2 < 0) continue;
                    var zz = e0 * z0 + e1 * z1s + e2 * z2;
                    var idx = py * W + px;
                    if (zz <= depth[idx]) continue;
                    depth[idx] = zz;
                    var lum = (0.30 + 0.70 * diffuse) * (0.70 + 0.30 * (zz - minZ) / zr);
                    shade[idx] = 1 + Math.round(254 * Math.min(1, lum));
                }
            }
        }
        return {width: W, height: H, ppm: ppm, minX: minX, maxX: maxX, minY: minY, maxY: maxY, shade: shade, depth: depth};
    };

    /* Canvas of a render tinted with a filament colour ("#rrggbb"). Browser only. */
    api.spriteCanvas = function (render, color) {
        var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(color || "") || [0, "88", "88", "88"];
        var floor = 60;  // keep very dark filaments readable on the dark bed
        var r = floor + parseInt(m[1], 16) * (255 - floor) / 255;
        var g = floor + parseInt(m[2], 16) * (255 - floor) / 255;
        var bl = floor + parseInt(m[3], 16) * (255 - floor) / 255;
        var canvas = document.createElement("canvas");
        canvas.width = render.width;
        canvas.height = render.height;
        var ctx = canvas.getContext("2d");
        var img = ctx.createImageData(render.width, render.height);
        var d = img.data, sh = render.shade;
        for (var i = 0; i < sh.length; i++) {
            if (!sh[i]) continue;
            var f = sh[i] / 255;
            d[i * 4] = r * f; d[i * 4 + 1] = g * f; d[i * 4 + 2] = bl * f; d[i * 4 + 3] = 255;
        }
        ctx.putImageData(img, 0, 0);
        return canvas;
    };

    /*
     * Canvas transform [a, b, c, d, e, f] that draws a render's pixels where api.place() puts the model:
     * rotate about Z, move the rotated hull's bbox centre to (x, y), then the bed's mm -> CSS px mapping
     * (screen x = offsetX + mm_x * scale, screen y = offsetY + (bedDepth - mm_y) * scale).
     */
    api.spriteMatrix = function (render, hull, x, y, rotation, view) {
        var th = (rotation || 0) * Math.PI / 180, ct = Math.cos(th), st = Math.sin(th);
        var bb = api.bbox(api.rotate(hull, rotation || 0));
        var ox = x - (bb.minX + bb.maxX) / 2, oy = y - (bb.minY + bb.maxY) / 2;
        var s = view.scale, k = s / render.ppm;
        return [k * ct, -k * st, k * st, k * ct,
                view.offsetX + s * (ct * render.minX - st * render.maxY + ox),
                view.offsetY + s * (view.depth - st * render.minX - ct * render.maxY - oy)];
    };

    /* ------------------------------------------------------------------ */
    /*  Convex hull (Andrew's monotone chain)                              */
    /* ------------------------------------------------------------------ */

    api.convexHull = function (points, count, grid) {
        if (grid === undefined) grid = 0.05;

        // Quantize and deduplicate (sort-based for performance)
        var raw = [];
        for (var i = 0; i < count; i++) {
            var rawX = points[i * 2];
            var rawY = points[i * 2 + 1];
            var qx = Math.round(rawX / grid) * grid;
            var qy = Math.round(rawY / grid) * grid;
            raw.push([qx, qy]);
        }
        raw.sort(function (a, b) { return a[0] !== b[0] ? a[0] - b[0] : a[1] - b[1]; });
        var uniq = [];
        for (var i = 0; i < raw.length; i++) {
            if (i === 0 || raw[i][0] !== raw[i - 1][0] || raw[i][1] !== raw[i - 1][1]) {
                uniq.push(raw[i]);
            }
        }

        // Sort by x, then y
        uniq.sort(function (a, b) {
            return a[0] - b[0] || a[1] - b[1];
        });

        var n = uniq.length;
        if (n <= 2) return uniq.slice();

        // Build lower hull
        var lower = [];
        for (var i = 0; i < n; i++) {
            while (lower.length >= 2 && cross2(lower[lower.length - 2], lower[lower.length - 1], uniq[i]) < 0) {
                lower.pop();
            }
            lower.push(uniq[i]);
        }

        // Build upper hull
        var upper = [];
        for (var i = n - 1; i >= 0; i--) {
            while (upper.length >= 2 && cross2(upper[upper.length - 2], upper[upper.length - 1], uniq[i]) < 0) {
                upper.pop();
            }
            upper.push(uniq[i]);
        }

        // Concatenate (last of each half = first of the other)
        lower.pop();
        upper.pop();
        return lower.concat(upper);
    };

    function cross2(o, a, b) {
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    }

    /* ------------------------------------------------------------------ */
    /*  Geometry helpers                                                   */
    /* ------------------------------------------------------------------ */

    api.rotate = function (poly, deg) {
        var rad = deg * Math.PI / 180;
        var c = Math.cos(rad);
        var s = Math.sin(rad);
        var out = [];
        for (var i = 0; i < poly.length; i++) {
            var px = poly[i][0];
            var py = poly[i][1];
            out.push([px * c - py * s, px * s + py * c]);
        }
        return out;
    };

    api.bbox = function (poly) {
        var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (var i = 0; i < poly.length; i++) {
            var x = poly[i][0];
            var y = poly[i][1];
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
        }
        return { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
    };

    api.place = function (hull, x, y, rotation) {
        var rotated = api.rotate(hull, rotation);
        var b = api.bbox(rotated);
        var cx = (b.minX + b.maxX) / 2;
        var cy = (b.minY + b.maxY) / 2;
        var out = [];
        for (var i = 0; i < rotated.length; i++) {
            out.push([rotated[i][0] - cx + x, rotated[i][1] - cy + y]);
        }
        return out;
    };

    api.insideBed = function (poly, width, depth) {
        for (var i = 0; i < poly.length; i++) {
            var px = poly[i][0];
            var py = poly[i][1];
            if (px < 0 || px > width || py < 0 || py > depth) return false;
        }
        return true;
    };

    /* ------------------------------------------------------------------ */
    /*  Overlap test: Separating Axis Theorem for convex polygons          */
    /* ------------------------------------------------------------------ */

    api.overlaps = function (a, b) {
        // Collect candidate axes (edges of both polygons)
        var axes = [];
        var polys = [a, b];
        for (var p = 0; p < 2; p++) {
            var poly = polys[p];
            var n = poly.length;
            for (var i = 0; i < n; i++) {
                var j = (i + 1) % n;
                var ex = poly[j][0] - poly[i][0];
                var ey = poly[j][1] - poly[i][1];
                // Normal
                var len = Math.sqrt(ex * ex + ey * ey);
                if (len > 0) {
                    axes.push([-ey / len, ex / len]);
                }
            }
        }

        // Test each axis
        for (var i = 0; i < axes.length; i++) {
            var ax = axes[i][0];
            var ay = axes[i][1];
            var minA = Infinity, maxA = -Infinity;
            var minB = Infinity, maxB = -Infinity;

            for (var k = 0; k < a.length; k++) {
                var proj = a[k][0] * ax + a[k][1] * ay;
                if (proj < minA) minA = proj;
                if (proj > maxA) maxA = proj;
            }
            for (var k = 0; k < b.length; k++) {
                var proj = b[k][0] * ax + b[k][1] * ay;
                if (proj < minB) minB = proj;
                if (proj > maxB) maxB = proj;
            }

            // If projections are strictly separated (touching = not overlapping)
            if (maxA <= minB || maxB <= minA) return false;
        }

        return true;
    };

    /* ------------------------------------------------------------------ */
    /*  Problems detection                                                 */
    /* ------------------------------------------------------------------ */

    api.problems = function (items, width, depth) {
        var outside = [];
        var overlapping = [];

        for (var i = 0; i < items.length; i++) {
            var poly = api.place(items[i].hull, items[i].x, items[i].y, items[i].rotation);
            if (!api.insideBed(poly, width, depth)) {
                outside.push(items[i].uid);
            }
        }

        for (var i = 0; i < items.length; i++) {
            for (var j = i + 1; j < items.length; j++) {
                var polyA = api.place(items[i].hull, items[i].x, items[i].y, items[i].rotation);
                var polyB = api.place(items[j].hull, items[j].x, items[j].y, items[j].rotation);
                if (api.overlaps(polyA, polyB)) {
                    overlapping.push([items[i].uid, items[j].uid]);
                }
            }
        }

        return { outside: outside, overlapping: overlapping };
    };

    /* ------------------------------------------------------------------ */
    /*  Bed widget (constructor)                                           */
    /* ------------------------------------------------------------------ */

    api.Bed = function (canvas, options) {
        if (!(this instanceof api.Bed)) return new api.Bed(canvas, options);

        var self = this;
        self.canvas = canvas;
        self.options = options || {};
        self.onChange = options.onChange || function () { };
        self.onSelect = options.onSelect || function () { };

        self.bedWidth = 0;
        self.bedDepth = 0;
        self.items = [];
        self.selectedUid = null;

        // Drawing parameters
        self.margin = 40; // CSS px margin for labels
        self.scale = 1;   // mm per CSS px
        self.offsetX = 0; // CSS px offset of bed origin in canvas coords
        self.offsetY = 0;

        // Interaction state
        self._dragging = false;
        self._rotating = false;
        self._rafId = null;
        self._listeners = [];

        // Bind methods
        self._onPointerDown = bindFn(self._onPointerDown, self);
        self._onPointerMove = bindFn(self._onPointerMove, self);
        self._onPointerUp = bindFn(self._onPointerUp, self);
        self._onPointerCancel = bindFn(self._onPointerCancel, self);
        self._onResize = bindFn(self._onResize, self);

        // Setup
        canvas.style.touchAction = "none";
        self._setupListeners();
        self._onResize();
    };

    var BedProto = api.Bed.prototype;

    BedProto._setupListeners = function () {
        var self = this;
        var canvas = self.canvas;

        var events = [
            { name: "pointerdown", fn: self._onPointerDown },
            { name: "pointermove", fn: self._onPointerMove },
            { name: "pointerup", fn: self._onPointerUp },
            { name: "pointercancel", fn: self._onPointerCancel }
        ];

        for (var i = 0; i < events.length; i++) {
            canvas.addEventListener(events[i].name, events[i].fn, false);
            self._listeners.push({ el: canvas, name: events[i].name, fn: events[i].fn });
        }

        window.addEventListener("resize", self._onResize, false);
        self._listeners.push({ el: window, name: "resize", fn: self._onResize });
    };

    BedProto.destroy = function () {
        var self = this;
        for (var i = 0; i < self._listeners.length; i++) {
            self._listeners[i].el.removeEventListener(self._listeners[i].name, self._listeners[i].fn, false);
        }
        self._listeners = [];
        if (self._rafId) {
            cancelAnimationFrame(self._rafId);
            self._rafId = null;
        }
    };

    BedProto.setBed = function (width, depth) {
        this.bedWidth = width;
        this.bedDepth = depth;
        this._recalc();
        this.redraw();
    };

    BedProto.setItems = function (items) {
        this.items = items;
        this.redraw();
    };

    BedProto.select = function (uid) {
        this.selectedUid = uid;
        this.onSelect(uid);
        this.redraw();
    };

    BedProto.redraw = function () {
        var self = this;
        if (self._rafId) {
            cancelAnimationFrame(self._rafId);
        }
        self._rafId = requestAnimationFrame(function () {
            self._draw();
            self._rafId = null;
        });
    };

    /* -- Sizing -- */

    BedProto._recalc = function () {
        var self = this;
        var parent = self.canvas.parentNode;
        var cssW = (parent && parent.clientWidth > 0) ? parent.clientWidth : 300;
        // Room for the Y labels on the left and the X labels underneath.
        self.padL = 34; self.padR = 8; self.padT = 8; self.padB = 20;
        var bedW = self.bedWidth || 350, bedD = self.bedDepth || 350;
        self.scale = Math.max(0.1, (cssW - self.padL - self.padR) / bedW);  // CSS px per mm
        var cssH = self.padT + bedD * self.scale + self.padB;

        var dpr = window.devicePixelRatio || 1;
        self.canvas.style.width = cssW + "px";
        self.canvas.style.height = cssH + "px";
        self.canvas.width = Math.round(cssW * dpr);
        self.canvas.height = Math.round(cssH * dpr);
        self.offsetX = self.padL;
        self.offsetY = self.padT;
    };

    BedProto._onResize = function () {
        this._recalc();
        this.redraw();
    };

    /* -- Coordinate conversion: bed mm (origin front-left, +Y to the back) <-> CSS px (back at the top) -- */

    BedProto.toBed = function (px, py) {
        return {
            x: (px - this.offsetX) / this.scale,
            y: (this.bedDepth || 350) - (py - this.offsetY) / this.scale
        };
    };

    BedProto.toScreen = function (x, y) {
        return {
            x: this.offsetX + x * this.scale,
            y: this.offsetY + ((this.bedDepth || 350) - y) * this.scale
        };
    };

    /* -- Drawing -- */

    BedProto._draw = function () {
        var self = this;
        var canvas = self.canvas;
        var ctx = canvas.getContext("2d");
        var dpr = window.devicePixelRatio || 1;

        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        // Clear
        ctx.clearRect(0, 0, canvas.width / dpr, canvas.height / dpr);

        // Colours
        var colors = self._getColors();

        // Whole canvas in the bed colour (the axis labels sit in the margin), bed outlined
        var bedTL = self.toScreen(0, self.bedDepth);
        var bedBR = self.toScreen(self.bedWidth, 0);
        ctx.fillStyle = colors.bed;
        ctx.fillRect(0, 0, canvas.width / dpr, canvas.height / dpr);
        ctx.strokeStyle = colors.text;
        ctx.globalAlpha = 0.5;
        ctx.lineWidth = 1;
        ctx.strokeRect(bedTL.x, bedTL.y, bedBR.x - bedTL.x, bedBR.y - bedTL.y);
        ctx.globalAlpha = 1;

        // Grid lines every 50mm
        ctx.strokeStyle = colors.grid;
        ctx.lineWidth = 0.5;

        // Vertical lines
        for (var gx = 0; gx <= self.bedWidth; gx += 50) {
            var s1 = self.toScreen(gx, 0);
            var s2 = self.toScreen(gx, self.bedDepth);
            ctx.beginPath();
            ctx.moveTo(s1.x, s1.y);
            ctx.lineTo(s2.x, s2.y);
            ctx.stroke();
        }

        // Horizontal lines
        for (var gy = 0; gy <= self.bedDepth; gy += 50) {
            var s1h = self.toScreen(0, gy);
            var s2h = self.toScreen(self.bedWidth, gy);
            ctx.beginPath();
            ctx.moveTo(s1h.x, s1h.y);
            ctx.lineTo(s2h.x, s2h.y);
            ctx.stroke();
        }

        // Labels every 100mm on left and bottom edges
        ctx.fillStyle = colors.text;
        ctx.font = "11px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";

        // Bottom edge (X axis)
        for (var lx = 100; lx < self.bedWidth; lx += 100) {
            var sl = self.toScreen(lx, 0);
            ctx.fillText("" + lx, sl.x, sl.y + 3);
        }

        // Left edge (Y axis)
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        for (var ly = 100; ly < self.bedDepth; ly += 100) {
            var sl2 = self.toScreen(0, ly);
            ctx.fillText("" + ly, self.offsetX - 4, sl2.y);
        }

        // Compute placed polygons and problems
        var placed = [];
        for (var i = 0; i < self.items.length; i++) {
            var item = self.items[i];
            var poly = api.place(item.hull, item.x, item.y, item.rotation);
            placed.push({ item: item, poly: poly });
        }

        var prob = api.problems(self.items, self.bedWidth, self.bedDepth);
        var outsideSet = {};
        for (var i = 0; i < prob.outside.length; i++) {
            outsideSet[prob.outside[i]] = true;
        }
        var overlapSet = {};
        for (var i = 0; i < prob.overlapping.length; i++) {
            var pair = prob.overlapping[i];
            overlapSet[pair[0]] = true;
            overlapSet[pair[1]] = true;
        }

        // Draw models
        for (var i = 0; i < placed.length; i++) {
            var p = placed[i];
            var poly = p.poly;
            var item = p.item;

            // Top-down render of the model when there is one, else a flat fill of its footprint
            ctx.beginPath();
            var sp0 = self.toScreen(poly[0][0], poly[0][1]);
            ctx.moveTo(sp0.x, sp0.y);
            for (var j = 1; j < poly.length; j++) {
                var spj = self.toScreen(poly[j][0], poly[j][1]);
                ctx.lineTo(spj.x, spj.y);
            }
            ctx.closePath();
            if (item.sprite && item.render) {
                var mtx = api.spriteMatrix(item.render, item.hull, item.x, item.y, item.rotation,
                    {scale: self.scale, offsetX: self.offsetX, offsetY: self.offsetY, depth: self.bedDepth});
                ctx.save();
                ctx.setTransform(dpr * mtx[0], dpr * mtx[1], dpr * mtx[2], dpr * mtx[3], dpr * mtx[4], dpr * mtx[5]);
                ctx.imageSmoothingEnabled = true;
                ctx.drawImage(item.sprite, 0, 0);
                ctx.restore();
            } else {
                ctx.fillStyle = item.color || "#888888";
                ctx.globalAlpha = 0.6;
                ctx.fill();
                ctx.globalAlpha = 1;
            }

            // Stroke
            var isSelected = item.uid === self.selectedUid;
            if (isSelected) {
                ctx.strokeStyle = colors.select;
                ctx.lineWidth = 2;
            } else if (outsideSet[item.uid]) {
                ctx.strokeStyle = colors.bad;
                ctx.lineWidth = 2;
            } else if (overlapSet[item.uid]) {
                ctx.strokeStyle = colors.warn;
                ctx.lineWidth = 2;
            } else {
                ctx.strokeStyle = item.color || "#888888";
                ctx.lineWidth = 1;
            }
            ctx.stroke();

            // Model name at centre (truncate to fit hull bbox width)
            var b = api.bbox(poly);
            var hullWmm = b.maxX - b.minX;
            var hullWCss = hullWmm * self.scale;
            var name = item.name || "";
            var maxChars = Math.max(6, Math.floor((hullWCss + 40) / 6)); // label may overhang the model a little
            if (name.length > maxChars) {
                name = name.substring(0, maxChars - 1) + "…";
            }
            if (name) {
                // under the model (its front edge), so the top-down render stays visible
                var ctr = self.toScreen((b.minX + b.maxX) / 2, b.minY);
                ctx.fillStyle = colors.text;
                ctx.font = "11px sans-serif";
                ctx.textAlign = "center";
                ctx.textBaseline = "top";
                ctx.globalAlpha = 0.9;
                ctx.fillText(name, ctr.x, ctr.y + 3);
                ctx.globalAlpha = 1;
            }

            // Rotate handle for selected model
            if (isSelected) {
                var topYmm = b.maxY;  // the back edge is drawn at the top
                var handlePos = self.toScreen((b.minX + b.maxX) / 2, topYmm);
                var handleOffset = 18; // CSS px above top of model
                var handleY = handlePos.y - handleOffset;
                var handleX = handlePos.x;
                var handleR = 6; // 12 CSS px diameter = 6 radius

                // Line from centre to handle
                var centre = self.toScreen((b.minX + b.maxX) / 2, (b.minY + b.maxY) / 2);
                ctx.strokeStyle = colors.select;
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.moveTo(centre.x, centre.y);
                ctx.lineTo(handleX, handleY);
                ctx.stroke();

                // Handle circle
                ctx.fillStyle = colors.select;
                ctx.beginPath();
                ctx.arc(handleX, handleY, handleR, 0, Math.PI * 2);
                ctx.fill();

                // Store handle info for hit testing
                self._handleInfo = { x: handleX, y: handleY, r: handleR };
                self._centreInfo = { x: centre.x, y: centre.y };
            }
        }
    };

    BedProto._getColors = function () {
        var self = this;
        var canvas = self.canvas;
        var computed;
        try {
            computed = window.getComputedStyle(canvas);
        } catch (e) {
            // Node environment
            return {
                bed: "#2b2f33",
                grid: "#3d4349",
                text: "#c8ccd0",
                bad: "#e5534b",
                warn: "#d4a72c",
                select: "#58a6ff"
            };
        }
        return {
            bed: computed.getPropertyValue("--rs-bed").trim() || "#2b2f33",
            grid: computed.getPropertyValue("--rs-grid").trim() || "#3d4349",
            text: computed.getPropertyValue("--rs-bed-text").trim() || "#c8ccd0",
            bad: computed.getPropertyValue("--rs-bad").trim() || "#e5534b",
            warn: computed.getPropertyValue("--rs-warn").trim() || "#d4a72c",
            select: computed.getPropertyValue("--rs-select").trim() || "#58a6ff"
        };
    };

    /* -- Interaction -- */

    BedProto._onPointerDown = function (e) {
        var self = this;
        var rect = self.canvas.getBoundingClientRect();
        var px = e.clientX - rect.left;
        var py = e.clientY - rect.top;

        // Check rotate handle first
        if (self.selectedUid !== null && self._handleInfo) {
            var dx = px - self._handleInfo.x;
            var dy = py - self._handleInfo.y;
            if (dx * dx + dy * dy <= self._handleInfo.r * self._handleInfo.r) {
                self._rotating = true;
                var selItem = null;
                for (var i = 0; i < self.items.length; i++) {
                    if (self.items[i].uid === self.selectedUid) {
                        selItem = self.items[i];
                        break;
                    }
                }
                if (selItem) {
                    var poly = api.place(selItem.hull, selItem.x, selItem.y, selItem.rotation);
                    var b = api.bbox(poly);
                    var ctr = self.toScreen((b.minX + b.maxX) / 2, (b.minY + b.maxY) / 2);
                    self._startRotation = selItem.rotation;
                    self._startAngle = Math.atan2(py - ctr.y, px - ctr.x);
                    self._selItem = selItem;
                    self.canvas.setPointerCapture(e.pointerId);
                    return;
                }
            }
        }

        // Hit test items (topmost first = last in items array)
        var hit = null;
        for (var i = self.items.length - 1; i >= 0; i--) {
            var item = self.items[i];
            var poly = api.place(item.hull, item.x, item.y, item.rotation);
            var bedPt = self.toBed(px, py);
            if (pointInConvex(poly, bedPt.x, bedPt.y)) {
                hit = item;
                break;
            }
        }

        if (hit) {
            self.selectedUid = hit.uid;
            self.onSelect(hit.uid);
            self._dragging = true;
            self._selItem = hit;
            var bedPt2 = self.toBed(px, py);
            self._dragStartX = hit.x - bedPt2.x;
            self._dragStartY = hit.y - bedPt2.y;
            self.canvas.setPointerCapture(e.pointerId);
            self.redraw();
        } else {
            if (self.selectedUid !== null) {
                self.selectedUid = null;
                self.onSelect(null);
                self.redraw();
            }
        }
    };

    BedProto._onPointerMove = function (e) {
        var self = this;
        if (!self._dragging && !self._rotating) return;

        var rect = self.canvas.getBoundingClientRect();
        var px = e.clientX - rect.left;
        var py = e.clientY - rect.top;

        if (self._rotating && self._selItem) {
            var poly = api.place(self._selItem.hull, self._selItem.x, self._selItem.y, self._selItem.rotation);
            var b = api.bbox(poly);
            var ctr = self.toScreen((b.minX + b.maxX) / 2, (b.minY + b.maxY) / 2);
            var angle = Math.atan2(py - ctr.y, px - ctr.x);
            // screen angles grow clockwise (y points down); bed rotation is counter-clockwise
            var deltaDeg = -(angle - self._startAngle) * 180 / Math.PI;
            var newRot = self._startRotation + deltaDeg;

            // Snap to 5 degrees unless Shift
            if (!e.shiftKey) {
                newRot = Math.round(newRot / 5) * 5;
            }

            // Normalize to [0, 360)
            newRot = ((newRot % 360) + 360) % 360;
            self._selItem.rotation = newRot;
            self.redraw();
        }

        if (self._dragging && self._selItem) {
            var bedPt = self.toBed(px, py);
            var newX = bedPt.x + self._dragStartX;
            var newY = bedPt.y + self._dragStartY;

            // Clamp to bed rectangle
            newX = Math.max(0, Math.min(newX, self.bedWidth));
            newY = Math.max(0, Math.min(newY, self.bedDepth));

            self._selItem.x = newX;
            self._selItem.y = newY;
            self.redraw();
        }
    };

    BedProto._onPointerUp = function (e) {
        var self = this;
        if (self._dragging && self._selItem) {
            self.onChange(self._selItem);
        }
        if (self._rotating && self._selItem) {
            self.onChange(self._selItem);
        }
        self._dragging = false;
        self._rotating = false;
        self._selItem = null;
    };

    BedProto._onPointerCancel = function (e) {
        var self = this;
        self._dragging = false;
        self._rotating = false;
        self._selItem = null;
    };

    /* ------------------------------------------------------------------ */
    /*  Utility functions                                                  */
    /* ------------------------------------------------------------------ */

    // Point-in-convex-polygon test (all cross products same sign)
    function pointInConvex(poly, px, py) {
        var n = poly.length;
        if (n < 3) return false;
        var sign = 0;
        for (var i = 0; i < n; i++) {
            var j = (i + 1) % n;
            var cross = (poly[j][0] - poly[i][0]) * (py - poly[i][1]) - (poly[j][1] - poly[i][1]) * (px - poly[i][0]);
            if (cross !== 0) {
                if (sign === 0) {
                    sign = cross > 0 ? 1 : -1;
                } else if ((cross > 0 ? 1 : -1) !== sign) {
                    return false;
                }
            }
        }
        return true;
    }

    // Bind a function to a context (ES5 compatible)
    function bindFn(fn, ctx) {
        return function () {
            return fn.apply(ctx, arguments);
        };
    }

    /* ------------------------------------------------------------------ */
    /*  Export                                                             */
    /* ------------------------------------------------------------------ */

    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
    if (typeof window !== "undefined") {
        window.RemoteSlicingBed = api;
    }
})();
