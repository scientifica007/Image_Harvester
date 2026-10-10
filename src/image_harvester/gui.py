"""GTK3/Pango Arabic RTL desktop UI. Crawling runs in a worker thread."""
from pathlib import Path
import queue
import subprocess
import threading
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk
from . import __version__
from .engine import Engine, Settings

def label(text, dim=False):
    w = Gtk.Label(label=text)
    w.set_xalign(1)
    w.set_line_wrap(True)
    if dim: w.get_style_context().add_class("dim-label")
    return w

def entry(text=""):
    w = Gtk.Entry()
    w.set_text(text)
    w.set_direction(Gtk.TextDirection.LTR)
    w.set_alignment(0)
    return w

def group(title):
    frame = Gtk.Frame(label=title)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for name in ("top", "bottom", "start", "end"):
        getattr(box, "set_margin_" + name)(12)
    frame.add(box)
    return frame, box

class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="حصّاد الصور — Image Harvester " + __version__)
        self.set_default_size(860, 705)
        self.set_size_request(680, 540)
        self.set_direction(Gtk.TextDirection.RTL)
        self.set_icon_name("image-harvester")
        self._events = queue.Queue()
        self._stopping = threading.Event()
        self._worker = None
        self._last_output = None
        self._closing = False
        self._build()
        self.connect("delete-event", self._on_close)
        GLib.timeout_add(100, self._drain)

    def _build(self):
        css = Gtk.CssProvider()
        css.load_from_data(b".title { font-size: 22px; font-weight: bold; } .metric { font-weight: bold; font-size: 16px; }")
        if Gdk.Screen.get_default():
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add(scroll)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        for side in ("start", "end", "top", "bottom"):
            getattr(root, "set_margin_" + side)(16)
        scroll.add(root)
        title = label("حصّاد الصور")
        title.get_style_context().add_class("title")
        root.pack_start(title, False, False, 0)
        root.pack_start(label("Image Harvester — تنزيل صور صفحة واحدة أو موقع كامل", True), False, False, 0)
        frame, box = group("المصدر ونطاق الاستخراج")
        root.pack_start(frame, False, False, 0)
        box.pack_start(label("رابط الصفحة أو الموقع (HTTP/HTTPS)"), False, False, 0)
        self.url_entry = entry()
        self.url_entry.set_placeholder_text("https://example.com/gallery")
        box.pack_start(self.url_entry, False, False, 0)
        self.url_entry.connect("activate", self._start)
        modes = Gtk.Box(spacing=16)
        self.page_radio = Gtk.RadioButton.new_with_label_from_widget(None, "صفحة واحدة")
        self.site_radio = Gtk.RadioButton.new_with_label_from_widget(self.page_radio, "موقع كامل (ضمن الحدود)")
        self.page_radio.set_active(True)
        self.page_radio.connect("toggled", self._mode_changed)
        modes.pack_start(self.page_radio, False, False, 0)
        modes.pack_start(self.site_radio, False, False, 0)
        box.pack_start(modes, False, False, 0)
        frame, box = group("مجلد الحفظ")
        root.pack_start(frame, False, False, 0)
        folders = Gtk.Box(spacing=8)
        self.folder_entry = entry(str(Path.home() / "Downloads" / "ImageHarvester"))
        self.folder_entry.set_hexpand(True)
        folders.pack_start(self.folder_entry, True, True, 0)
        pick = Gtk.Button(label="اختيار المجلد")
        pick.connect("clicked", self._choose_folder)
        folders.pack_start(pick, False, False, 0)
        box.pack_start(folders, False, False, 0)
        box.pack_start(label("تُحفظ الصور في مجلد مستقل مع تقريري JSON وCSV.", True), False, False, 0)
        frame, box = group("حدود الاستخراج والأداء")
        root.pack_start(frame, False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8, column_homogeneous=True)
        box.pack_start(grid, False, False, 0)
        self.limit_entries = []
        for i, (name, default) in enumerate([
            ("عدد الصفحات", "50"), ("عمق التتبع", "2"), ("روابط الصور", "300"),
            ("حجم الصورة (MB)", "15"), ("الإجمالي (MB)", "250"), ("تأخير الطلب (ثانية)", "0.35"),
        ]):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            cell.pack_start(label(name), False, False, 0)
            field = entry(default)
            field.set_width_chars(8)
            cell.pack_start(field, False, False, 0)
            grid.attach(cell, i % 3, i // 3, 1, 1)
            self.limit_entries.append(field)
        box.pack_start(label("تُراعى قواعد robots.txt، والطلبات متسلسلة لتقليل الحمل.", True), False, False, 0)
        frame, box = group("الحالة والسجل")
        root.pack_start(frame, True, True, 0)
        stats = Gtk.Grid(column_spacing=10, row_spacing=5, column_homogeneous=True)
        box.pack_start(stats, False, False, 0)
        self.stats_labels = {}
        for i, (key, name) in enumerate([
            ("pages", "صفحات"), ("discovered", "مكتشفة"), ("downloaded", "محفوظة"),
            ("duplicates", "مكررة"), ("failed", "أخطاء"), ("robots_skipped", "ممنوعة"),
        ]):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            count = label("0")
            count.set_xalign(.5)
            count.get_style_context().add_class("metric")
            tag = label(name, True)
            tag.set_xalign(.5)
            cell.pack_start(count, False, False, 0)
            cell.pack_start(tag, False, False, 0)
            stats.attach(cell, i % 3, i // 3, 1, 1)
            self.stats_labels[key] = count
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroll.set_min_content_height(90)
        log_scroll.set_vexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_direction(Gtk.TextDirection.RTL)
        self.log_view.set_justification(Gtk.Justification.RIGHT)
        self.log_buffer = self.log_view.get_buffer()
        log_scroll.add(self.log_view)
        box.pack_start(log_scroll, True, True, 0)
        actions = Gtk.Box(spacing=8)
        root.pack_start(actions, False, False, 0)
        self.start_btn = Gtk.Button(label="بدء الاستخراج")
        self.start_btn.get_style_context().add_class("suggested-action")
        self.start_btn.connect("clicked", self._start)
        actions.pack_start(self.start_btn, False, False, 0)
        self.stop_btn = Gtk.Button(label="إيقاف")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", self._stop)
        actions.pack_start(self.stop_btn, False, False, 0)
        self.open_btn = Gtk.Button(label="فتح مجلد النتائج")
        self.open_btn.set_sensitive(False)
        self.open_btn.connect("clicked", self._open_folder)
        actions.pack_start(self.open_btn, False, False, 0)
        self.status_label = label("جاهز. أدخل رابط الموقع ثم اضغط «بدء الاستخراج».", True)
        root.pack_start(self.status_label, False, False, 0)
        self._mode_changed()

    def _mode_changed(self, *_):
        for field in self.limit_entries[:2]:
            field.set_sensitive(self.site_radio.get_active())

    def _choose_folder(self, *_):
        dialog = Gtk.FileChooserDialog(title="اختر مجلد الحفظ", parent=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("إلغاء", Gtk.ResponseType.CANCEL, "اختيار", Gtk.ResponseType.OK)
        try:
            if dialog.run() == Gtk.ResponseType.OK and dialog.get_filename():
                self.folder_entry.set_text(dialog.get_filename())
        finally:
            dialog.destroy()

    def _alert(self, title, message, question=False):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION if question else Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.YES_NO if question else Gtk.ButtonsType.OK, text=title)
        dialog.format_secondary_text(message)
        try:
            result = dialog.run()
            return result == Gtk.ResponseType.YES if question else True
        finally:
            dialog.destroy()

    def _start(self, *_):
        if self._worker and self._worker.is_alive(): return
        try:
            v = [f.get_text().strip() for f in self.limit_entries]
            config = Settings(url=self.url_entry.get_text().strip(),
                output_dir=self.folder_entry.get_text().strip(),
                mode="page" if self.page_radio.get_active() else "site",
                max_pages=int(v[0]), max_depth=int(v[1]), max_images=int(v[2]),
                max_image_mb=int(v[3]), max_total_mb=int(v[4]), delay_seconds=float(v[5]))
            config.validate()
        except (ValueError, TypeError) as exc:
            self._alert("بيانات غير صحيحة", str(exc))
            return
        self._stopping = threading.Event()
        self._last_output = None
        self.open_btn.set_sensitive(False)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        for widget in self.stats_labels.values(): widget.set_text("0")
        self.status_label.set_text("يجري استخراج الصور...")
        self.log_buffer.set_text("")
        self._worker = threading.Thread(target=self._run, args=(config,), daemon=True)
        self._worker.start()

    def _run(self, config):
        try:
            Engine(config, on_event=self._events.put, stop_event=self._stopping).run()
        except Exception as exc:
            self._events.put({"kind": "error", "text": str(exc)})

    def _stop(self, *_):
        self._stopping.set()
        self.stop_btn.set_sensitive(False)
        self.status_label.set_text("يجري الإيقاف وكتابة التقرير...")

    def _append_log(self, line):
        count = self.log_buffer.get_line_count()
        if count > 350:
            self.log_buffer.delete(self.log_buffer.get_start_iter(),
                self.log_buffer.get_iter_at_line(min(50, count - 1)))
        self.log_buffer.insert(self.log_buffer.get_end_iter(), line + "\n")

    def _drain(self):
        try:
            for _ in range(80):
                event = self._events.get_nowait()
                kind = event.get("kind")
                if kind == "log":
                    self._append_log(event["text"])
                elif kind == "stats":
                    for key, widget in self.stats_labels.items():
                        widget.set_text(str(event["stats"][key]))
                elif kind == "done":
                    result = event["result"]
                    self._last_output = result["output_dir"]
                    self.open_btn.set_sensitive(True)
                    self.start_btn.set_sensitive(True)
                    self.stop_btn.set_sensitive(False)
                    outcome = "أوقف المستخدم العملية" if result["stopped"] else "اكتملت العملية"
                    self.status_label.set_text(outcome + ". يمكنك فتح مجلد النتائج.")
                elif kind == "error":
                    self._append_log("خطأ: " + event["text"])
                    self.status_label.set_text("توقفت العملية بسبب خطأ. راجع السجل.")
                    self.start_btn.set_sensitive(True)
                    self.stop_btn.set_sensitive(False)
        except queue.Empty:
            pass
        if self._closing and (not self._worker or not self._worker.is_alive()) and self._events.empty():
            Gtk.main_quit()
            return False
        return True

    def _open_folder(self, *_):
        if self._last_output:
            try:
                subprocess.Popen(["xdg-open", self._last_output])
            except OSError as exc:
                self._alert("تعذر فتح المجلد", str(exc))

    def _on_close(self, *_):
        if self._worker and self._worker.is_alive():
            if self._closing: return True
            if not self._alert("عملية جارية", "هل تريد إيقاف التنزيل والخروج بعد كتابة التقرير؟", True):
                return True
            self._stopping.set()
            self._closing = True
            self.start_btn.set_sensitive(False)
            self.stop_btn.set_sensitive(False)
            self.status_label.set_text("يجري إنهاء التنزيل قبل الإغلاق...")
            return True
        Gtk.main_quit()
        return False

def main():
    Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
    window = MainWindow()
    window.show_all()
    Gtk.main()
