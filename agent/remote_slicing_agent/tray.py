"""The agent's icon in the Windows notification area (system tray).

``Tray`` is plain Win32 through ctypes, so the agent takes on no dependency and still costs nothing while idle: the
tray's own thread sleeps in GetMessage until the icon is clicked or the status changes. ``AgentTray`` decides what
it shows: the icon and tooltip for the agent's state, and the menu (open the tab, the log, Start with Windows, Exit).

A second launch finds the running copy's hidden window by its class name and asks it to say it is running
(``announce_running``), so starting it from the Start menu never adds a second icon.
"""

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from urllib.parse import urlparse

from . import __version__, config

ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

WM_DESTROY, WM_CLOSE, WM_NULL = 0x0002, 0x0010, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY, WM_REFRESH, WM_BALLOON, WM_ANNOUNCE = 0x8001, 0x8002, 0x8003, 0x8004  # WM_APP + n
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_NOSOUND = 0x1, 0x10
CLASS_NAME = "RemoteSlicingAgentTray"
MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY, TPM_BOTTOMALIGN, TPM_RIGHTALIGN = 0x2, 0x100, 0x80, 0x20, 0x8
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON = 49
MB_YESNO, MB_ICONWARNING, MB_ICONERROR, MB_SETFOREGROUND, IDYES = 0x4, 0x30, 0x10, 0x10000, 6
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("guidItem", GUID)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


