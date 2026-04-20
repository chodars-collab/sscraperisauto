import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
from dataclasses import dataclass
from typing import Optional, List
import re

import feedparser
import requests
from bs4 import BeautifulSoup


DEFAULT_RSS_URL = "https://www.ss.lv/lv/transport/cars/rss/"
DEFAULT_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass
class CarAd:
    ad_id: str
    title: str
    link: str
    published: str
    source: str


class SSLvHybridWatcher:
    def __init__(self) -> None:
        self.seen_ids = set()
        self.is_primed = False
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT})

    def fetch_new_hybrid_ads(self, rss_url: str) -> List[CarAd]:
        feed = feedparser.parse(rss_url)
        if feed.bozo:
            raise RuntimeError(f"RSS parsing failed: {feed.bozo_exception}")

        found: List[CarAd] = []
        for entry in feed.entries:
            ad_id = self._extract_ad_id(entry)
            if not ad_id or ad_id in self.seen_ids:
                continue

            # Prime the watcher with current feed state first; track only future ads.
            if self.is_primed and self._is_hybrid(entry):
                car_ad = CarAd(
                    ad_id=ad_id,
                    title=getattr(entry, "title", "(No title)"),
                    link=getattr(entry, "link", ""),
                    published=getattr(entry, "published", ""),
                    source="feed",
                )
                found.append(car_ad)

            self.seen_ids.add(ad_id)

        self.is_primed = True
        return found

    def _extract_ad_id(self, entry) -> Optional[str]:
        link = getattr(entry, "link", "")
        if link:
            match = re.search(r"/(\d+)\.html", link)
            if match:
                return match.group(1)

        guid = getattr(entry, "id", "")
        if guid:
            return guid.strip()

        return None

    def _is_hybrid(self, entry) -> bool:
        title = (getattr(entry, "title", "") or "").lower()
        summary = (getattr(entry, "summary", "") or "").lower()
        content_blob = f"{title}\n{summary}"

        if "dzinējs" in content_blob and "hibr" in content_blob:
            return True

        link = getattr(entry, "link", "")
        if not link:
            return False

        try:
            response = self.http.get(link, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(" ", strip=True).lower()

            # Fallback heuristic when structured fields are not easy to target.
            if "dzinējs" in text and "hibr" in text:
                return True
        except requests.RequestException:
            return False

        return False


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SS.lv Hybrid Car Watcher")
        self.geometry("980x600")

        self.watcher = SSLvHybridWatcher()
        self.running = False
        self.worker_thread: Optional[threading.Thread] = None
        self.events = queue.Queue()

        self._build_ui()
        self.after(250, self._process_events)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        config = ttk.LabelFrame(root, text="Watcher settings", padding=10)
        config.pack(fill=tk.X)

        ttk.Label(config, text="RSS URL:").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.rss_var = tk.StringVar(value=DEFAULT_RSS_URL)
        self.rss_entry = ttk.Entry(config, textvariable=self.rss_var, width=90)
        self.rss_entry.grid(row=0, column=1, sticky=tk.EW, pady=4)

        ttk.Label(config, text="Check every (sec):").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_SECONDS))
        self.interval_entry = ttk.Entry(config, textvariable=self.interval_var, width=15)
        self.interval_entry.grid(row=1, column=1, sticky=tk.W, pady=4)

        button_row = ttk.Frame(config)
        button_row.grid(row=2, column=1, sticky=tk.W, pady=(8, 0))
        self.start_button = ttk.Button(button_row, text="Start", command=self.start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(button_row, text="Stop", command=self.stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        config.columnconfigure(1, weight=1)

        status_frame = ttk.Frame(root)
        status_frame.pack(fill=tk.X, pady=(10, 6))
        ttk.Label(status_frame, text="Status:").pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var)
        self.status_label.pack(side=tk.LEFT, padx=(6, 0))

        columns = ("time", "title", "published", "link")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=18)
        self.tree.heading("time", text="Detected")
        self.tree.heading("title", text="Title")
        self.tree.heading("published", text="Published")
        self.tree.heading("link", text="Link")

        self.tree.column("time", width=130, anchor=tk.W)
        self.tree.column("title", width=280, anchor=tk.W)
        self.tree.column("published", width=180, anchor=tk.W)
        self.tree.column("link", width=360, anchor=tk.W)

        self.tree.pack(fill=tk.BOTH, expand=True)

        self.tree.bind("<Double-1>", self._open_selected_link)

        footer = ttk.Label(
            root,
            text="Tip: Double-click a row to open the ad in your browser.",
            foreground="#555555",
        )
        footer.pack(anchor=tk.W, pady=(6, 0))

    def start(self) -> None:
        if self.running:
            return

        rss_url = self.rss_var.get().strip()
        if not rss_url:
            messagebox.showerror("Missing RSS URL", "Please provide an RSS URL.")
            return

        try:
            interval = int(self.interval_var.get().strip())
            if interval < 10:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid interval", "Please enter an integer >= 10 seconds.")
            return

        self.running = True
        self._set_controls_running(True)
        self.status_var.set("Running... Priming feed state.")

        self.worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(rss_url, interval),
            daemon=True,
        )
        self.worker_thread.start()

    def stop(self) -> None:
        self.running = False
        self._set_controls_running(False)
        self.status_var.set("Stopped")

    def _set_controls_running(self, running: bool) -> None:
        self.start_button.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.config(state=tk.NORMAL if running else tk.DISABLED)
        self.rss_entry.config(state=tk.DISABLED if running else tk.NORMAL)
        self.interval_entry.config(state=tk.DISABLED if running else tk.NORMAL)

    def _worker_loop(self, rss_url: str, interval: int) -> None:
        while self.running:
            try:
                ads = self.watcher.fetch_new_hybrid_ads(rss_url)
                self.events.put(("ads", ads))
            except Exception as exc:
                self.events.put(("error", str(exc)))

            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    def _process_events(self) -> None:
        while True:
            try:
                event_type, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event_type == "ads":
                ads: List[CarAd] = payload
                if ads:
                    now = time.strftime("%Y-%m-%d %H:%M:%S")
                    for ad in ads:
                        self.tree.insert("", 0, values=(now, ad.title, ad.published, ad.link))
                    self.status_var.set(f"Found {len(ads)} new hybrid ad(s).")
                else:
                    if self.watcher.is_primed:
                        self.status_var.set("No new hybrid ads in latest check.")
                    else:
                        self.status_var.set("Priming initial feed state...")
            elif event_type == "error":
                self.status_var.set(f"Error: {payload}")

        self.after(250, self._process_events)

    def _open_selected_link(self, _event) -> None:
        import webbrowser

        selected = self.tree.selection()
        if not selected:
            return

        row = self.tree.item(selected[0], "values")
        if len(row) >= 4 and row[3]:
            webbrowser.open(row[3])


if __name__ == "__main__":
    app = App()
    app.mainloop()
