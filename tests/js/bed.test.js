/*
 * Remote Slicing - Bed module unit tests.
 * Run with: node --test tests/js/
 */
var test = require("node:test");
var assert = require("node:assert");
var api = require("../../octoprint_remoteslicing/static/js/remoteslicing_bed.js");

/* ------------------------------------------------------------------ */
/*  Helper: build a binary STL of a 10x20x5 box                         */
/* ------------------------------------------------------------------ */

function buildBinaryStlBox() {
    // 12 triangles for a box centered at origin, 10x20x5
    // vertices: (-5,-10,-2.5), (5,-10,-2.5), (5,10,-2.5), (-5,10,-2.5) for bottom
    //           (-5,-10,2.5), (5,-10,2.5), (5,10,2.5), (-5,10,2.5) for top
    var triangles = [
        // front face (y = -10)
        { n: [0, -1, 0], v: [[-5, -10, -2.5], [5, -10, -2.5], [5, -10, 2.5]] },
        { n: [0, -1, 0], v: [[-5, -10, -2.5], [5, -10, 2.5], [-5, -10, 2.5]] },
        // back face (y = 10)
        { n: [0, 1, 0], v: [[-5, 10, -2.5], [5, 10, 2.5], [5, 10, -2.5]] },
        { n: [0, 1, 0], v: [[-5, 10, -2.5], [-5, 10, 2.5], [5, 10, 2.5]] },
        // left face (x = -5)
        { n: [-1, 0, 0], v: [[-5, -10, -2.5], [-5, 10, -2.5], [-5, 10, 2.5]] },
        { n: [-1, 0, 0], v: [[-5, -10, -2.5], [-5, 10, 2.5], [-5, -10, 2.5]] },
        // right face (x = 5)
        { n: [1, 0, 0], v: [[5, -10, -2.5], [5, 10, 2.5], [5, 10, -2.5]] },
        { n: [1, 0, 0], v: [[5, -10, -2.5], [5, -10, 2.5], [5, 10, 2.5]] },
        // bottom face (z = -2.5)
        { n: [0, 0, -1], v: [[-5, -10, -2.5], [5, 10, -2.5], [5, -10, -2.5]] },
        { n: [0, 0, -1], v: [[-5, -10, -2.5], [-5, 10, -2.5], [5, 10, -2.5]] },
        // top face (z = 2.5)
        { n: [0, 0, 1], v: [[-5, -10, 2.5], [5, -10, 2.5], [5, 10, 2.5]] },
        { n: [0, 0, 1], v: [[-5, -10, 2.5], [5, 10, 2.5], [-5, 10, 2.5]] }
    ];

    var bufSize = 84 + triangles.length * 50;
    var buf = new ArrayBuffer(bufSize);
    var view = new DataView(buf);
    var u32 = new Uint32Array(buf);

    // Header: 80 bytes of zeros
    // Count at offset 80
    u32[20] = triangles.length; // 80 / 4 = 20

    for (var i = 0; i < triangles.length; i++) {
        var off = 84 + i * 50;
        var t = triangles[i];

        // Normal (3 floats)
        view.setFloat32(off, t.n[0], true);
        view.setFloat32(off + 4, t.n[1], true);
        view.setFloat32(off + 8, t.n[2], true);

        // 3 vertices (9 floats)
        for (var v = 0; v < 3; v++) {
            view.setFloat32(off + 12 + v * 12, t.v[v][0], true);
            view.setFloat32(off + 12 + v * 12 + 4, t.v[v][1], true);
            view.setFloat32(off + 12 + v * 12 + 8, t.v[v][2], true);
        }

        // Attribute byte count (uint16)
        view.setUint16(off + 48, 0, true);
    }

    return buf;
}

/* ------------------------------------------------------------------ */
/*  Helper: build an ASCII STL of the same 10x20x5 box                 */
/* ------------------------------------------------------------------ */

