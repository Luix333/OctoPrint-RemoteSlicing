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

## Development

```bash
python -m unittest discover -s tests                      # OctoPrint core
python -m unittest discover -s agent/tests -t agent       # PC agent
node --test "tests/js/*.test.js"                          # bed geometry (browser code)
python <cura-plugin-dev>/scripts/build_curapackage.py cura_plugin/RemoteSlicingSnapshot --id RemoteSlicingSnapshot --out dist
```

## License

AGPL-3.0-or-later
