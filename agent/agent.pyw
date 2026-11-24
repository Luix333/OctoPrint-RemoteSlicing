"""Starts the resident agent from anywhere: what Start with Windows and the Start menu entry run.
Windows' Run key gives no working directory, so the package is found from this file's folder; pythonw runs it,
so there is no console window. A copy that is already running is told to say so instead."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remote_slicing_agent.main import main  # noqa: E402

sys.exit(main(["run"] + sys.argv[1:]))