function buildAsciiStlBox() {
    var lines = ["solid box"];
    var triangles = [
        { n: [0, -1, 0], v: [[-5, -10, -2.5], [5, -10, -2.5], [5, -10, 2.5]] },
        { n: [0, -1, 0], v: [[-5, -10, -2.5], [5, -10, 2.5], [-5, -10, 2.5]] },
        { n: [0, 1, 0], v: [[-5, 10, -2.5], [5, 10, 2.5], [5, 10, -2.5]] },
        { n: [0, 1, 0], v: [[-5, 10, -2.5], [-5, 10, 2.5], [5, 10, 2.5]] },
        { n: [-1, 0, 0], v: [[-5, -10, -2.5], [-5, 10, -2.5], [-5, 10, 2.5]] },
        { n: [-1, 0, 0], v: [[-5, -10, -2.5], [-5, 10, 2.5], [-5, -10, 2.5]] },
        { n: [1, 0, 0], v: [[5, -10, -2.5], [5, 10, 2.5], [5, 10, -2.5]] },
        { n: [1, 0, 0], v: [[5, -10, -2.5], [5, -10, 2.5], [5, 10, 2.5]] },
        { n: [0, 0, -1], v: [[-5, -10, -2.5], [5, 10, -2.5], [5, -10, -2.5]] },
        { n: [0, 0, -1], v: [[-5, -10, -2.5], [-5, 10, -2.5], [5, 10, -2.5]] },
        { n: [0, 0, 1], v: [[-5, -10, 2.5], [5, -10, 2.5], [5, 10, 2.5]] },
        { n: [0, 0, 1], v: [[-5, -10, 2.5], [5, 10, 2.5], [-5, 10, 2.5]] }
    ];

    for (var i = 0; i < triangles.length; i++) {
        var t = triangles[i];
        lines.push("  facet normal " + t.n[0] + " " + t.n[1] + " " + t.n[2]);
        lines.push("    outer loop");
        for (var v = 0; v < 3; v++) {
            lines.push("      vertex " + t.v[v][0] + " " + t.v[v][1] + " " + t.v[v][2]);
        }
        lines.push("    endloop");
        lines.push("  endfacet");
    }
    lines.push("endsolid box");

    var text = lines.join("\n");
    var encoder = new TextEncoder();
    return encoder.encode(text).buffer;
}

/* ------------------------------------------------------------------ */
/*  Tests                                                                */
/* ------------------------------------------------------------------ */

test("parseStl: binary STL of 10x20x5 box", function () {
    var buf = buildBinaryStlBox();
    var result = api.parseStl(buf);
    assert.strictEqual(result.count, 36); // 12 triangles * 3 vertices
    assert.strictEqual(result.minZ, -2.5);
    assert.strictEqual(result.maxZ, 2.5);
    assert.ok(result.points instanceof Float64Array);
    assert.strictEqual(result.points.length, 72); // 36 * 2 (x,y)
});

test("parseStl: ASCII STL of 10x20x5 box", function () {
    var buf = buildAsciiStlBox();
    var result = api.parseStl(buf);
    assert.strictEqual(result.count, 36);
    assert.strictEqual(result.minZ, -2.5);
    assert.strictEqual(result.maxZ, 2.5);
    assert.ok(result.points instanceof Float64Array);
});

test("parseStl: binary and ASCII produce same points", function () {
    var binResult = api.parseStl(buildBinaryStlBox());
    var ascResult = api.parseStl(buildAsciiStlBox());
    assert.strictEqual(binResult.count, ascResult.count);
    for (var i = 0; i < binResult.points.length; i++) {
        assert.strictEqual(binResult.points[i], ascResult.points[i]);
    }
});

test("parseStl: garbage throws", function () {
    var garbage = new ArrayBuffer(16);
    assert.throws(function () { api.parseStl(garbage); }, Error);
});

test("convexHull: box hull is 4 corners CCW", function () {
    var result = api.parseStl(buildBinaryStlBox());
    var hull = api.convexHull(result.points, result.count);
    assert.strictEqual(hull.length, 4);
    // The 4 corners of a 10x20 box centred at origin: (-5,-10), (-5,10), (5,10), (5,-10)
    // CCW order starting from bottom-left
    assert.strictEqual(hull[0][0], -5);
    assert.strictEqual(hull[0][1], -10);
    assert.strictEqual(hull[1][0], 5);
    assert.strictEqual(hull[1][1], -10);
    assert.strictEqual(hull[2][0], 5);
    assert.strictEqual(hull[2][1], 10);
    assert.strictEqual(hull[3][0], -5);
    assert.strictEqual(hull[3][1], 10);
});

