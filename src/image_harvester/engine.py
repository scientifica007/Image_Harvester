"""Bounded, robots-aware image discovery and download engine (stdlib only)."""
from __future__ import annotations

import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
import re
import threading
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

USER_AGENT = "ImageHarvester/0.1.0 (+local desktop image downloader)"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".bmp", ".ico", ".tif", ".tiff", ".apng", ".heic", ".heif"}
BLOCKED_PAGE_SUFFIXES = {".pdf", ".zip", ".gz", ".mp4", ".webm", ".mov", ".mp3", ".css", ".js", ".xml", ".json", ".woff", ".woff2", ".exe", ".deb"}
MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg", "image/avif": ".avif", "image/bmp": ".bmp", "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico", "image/tiff": ".tiff", "image/apng": ".apng", "image/heic": ".heic", "image/heif": ".heif"}
CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?['\"]([^'\"]+\.css(?:\?[^'\"]*)?)['\"]\s*\)?", re.I)
DATA_ATTRIBUTES = ("data-src", "data-original", "data-lazy-src", "data-actualsrc", "data-image", "data-background", "data-bg", "data-bg-src", "data-zoom-image", "data-full", "data-hires")


@dataclass(frozen=True)
class Settings:
    url: str
    output_dir: str
    mode: str = "page"  # page or site
    max_depth: int = 2
    max_pages: int = 50
    max_images: int = 300  # number of unique discovered image URLs
    max_image_mb: int = 15
    max_total_mb: int = 250
    delay_seconds: float = 0.35
    timeout_seconds: int = 12
    respect_robots: bool = True

    def validate(self) -> None:
        parts = urlsplit(self.url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("يرجى إدخال عنوان HTTP أو HTTPS صحيح، دون بيانات دخول في الرابط.")
        if self.mode not in ("page", "site"):
            raise ValueError("وضع الاستخراج غير معروف.")
        if not 0 <= self.max_depth <= 6:
            raise ValueError("عمق التتبع يجب أن يكون من 0 إلى 6.")
        if not 1 <= self.max_pages <= 500:
            raise ValueError("الحد الأقصى للصفحات يجب أن يكون من 1 إلى 500.")
        if not 1 <= self.max_images <= 3000:
            raise ValueError("الحد الأقصى للصور يجب أن يكون من 1 إلى 3000.")
        if not 1 <= self.max_image_mb <= 100 or not 1 <= self.max_total_mb <= 5000:
            raise ValueError("حدود أحجام الصور غير صالحة.")
        if not 0 <= self.delay_seconds <= 10 or not 2 <= self.timeout_seconds <= 120:
            raise ValueError("التأخير أو المهلة غير صالحين.")
        if not self.output_dir.strip():
            raise ValueError("حدد مجلد حفظ النتائج.")


@dataclass
class Stats:
    pages: int = 0
    discovered: int = 0
    downloaded: int = 0
    duplicates: int = 0
    failed: int = 0
    robots_skipped: int = 0
    bytes_saved: int = 0


@dataclass
class RunResult:
    output_dir: str
    stats: Stats
    stopped: bool
    records: list[dict] = field(default_factory=list)


def url_normalize(base: str, raw: str) -> str | None:
    """Canonicalize enough to deduplicate references; retain query parameters."""
    value = html.unescape((raw or "").strip().strip("'\""))
    if not value or value.startswith(("#", "data:", "blob:", "javascript:", "mailto:", "tel:")):
        return None
    try:
        absolute = urljoin(base, value)
        p = urlsplit(absolute)
        if p.scheme.lower() not in ("http", "https") or not p.hostname or p.username or p.password:
            return None
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))
    except ValueError:
        return None


def site_hostname(host: str) -> str:
    return host.lower().removeprefix("www.")


def is_site_page(url: str, seed: str) -> bool:
    try:
        a, b = urlsplit(url), urlsplit(seed)
        return (site_hostname(a.hostname or "") == site_hostname(b.hostname or "")
                and (a.port == b.port if b.port is not None else a.port is None))
    except ValueError:
        return False


