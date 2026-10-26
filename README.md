# Remote Slicing for OctoPrint

Send models to your printer from OctoPrint, from any device, and have them sliced on your PC by
CuraEngine with **the Cura settings you last used**: same printer, profile, materials and MMU extruders.

1. Upload an STL or a SolidWorks part in the **Remote Slicing** tab, or pick one from your PC's Dropbox
   folder.
2. Drag and rotate the models on the top-down bed and choose an extruder (T0, T1, ...) for each one.
3. Press **Slice & upload**, or **Slice, upload & print**.

The G-code lands in `RemoteSlicing/` in OctoPrint's file list, with the same header (print time,
filament per extruder) that Cura writes.

## How it works

Three parts, all in this repository:

| Part | Runs on | Does |
|---|---|---|
| `octoprint_remoteslicing/` | the OctoPrint host (e.g. the printer's Raspberry Pi) | The tab, the model library and the job queue. |
| `cura_plugin/RemoteSlicingSnapshot/` | Cura on your PC | Saves exactly what Cura would send to CuraEngine, to `%LOCALAPPDATA%\RemoteSlicing\snapshot.json`. It does this when Cura starts and two seconds after any settings change. |
| `agent/` | your PC (Windows) | A tiny background program. When a job is queued, it runs CuraEngine headlessly with that snapshot and uploads the G-code. |

Slicing doesn't need Cura open; the snapshot is as fresh as the last time Cura ran. Because the
settings come from Cura's own slice-message builder, the headless result is identical to slicing the same
plate in Cura (checked line by line on a 12-extruder Voron profile, including start-G-code tokens and tool
changes).

Your **post-processing scripts** (Extensions → Post Processing → Modify G-Code) run on every job too, in
the same order and with the same settings. The agent runs Cura's own script files on the G-code, split into the
same per-layer chunks Cura uses, so Search and Replace and similar scripts change exactly the same lines.
Create Thumbnail gets a picture of the plate rendered on the PC: the same viewpoint and lighting as Cura's,
but not pixel-identical. A script that needs more of Cura than that (the print-information panel, Qt, …)
fails the job with its name, instead of being skipped silently.

The agent costs almost nothing while idle. It keeps one connection to OctoPrint open, sends a
heartbeat once a minute, and runs in Windows' efficiency mode. Slicing happens in a short-lived child process
at below-normal priority.

## Install

### 1. OctoPrint plugin
In OctoPrint go to **Settings → Plugin Manager → Get More → … from URL** and enter:

```
https://github.com/Luix333/OctoPrint-RemoteSlicing/archive/main.zip
```

Restart OctoPrint afterwards.

### 2. Cura plugin
Drag `RemoteSlicingSnapshot-<version>.curapackage` (from the release, or built with the command below)
onto Cura's window and restart Cura. **Extensions → Remote Slicing → Export settings now** confirms it works.

### 3. PC agent
Needs Python 3.10 or newer with `numpy` and `websocket-client`:

```bash
python -m pip install numpy websocket-client
```

Then, from the `agent` folder:

```bash
python -m remote_slicing_agent setup --url http://<your-octoprint-address>
```

OctoPrint shows a dialog asking to allow **Remote Slicing Agent**. Approve it and the agent stores its
key. Start the agent with `pythonw -m remote_slicing_agent run`, or let it start with Windows:
`python -m remote_slicing_agent install-startup`. `python -m remote_slicing_agent status` shows what it
is doing.

SolidWorks parts are converted by SolidWorks itself, through the
[Cura SolidWorks plugin](https://github.com/Luix333/LegacyCuraSolidWorksPlugin) (v1.0.0+) installed in Cura.
Dropbox browsing lists the folder that the Dropbox desktop app syncs on the PC.

### Slicing from Explorer's right-click menu

```bash
python -m remote_slicing_agent install-menu
```

This adds a **Slice...** submenu to the right-click menu of `.stl` and `.sldprt` files:

```
Slice...
    and upload (center)
    upload and print (top-left)   ... (top-mid, top-right, mid-left, center, mid-right, btm-left, btm-mid)
    upload and print (btm-right)
    more options
```

It is added for your Windows user only, so no admin rights are needed. On Windows 11 it appears under
**Show more options**, unless you have the classic menu back.

- **Several files at once:** select them all and choose an entry once. They become **one job**, with every part on
  the same plate.
- **The quick entries:** the parts are packed together in rows, as they lie in their files, on Cura's default
  extruder.
  - **and upload** puts the group in the middle of the bed and uploads it.
  - Each **upload and print** entry puts it at that spot and starts printing. "Top" is the back of the bed, as on
    the tab's plate.
  - Corner and side spots keep 10 mm from the bed edge, on top of Cura's own border.
- **more options:** opens a plate editor like the Remote Slicing tab before anything is sent. You can:
  - drag parts, turn them (mouse wheel, buttons or degrees) and tilt them in 90° steps;
  - pick each part's extruder;
  - duplicate or remove parts;
  - arrange them, or move them all to one of the nine spots.

  Parts outside the bed or too tall are shown in red and block slicing. Overlaps ask first.
- **What happens next:** a small window converts SolidWorks parts on the PC and adds the models to the Remote
  Slicing library. It queues one ordinary job, the same as the tab's Jobs list shows, which the agent slices with
  your Cura settings and post-processing scripts.
- **Printing:** a print starts only if the printer is connected and idle, the same check as the tab's button.
- **The window:** it follows the job, closes itself once the G-code is uploaded (or the print has started), and
  can cancel the job.
- **Limits:** parts that don't fit where they were asked to go are refused before anything is sent. If the agent
  isn't running, it is started.

`python -m remote_slicing_agent remove-menu` takes the entries out again. The entries point at this `agent`
folder and the Python that ran `install-menu`, so run it again if either moves.

## Development

```bash
python -m unittest discover -s tests                      # OctoPrint core
python -m unittest discover -s agent/tests -t agent       # PC agent
node --test "tests/js/*.test.js"                          # bed geometry (browser code)
python <cura-plugin-dev>/scripts/build_curapackage.py cura_plugin/RemoteSlicingSnapshot --id RemoteSlicingSnapshot --out dist
```

## License

AGPL-3.0-or-later