test("convexHull: circle points hull has 300-360 points, no interior point inside", function () {
    var R = 100;
    var points = [];
    var count = 0;

    // 360 boundary points
    for (var deg = 0; deg < 360; deg++) {
        var rad = deg * Math.PI / 180;
        points.push(R * Math.cos(rad), R * Math.sin(rad));
        count++;
    }

    // Random interior points
    var rng = (function (seed) {
        var s = seed;
        return function () {
            s = (s * 16807) % 2147483647;
            return s / 2147483647;
        };
    })(42);

    for (var i = 0; i < 1000; i++) {
        var r = rng() * R * 0.9;
        var a = rng() * 2 * Math.PI;
        points.push(r * Math.cos(a), r * Math.sin(a));
        count++;
    }

    var hull = api.convexHull(points, count);
    assert.ok(hull.length >= 300, "hull has " + hull.length + " points, expected >= 300");
    assert.ok(hull.length <= 360, "hull has " + hull.length + " points, expected <= 360");

    // No interior point should be outside the hull (the hull should contain all boundary points)
    // Check that an interior point (0,0) is inside the hull
    // We can check that the hull encloses the origin by checking all cross products have same sign
    var sign = 0;
    for (var i = 0; i < hull.length; i++) {
        var j = (i + 1) % hull.length;
        var cross = (hull[j][0] - hull[i][0]) * (0 - hull[i][1]) - (hull[j][1] - hull[i][1]) * (0 - hull[i][0]);
        if (cross !== 0) {
            if (sign === 0) sign = cross > 0 ? 1 : -1;
            assert.strictEqual(cross > 0 ? 1 : -1, sign, "origin should be inside hull");
        }
    }
});

test("convexHull: 2,000,000 points in under 2000 ms", function () {
    var points = [];
    var count = 0;
    var rng = (function (seed) {
        var s = seed;
        return function () {
            s = (s * 16807) % 2147483647;
            return s / 2147483647;
        };
    })(123);

    for (var i = 0; i < 2000000; i++) {
        points.push(rng() * 1000 - 500, rng() * 1000 - 500);
        count++;
    }

    var start = Date.now();
    var hull = api.convexHull(points, count);
    var elapsed = Date.now() - start;
    assert.ok(hull.length > 0, "hull should have points");
    assert.ok(elapsed < 2000, "took " + elapsed + " ms, expected < 2000");
});

test("place(): box hull at (50,60) rotation 0 -> bbox 45..55 x 50..70", function () {
    var result = api.parseStl(buildBinaryStlBox());
    var hull = api.convexHull(result.points, result.count);
    var placed = api.place(hull, 50, 60, 0);
    var b = api.bbox(placed);
    assert.strictEqual(b.minX, 45);
    assert.strictEqual(b.maxX, 55);
    assert.strictEqual(b.minY, 50);
    assert.strictEqual(b.maxY, 70);
});

test("place(): box hull at (50,60) rotation 90 -> bbox 40..60 x 55..65", function () {
    var result = api.parseStl(buildBinaryStlBox());
    var hull = api.convexHull(result.points, result.count);
    var placed = api.place(hull, 50, 60, 90);
    var b = api.bbox(placed);
    assert.strictEqual(b.minX, 40);
    assert.strictEqual(b.maxX, 60);
    assert.strictEqual(b.minY, 55);
    assert.strictEqual(b.maxY, 65);
});

test("insideBed: true when fully inside", function () {
    var poly = [[1, 1], [9, 1], [9, 9], [1, 9]];
    assert.strictEqual(api.insideBed(poly, 10, 10), true);
});

test("insideBed: true when on the edge", function () {
    var poly = [[0, 0], [10, 0], [10, 10], [0, 10]];
    assert.strictEqual(api.insideBed(poly, 10, 10), true);
});

test("insideBed: false when outside", function () {
    var poly = [[-1, 0], [1, 0], [1, 2], [-1, 2]];
    assert.strictEqual(api.insideBed(poly, 10, 10), false);
});

test("overlaps: overlapping squares", function () {
    var a = [[0, 0], [3, 0], [3, 3], [0, 3]];
    var b = [[2, 2], [5, 2], [5, 5], [2, 5]];
    assert.strictEqual(api.overlaps(a, b), true);
});

test("overlaps: separated squares", function () {
    var a = [[0, 0], [2, 0], [2, 2], [0, 2]];
    var b = [[3, 3], [5, 3], [5, 5], [3, 5]];
    assert.strictEqual(api.overlaps(a, b), false);
});

test("overlaps: touching along edge", function () {
    var a = [[0, 0], [2, 0], [2, 2], [0, 2]];
    var b = [[2, 0], [4, 0], [4, 2], [2, 2]];
    assert.strictEqual(api.overlaps(a, b), false);
});

test("problems(): reports outside item and overlapping pair", function () {
    var boxResult = api.parseStl(buildBinaryStlBox());
    var boxHull = api.convexHull(boxResult.points, boxResult.count);

    var items = [
        { uid: "a", hull: boxHull, x: 50, y: 50, rotation: 0 },
        { uid: "b", hull: boxHull, x: 52, y: 52, rotation: 0 }, // overlaps with a
        { uid: "c", hull: boxHull, x: 150, y: 150, rotation: 0 } // outside a 100x100 bed
    ];

    var prob = api.problems(items, 100, 100);
    assert.ok(prob.outside.indexOf("c") !== -1, "c should be outside");
    assert.ok(prob.outside.indexOf("a") === -1, "a should not be outside");

    var foundOverlap = false;
    for (var i = 0; i < prob.overlapping.length; i++) {
        var pair = prob.overlapping[i];
        if ((pair[0] === "a" && pair[1] === "b") || (pair[0] === "b" && pair[1] === "a")) {
            foundOverlap = true;
        }
    }
    assert.ok(foundOverlap, "a and b should overlap");
});