def is_image_link(url: str) -> bool:
    return Path(urlsplit(url).path.lower()).suffix in IMAGE_SUFFIXES


def is_potential_image(url: str) -> bool:
    # Retain unknown endpoint paths (CDN/image?id=123) while discarding obvious assets.
    suffix = Path(urlsplit(url).path.lower()).suffix
    return suffix not in (BLOCKED_PAGE_SUFFIXES | {".ttf", ".otf", ".eot", ".map", ".m3u8"})


def is_navigation_link(url: str) -> bool:
    suffix = Path(urlsplit(url).path.lower()).suffix
    return suffix not in BLOCKED_PAGE_SUFFIXES and suffix not in IMAGE_SUFFIXES


def css_urls(content: str, base: str) -> tuple[list[str], list[str]]:
    imports = [u for m in CSS_IMPORT.finditer(content) if (u := url_normalize(base, m.group(1)))]
    other = []
    for match in CSS_URL.finditer(content):
        val = url_normalize(base, match.group(2))
        if val and val not in imports:
            other.append(val)
    return other, imports


def srcset_urls(srcset: str) -> list[str]:
    # Standard srcset candidates are whitespace separated after each comma. This
    # parser supports the common URL + width/density cases; data: URIs are skipped.
    return [item.strip().split()[0] for item in srcset.split(",") if item.strip()]


