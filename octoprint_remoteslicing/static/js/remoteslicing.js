/*
 * View model for Remote Slicing: model library, PC Dropbox browser, 2D bed plate and jobs.
 *
 * State comes from GET state and the pushed {"type": "state"} messages; the plate itself lives only in the
 * browser (and localStorage) until it is submitted. Messages of type "agent.*" are for the PC agent.
 */
$(function () {
    var PLATE_KEY = "remoteslicing.plate";
    var PREVIEW_KEY = "remoteslicing.previewOpen";   // old true/false setting, read once for the default
    var PREVIEW_MODE_KEY = "remoteslicing.previewMode";
    var REMEMBER_KEY = "remoteslicing.rememberFolder";
    var FOLDER_KEY = "remoteslicing.dropboxFolder";
    var ORIENT_KEY = "remoteslicing.modelOrientation";  // library model id -> {tiltX, tiltY, rotation}

    // Per-browser conveniences; private mode or blocked storage just means nothing is remembered.
    function loadPref(key, fallback) {
        try {
            var raw = localStorage.getItem(key);
            return raw === null ? fallback : JSON.parse(raw);
        } catch (e) {
            return fallback;
        }
    }
    function savePref(key, value) {
        try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* not remembered */ }
    }
    var BED = window.RemoteSlicingBed;

    function TaskRow(d) {
        var self = this;
        self.id = d.id;
        self.kind = d.kind;
        self.state = ko.observable();
        self.progress = ko.observable(0);
        self.stage = ko.observable("");
        self.error = ko.observable("");
        self.title = ko.observable("");
        self.details = ko.observable("");
        self.printNote = ko.observable("");
        self.printStarted = ko.observable(false);
        self.cancellable = ko.pureComputed(function () {
            return self.state() === "queued" || self.state() === "running";
        });
        self.update = function (t) {
            var p = t.payload || {}, r = t.result || {};
            self.state(t.state);
            self.progress(t.progress || 0);
            self.stage(t.cancel_requested ? "cancelling…" : (t.stage || ""));
            self.error(t.state === "failed" ? (t.error || "failed") : "");
            if (t.kind === "slice") {
                self.title(p.name + " · " + (p.items || []).length + " model(s)" + (p.start_print ? " · print" : ""));
            } else if (t.kind === "fetch") {
                self.title("Fetch " + String(p.path || "").split("/").pop());
            } else {
                self.title("Convert " + (p.name || "") + ".sldprt");
            }
            var details = "";
            if (t.state === "done" && t.kind === "slice") {
                details = r.path || "";
                if (r.print_time_s) { details += " · " + formatDuration(r.print_time_s); }
                if (r.filament_mm3) { details += " · " + (r.filament_mm3 / 1000).toFixed(1) + " cm³"; }
            }
            self.details(details);
            self.printNote(r.print_note || "");
            self.printStarted(!!r.print_started);
        };
        self.update(d);
    }

    function formatDuration(s) {
        var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
        return h ? h + " h " + m + " min" : m + " min";
    }

    function reconcile(arr, rows, make) {
        var byId = {};
        _.each(arr(), function (vm) { byId[vm.id] = vm; });
        arr(_.map(rows, function (d) {
            var vm = byId[d.id];
            if (vm) { vm.update(d); return vm; }
            return make(d);
        }));
    }

    function RemoteSlicingViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];
        self.access = parameters[1];
        var base = OctoPrint.getBlueprintUrl("remoteslicing");
        var uidCounter = 0;
        var syncing = false;

        // ------------------------------------------------------------------ server state
        self.models = ko.observableArray([]);
        self.tasks = ko.observableArray([]);
        self.agent = ko.observable({online: false, last_seen: null, snapshot: null});
        self.serverOffset = 0;
        self.now = ko.observable(Date.now() / 1000);
        self.autoAdd = {};  // fetch/convert task id -> true: add the resulting model to the plate

        self.can = function (name) {
            try {
                return self.loginState.hasPermission(self.access.permissions[name]);
            } catch (e) {
                return false;
            }
        };
        self.canUpload = ko.pureComputed(function () { self.loginState.isUser(); return self.can("FILES_UPLOAD"); });
        self.canDelete = ko.pureComputed(function () { self.loginState.isUser(); return self.can("FILES_DELETE"); });

        self.agentOnline = ko.pureComputed(function () { return !!self.agent().online; });
        self.snapshot = ko.pureComputed(function () { return self.agent().snapshot || null; });
        self.ago = function (t) {
            if (!t) { return "never"; }
            var s = Math.max(0, self.now() + self.serverOffset - t);
            if (s < 90) { return "just now"; }
            if (s < 5400) { return Math.round(s / 60) + " min ago"; }
            if (s < 129600) { return Math.round(s / 3600) + " h ago"; }
            return Math.round(s / 86400) + " days ago";
        };
        self.lastSeenText = ko.pureComputed(function () { return self.ago(self.agent().last_seen); });
        self.settingsText = ko.pureComputed(function () {
            var snap = self.snapshot();
            if (!snap) { return "No Cura settings yet: open Cura on the PC once with the Remote Slicing Snapshot plugin."; }
            var s = snap.summary || {};
            var created = Date.parse(snap.created) / 1000;
            return [s.machine, s.profile, s.layer_height + " mm", Math.round(s.infill) + "% infill",
                "settings from " + self.ago(created)].join(" · ");
        });
        self.bedSize = ko.pureComputed(function () {
            var snap = self.snapshot();
            var bed = snap && snap.bed;
            return {width: (bed && bed.width) || 350, depth: (bed && bed.depth) || 350};
        });
        self.bedText = ko.pureComputed(function () { return self.bedSize().width + " × " + self.bedSize().depth + " mm"; });
        self.extruders = ko.pureComputed(function () {
            var snap = self.snapshot();
            return (snap && snap.summary && snap.summary.extruders) || [{position: 0, enabled: true, material: "", color: ""}];
        });
        self.extruderOptions = ko.pureComputed(function () {
            return _.map(self.extruders(), function (e) {
                return {nr: e.position, enabled: e.enabled !== false,
                        label: "T" + e.position + (e.material ? " · " + e.material : "") + (e.enabled === false ? " (off)" : "")};
            });
        });
        self.disableExtruder = function (option, item) {
            if (item) { ko.applyBindingsToNode(option, {disable: !item.enabled}, item); }
        };
        self.extruderColor = function (nr) {
            var e = _.find(self.extruders(), function (x) { return x.position === nr; });
            return (e && e.color) || "#888888";
        };

        self.applyState = function (s) {
            if (s.server_time) { self.serverOffset = s.server_time - Date.now() / 1000; }
            self.models(s.models || []);
            self.agent(s.agent || {online: false});
            reconcile(self.tasks, s.tasks || [], function (d) { return new TaskRow(d); });
            _.each(s.tasks || [], function (t) {
                if (self.autoAdd[t.id] && t.state !== "queued" && t.state !== "running") {
                    delete self.autoAdd[t.id];
                    var fetchingNow = _.clone(self.fetching());
                    if (t.payload && t.payload.path) { delete fetchingNow[t.payload.path]; self.fetching(fetchingNow); }
                    if (t.state === "done" && t.result && t.result.model_id) {
                        var model = _.find(self.models(), function (m) { return m.id === t.result.model_id; });
                        if (model) { self.addToPlate(model); }
                    } else if (t.state === "failed") {
                        self.notify(t.error || "Fetching the model failed", "error");
                    }
                }
            });
            // drop plate items whose model is gone, and orientations remembered for deleted models
            var ids = _.pluck(self.models(), "id");
            self.items.remove(function (it) { return !_.contains(ids, it.modelId); });
            var stale = _.filter(_.keys(self.modelOrient()), function (id) { return !_.contains(ids, id); });
            if (stale.length && ids.length) {
                var kept = _.omit(self.modelOrient(), stale);
                self.modelOrient(kept);
                savePref(ORIENT_KEY, kept);
            }
        };

        self.refresh = function () {
            if (!self.loginState.isUser()) { return; }
            OctoPrint.get(base + "state").done(self.applyState);
        };

        self.notify = function (text, type) {
            new PNotify({title: "Remote Slicing", text: _.escape(text), type: type || "info", hide: true});
        };
        self.failure = function (prefix) {
            return function (xhr) {
                var msg = (xhr.responseJSON && xhr.responseJSON.error) || xhr.statusText || "request failed";
                self.notify(prefix + ": " + msg, "error");
            };
        };

        // ------------------------------------------------------------------ plate
        self.plateName = ko.observable("plate");
        self.items = ko.observableArray([]);
        self.selectedUid = ko.observable(null);
        self.meshes = {};  // model id -> jQuery promise of the parsed STL (footprints are computed per tilt)
        self.lastExtruder = 0;

        // Orientation chosen in the preview for library models; Add puts the model on the plate that way.
        self.modelOrient = ko.observable(loadPref(ORIENT_KEY, {}));
        self.orientationOf = function (modelId) {
            var o = self.modelOrient()[modelId] || {};
            return {tiltX: Number(o.tiltX) || 0, tiltY: Number(o.tiltY) || 0, rotation: Number(o.rotation) || 0};
        };
        function setOrientation(modelId, o) {
            var map = _.clone(self.modelOrient());
            if (!o.tiltX && !o.tiltY && !o.rotation) { delete map[modelId]; } else { map[modelId] = o; }
            self.modelOrient(map);
            savePref(ORIENT_KEY, map);
        }

        function Item(model, x, y, rotation, extruder, tiltX, tiltY) {
            var it = this;
            it.uid = "i" + (++uidCounter);
            it.modelId = model.id;
            it.name = model.name;
            it.x = ko.observable(x);
            it.y = ko.observable(y);
            it.rotation = ko.observable(rotation || 0);
            it.extruder = ko.observable(extruder || 0);
            it.tiltX = ko.observable(tiltX || 0);  // 90-degree steps about X, then Y: which face is down
            it.tiltY = ko.observable(tiltY || 0);
            it.tiltText = ko.pureComputed(function () {
                return (it.tiltX() || it.tiltY()) ? "X" + it.tiltX() + "° Y" + it.tiltY() + "°" : "flat";
            });
            it.mesh = null;
            it.hull = ko.observable(null);
            it.render = ko.observable(null);  // top-down raster for the bed, per tilt
            it.loading = ko.observable(true);
            it.error = ko.observable("");
            it.extruder.subscribe(function (v) { self.lastExtruder = v; });
        }

        self.loadMesh = function (modelId) {
            if (!self.meshes[modelId]) {
                var d = $.Deferred();
                fetch(base + "models/" + modelId + "/stl", {credentials: "same-origin"})
                    .then(function (r) { if (!r.ok) { throw new Error("download failed (" + r.status + ")"); } return r.arrayBuffer(); })
                    .then(function (buf) {
                        var parsed = BED.parseStl(buf);
                        parsed.hulls = {};    // "tiltX,tiltY" -> footprint
                        parsed.renders = {};  // "tiltX,tiltY" -> top-down raster (with .sprites per colour)
                        d.resolve(parsed);
                    })
                    .catch(function (e) { delete self.meshes[modelId]; d.reject(e.message || String(e)); });
                self.meshes[modelId] = d.promise();
            }
            return self.meshes[modelId];
        };

        // Footprint of the model as it lies on the bed: the convex hull of its X/Y after the tilt.
        function renderFor(mesh, tiltX, tiltY) {
            var key = tiltX + "," + tiltY;
            if (!mesh.renders[key]) {
                mesh.renders[key] = BED.renderTop(mesh.xyz, mesh.count, tiltX, tiltY, 512);
                mesh.renders[key].sprites = {};
            }
            return mesh.renders[key];
        }
        function spriteFor(render, color) {
            if (!render.sprites[color]) { render.sprites[color] = BED.spriteCanvas(render, color); }
            return render.sprites[color];
        }

        function hullFor(mesh, tiltX, tiltY) {
            var key = tiltX + "," + tiltY;
            if (!mesh.hulls[key]) {
                var points = (tiltX || tiltY) ? BED.tiltPoints(mesh.xyz, mesh.count, tiltX, tiltY) : mesh.points;
                mesh.hulls[key] = BED.convexHull(points, mesh.count);
            }
            return mesh.hulls[key];
        }

        self.makeItem = function (model, x, y, rotation, extruder, tiltX, tiltY) {
            var item = new Item(model, x, y, rotation, extruder, tiltX, tiltY);
            var refresh = function () {
                if (item.mesh) {
                    var tx = Number(item.tiltX()), ty = Number(item.tiltY());
                    item.render(renderFor(item.mesh, tx, ty));
                    item.hull(hullFor(item.mesh, tx, ty));
                }
            };
            item.tiltX.subscribe(refresh);
            item.tiltY.subscribe(refresh);
            self.loadMesh(model.id)
                .done(function (mesh) { item.mesh = mesh; refresh(); item.loading(false); })
                .fail(function (msg) { item.error(msg); item.loading(false); });
            return item;
        };
        self.tilt = function (axis, item) {
            var obs = axis === "x" ? item.tiltX : item.tiltY;
            obs((Number(obs()) + 90) % 360);
            self.selectItem(item.uid);
        };

        self.addToPlate = function (model) {
            var size = self.bedSize(), n = self.items().length;
            var x = (size.width / 2 + 20 * n) % size.width;
            var o = self.orientationOf(model.id);
            var item = self.makeItem(model, Math.round(x), Math.round(size.depth / 2), o.rotation, self.lastExtruder, o.tiltX, o.tiltY);
            self.items.push(item);
            self.selectItem(item.uid);
        };
        self.duplicateItem = function (item) {
            var model = {id: item.modelId, name: item.name};
            var copy = self.makeItem(model, Number(item.x()) + 20, Number(item.y()), Number(item.rotation()), item.extruder(),
                                     Number(item.tiltX()), Number(item.tiltY()));
            self.items.push(copy);
            self.selectItem(copy.uid);
        };
        self.removeItem = function (item) { self.items.remove(item); };
        self.selectItem = function (uid) {
            if (self.selectedUid() === uid) { return; }
            self.selectedUid(uid);
            if (self.bed) { syncing = true; self.bed.select(uid); syncing = false; }
        };

        self.bedItems = ko.pureComputed(function () {
            return _.compact(_.map(self.items(), function (it) {
                var hull = it.hull();
                if (!hull) { return null; }
                var color = self.extruderColor(Number(it.extruder())), render = it.render();
                return {uid: it.uid, name: it.name, hull: hull, x: Number(it.x()) || 0, y: Number(it.y()) || 0,
                        rotation: Number(it.rotation()) || 0, color: color,
                        render: render, sprite: render ? spriteFor(render, color) : null};
            }));
        });
        self.problems = ko.pureComputed(function () {
            var size = self.bedSize();
            return BED.problems(self.bedItems(), size.width, size.depth);
        });
        function nameOf(uid) {
            var it = _.find(self.items(), function (i) { return i.uid === uid; });
            return it ? it.name : uid;
        }
        self.outsideNames = ko.pureComputed(function () { return _.map(self.problems().outside, nameOf); });
        self.overlapText = ko.pureComputed(function () {
            var pairs = self.problems().overlapping;
            if (!pairs.length) { return ""; }
            return "Overlapping: " + _.map(pairs, function (p) { return nameOf(p[0]) + " + " + nameOf(p[1]); }).join(", ");
        });

        self.sliceBlocker = ko.pureComputed(function () {
            if (!self.items().length) { return ""; }
            if (!self.agentOnline()) { return "The PC agent is offline."; }
            if (_.some(self.items(), function (i) { return i.loading(); })) { return "Loading models…"; }
            if (_.some(self.items(), function (i) { return i.error(); })) { return "A model could not be loaded."; }
            if (self.outsideNames().length) { return "Move every model onto the bed."; }
            return "";
        });
        self.canSlice = ko.pureComputed(function () {
            return self.items().length > 0 && !self.sliceBlocker() && self.canUpload();
        });
        self.canSliceAndPrint = ko.pureComputed(function () {
            self.loginState.isUser();
            return self.canSlice() && self.can("PRINT");
        });
        self.slice = function (startPrint) {
            var items = _.map(self.items(), function (it) {
                return {model_id: it.modelId, x: Number(it.x()), y: Number(it.y()),
                        rotation: Number(it.rotation()), extruder: Number(it.extruder()),
                        tilt_x: Number(it.tiltX()), tilt_y: Number(it.tiltY())};
            });
            var send = function () {
                OctoPrint.postJson(base + "slice", {name: self.plateName(), items: items, start_print: startPrint})
                    .done(function () { self.notify(startPrint ? "Queued: slice, upload & print" : "Slicing job queued"); })
                    .fail(self.failure("Could not queue the job"));
            };
            if (!startPrint) { send(); return; }
            showConfirmationDialog({
                title: "Slice and print",
                message: "Slice " + items.length + " model(s) and start printing as soon as the file is uploaded? " +
                    "The printer must be idle and the bed clear.",
                proceed: "Slice & print",
                onproceed: send
            });
        };

        // ------------------------------------------------------------------ library
        self.uploadProgress = ko.observable(null);
        self.uploadSelected = function (data, event) {
            var files = _.toArray(event.target.files || []);
            event.target.value = "";
            var next = function () {
                var file = files.shift();
                if (!file) { self.uploadProgress(null); return; }
                self.uploadProgress(0);
                OctoPrint.upload(base + "models", file, file.name)
                    .progress(function (e) { if (e.total) { self.uploadProgress(Math.round(100 * e.loaded / e.total)); } })
                    .done(function (resp) {
                        resp = typeof resp === "string" ? JSON.parse(resp) : resp;
                        if (resp && resp.task) {
                            self.autoAdd[resp.task.id] = true;
                            self.notify(file.name + ": converting in SolidWorks on the PC");
                        } else if (resp && resp.model) {
                            self.models.unshift(resp.model);
                        }
                        next();
                    })
                    .fail(function (xhr) { self.failure("Upload of " + file.name + " failed")(xhr); next(); });
            };
            next();
        };
        self.deleteModel = function (model) {
            showConfirmationDialog({
                title: "Delete model",
                message: "Delete " + model.name + " from the library?",
                proceed: "Delete",
                onproceed: function () {
                    OctoPrint.postJson(base + "models/" + model.id + "/delete", {}).fail(self.failure("Could not delete"));
                }
            });
        };
        self.formatSize = function (bytes) {
            if (bytes >= 1048576) { return (bytes / 1048576).toFixed(1) + " MB"; }
            return Math.max(1, Math.round(bytes / 1024)) + " KB";
        };

        // ------------------------------------------------------------------ dropbox browser
        self.browserVisible = ko.observable(false);
        self.browserPath = ko.observable("");
        self.browserDirs = ko.observableArray([]);
        self.browserFiles = ko.observableArray([]);
        self.browserTruncated = ko.observable(false);
        self.browserLoading = ko.observable(false);
        self.browserError = ko.observable("");
        self.fetching = ko.observable({});
        self.requestId = null;
        var listingTimer = null;

        self.crumbs = ko.pureComputed(function () {
            var parts = self.browserPath() ? self.browserPath().split("/") : [];
            return _.map(parts, function (name, i) { return {name: name, path: parts.slice(0, i + 1).join("/")}; });
        });
        self.joinPath = function (name) { return self.browserPath() ? self.browserPath() + "/" + name : name; };
        self.rememberFolder = ko.observable(loadPref(REMEMBER_KEY, true));
        self.rememberFolder.subscribe(function (on) {
            savePref(REMEMBER_KEY, on);
            savePref(FOLDER_KEY, on ? self.browserPath() : "");
        });
        self.toggleBrowser = function () {
            self.browserVisible(!self.browserVisible());
            if (self.browserVisible()) {
                var start = self.browserPath() || (self.rememberFolder() ? loadPref(FOLDER_KEY, "") : "");
                self.browse(start || "", true);
            }
        };
        self.browse = function (path, fallbackToRoot) {
            self.fallbackToRoot = !!fallbackToRoot && !!path;  // a remembered folder that has gone away
            self.browserLoading(true);
            self.browserError("");
            clearTimeout(listingTimer);
            OctoPrint.postJson(base + "list", {path: path})
                .done(function (resp) {
                    self.requestId = resp.request_id;
                    self.browserPath(path);
                    var early = self.earlyListings[resp.request_id];
                    if (early) {
                        delete self.earlyListings[resp.request_id];
                        self.onListing(early);
                        return;
                    }
                    listingTimer = setTimeout(function () {
                        if (self.browserLoading()) {
                            self.browserLoading(false);
                            self.browserError("The PC agent did not answer.");
                        }
                    }, 10000);
                })
                .fail(function (xhr) {
                    self.browserLoading(false);
                    self.browserError((xhr.responseJSON && xhr.responseJSON.error) || "Could not list the folder.");
                });
        };
        // The agent's answer arrives over the socket and can beat the HTTP reply that tells us its request id,
        // so answers are kept briefly and matched up from either side.
        self.earlyListings = {};
        self.onListing = function (d) {
            if (d.request_id !== self.requestId) {
                self.earlyListings[d.request_id] = d;
                _.each(_.keys(self.earlyListings).slice(0, -5), function (k) { delete self.earlyListings[k]; });
                return;
            }
            clearTimeout(listingTimer);
            if (d.error && self.fallbackToRoot) {
                self.fallbackToRoot = false;
                self.browse("");
                return;
            }
            if (!d.error && self.rememberFolder()) { savePref(FOLDER_KEY, d.path || ""); }
            self.browserLoading(false);
            self.browserError(d.error || "");
            self.browserDirs(d.dirs || []);
            self.browserFiles(d.files || []);
            self.browserTruncated(!!d.truncated);
        };
        self.fetchFile = function (file) {
            var path = self.joinPath(file.name);
            OctoPrint.postJson(base + "fetch", {path: path})
                .done(function (resp) {
                    self.autoAdd[resp.task.id] = true;
                    var f = _.clone(self.fetching()); f[path] = true; self.fetching(f);
                })
                .fail(self.failure("Could not fetch " + file.name));
        };

        // ------------------------------------------------------------------ preview panel
        // "compact" (beside the model list), "full" (whole width) or "collapsed" (hidden)
        self.previewMode = ko.observable(loadPref(PREVIEW_MODE_KEY, loadPref(PREVIEW_KEY, true) ? "compact" : "collapsed"));
        self.previewOpen = ko.pureComputed(function () { return self.previewMode() !== "collapsed"; });
        self.previewModelId = ko.observable(null);  // a library model clicked for preview
        self.view = {az: -30, el: 30};
        self.setPreviewMode = function (mode) {
            self.previewMode(mode);
            savePref(PREVIEW_MODE_KEY, mode);
            if (mode !== "collapsed") { setTimeout(function () { self.drawPreview(false); }, 0); }  // after layout
        };
        self.previewModel = function (model) {
            self.selectItem(null);
            self.previewModelId(model.id);
            if (!self.previewOpen()) { self.setPreviewMode("compact"); }
        };
        // What to show: the selected plate model as it will be printed, else the library model last clicked.
        self.previewTarget = ko.pureComputed(function () {
            var uid = self.selectedUid();
            var it = uid && _.find(self.items(), function (i) { return i.uid === uid; });
            if (it) {
                return {modelId: it.modelId, name: it.name, tiltX: Number(it.tiltX()), tiltY: Number(it.tiltY()),
                        rotation: Number(it.rotation()) || 0, color: self.extruderColor(Number(it.extruder())), item: it};
            }
            var id = self.previewModelId();
            var model = id && _.find(self.models(), function (m) { return m.id === id; });
            if (!model) { return null; }
            var o = self.orientationOf(model.id);
            return {modelId: model.id, name: model.name, tiltX: o.tiltX, tiltY: o.tiltY, rotation: o.rotation,
                    color: "#a8b4be", model: model};
        }).extend({rateLimit: 30});
        self.previewTitle = ko.pureComputed(function () { var t = self.previewTarget(); return t ? t.name : ""; });
        self.previewSize = ko.observable("");
        self.previewBusy = ko.observable(false);
        // Turn the previewed model 90 degrees about X or Y (which face is down) or Z (its rotation on the bed).
        // A plate model changes in place; a library model remembers it for the next Add.
        self.orient = function (axis) {
            var t = self.previewTarget();
            if (!t) { return; }
            if (t.item) {
                if (axis === "z") { t.item.rotation((Number(t.item.rotation()) + 90) % 360); }
                else { self.tilt(axis, t.item); }
                return;
            }
            var o = self.orientationOf(t.modelId);
            if (axis === "x") { o.tiltX = (o.tiltX + 90) % 360; }
            else if (axis === "y") { o.tiltY = (o.tiltY + 90) % 360; }
            else { o.rotation = (o.rotation + 90) % 360; }
            setOrientation(t.modelId, o);
        };
        self.resetOrientation = function () {
            var t = self.previewTarget();
            if (!t) { return; }
            if (t.item) { t.item.tiltX(0); t.item.tiltY(0); t.item.rotation(0); }
            else { setOrientation(t.modelId, {tiltX: 0, tiltY: 0, rotation: 0}); }
        };
        self.previewOrientText = ko.pureComputed(function () {
            var t = self.previewTarget();
            return t ? "X" + t.tiltX + "° Y" + t.tiltY + "° Z" + Math.round(t.rotation) + "°" : "";
        });
        self.previewIsLibrary = ko.pureComputed(function () { var t = self.previewTarget(); return !!(t && t.model); });
        self.addPreviewed = function () { var t = self.previewTarget(); if (t && t.model) { self.addToPlate(t.model); } };
        self.setView = function (az, el) { self.view.az = az; self.view.el = el; self.drawPreview(false); };

        self.drawPreview = function (quick) {
            var canvas = document.getElementById("remoteslicing_preview");
            var target = self.previewTarget();
            if (!canvas || !self.previewOpen()) { return; }
            var cssW = canvas.parentNode.clientWidth || 300;
            var cssH = Math.round(Math.min(cssW * (self.previewMode() === "full" ? 0.75 : 0.9), 380));
            var dpr = window.devicePixelRatio || 1;
            if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
                canvas.width = Math.round(cssW * dpr);
                canvas.height = Math.round(cssH * dpr);
                canvas.style.width = cssW + "px";
                canvas.style.height = cssH + "px";
            }
            var ctx = canvas.getContext("2d");
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.fillStyle = getComputedStyle(canvas).getPropertyValue("--rs-bed").trim() || "#2b2f33";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            if (!target) { self.previewSize(""); return; }
            var token = self.previewToken = (self.previewToken || 0) + 1;
            self.previewBusy(true);
            self.loadMesh(target.modelId).done(function (mesh) {
                if (token !== self.previewToken) { return; }  // a newer request superseded this one
                self.previewBusy(false);
                var m = BED.mul3(BED.viewMatrix(self.view.az, self.view.el), BED.modelMatrix(target.tiltX, target.tiltY, target.rotation));
                var longest = Math.max(canvas.width, canvas.height) * 0.9;
                var r = BED.renderView(mesh.xyz, mesh.count, m, {maxPx: quick ? Math.min(240, longest) : longest, maxPpm: 60});
                var sprite = BED.spriteCanvas(r, target.color);
                var k = Math.min(canvas.width * 0.9 / r.width, canvas.height * 0.9 / r.height);
                var w = r.width * k, h = r.height * k;
                ctx.imageSmoothingEnabled = true;
                ctx.drawImage(sprite, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
                var size = BED.modelSize(mesh.xyz, mesh.count, target.tiltX, target.tiltY, target.rotation);
                self.previewSize(size[0].toFixed(1) + " × " + size[1].toFixed(1) + " × " + size[2].toFixed(1) + " mm");
            }).fail(function (msg) {
                self.previewBusy(false);
                self.previewSize("could not load: " + msg);
            });
        };

        self.attachPreview = function () {
            var canvas = document.getElementById("remoteslicing_preview");
            if (!canvas) { return; }
            canvas.style.touchAction = "none";
            var drag = null, pending = false;
            var redraw = function (quick) {
                if (pending) { return; }
                pending = true;
                requestAnimationFrame(function () { pending = false; self.drawPreview(quick); });
            };
            canvas.addEventListener("pointerdown", function (e) {
                drag = {x: e.clientX, y: e.clientY, az: self.view.az, el: self.view.el};
                try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
            });
            canvas.addEventListener("pointermove", function (e) {
                if (!drag) { return; }
                self.view.az = drag.az - (e.clientX - drag.x) * 0.5;
                self.view.el = Math.max(-30, Math.min(90, drag.el + (e.clientY - drag.y) * 0.5));
                redraw(true);
            });
            var end = function () { if (drag) { drag = null; self.drawPreview(false); } };
            canvas.addEventListener("pointerup", end);
            canvas.addEventListener("pointercancel", end);
            canvas.addEventListener("dblclick", function () { self.setView(-30, 30); });
            self.previewTarget.subscribe(function () { self.drawPreview(false); });
            if (window.ResizeObserver) {
                new ResizeObserver(function () { self.drawPreview(false); }).observe(canvas.parentNode);
            }
            self.drawPreview(false);
        };

        // ------------------------------------------------------------------ jobs
        self.stateClass = function (state) {
            return {queued: "label-info", running: "label-warning", done: "label-success",
                    failed: "label-important", cancelled: ""}[state] || "";
        };
        self.cancelTask = function (task) {
            OctoPrint.postJson(base + "tasks/" + task.id + "/cancel", {}).fail(self.failure("Could not cancel"));
        };
        self.hasFinished = ko.pureComputed(function () {
            return _.some(self.tasks(), function (t) { return !t.cancellable(); });
        });
        self.clearFinished = function () {
            OctoPrint.postJson(base + "tasks/clear", {}).fail(self.failure("Could not clear the jobs"));
        };

        // ------------------------------------------------------------------ persistence of the plate
        self.savePlate = function () {
            try {
                localStorage.setItem(PLATE_KEY, JSON.stringify({
                    name: self.plateName(),
                    items: _.map(self.items(), function (it) {
                        return {modelId: it.modelId, name: it.name, x: Number(it.x()), y: Number(it.y()),
                                rotation: Number(it.rotation()), extruder: Number(it.extruder()),
                                tiltX: Number(it.tiltX()), tiltY: Number(it.tiltY())};
                    })
                }));
            } catch (e) { /* private mode or blocked storage: the plate just isn't remembered */ }
        };
        self.restorePlate = function () {
            var saved = null;
            try { saved = JSON.parse(localStorage.getItem(PLATE_KEY) || "null"); } catch (e) { saved = null; }
            if (!saved) { return; }
            self.plateName(saved.name || "plate");
            self.items(_.map(saved.items || [], function (s) {
                return self.makeItem({id: s.modelId, name: s.name}, s.x, s.y, s.rotation, s.extruder, s.tiltX || 0, s.tiltY || 0);
            }));
        };
        // Restore before the autosave below exists: a computed evaluates once on creation and would
        // otherwise overwrite the saved plate with the empty one.
        self.restorePlate();
        ko.computed(function () {
            self.plateName();
            _.each(self.items(), function (it) { it.x(); it.y(); it.rotation(); it.extruder(); it.tiltX(); it.tiltY(); });
            self.savePlate();
        }).extend({rateLimit: 300});

        // ------------------------------------------------------------------ bed widget
        self.bed = null;
        self.attachBed = function () {
            var canvas = document.getElementById("remoteslicing_bed");
            if (!canvas || self.bed || !BED) { return; }
            self.bed = new BED.Bed(canvas, {
                onChange: function (b) {
                    var it = _.find(self.items(), function (i) { return i.uid === b.uid; });
                    if (!it) { return; }
                    syncing = true;
                    it.x(Math.round(b.x * 10) / 10);
                    it.y(Math.round(b.y * 10) / 10);
                    it.rotation(Math.round(b.rotation * 10) / 10);
                    syncing = false;
                },
                onSelect: function (uid) {
                    if (!syncing) { self.selectedUid(uid); }
                }
            });
            var size = self.bedSize();
            self.bed.setBed(size.width, size.depth);
            self.bed.setItems(self.bedItems());
            self.bedSize.subscribe(function (s) { self.bed.setBed(s.width, s.depth); });
            self.bedItems.subscribe(function (list) { if (!syncing) { self.bed.setItems(list); } else { self.bed.redraw(); } });
            if (window.ResizeObserver) {
                new ResizeObserver(function () {
                    var size = self.bedSize();
                    self.bed.setBed(size.width, size.depth);
                }).observe(canvas.parentNode);
            }
        };

        self.watchWidth = function () {
            var root = document.getElementById("remoteslicing_tab");
            if (!root || !window.ResizeObserver) { return; }
            new ResizeObserver(function () {
                root.classList.toggle("rs-narrow", root.clientWidth < 760);
            }).observe(root);
        };

        // ------------------------------------------------------------------ OctoPrint hooks
        self.onAfterBinding = function () { self.attachBed(); self.attachPreview(); self.watchWidth(); };
        self.onStartupComplete = self.onUserLoggedIn = function () { self.refresh(); };
        self.onAfterTabChange = function (current) {
            if (current === "#tab_plugin_remoteslicing" && self.bed) {
                var size = self.bedSize();
                self.bed.setBed(size.width, size.depth);  // the canvas had no width while the tab was hidden
            }
            if (current === "#tab_plugin_remoteslicing") { self.drawPreview(false); }
        };
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "remoteslicing" || !data || !data.type) { return; }
            if (data.type === "state") { self.applyState(data); }
            else if (data.type === "listing") { self.onListing(data); }
        };
        setInterval(function () { self.now(Date.now() / 1000); }, 30000);
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: RemoteSlicingViewModel,
        dependencies: ["loginStateViewModel", "accessViewModel"],
        elements: ["#remoteslicing_tab"]
    });
});
