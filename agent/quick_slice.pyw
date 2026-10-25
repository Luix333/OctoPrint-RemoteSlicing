"""Explorer's "Slice and upload" / "Slice, upload & print" entry point (registered by
``python -m remote_slicing_agent install-menu``). pythonw runs it, so there is no console window."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remote_slicing_agent.quick_slice import main  # noqa: E402

sys.exit(main())
