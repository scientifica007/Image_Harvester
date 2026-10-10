import os, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except (ImportError, ValueError):
    Gtk = None

@unittest.skipUnless(Gtk is not None and os.environ.get("DISPLAY"), "GTK display required")
class ArabicUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        from image_harvester.gui import MainWindow
        cls.Win = MainWindow

    def setUp(self):
        self.win = self.Win()

    def tearDown(self):
        self.win.destroy()

    def test_rtl_and_ltr(self):
        self.assertEqual(self.win.get_direction(), Gtk.TextDirection.RTL)
        self.assertEqual(self.win.url_entry.get_direction(), Gtk.TextDirection.LTR)
        self.assertEqual(self.win.folder_entry.get_direction(), Gtk.TextDirection.LTR)
        self.assertEqual(self.win.status_label.get_direction(), Gtk.TextDirection.RTL)

    def test_modes_and_stats(self):
        self.assertFalse(self.win.limit_entries[0].get_sensitive())
        self.win.site_radio.set_active(True)
        self.assertTrue(self.win.limit_entries[0].get_sensitive())
        self.assertEqual(len(self.win.stats_labels), 6)
        self.win._events.put({"kind": "stats", "stats": {
            "pages": 2, "discovered": 6, "downloaded": 3,
            "duplicates": 1, "failed": 0, "robots_skipped": 0
        }})
        self.win._drain()
        self.assertEqual(self.win.stats_labels["downloaded"].get_text(), "3")
