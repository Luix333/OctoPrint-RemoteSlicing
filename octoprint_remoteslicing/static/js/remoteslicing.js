/*
 * View model for Remote Slicing: model library, PC Dropbox browser, 2D bed plate and jobs.
 *
 * State comes from GET state and the pushed {"type": "state"} messages; the plate itself lives only in the
 * browser (and localStorage) until it is submitted. Messages of type "agent.*" are for the PC agent.
 */
$(function () {
    var PLATE_KEY = "remoteslicing.plate";
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
            // drop plate items whose model is gone
            var ids = _.pluck(self.models(), "id");
            self.items.remove(function (it) { return !_.contains(ids, it.modelId); });
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
        self.hulls = {};  // model id -> jQuery promise of a hull
        self.lastExtruder = 0;

        function Item(model, x, y, rotation, extruder) {
            var it = this;
            it.uid = "i" + (++uidCounter);
            it.modelId = model.id;
            it.name = model.name;
            it.x = ko.observable(x);
            it.y = ko.observable(y);
            it.rotation = ko.observable(rotation || 0);
            it.extruder = ko.observable(extruder || 0);
            it.hull = ko.observable(null);
            it.loading = ko.observable(true);
            it.error = ko.observable("");
            it.extruder.subscribe(function (v) { self.lastExtruder = v; });
        }

        self.loadHull = function (modelId) {
            if (!self.hulls[modelId]) {
                var d = $.Deferred();
                fetch(base + "models/" + modelId + "/stl", {credentials: "same-origin"})
                    .then(function (r) { if (!r.ok) { throw new Error("download failed (" + r.status + ")"); } return r.arrayBuffer(); })
                    .then(function (buf) {
                        var parsed = BED.parseStl(buf);
                        d.resolve(BED.convexHull(parsed.points, parsed.count));
                    })
                    .catch(function (e) { delete self.hulls[modelId]; d.reject(e.message || String(e)); });
                self.hulls[modelId] = d.promise();
            }
            return self.hulls[modelId];
        };

        self.makeItem = function (model, x, y, rotation, extruder) {
            var item = new Item(model, x, y, rotation, extruder);
            self.loadHull(model.id)
                .done(function (hull) { item.hull(hull); item.loading(false); })
                .fail(function (msg) { item.error(msg); item.loading(false); });
            return item;
        };

        self.addToPlate = function (model) {
            var size = self.bedSize(), n = self.items().length;
            var x = (size.width / 2 + 20 * n) % size.width;
            var item = self.makeItem(model, Math.round(x), Math.round(size.depth / 2), 0, self.lastExtruder);
            self.items.push(item);
            self.selectItem(item.uid);
        };
        self.duplicateItem = function (item) {
            var model = {id: item.modelId, name: item.name};
            var copy = self.makeItem(model, Number(item.x()) + 20, Number(item.y()), Number(item.rotation()), item.extruder());
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
                return {uid: it.uid, name: it.name, hull: hull, x: Number(it.x()) || 0, y: Number(it.y()) || 0,
                        rotation: Number(it.rotation()) || 0, color: self.extruderColor(Number(it.extruder()))};
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
                        rotation: Number(it.rotation()), extruder: Number(it.extruder())};
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
        self.toggleBrowser = function () {
            self.browserVisible(!self.browserVisible());
            if (self.browserVisible()) { self.browse(self.browserPath()); }
        };
        self.browse = function (path) {
            self.browserLoading(true);
            self.browserError("");
            clearTimeout(listingTimer);
            OctoPrint.postJson(base + "list", {path: path})
                .done(function (resp) {
                    self.requestId = resp.request_id;
                    self.browserPath(path);
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
        self.onListing = function (d) {
            if (d.request_id !== self.requestId) { return; }
            clearTimeout(listingTimer);
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
                                rotation: Number(it.rotation()), extruder: Number(it.extruder())};
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
                return self.makeItem({id: s.modelId, name: s.name}, s.x, s.y, s.rotation, s.extruder);
            }));
        };
        // Restore before the autosave below exists: a computed evaluates once on creation and would
        // otherwise overwrite the saved plate with the empty one.
        self.restorePlate();
        ko.computed(function () {
            self.plateName();
            _.each(self.items(), function (it) { it.x(); it.y(); it.rotation(); it.extruder(); });
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
        self.onAfterBinding = function () { self.attachBed(); self.watchWidth(); };
        self.onStartupComplete = self.onUserLoggedIn = function () { self.refresh(); };
        self.onAfterTabChange = function (current) {
            if (current === "#tab_plugin_remoteslicing" && self.bed) {
                var size = self.bedSize();
                self.bed.setBed(size.width, size.depth);  // the canvas had no width while the tab was hidden
            }
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