class PageParser(HTMLParser):
    """Collect image/CSS/navigation references, including common lazy attributes."""
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.images: list[str] = []
        self.links: list[str] = []
        self.stylesheets: list[str] = []
        self._in_style = False
        self._style_parts: list[str] = []

    def _url(self, value: str | None) -> str | None:
        return url_normalize(self.base_url, value or "")

    def _image(self, value: str | None) -> None:
        u = self._url(value)
        if u and is_potential_image(u):
            self.images.append(u)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and attrs_d.get("href"):
            self.base_url = self._url(attrs_d["href"]) or self.base_url
        if tag == "style":
            self._in_style = True
        if "style" in attrs_d:
            images, _ = css_urls(attrs_d["style"], self.base_url)
            self.images.extend(u for u in images if is_potential_image(u))
        if tag in ("img", "source"):
            for attr in ("src", "srcset", "data-srcset", "data-lazy-srcset"):
                if attr in attrs_d:
                    if "srcset" in attr:
                        for src in srcset_urls(attrs_d[attr]):
                            self._image(src)
                    else:
                        self._image(attrs_d[attr])
            for attr in DATA_ATTRIBUTES:
                if attr in attrs_d:
                    self._image(attrs_d[attr])
        if tag == "video":
            self._image(attrs_d.get("poster"))
        if tag in ("body", "table", "td", "div", "section", "a", "span"):
            if "background" in attrs_d:
                self._image(attrs_d["background"])
            for attr in DATA_ATTRIBUTES:
                if attr in attrs_d:
                    value = attrs_d[attr]
                    if value.startswith("url("):
                        urls, _ = css_urls(value, self.base_url)
                        self.images.extend(u for u in urls if is_potential_image(u))
                    else:
                        self._image(value)
        if tag == "image":  # SVG <image href="...">
            self._image(attrs_d.get("href") or attrs_d.get("xlink:href"))
        if tag == "meta":
            prop = (attrs_d.get("property") or attrs_d.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src", "image"):
                self._image(attrs_d.get("content"))
        if tag == "link":
            rel = attrs_d.get("rel", "").lower().split()
            href = self._url(attrs_d.get("href"))
            if href:
                if "stylesheet" in rel:
                    self.stylesheets.append(href)
                elif any(r in rel for r in ("icon", "apple-touch-icon", "image_src")) or ("preload" in rel and attrs_d.get("as", "").lower() == "image"):
                    self.images.append(href)
                if "preload" in rel and "imagesrcset" in attrs_d:
                    for src in srcset_urls(attrs_d["imagesrcset"]):
                        self._image(src)
        if tag == "a":
            target = self._url(attrs_d.get("href"))
            if target:
                if is_image_link(target):
                    self.images.append(target)
                elif is_navigation_link(target):
                    self.links.append(target)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_parts.append(data)

    def finish(self) -> None:
        if self._style_parts:
            css = "\n".join(self._style_parts)
            urls, _ = css_urls(css, self.base_url)
            self.images.extend(u for u in urls if is_potential_image(u))


class StopRequested(Exception):
    pass


class DownloadLimit(Exception):
    pass


class Engine:
    def __init__(self, settings: Settings, on_event: Callable[[dict], None] | None = None, stop_event: threading.Event | None = None):
        settings.validate()
        self.settings = settings
        self.emit = on_event or (lambda _: None)
        self.stop_event = stop_event or threading.Event()
        self.stats = Stats()
        self.records: list[dict] = []
        self._seen_images: set[str] = set()
        self._seen_styles: set[str] = set()
        self._seen_sha: set[str] = set()
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request_time: float | None = None
        self._folder: Path | None = None

    def log(self, message: str) -> None:
        self.emit({"kind": "log", "text": message})

    def update(self) -> None:
        self.emit({"kind": "stats", "stats": asdict(self.stats)})

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested

    def _request(self, url: str):
        import time
        self.check_stop()
        if self._last_request_time is not None:
            wait = self.settings.delay_seconds - (time.monotonic() - self._last_request_time)
            if wait > 0 and self.stop_event.wait(wait):
                raise StopRequested
        self.check_stop()
        self._last_request_time = time.monotonic()
        return urlopen(Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}), timeout=self.settings.timeout_seconds)

    def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = RobotFileParser()
            parser.set_url(origin + "/robots.txt")
            try:
                with self._request(origin + "/robots.txt") as response:
                    data = response.read(512 * 1024 + 1)
                    if len(data) > 512 * 1024:
                        self.log(f"ملف robots.txt كبير جدًا، تم منع الزحف لهذا المضيف: {origin}")
                        self._robots[origin] = None
                        return False
                parser.parse(data.decode("utf-8", errors="replace").splitlines())
                self._robots[origin] = parser
            except HTTPError as exc:
                if exc.code in (404, 410):
                    parser.parse([])  # no robots.txt: allow
                    self._robots[origin] = parser
                else:
                    self.log(f"تعذر التحقق من robots.txt ({exc.code})؛ تم تخطي المضيف: {origin}")
                    self._robots[origin] = None
            except (URLError, TimeoutError, OSError) as exc:
                self.log(f"تعذر قراءة robots.txt؛ تم تخطي المضيف {origin}: {exc}")
                self._robots[origin] = None
        rp = self._robots[origin]
        permitted = rp is not None and rp.can_fetch(USER_AGENT, url)
        if not permitted:
            self.stats.robots_skipped += 1
        return permitted

    def _read_text(self, url: str, cap: int, expected: str) -> tuple[str, str] | None:
        if not self.allowed(url):
            return None
        try:
            with self._request(url) as r:
                final = r.geturl()
                if expected == "html" and not is_site_page(final, self.settings.url):
                    self.log(f"تم تخطي إعادة توجيه خارج الموقع: {final}")
                    return None
                if self.settings.respect_robots and final != url and not self.allowed(final):
                    return None
                mime = r.headers.get_content_type().lower()
                if expected == "html" and mime not in ("text/html", "application/xhtml+xml"):
                    if mime.startswith("image/"):
                        self._download_image(final)
                    else:
                        self.log(f"تم تخطي صفحة غير HTML: {url}")
                    return None
                if expected == "css" and mime not in ("text/css", "text/plain", "application/octet-stream"):
                    return None
                data = r.read(cap + 1)
                if len(data) > cap:
                    self.log(f"تم تخطي ملف نصي يتجاوز حد المعالجة: {url}")
                    return None
                charset = r.headers.get_content_charset() or "utf-8"
                try:
                    return data.decode(charset, errors="replace"), final
                except LookupError:
                    return data.decode("utf-8", errors="replace"), final
        except StopRequested:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self.log(f"تعذر قراءة المورد: {url} ({exc})")
            return None

    def _collect_stylesheet(self, url: str, add_image: Callable[[str, str], None], max_import_depth: int = 2) -> None:
        todo = deque([(url, 0)])
        while todo and len(self._seen_styles) < 60:
            self.check_stop()
            current, depth = todo.popleft()
            if current in self._seen_styles:
                continue
            self._seen_styles.add(current)
            data = self._read_text(current, 1024 * 1024, "css")
            if not data:
                continue
            content, final = data
            images, imports = css_urls(content, final)
            for item in images:
                if is_potential_image(item):
                    add_image(item, final)
            if depth < max_import_depth:
                for item in imports:
                    todo.append((item, depth + 1))

    def _download_image(self, url: str, source: str = "") -> None:
        self.check_stop()
        if not self.allowed(url):
            self.log(f"ممنوع بواسطة robots.txt: {url}")
            return
        assert self._folder is not None
        part = self._folder / "images" / (".incoming-" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".part")
        max_image = self.settings.max_image_mb * 1024 * 1024
        max_total = self.settings.max_total_mb * 1024 * 1024
        h = hashlib.sha256()
        size = 0
        first = b""
        mime = ""
        final = url
        try:
            with self._request(url) as r:
                final = r.geturl()
                if self.settings.respect_robots and final != url and not self.allowed(final):
                    return
                mime = r.headers.get_content_type().lower()
                if not (mime.startswith("image/") or mime == "application/octet-stream"):
                    self.log(f"ليس صورة (Content-Type={mime}): {url}")
                    self.stats.failed += 1
                    return
                try:
                    declared = int(r.headers.get("Content-Length", "0"))
                except ValueError:
                    declared = 0
                if declared > max_image or self.stats.bytes_saved + declared > max_total:
                    self.log(f"الصورة تتجاوز الحجم المسموح: {url}")
                    self.stats.failed += 1
                    return
                with part.open("wb") as f:
                    while True:
                        self.check_stop()
                        chunk = r.read(64 * 1024)
                        if not chunk:
                            break
                        if not first:
                            first = chunk[:512]
                        size += len(chunk)
                        if size > max_image or self.stats.bytes_saved + size > max_total:
                            raise DownloadLimit("تم تجاوز حد حجم صورة أو إجمالي التنزيل")
                        f.write(chunk)
                        h.update(chunk)
            actual_mime = _detect_image_mime(first)
            if not actual_mime:
                self.stats.failed += 1
                self.log(f"تم رفض ملف لا يحتوي على صورة معروفة: {url}")
                return
            digest = h.hexdigest()
            if digest in self._seen_sha:
                self.stats.duplicates += 1
                self.records.append({"url": url, "source": source, "file": "", "size_bytes": size, "sha256": digest, "status": "duplicate"})
                return
            self._seen_sha.add(digest)
            suffix = MIME_EXT.get(actual_mime, ".img")
            raw_name = unquote(Path(urlsplit(final).path).stem) or "image"
            stem = re.sub(r"[^\w.-]", "_", raw_name, flags=re.UNICODE).strip("._")[:65] or "image"
            final_name = f"{self.stats.downloaded + 1:05d}-{stem}-{digest[:8]}{suffix}"
            final_path = self._folder / "images" / final_name
            part.replace(final_path)
            self.stats.downloaded += 1
            self.stats.bytes_saved += size
            self.records.append({"url": url, "source": source, "file": str(Path("images") / final_name), "size_bytes": size, "sha256": digest, "status": "saved"})
            self.log(f"حُفظت الصورة: {final_name}")
        except StopRequested:
            raise
        except DownloadLimit as exc:
            self.stats.failed += 1
            self.log(f"{exc}: {url}")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self.stats.failed += 1
            self.log(f"فشل تنزيل الصورة: {url} ({exc})")
        finally:
            part.unlink(missing_ok=True)
            self.update()

    def _make_folder(self) -> Path:
        parent = Path(self.settings.output_dir).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        hostname = re.sub(r"[^a-zA-Z0-9_-]", "_", urlsplit(self.settings.url).hostname or "site")[:70]
        base = hostname + "_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = parent / base
        counter = 2
        while folder.exists():
            folder = parent / f"{base}_{counter}"
            counter += 1
        (folder / "images").mkdir(parents=True)
        return folder

    def _write_manifest(self, stopped: bool) -> None:
        assert self._folder is not None
        metadata = {"application": "Image Harvester", "version": "0.1.0", "timestamp": datetime.now().isoformat(timespec="seconds"), "settings": asdict(self.settings), "statistics": asdict(self.stats), "stopped": stopped, "records": self.records}
        (self._folder / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        with (self._folder / "images.csv").open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=("url", "source", "file", "size_bytes", "sha256", "status"))
            writer.writeheader()
            writer.writerows(self.records)

    def run(self) -> RunResult:
        s = self.settings
        seed = url_normalize(s.url, s.url)
        assert seed is not None
        self._folder = self._make_folder()
        self.log(f"مجلد النتائج: {self._folder}")
        self.log("يجري الالتزام بـ robots.txt؛ لا يُشغّل JavaScript في هذا الإصدار.")
        pages = deque([(seed, 0)])
        visited: set[str] = set()
        stopped = False

        def add_image(url: str, source: str) -> None:
            if url in self._seen_images or len(self._seen_images) >= s.max_images:
                return
            self._seen_images.add(url)
            self.stats.discovered = len(self._seen_images)
            self.update()
            self._download_image(url, source)

        try:
            while pages and len(visited) < (1 if s.mode == "page" else s.max_pages):
                self.check_stop()
                page, depth = pages.popleft()
                if page in visited or not is_site_page(page, seed):
                    continue
                visited.add(page)
                content = self._read_text(page, 4 * 1024 * 1024, "html")
                if not content:
                    continue
                doc, actual_page = content
                self.stats.pages += 1
                self.log(f"تحليل الصفحة {self.stats.pages}: {actual_page}")
                self.update()
                parsed = PageParser(actual_page)
                parsed.feed(doc)
                parsed.finish()
                for u in parsed.images:
                    add_image(u, actual_page)
                for css in parsed.stylesheets:
                    self._collect_stylesheet(css, add_image)
                if s.mode == "site" and depth < s.max_depth:
                    for link in parsed.links:
                        if is_site_page(link, seed) and link not in visited:
                            pages.append((link, depth + 1))
                if len(self._seen_images) >= s.max_images:
                    self.log("بلوغ الحد الأقصى للصور المكتشفة.")
                    break
        except StopRequested:
            stopped = True
            self.log("أوقِفت العملية بناءً على طلب المستخدم.")
        finally:
            self._write_manifest(stopped)
            self.update()
        result = RunResult(str(self._folder), self.stats, stopped, self.records)
        self.emit({"kind": "done", "result": {"output_dir": result.output_dir, "stats": asdict(result.stats), "stopped": stopped}})
        return result


def _detect_image_mime(first: bytes) -> str | None:
    if first.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if first.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if first.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if first.startswith(b"RIFF") and first[8:12] == b"WEBP":
        return "image/webp"
    if first.startswith(b"BM"):
        return "image/bmp"
    if first.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if first.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(first) >= 12 and first[4:8] == b"ftyp":
        if first[8:12] in (b"avif", b"avis"):
            return "image/avif"
        if first[8:12] in (b"heic", b"heix", b"mif1", b"hevc"):
            return "image/heic"
    if first.lstrip().startswith((b"<svg", b"<?xml")) and b"<svg" in first.lower():
        return "image/svg+xml"
    return None