def _api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sig = {
        (user32, "RegisterClassExW"): ([ctypes.POINTER(WNDCLASSEXW)], wintypes.ATOM),
        (user32, "CreateWindowExW"): ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID], wintypes.HWND),
        (user32, "DefWindowProcW"): ([wintypes.HWND, wintypes.UINT, WPARAM, LPARAM], LRESULT),
        (user32, "DestroyWindow"): ([wintypes.HWND], wintypes.BOOL),
        (user32, "GetMessageW"): ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT],
                                  wintypes.BOOL),
        (user32, "TranslateMessage"): ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        (user32, "DispatchMessageW"): ([ctypes.POINTER(wintypes.MSG)], LRESULT),
        (user32, "PostMessageW"): ([wintypes.HWND, wintypes.UINT, WPARAM, LPARAM], wintypes.BOOL),
        (user32, "PostQuitMessage"): ([ctypes.c_int], None),
        (user32, "RegisterWindowMessageW"): ([wintypes.LPCWSTR], wintypes.UINT),
        (user32, "CreatePopupMenu"): ([], wintypes.HMENU),
        (user32, "AppendMenuW"): ([wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
        (user32, "SetMenuDefaultItem"): ([wintypes.HMENU, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        (user32, "TrackPopupMenu"): ([wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.LPVOID], ctypes.c_int),
        (user32, "DestroyMenu"): ([wintypes.HMENU], wintypes.BOOL),
        (user32, "GetCursorPos"): ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        (user32, "SetForegroundWindow"): ([wintypes.HWND], wintypes.BOOL),
        (user32, "LoadImageW"): ([wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT], wintypes.HANDLE),
        (user32, "DestroyIcon"): ([wintypes.HICON], wintypes.BOOL),
        (user32, "MessageBoxW"): ([wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT], ctypes.c_int),
        (user32, "GetSystemMetrics"): ([ctypes.c_int], ctypes.c_int),
        (user32, "FindWindowW"): ([wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
        (shell32, "Shell_NotifyIconW"): ([wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], wintypes.BOOL),
        (shell32, "Shell_NotifyIconGetRect"): ([ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)],
                                               ctypes.c_long),
        (kernel32, "GetModuleHandleW"): ([wintypes.LPCWSTR], wintypes.HMODULE),
    }
    for (dll, name), (args, res) in sig.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, res
    try:  # Windows 10 1607+: crisp icon and menu at display scaling, for this thread's windows only
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetDpiForSystem.restype = wintypes.UINT
        user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
        user32.GetSystemMetricsForDpi.restype = ctypes.c_int
    except AttributeError:
        pass
    return user32, shell32, kernel32


def announce_running(class_name=CLASS_NAME):
    """Ask a running copy to say it is running; False when no copy with a tray icon is there."""
    user32 = _api()[0]
    hwnd = user32.FindWindowW(class_name, None)
    return bool(hwnd) and bool(user32.PostMessageW(hwnd, WM_ANNOUNCE, 0, 0))


class Tray:
    """One notification-area icon with a menu on either click, run on a thread of its own.

    ``menu()`` returns the items each time the menu opens: ``None`` for a separator, else ``(command id, text,
    flags)`` with flags from ``{"default", "checked", "disabled"}``. ``on_command(id)`` runs on the tray's thread, as
    does ``on_announce()`` when another launch asks this copy to show itself.
    """

    def __init__(self, menu, on_command, log=None, uid=1, class_name=CLASS_NAME, on_announce=None):
        self.menu, self.on_command, self.on_announce = menu, on_command, on_announce
        self.log = log or logging.getLogger("remote_slicing_agent")
        self.uid, self.class_name = uid, class_name
        self.hwnd = None
        self._look = (None, "")
        self._balloon = ("", "")
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._icons = {}
        self._in_menu = False
        self._thread = None

    # ------------------------------------------------------------- any thread

    def start(self, icon, tip, timeout=5.0):
        """Show the icon; False when the tray couldn't be created (the agent then just runs without it)."""
        self._look = (icon, tip)
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout) and self.hwnd is not None

    def update(self, icon, tip):
        with self._lock:
            self._look = (icon, tip)
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def balloon(self, title, text):
        """A notification from the icon (Windows 11 shows it as a toast)."""
        with self._lock:
            self._balloon = (title, text)
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_BALLOON, 0, 0)

    def stop(self, timeout=3.0):
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def is_shown(self):
        """Whether the shell has the icon (it may sit in the overflow area): Shell_NotifyIconGetRect finds it."""
        if not self.hwnd:
            return False
        ident = NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(NOTIFYICONIDENTIFIER), hWnd=self.hwnd, uID=self.uid)
        rect = wintypes.RECT()
        return self.shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) == 0

    # ----------------------------------------------------------- tray thread

    def ask(self, text, title="Remote Slicing agent"):
        """Yes/No question from a menu command."""
        flags = MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
        return self.user32.MessageBoxW(self.hwnd, text, title, flags) == IDYES

    def tell(self, text, title="Remote Slicing agent"):
        self.user32.MessageBoxW(self.hwnd, text, title, MB_ICONERROR | MB_SETFOREGROUND)

    def _run(self):
        try:
            self.user32, self.shell32, kernel32 = _api()
            try:
                self.user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
                self._icon_size = self.user32.GetSystemMetricsForDpi(SM_CXSMICON, self.user32.GetDpiForSystem())
            except (AttributeError, OSError):
                self._icon_size = self.user32.GetSystemMetrics(SM_CXSMICON)
            self._taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")
            self._wndproc = WNDPROC(self._window_proc)  # kept: Windows calls it for as long as the window lives
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=hinst,
                             lpszClassName=self.class_name)
            if not self.user32.RegisterClassExW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            # a hidden top-level window, not a message-only one: only those hear TaskbarCreated, and FindWindow
            # finds it for announce_running
            self.hwnd = self.user32.CreateWindowExW(0, self.class_name, "Remote Slicing agent", 0, 0, 0, 0, 0,
                                                    None, None, hinst, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._notify(NIM_ADD):
                self.log.warning("the tray icon couldn't be added yet; it is retried when the taskbar starts")
        except Exception:
            self.log.exception("no tray icon")
            self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))
        for icon in self._icons.values():
            self.user32.DestroyIcon(icon)
        self._icons.clear()

    def _icon(self, path):
        if path not in self._icons:
            self._icons[path] = self.user32.LoadImageW(None, path, IMAGE_ICON, self._icon_size, self._icon_size,
                                                       LR_LOADFROMFILE)
        return self._icons[path]

    def _notify(self, action, balloon=None):
        with self._lock:
            icon, tip = self._look
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=self.uid)
        if action != NIM_DELETE:
            data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            data.uCallbackMessage = WM_TRAY
            data.hIcon = self._icon(icon) if icon else None
            data.szTip = tip[:127]
        if balloon:
            data.uFlags |= NIF_INFO
            data.szInfoTitle, data.szInfo = balloon[0][:63], balloon[1][:255]
            data.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND
        return bool(self.shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

    def _window_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY and (lparam & 0xFFFF) in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._show_menu()
                return 0
            if msg == WM_REFRESH:
                self._notify(NIM_MODIFY)
                return 0
            if msg == WM_BALLOON:
                with self._lock:
                    balloon = self._balloon
                self._notify(NIM_MODIFY, balloon)
                return 0
            if msg == WM_ANNOUNCE:
                if self.on_announce:
                    self.on_announce()
                return 0
            if msg == self._taskbar_created:  # Explorer restarted: the icon has to be added again
                self._notify(NIM_ADD)
                return 0
            if msg == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._notify(NIM_DELETE)
                self.hwnd = None
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:  # an exception can't cross back into Windows: log it and carry on
            self.log.exception("tray")
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def build_menu(self):
        hmenu = self.user32.CreatePopupMenu()
        for item in self.menu():
            if item is None:
                self.user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            command, text, flags = item
            style = MF_STRING | (MF_CHECKED if "checked" in flags else 0) | (MF_GRAYED if "disabled" in flags else 0)
            self.user32.AppendMenuW(hmenu, style, command, text)
            if "default" in flags:
                self.user32.SetMenuDefaultItem(hmenu, command, False)
        return hmenu

    def _show_menu(self):
        if self._in_menu:  # a click while a question from the menu is still open
            return
        self._in_menu = True
        try:
            hmenu = self.build_menu()
            point = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            self.user32.SetForegroundWindow(self.hwnd)  # or the menu doesn't close when clicking elsewhere
            command = self.user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY | TPM_BOTTOMALIGN | TPM_RIGHTALIGN,
                point.x, point.y, 0, self.hwnd, None)
            self.user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
            self.user32.DestroyMenu(hmenu)
            if command:
                self.on_command(command)
        finally:
            self._in_menu = False


