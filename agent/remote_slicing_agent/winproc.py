"""Windows process helpers for the resident agent.

Provides ctypes wrappers for power-throttling, priority class changes,
working-set trimming, job-object lifecycle management, and single-instance
enforcement.  All functions are no-ops returning ``False``/``None`` on
non-Windows platforms so the module is safe to import everywhere.
"""

import ctypes
import os
import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # ------------------------------------------------------------------
    # Type aliases
    # ------------------------------------------------------------------
    HANDLE = ctypes.c_void_p
    DWORD = ctypes.c_uint32
    BOOL = ctypes.c_int
    SIZE_T = ctypes.c_size_t

    # ------------------------------------------------------------------
    # Priority class constants
    # ------------------------------------------------------------------
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

    # ------------------------------------------------------------------
    # ProcessPowerThrottling constants
    # ------------------------------------------------------------------
    ProcessPowerThrottling = 4
    PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1

    # ------------------------------------------------------------------
    # Job object constants
    # ------------------------------------------------------------------
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    # ------------------------------------------------------------------
    # Kernel32 function signatures
    # ------------------------------------------------------------------
    _kernel32.GetCurrentProcess.restype = HANDLE

    _kernel32.SetPriorityClass.argtypes = [HANDLE, DWORD]
    _kernel32.SetPriorityClass.restype = BOOL

    _kernel32.SetProcessInformation.argtypes = [
        HANDLE, DWORD, ctypes.c_void_p, DWORD
    ]
    _kernel32.SetProcessInformation.restype = BOOL

    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, BOOL, ctypes.c_wchar_p]
    _kernel32.CreateMutexW.restype = HANDLE

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.CreateJobObjectW.restype = HANDLE

    _kernel32.SetInformationJobObject.argtypes = [
        HANDLE, DWORD, ctypes.c_void_p, DWORD
    ]
    _kernel32.SetInformationJobObject.restype = BOOL

    _kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    _kernel32.AssignProcessToJobObject.restype = BOOL

    _kernel32.CloseHandle.argtypes = [HANDLE]
    _kernel32.CloseHandle.restype = BOOL

    # ------------------------------------------------------------------
    # Psapi function signatures
    # ------------------------------------------------------------------
    _psapi.EmptyWorkingSet.argtypes = [HANDLE]
    _psapi.EmptyWorkingSet.restype = BOOL

    _psapi.GetProcessMemoryInfo.argtypes = [
        HANDLE, ctypes.c_void_p, DWORD
    ]
    _psapi.GetProcessMemoryInfo.restype = BOOL

    # ------------------------------------------------------------------
    # Structures
    # ------------------------------------------------------------------

    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [
            ("Version", DWORD),
            ("ControlMask", DWORD),
            ("StateMask", DWORD),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", DWORD),
            ("PageFaultCount", DWORD),
            ("PeakWorkingSetSize", SIZE_T),
            ("WorkingSetSize", SIZE_T),
            ("QuotaPeakPagedPoolUsage", SIZE_T),
            ("QuotaPagedPoolUsage", SIZE_T),
            ("QuotaPeakNonPagedPoolUsage", SIZE_T),
            ("QuotaNonPagedPoolUsage", SIZE_T),
            ("PagefileUsage", SIZE_T),
            ("PeakPagefileUsage", SIZE_T),
        ]

    class LARGE_INTEGER(ctypes.Structure):
        _fields_ = [("Low", DWORD), ("High", DWORD)]

    # PerProcessUserTimeLimit / PerJobUserTimeLimit are LARGE_INTEGER
    # which is 8 bytes == c_int64
    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", DWORD),
            ("Affinity", SIZE_T),
            ("PriorityClass", DWORD),
            ("SchedulingClass", DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]