// Tilt must match the PC agent's mesh.place (agent/tests/test_mesh.py uses the same box and numbers).
test("tiltPoints lays the 10x20x5 box on its other faces like the agent does", function () {
    var parsed = api.parseStl(buildBinaryStlBox());
    assert.ok(parsed.xyz && parsed.xyz.length === parsed.count * 3);
    var cases = [[0, 0, 10, 20], [90, 0, 10, 5], [0, 90, 5, 20], [90, 90, 20, 5], [180, 0, 10, 20], [270, 270, 20, 5]];
    cases.forEach(function (c) {
        var pts = api.tiltPoints(parsed.xyz, parsed.count, c[0], c[1]);
        var b = api.bbox(api.place(api.convexHull(pts, parsed.count), 50, 60, 0));
        assert.ok(Math.abs((b.maxX - b.minX) - c[2]) < 0.06, "width for tilt " + c[0] + "/" + c[1]);
        assert.ok(Math.abs((b.maxY - b.minY) - c[3]) < 0.06, "depth for tilt " + c[0] + "/" + c[1]);
        assert.ok(Math.abs((b.minX + b.maxX) / 2 - 50) < 1e-6 && Math.abs((b.minY + b.maxY) / 2 - 60) < 1e-6);
    });
});

test("tiltPoints is right-handed: +90 about Y turns +Z into +X", function () {
    var xyz = new Float32Array([0, 0, 0, 0, 0, 10]);
    var pts = api.tiltPoints(xyz, 2, 0, 90);
    assert.ok(Math.abs(pts[2] - 10) < 1e-6 && Math.abs(pts[3]) < 1e-6);
    pts = api.tiltPoints(new Float32Array([0, 0, 0, 0, 0, 10]), 2, 90, 0);  // +Z goes to -Y about X
    assert.ok(Math.abs(pts[3] + 10) < 1e-6);
});

// ---- top-down render ---------------------------------------------------------------------------
function boxTris(x0, y0, z0, x1, y1, z1) {
    // 12 outward-facing triangles of an axis-aligned box, as a flat xyz array
    var c = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]];
    var f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]];
    var out = [];
    f.forEach(function (t) { t.forEach(function (i) { out.push(c[i][0], c[i][1], c[i][2]); }); });
    return out;
}

test("renderTop: a flat box fills its whole raster at the top height", function () {
    var xyz = new Float32Array(boxTris(0, 0, 0, 10, 20, 5));
    var r = api.renderTop(xyz, xyz.length / 3, 0, 0, 512);
    assert.strictEqual(r.ppm, 8);
    assert.strictEqual(r.width, 80);
    assert.strictEqual(r.height, 160);
    var filled = 0;
    for (var i = 0; i < r.shade.length; i++) { if (r.shade[i]) { filled++; assert.ok(Math.abs(r.depth[i] - 5) < 1e-4); } }
    assert.strictEqual(filled, r.shade.length);
});

test("renderTop: the z-buffer keeps the higher surface and tilt changes the view", function () {
    var tris = boxTris(0, 0, 0, 20, 20, 2).concat(boxTris(8, 8, 2, 12, 12, 10));  // post on a plate
    var xyz = new Float32Array(tris);
    var r = api.renderTop(xyz, xyz.length / 3, 0, 0, 200);
    var at = function (mmX, mmY) { var u = Math.floor((mmX - r.minX) * r.ppm), v = Math.floor((r.maxY - mmY) * r.ppm); return u + v * r.width; };
    assert.ok(Math.abs(r.depth[at(10, 10)] - 10) < 1e-4, "post top seen in the middle");
    assert.ok(Math.abs(r.depth[at(2, 2)] - 2) < 1e-4, "plate top seen at the corner");
    assert.ok(r.shade[at(10, 10)] > r.shade[at(2, 2)], "higher surface drawn brighter");
    var back = at(10, 19), front = at(10, 1);  // row 0 is the back edge: +Y is up the image
    assert.ok(back < front);
    var tilted = api.renderTop(new Float32Array(boxTris(0, 0, 0, 10, 20, 5)), 36, 90, 0, 512);
    assert.strictEqual(tilted.width, 80);   // 10 mm
    assert.strictEqual(tilted.height, 40);  // 5 mm: stood on its 10 x 5 face
});