OPEN_TAB, SHOW_LOG, STARTUP, EXIT = 1, 2, 3, 4
JOB_TEXT = {"slice": "Slicing a plate", "fetch": "Getting a file from the PC's Dropbox",
            "preview": "Getting a file from the PC's Dropbox", "convert": "Converting a SolidWorks part"}


class AgentTray:
    """What the tray shows for an ``Agent``: its state as icon and tooltip, and the menu's commands.
    ``startup`` is an ``autostart.Startup``; the menu reads it each time it opens, since Task Manager can change it."""

    def __init__(self, agent, url, startup, log=None, tray=None, open_path=None):
        self.agent, self.url, self.startup = agent, url.rstrip("/"), startup
        self.log = log or logging.getLogger("remote_slicing_agent")
        self.tray = tray or Tray(self.menu, self.command, self.log, on_announce=self.announce)
        self.open_path = open_path or os.startfile
        agent.on_status = lambda state, detail: self.tray.update(*self.look())

    def start(self, quiet=False):
        """Show the icon; unless ``quiet`` (logon, or started by Explorer's menu), also say so, since Windows 11
        puts a new icon under the ^ and it would otherwise go unseen."""
        if not self.tray.start(*self.look()):
            self.log.warning("running without a tray icon")
            return False
        if not quiet:
            self.tray.balloon("Remote Slicing agent is running", self.status_text())
        return True

    def announce(self):
        self.tray.balloon("Remote Slicing agent is already running", self.status_text())

    def stop(self):
        self.tray.stop()

    def status_text(self):
        state, detail = self.agent.status
        host = urlparse(self.url).netloc or self.url
        if state == "online":
            return "Online: OctoPrint at " + host
        if state == "busy":
            return JOB_TEXT.get(detail, "Working on a job")
        if state == "offline":
            return "Offline: retrying " + host + (" (" + detail + ")" if detail else "")
        return "Connecting to OctoPrint at " + host

    def look(self):
        state = self.agent.status[0]
        icon = {"online": "online", "busy": "slicing"}.get(state, "offline")
        return os.path.join(ICONS, icon + ".ico"), ("Remote Slicing agent\n" + self.status_text())[:127]

    def menu(self):
        return [
            (0, "Remote Slicing agent " + __version__, {"disabled"}),
            (0, self.status_text(), {"disabled"}),
            None,
            (OPEN_TAB, "Open Remote Slicing in OctoPrint", {"default"}),
            (SHOW_LOG, "Show the agent's log", set()),
            None,
            (STARTUP, "Start with Windows", {"checked"} if self.startup.enabled() else set()),
            None,
            (EXIT, "Exit", set()),
        ]

    def command(self, command):
        if command == OPEN_TAB:
            self.open_path(self.url + "/#tab_plugin_remoteslicing")
        elif command == SHOW_LOG:
            self.open_path(config.log_path())
        elif command == STARTUP:
            wanted = not self.startup.enabled()
            try:
                self.startup.set(wanted)
                self.log.info("start with Windows %s, from the tray", "on" if wanted else "off")
            except Exception as e:
                self.log.exception("couldn't change Start with Windows")
                self.tray.tell("Couldn't change Start with Windows: %s" % e)
        elif command == EXIT:
            job = self.agent.job
            if job and not self.tray.ask(
                    "%s right now. Exit anyway? The job stops; OctoPrint hands it to the agent again the next "
                    "time it runs." % JOB_TEXT.get(job["task"].get("kind"), "The agent is working on a job")):
                return
            self.log.info("exit from the tray")
            self.agent.stop()
