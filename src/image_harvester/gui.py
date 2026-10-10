"""Small, responsive Tkinter GUI. Network work never runs on the UI thread."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .engine import Engine, Settings


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Image Harvester {__version__} — حصّاد الصور")
        self.geometry("860x690")
        self.minsize(690, 560)
        self._events: queue.Queue[dict] = queue.Queue()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_output: str | None = None
        self._closing = False
        self.url_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="page")
        self.folder_var = tk.StringVar(value=str(Path.home() / "Downloads" / "ImageHarvester"))
        self.depth_var = tk.StringVar(value="2")
        self.pages_var = tk.StringVar(value="50")
        self.images_var = tk.StringVar(value="300")
        self.image_mb_var = tk.StringVar(value="15")
        self.total_mb_var = tk.StringVar(value="250")
        self.delay_var = tk.StringVar(value="0.35")
        self.summary_var = tk.StringVar(value="صفحات: 0   |   صور مكتشفة: 0   |   محفوظة: 0   |   مكررة: 0   |   أخطاء: 0")
        self.status_var = tk.StringVar(value="جاهز. أدخل عنوان صفحة أو موقع ثم اضغط بدء.")
        self._build()
        self.mode_var.trace_add("write", lambda *_: self._mode_changed())
        self._mode_changed()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Sans", 16, "bold"))
        style.configure("Info.TLabel", font=("Sans", 9))
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)
        ttk.Label(root, text="حصّاد الصور  |  Image Harvester", style="Title.TLabel", anchor="e").grid(row=0, column=0, sticky="ew")
        ttk.Label(root, text="استخراج الصور من صفحة واحدة أو من صفحات الموقع — دون نسخ صفحات HTML", style="Info.TLabel", anchor="e").grid(row=1, column=0, sticky="ew", pady=(0, 10))
        basic = ttk.LabelFrame(root, text="المصدر ونطاق الاستخراج", padding=10)
        basic.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        basic.columnconfigure(0, weight=1)
        ttk.Label(basic, text="رابط الصفحة أو الموقع (http/https)", anchor="e").grid(row=0, column=0, sticky="ew")
        ttk.Entry(basic, textvariable=self.url_var, justify="left").grid(row=1, column=0, sticky="ew", pady=(3, 7))
        modes = ttk.Frame(basic)
        modes.grid(row=2, column=0, sticky="e")
        ttk.Radiobutton(modes, text="صفحة واحدة", value="page", variable=self.mode_var).pack(side="right", padx=8)
        ttk.Radiobutton(modes, text="موقع كامل (ضمن الحدود)", value="site", variable=self.mode_var).pack(side="right", padx=8)

        output = ttk.LabelFrame(root, text="حفظ الصور", padding=10)
        output.grid(row=3, column=0, sticky="ew", pady=(0, 7))
        output.columnconfigure(0, weight=1)
        ttk.Entry(output, textvariable=self.folder_var, justify="left").grid(row=0, column=0, sticky="ew", padx=(0, 7))
        ttk.Button(output, text="اختيار المجلد", command=self._choose_folder).grid(row=0, column=1)
        ttk.Label(output, text="كل عملية تنشئ مجلدًا مستقلًا يضم images وmanifest.json وimages.csv", style="Info.TLabel", anchor="e").grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        limits = ttk.LabelFrame(root, text="الحدود والأداء (قيم محافظة لجهاز ضعيف)", padding=10)
        limits.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        for i in range(4):
            limits.columnconfigure(i, weight=1)
        entries = [
            ("عدد الصفحات", self.pages_var), ("عمق التتبع", self.depth_var),
            ("روابط الصور", self.images_var), ("حجم الصورة (MB)", self.image_mb_var),
            ("الإجمالي (MB)", self.total_mb_var), ("تأخير الطلب (ثانية)", self.delay_var),
        ]
        self._page_fields = []
        for i, (label, variable) in enumerate(entries):
            r, c = divmod(i, 3)
            cell = ttk.Frame(limits)
            cell.grid(row=r, column=c, sticky="ew", padx=4, pady=2)
            ttk.Label(cell, text=label, anchor="e").pack(fill="x")
            field = ttk.Entry(cell, textvariable=variable, width=9, justify="center")
            field.pack(fill="x", pady=2)
            if i <= 1:
                self._page_fields.append(field)
        ttk.Label(limits, text="robots.txt مفعّل دائمًا؛ الطلبات متسلسلة لتقليل الحمل.", style="Info.TLabel", anchor="e").grid(row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0))

        activity = ttk.LabelFrame(root, text="سجل العملية", padding=8)
        activity.grid(row=5, column=0, sticky="nsew", pady=(0, 7))
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(1, weight=1)
        ttk.Label(activity, textvariable=self.summary_var, anchor="e").grid(row=0, column=0, sticky="ew", pady=(0, 5))
        logs_frame = ttk.Frame(activity)
        logs_frame.grid(row=1, column=0, sticky="nsew")
        logs_frame.columnconfigure(0, weight=1)
        logs_frame.rowconfigure(0, weight=1)
        self.log_box = tk.Text(logs_frame, wrap="word", font=("Sans", 9), state="disabled", height=7)
        self.log_box.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(logs_frame, orient="vertical", command=self.log_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(root)
        buttons.grid(row=6, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        self.start_btn = ttk.Button(buttons, text="بدء الاستخراج", command=self._start)
        self.start_btn.pack(side="right", padx=(6, 0))
        self.stop_btn = ttk.Button(buttons, text="إيقاف", command=self._stop, state="disabled")
        self.stop_btn.pack(side="right", padx=6)
        self.open_btn = ttk.Button(buttons, text="فتح مجلد النتائج", command=self._open_folder, state="disabled")
        self.open_btn.pack(side="right")
        ttk.Label(root, textvariable=self.status_var, anchor="e").grid(row=7, column=0, sticky="ew", pady=(7, 0))

    def _mode_changed(self):
        state = "normal" if self.mode_var.get() == "site" else "disabled"
        for f in self._page_fields:
            f.configure(state=state)

    def _choose_folder(self):
        choice = filedialog.askdirectory(title="اختر مجلد الحفظ", initialdir=str(Path.home()))
        if choice:
            self.folder_var.set(choice)

    def _start(self):
        if self._worker and self._worker.is_alive():
            return
        try:
            config = Settings(
                url=self.url_var.get().strip(), output_dir=self.folder_var.get().strip(),
                mode=self.mode_var.get(), max_pages=int(self.pages_var.get()),
                max_depth=int(self.depth_var.get()), max_images=int(self.images_var.get()),
                max_image_mb=int(self.image_mb_var.get()), max_total_mb=int(self.total_mb_var.get()),
                delay_seconds=float(self.delay_var.get()),
            )
            config.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("بيانات غير صحيحة", str(exc))
            return
        self._stopping = threading.Event()
        self._last_output = None
        self.open_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.summary_var.set("بدء عملية جديدة...")
        self.status_var.set("يجري استخراج الصور...")
        self._clear_log()
        self._worker = threading.Thread(target=self._run, args=(config,), daemon=True)
        self._worker.start()

    def _run(self, config: Settings):
        try:
            Engine(config, on_event=self._events.put, stop_event=self._stopping).run()
        except Exception as exc:
            self._events.put({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})

    def _stop(self):
        self._stopping.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("طلب الإيقاف قيد التنفيذ (قد يستغرق مهلة الاتصال الحالية)...")

    def _append_log(self, line: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line + "\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 350:
            self.log_box.delete("1.0", "51.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _drain(self):
        try:
            for _ in range(80):
                event = self._events.get_nowait()
                kind = event.get("kind")
                if kind == "log":
                    self._append_log(event["text"])
                elif kind == "stats":
                    stats = event["stats"]
                    self.summary_var.set(
                        f"صفحات: {stats['pages']}   |   مكتشفة: {stats['discovered']}   |   محفوظة: {stats['downloaded']}   |   "
                        f"مكررة: {stats['duplicates']}   |   أخطاء: {stats['failed']}   |   robots: {stats['robots_skipped']}"
                    )
                elif kind == "done":
                    result = event["result"]
                    self._last_output = result["output_dir"]
                    self.open_btn.configure(state="normal")
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    status = "أوقف المستخدم العملية" if result["stopped"] else "اكتملت العملية"
                    self.status_var.set(f"{status}. النتائج: {self._last_output}")
                elif kind == "error":
                    self._append_log("خطأ غير متوقع: " + event["text"])
                    self.status_var.set("توقفت العملية بخطأ. راجع السجل.")
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            if self._closing and (self._worker is None or not self._worker.is_alive()) and self._events.empty():
                self.destroy()
            else:
                self.after(100, self._drain)

    def _open_folder(self):
        if self._last_output:
            try:
                subprocess.Popen(["xdg-open", self._last_output])
            except OSError as exc:
                messagebox.showerror("تعذر فتح المجلد", str(exc))

    def _close(self):
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno("عملية جارية", "هناك عملية استخراج جارية. هل تريد إيقافها والخروج؟"):
                return
            self._stopping.set()
            self._closing = True
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.status_var.set("إنهاء التنزيل وكتابة التقرير قبل إغلاق التطبيق...")
            return
        self.destroy()


def main():
    app = MainWindow()
    app.mainloop()