def set_below_normal(handle=None):
    """Set the process priority class to *Below Normal*.

    Returns ``True`` on success, ``False`` on failure or non-Windows.
    """
    if not IS_WINDOWS:
        return False
    try:
        if handle is None:
            handle = _kernel32.GetCurrentProcess()
        return bool(_kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS))
    except OSError:
        return False


def set_eco_qos(enabled=True, handle=None):
    """Enable or disable EcoQoS (efficiency mode) for the process.

    Returns ``True`` on success, ``False`` on failure or non-Windows.
    """
    if not IS_WINDOWS:
        return False
    try:
        if handle is None:
            handle = _kernel32.GetCurrentProcess()
        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = 1
        state.ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        state.StateMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED if enabled else 0
        return bool(
            _kernel32.SetProcessInformation(
                handle,
                ProcessPowerThrottling,
                ctypes.addressof(state),
                ctypes.sizeof(state),
            )
        )
    except OSError:
        return False


def empty_working_set():
    """Trim the working set of the current process.

    Returns ``True`` on success, ``False`` on failure or non-Windows.
    """
    if not IS_WINDOWS:
        return False
    try:
        handle = _kernel32.GetCurrentProcess()
        return bool(_psapi.EmptyWorkingSet(handle))
    except OSError:
        return False


class KillOnCloseJob:
    """Job object that kills all assigned processes on handle close.

    Parameters
    ----------
    create: bool
        If ``True`` (default) the job is created in ``__init__``.  Pass
        ``False`` during unit-test mocking to skip real handle creation.

    Attributes
    ----------
    ok: bool
        ``True`` when the job was created and configured successfully.
    """

    def __init__(self, create=True):
        self._handle = None
        self.ok = False
        if not IS_WINDOWS:
            return
        if not create:
            return
        try:
            self._handle = _kernel32.CreateJobObjectW(None, None)
            if self._handle is None:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = bool(
                _kernel32.SetInformationJobObject(
                    self._handle,
                    JobObjectExtendedLimitInformation,
                    ctypes.addressof(info),
                    ctypes.sizeof(info),
                )
            )
            if not ok:
                _kernel32.CloseHandle(self._handle)
                self._handle = None
                return
            self.ok = True
        except OSError:
            if self._handle is not None:
                try:
                    _kernel32.CloseHandle(self._handle)
                except OSError:
                    pass
            self._handle = None

    def assign(self, popen):
        """Assign a subprocess to this job object.

        Parameters
        ----------
        popen: subprocess.Popen
            The child process to assign.

        Returns
        -------
        bool
            ``True`` on success.
        """
        if not self.ok:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(self._handle, popen._handle))
        except (OSError, AttributeError):
            return False

    def close(self):
        """Close the job handle, killing all assigned processes."""
        if self._handle is not None:
            try:
                _kernel32.CloseHandle(self._handle)
            except OSError:
                pass
            self._handle = None


def single_instance(name="Local\\RemoteSlicingAgent"):
    """Ensure only one agent instance runs at a time.

    Parameters
    ----------
    name: str
        Mutex name (must be a valid Windows mutex name).

    Returns
    -------
    HANDLE or None
        The mutex handle on success (caller should keep it alive), or
        ``None`` if another instance is already running.  Returns ``True``
        on non-Windows platforms (always allowed).
    """
    if not IS_WINDOWS:
        return True
    try:
        handle = _kernel32.CreateMutexW(None, False, name)
        if handle is None:
            return None
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            _kernel32.CloseHandle(handle)
            return None
        return handle
    except OSError:
        return None


def process_memory():
    """Return the current process working set size in bytes.

    Returns ``None`` on failure or non-Windows.
    """
    if not IS_WINDOWS:
        return None
    try:
        handle = _kernel32.GetCurrentProcess()
        pmc = PROCESS_MEMORY_COUNTERS()
        ok = _psapi.GetProcessMemoryInfo(handle, ctypes.addressof(pmc), ctypes.sizeof(pmc))
        if not ok:
            return None
        return pmc.WorkingSetSize
    except OSError:
        return None
