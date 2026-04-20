import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, List
import re
import unicodedata

import feedparser
import requests
from bs4 import BeautifulSoup


DEFAULT_RSS_URL = "https://www.ss.lv/lv/transport/cars/rss/"
DEFAULT_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass
class FilterSettings:
    model_query: str
    min_price: Optional[int]
    max_price: Optional[int]
    min_year: Optional[int]
    max_year: Optional[int]
    allow_today: bool
    allow_yesterday: bool


@dataclass
class CarAd:
    ad_id: str
    title: str
    link: str
    published: str
    source: str
    model: str
    year: Optional[int]
    price_eur: Optional[int]
    published_date: Optional[date]


class SSLvHybridWatcher:
    def __init__(self) -> None:
        self.seen_ids = set()
        self.is_primed = False
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT})

    def fetch_new_hybrid_ads(self, rss_url: str, filters: FilterSettings) -> List[CarAd]:
        feed = feedparser.parse(rss_url)
        if feed.bozo:
            raise RuntimeError(f"RSS parsing failed: {feed.bozo_exception}")

        found: List[CarAd] = []
        for entry in feed.entries:
            ad_id = self._extract_ad_id(entry)
            if not ad_id or ad_id in self.seen_ids:
                continue

            if self.is_primed and self._is_hybrid(entry):
                car_ad = self._build_car_ad(entry, ad_id)
                if self._passes_filters(car_ad, filters):
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

    def _normalize_text(self, text: str) -> str:
        decomposed = unicodedata.normalize("NFKD", text)
        return "".join(char for char in decomposed if not unicodedata.combining(char))

    def _is_hybrid(self, entry) -> bool:
        title = (getattr(entry, "title", "") or "").lower()
        summary = (getattr(entry, "summary", "") or "").lower()
        content_blob = self._normalize_text(f"{title}\n{summary}")

        if "dzin" in content_blob and "hibr" in content_blob:
            return True

        link = getattr(entry, "link", "")
        if not link:
            return False

        try:
            response = self.http.get(link, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            text = self._normalize_text(soup.get_text(" ", strip=True).lower())

            if "dzin" in text and "hibr" in text:
                return True
        except requests.RequestException:
            return False

        return False

    def _build_car_ad(self, entry, ad_id: str) -> CarAd:
        title = getattr(entry, "title", "(No title)")
        summary = getattr(entry, "summary", "")
        published = getattr(entry, "published", "")
        text_blob = f"{title} {summary}".strip()

        return CarAd(
            ad_id=ad_id,
            title=title,
            link=getattr(entry, "link", ""),
            published=published,
            source="feed",
            model=self._extract_model(title),
            year=self._extract_year(text_blob),
            price_eur=self._extract_price_eur(text_blob),
            published_date=self._extract_published_date(entry, published),
        )

    def _extract_model(self, title: str) -> str:
        first_part = (title or "").split(",", 1)[0].strip()
        return first_part or "Unknown"

    def _extract_year(self, text: str) -> Optional[int]:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
        if not match:
            return None
        return int(match.group(1))

    def _extract_price_eur(self, text: str) -> Optional[int]:
        text_lower = self._normalize_text(text.lower()).replace("€", " eur ")
        euro_match = re.search(r"(\d[\d\s]{2,})\s*(?:eur|euro)\b", text_lower)
        if not euro_match:
            euro_match = re.search(r"\bcena\s*[:\-]?\s*(\d[\d\s]{2,})\b", text_lower)

        if not euro_match:
            return None

        value = re.sub(r"\D", "", euro_match.group(1))
        if not value:
            return None

        return int(value)

    def _extract_published_date(self, entry, published: str) -> Optional[date]:
        if getattr(entry, "published_parsed", None):
            parsed = entry.published_parsed
            return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)

        match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", published)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            year = int(match.group(3))
            try:
                return date(year, month, day)
            except ValueError:
                return None

        return None

    def _passes_filters(self, ad: CarAd, filters: FilterSettings) -> bool:
        if filters.model_query:
            model_query = filters.model_query.lower()
            if model_query not in ad.model.lower() and model_query not in ad.title.lower():
                return False

        if filters.min_price is not None:
            if ad.price_eur is None or ad.price_eur < filters.min_price:
                return False

        if filters.max_price is not None:
            if ad.price_eur is None or ad.price_eur > filters.max_price:
                return False

        if filters.min_year is not None:
            if ad.year is None or ad.year < filters.min_year:
                return False

        if filters.max_year is not None:
            if ad.year is None or ad.year > filters.max_year:
                return False

        if filters.allow_today or filters.allow_yesterday:
            if ad.published_date is None:
                return False

            today = date.today()
            yesterday = today - timedelta(days=1)
            allowed_dates = set()
            if filters.allow_today:
                allowed_dates.add(today)
            if filters.allow_yesterday:
                allowed_dates.add(yesterday)

            if ad.published_date not in allowed_dates:
                return False

        return True


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SS.lv Hybrid Car Watcher")
        self.geometry("1220x660")

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
        self.rss_entry = ttk.Entry(config, textvariable=self.rss_var, width=100)
        self.rss_entry.grid(row=0, column=1, columnspan=5, sticky=tk.EW, pady=4)

        ttk.Label(config, text="Check every (sec):").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_SECONDS))
        self.interval_entry = ttk.Entry(config, textvariable=self.interval_var, width=10)
        self.interval_entry.grid(row=1, column=1, sticky=tk.W, pady=4)

        ttk.Label(config, text="Model contains:").grid(row=1, column=2, sticky=tk.W, padx=(16, 8), pady=4)
        self.model_var = tk.StringVar(value="")
        self.model_entry = ttk.Entry(config, textvariable=self.model_var, width=24)
        self.model_entry.grid(row=1, column=3, sticky=tk.W, pady=4)

        ttk.Label(config, text="Price EUR:").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.min_price_var = tk.StringVar(value="")
        self.max_price_var = tk.StringVar(value="")
        self.min_price_entry = ttk.Entry(config, textvariable=self.min_price_var, width=10)
        self.max_price_entry = ttk.Entry(config, textvariable=self.max_price_var, width=10)
        self.min_price_entry.grid(row=2, column=1, sticky=tk.W, pady=4)
        ttk.Label(config, text="to").grid(row=2, column=2, sticky=tk.W, pady=4)
        self.max_price_entry.grid(row=2, column=3, sticky=tk.W, pady=4)

        ttk.Label(config, text="Year:").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.min_year_var = tk.StringVar(value="")
        self.max_year_var = tk.StringVar(value="")
        self.min_year_entry = ttk.Entry(config, textvariable=self.min_year_var, width=10)
        self.max_year_entry = ttk.Entry(config, textvariable=self.max_year_var, width=10)
        self.min_year_entry.grid(row=3, column=1, sticky=tk.W, pady=4)
        ttk.Label(config, text="to").grid(row=3, column=2, sticky=tk.W, pady=4)
        self.max_year_entry.grid(row=3, column=3, sticky=tk.W, pady=4)

        ttk.Label(config, text="Added date:").grid(row=4, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.today_var = tk.BooleanVar(value=True)
        self.yesterday_var = tk.BooleanVar(value=True)
        self.today_check = ttk.Checkbutton(config, text="Today", variable=self.today_var)
        self.yesterday_check = ttk.Checkbutton(config, text="Yesterday", variable=self.yesterday_var)
        self.today_check.grid(row=4, column=1, sticky=tk.W, pady=4)
        self.yesterday_check.grid(row=4, column=2, sticky=tk.W, pady=4)

        button_row = ttk.Frame(config)
        button_row.grid(row=5, column=1, sticky=tk.W, pady=(8, 0))
        self.start_button = ttk.Button(button_row, text="Start", command=self.start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(button_row, text="Stop", command=self.stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        config.columnconfigure(5, weight=1)

        status_frame = ttk.Frame(root)
        status_frame.pack(fill=tk.X, pady=(10, 6))
        ttk.Label(status_frame, text="Status:").pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var)
        self.status_label.pack(side=tk.LEFT, padx=(6, 0))

        columns = ("time", "model", "year", "price", "published", "title", "link")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", height=18)
        self.tree.heading("time", text="Detected")
        self.tree.heading("model", text="Model")
        self.tree.heading("year", text="Year")
        self.tree.heading("price", text="Price (EUR)")
        self.tree.heading("published", text="Published")
        self.tree.heading("title", text="Title")
        self.tree.heading("link", text="Link")

        self.tree.column("time", width=135, anchor=tk.W)
        self.tree.column("model", width=160, anchor=tk.W)
        self.tree.column("year", width=70, anchor=tk.W)
        self.tree.column("price", width=95, anchor=tk.W)
        self.tree.column("published", width=175, anchor=tk.W)
        self.tree.column("title", width=280, anchor=tk.W)
        self.tree.column("link", width=300, anchor=tk.W)

        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self._open_selected_link)

        footer = ttk.Label(
            root,
            text="Tip: Double-click a row to open the ad in your browser.",
            foreground="#555555",
        )
        footer.pack(anchor=tk.W, pady=(6, 0))

    def _parse_optional_int(self, value: str, label: str) -> Optional[int]:
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.isdigit():
            raise ValueError(f"{label} must be a whole number.")
        return int(cleaned)

    def _build_filters(self) -> FilterSettings:
        min_price = self._parse_optional_int(self.min_price_var.get(), "Min price")
        max_price = self._parse_optional_int(self.max_price_var.get(), "Max price")
        min_year = self._parse_optional_int(self.min_year_var.get(), "Min year")
        max_year = self._parse_optional_int(self.max_year_var.get(), "Max year")

        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("Min price cannot be greater than max price.")

        if min_year is not None and max_year is not None and min_year > max_year:
            raise ValueError("Min year cannot be greater than max year.")

        return FilterSettings(
            model_query=self.model_var.get().strip(),
            min_price=min_price,
            max_price=max_price,
            min_year=min_year,
            max_year=max_year,
            allow_today=self.today_var.get(),
            allow_yesterday=self.yesterday_var.get(),
        )

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

        try:
            filters = self._build_filters()
        except ValueError as exc:
            messagebox.showerror("Invalid filter", str(exc))
            return

        self.running = True
        self._set_controls_running(True)
        self.status_var.set("Running... Priming feed state.")

        self.worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(rss_url, interval, filters),
            daemon=True,
        )
        self.worker_thread.start()

    def stop(self) -> None:
        self.running = False
        self._set_controls_running(False)
        self.status_var.set("Stopped")

    def _set_controls_running(self, running: bool) -> None:
        state = tk.DISABLED if running else tk.NORMAL
        self.start_button.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.config(state=tk.NORMAL if running else tk.DISABLED)
        self.rss_entry.config(state=state)
        self.interval_entry.config(state=state)
        self.model_entry.config(state=state)
        self.min_price_entry.config(state=state)
        self.max_price_entry.config(state=state)
        self.min_year_entry.config(state=state)
        self.max_year_entry.config(state=state)
        self.today_check.config(state=state)
        self.yesterday_check.config(state=state)

    def _worker_loop(self, rss_url: str, interval: int, filters: FilterSettings) -> None:
        while self.running:
            try:
                ads = self.watcher.fetch_new_hybrid_ads(rss_url, filters)
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
                        price = "" if ad.price_eur is None else str(ad.price_eur)
                        year = "" if ad.year is None else str(ad.year)
                        self.tree.insert("", 0, values=(now, ad.model, year, price, ad.published, ad.title, ad.link))
                    self.status_var.set(f"Found {len(ads)} new hybrid ad(s) matching filters.")
                else:
                    if self.watcher.is_primed:
                        self.status_var.set("No new hybrid ads matching filters in latest check.")
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
        if len(row) >= 7 and row[6]:
            webbrowser.open(row[6])


if __name__ == "__main__":
    app = App()
    app.mainloop()