test("spriteMatrix puts the render's corners on the placed footprint's corners", function () {
    var xyz = new Float32Array(boxTris(-3, -7, 0, 7, 13, 5));
    var count = xyz.length / 3;
    var r = api.renderTop(xyz, count, 0, 0, 512);
    var hull = api.convexHull(api.tiltPoints(xyz, count, 0, 0), count);
    var view = {scale: 2, offsetX: 30, offsetY: 10, depth: 350};
    var m = api.spriteMatrix(r, hull, 100, 120, 30, view);
    var placed = api.place(hull, 100, 120, 30);
    var screen = placed.map(function (p) { return [view.offsetX + p[0] * view.scale, view.offsetY + (view.depth - p[1]) * view.scale]; });
    [[0, 0], [r.width, 0], [0, r.height], [r.width, r.height]].forEach(function (uv) {
        var sx = m[0] * uv[0] + m[2] * uv[1] + m[4], sy = m[1] * uv[0] + m[3] * uv[1] + m[5];
        var best = Math.min.apply(null, screen.map(function (q) { return Math.hypot(q[0] - sx, q[1] - sy); }));
        assert.ok(best < 0.2, "corner " + uv + " is " + best.toFixed(3) + " px from the footprint");
    });
});

test("renderTop handles a 200k-triangle mesh quickly", function () {
    var tris = [];
    for (var i = 0; i < 200000 / 12; i++) { var x = (i % 130) * 2, y = Math.floor(i / 130) * 2; tris.push.apply(tris, boxTris(x, y, 0, x + 1.5, y + 1.5, 1 + (i % 7))); }
    var xyz = new Float32Array(tris), t0 = Date.now();
    api.renderTop(xyz, xyz.length / 3, 0, 0, 512);
    assert.ok(Date.now() - t0 < 2000, "took " + (Date.now() - t0) + " ms");
});

// ---- preview camera --------------------------------------------------------------------------------
test("viewMatrix: elevation 90 is the top view, 0 is the front view, azimuth turns around", function () {
    var xyz = new Float32Array(boxTris(0, 0, 0, 10, 20, 5)), n = xyz.length / 3;
    var top = api.renderView(xyz, n, api.viewMatrix(0, 90), {maxPx: 160, maxPpm: 4});
    assert.deepStrictEqual([top.width, top.height], [40, 80]);           // 10 x 20 mm at 4 px/mm
    var front = api.renderView(xyz, n, api.viewMatrix(0, 0), {maxPx: 160, maxPpm: 4});
    assert.deepStrictEqual([front.width, front.height], [40, 20]);       // 10 wide, 5 tall
    for (var i = 0; i < front.depth.length; i++) { assert.ok(Math.abs(front.depth[i]) < 1e-4); }  // front face y=0 is nearest
    var side = api.renderView(xyz, n, api.viewMatrix(90, 0), {maxPx: 160, maxPpm: 4});
    assert.deepStrictEqual([side.width, side.height], [80, 20]);         // 20 deep, 5 tall
});

test("renderView draws meshes with inverted winding too (two-sided) and fills an iso view", function () {
    var tris = boxTris(0, 0, 0, 20, 20, 2).concat(boxTris(8, 8, 2, 12, 12, 10));
    var flipped = [];
    for (var i = 0; i < tris.length; i += 9) { flipped.push(tris[i], tris[i + 1], tris[i + 2], tris[i + 6], tris[i + 7], tris[i + 8], tris[i + 3], tris[i + 4], tris[i + 5]); }
    [tris, flipped].forEach(function (t) {
        var xyz = new Float32Array(t);
        var r = api.renderView(xyz, xyz.length / 3, api.viewMatrix(30, 30), {maxPx: 200});
        var filled = 0;
        for (var k = 0; k < r.shade.length; k++) { if (r.shade[k]) filled++; }
        assert.ok(filled / r.shade.length > 0.45, "iso view filled " + (filled / r.shade.length).toFixed(2));
    });
});

test("modelSize follows the tilt", function () {
    var xyz = new Float32Array(boxTris(0, 0, 0, 10, 20, 5)), n = xyz.length / 3;
    assert.deepStrictEqual(api.modelSize(xyz, n, 0, 0).map(Math.round), [10, 20, 5]);
    assert.deepStrictEqual(api.modelSize(xyz, n, 90, 0).map(Math.round), [10, 5, 20]);
    assert.deepStrictEqual(api.modelSize(xyz, n, 0, 90).map(Math.round), [5, 20, 10]);
});
