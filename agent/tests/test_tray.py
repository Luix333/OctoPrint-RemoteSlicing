import ctypes
import os
import sys
import threading
import time
import unittest
from unittest import mock

if sys.platform == "win32":
    from remote_slicing_agent import tray


class FakeTray:
    def __init__(self):
        self.looks, self.balloons, self.told = [], [], []
        self.answer = True
        self.started = None

    def start(self, icon, tip):
        self.started = (icon, tip)
        return True

    def update(self, icon, tip):
        self.looks.append((icon, tip))

    def balloon(self, title, text):
        self.balloons.append((title, text))

    def ask(self, text):
        self.asked = text
        return self.answer

    def tell(self, text):
        self.told.append(text)

    def stop(self):
        pass


class FakeAgent:
    def __init__(self):
        self.status = ("connecting", "")
        self.job = None
        self.on_status = None
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeStartup:
    def __init__(self, on=False, fail=False):
        self.on, self.fail = on, fail

    def enabled(self):
        return self.on

    def set(self, on):
        if self.fail:
            raise OSError("access denied")
        self.on = on


@unittest.skipUnless(sys.platform == "win32", "Windows tray")
class AgentTrayTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.fake, self.startup, self.opened = FakeAgent(), FakeTray(), FakeStartup(), []
        self.ui = tray.AgentTray(self.agent, "http://10.147.17.100/", self.startup, log=mock.Mock(), tray=self.fake,
                                 open_path=self.opened.append)

    def test_icon_and_tooltip_follow_the_agent(self):
        for state, detail, icon, text in (("connecting", "", "offline", "Connecting to OctoPrint at 10.147.17.100"),
                                          ("online", "", "online", "Online: OctoPrint at 10.147.17.100"),
                                          ("busy", "slice", "slicing", "Slicing a plate"),
                                          ("offline", "timed out", "offline", "Offline: retrying 10.147.17.100 (timed out)")):
            self.agent.status = (state, detail)
            self.agent.on_status(state, detail)
            path, tip = self.fake.looks[-1]
            self.assertEqual(os.path.basename(path), icon + ".ico")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(tip, "Remote Slicing agent\n" + text)

    def test_tooltip_fits_windows_limit(self):
        self.agent.status = ("offline", "x" * 300)
        self.assertLessEqual(len(self.ui.look()[1]), 127)

    def test_menu(self):
        self.agent.status = ("online", "")
        items = self.ui.menu()
        texts = [item[1] if item else "-" for item in items]
        self.assertEqual(texts, ["Remote Slicing agent " + tray.__version__, "Online: OctoPrint at 10.147.17.100", "-",
                                 "Open Remote Slicing in OctoPrint", "Show the agent's log", "-",
                                 "Start with Windows", "-", "Exit"])
        self.assertEqual(items[6][2], set())
        self.startup.on = True
        self.assertEqual(self.ui.menu()[6][2], {"checked"})  # read again each time the menu opens

    def test_start_says_it_is_running_unless_quiet(self):
        self.ui.start()
        self.assertEqual(self.fake.balloons[0][0], "Remote Slicing agent is running")
        self.fake.balloons.clear()
        self.ui.start(quiet=True)
        self.assertEqual(self.fake.balloons, [])

    def test_a_second_launch_gets_already_running(self):
        self.ui.announce()
        self.assertEqual(self.fake.balloons[0][0], "Remote Slicing agent is already running")

    def test_open_tab_and_log(self):
        self.ui.command(tray.OPEN_TAB)
        self.ui.command(tray.SHOW_LOG)
        self.assertEqual(self.opened[0], "http://10.147.17.100/#tab_plugin_remoteslicing")
        self.assertTrue(self.opened[1].endswith("agent.log"))

    def test_start_with_windows_toggles(self):
        self.ui.command(tray.STARTUP)
        self.assertTrue(self.startup.on)
        self.ui.command(tray.STARTUP)
        self.assertFalse(self.startup.on)

    def test_start_with_windows_failure_is_shown(self):
        self.startup.fail = True
        self.ui.command(tray.STARTUP)
        self.assertIn("access denied", self.fake.told[0])

    def test_exit_stops_the_agent(self):
        self.ui.command(tray.EXIT)
        self.assertTrue(self.agent.stopped)

    def test_exit_during_a_job_asks_first(self):
        self.agent.job = {"task": {"kind": "slice"}}
        self.fake.answer = False
        self.ui.command(tray.EXIT)
        self.assertFalse(self.agent.stopped)
        self.assertIn("Slicing a plate right now", self.fake.asked)
        self.fake.answer = True
        self.ui.command(tray.EXIT)
        self.assertTrue(self.agent.stopped)


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("REMOTE_SLICING_TRAY_TEST") == "1",
                     "puts a real icon in the notification area for a second: REMOTE_SLICING_TRAY_TEST=1")
class RealTrayTests(unittest.TestCase):
    """The Win32 side against the real shell."""

    def test_icon_menu_announce_and_cleanup(self):
        commands, announced = [], threading.Event()
        items = [(0, "Remote Slicing agent test", {"disabled"}), None, (7, "Open", {"default"}),
                 (8, "Start with Windows", {"checked"})]
        icons = os.path.join(os.path.dirname(tray.__file__), "icons")
        t = tray.Tray(lambda: items, commands.append, uid=7, class_name="RemoteSlicingAgentTrayTest",
                      on_announce=announced.set)
        self.assertTrue(t.start(os.path.join(icons, "online.ico"), "test tip"))
        try:
            self.assertTrue(t.is_shown())
            t.update(os.path.join(icons, "slicing.ico"), "busy now")
            t.balloon("Remote Slicing agent test", "a balloon")
            self.assertTrue(tray.announce_running("RemoteSlicingAgentTrayTest"))
            self.assertTrue(announced.wait(3))
            self.assertFalse(tray.announce_running("NoSuchTrayClass"))

            hmenu = t.build_menu()
            user32 = t.user32
            user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
            user32.GetMenuStringW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int,
                                              ctypes.c_uint]
            user32.GetMenuState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
            user32.GetMenuDefaultItem.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
            found = []
            for i in range(user32.GetMenuItemCount(hmenu)):
                buf = ctypes.create_unicode_buffer(128)
                user32.GetMenuStringW(hmenu, i, buf, 128, 0x400)  # MF_BYPOSITION
                found.append((buf.value, user32.GetMenuState(hmenu, i, 0x400)))
            self.assertEqual([text for text, _ in found], ["Remote Slicing agent test", "", "Open",
                                                           "Start with Windows"])
            self.assertTrue(found[0][1] & tray.MF_GRAYED)
            self.assertTrue(found[1][1] & tray.MF_SEPARATOR)
            self.assertTrue(found[3][1] & tray.MF_CHECKED)
            self.assertEqual(user32.GetMenuDefaultItem(hmenu, 0, 0), 7)
            user32.DestroyMenu(hmenu)
            hwnd = t.hwnd
        finally:
            t.stop()
        self.assertIsNone(t.hwnd)
        ident = tray.NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(tray.NOTIFYICONIDENTIFIER), hWnd=hwnd, uID=7)
        rect = tray.wintypes.RECT()
        self.assertNotEqual(t.shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)), 0)


if __name__ == "__main__":
    unittest.main()
