import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
from dataclasses import dataclass
from datetime import date, timedelta, datetime
from typing import Optional, List
import re
import unicodedata
import hashlib
import json
from urllib.parse import urljoin
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup


DEFAULT_RSS_URL = "https://www.ss.lv/lv/transport/cars/rss/"
DEFAULT_CARS_URL = "https://www.ss.lv/lv/transport/cars/"
DEFAULT_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
MAX_LISTING_PAGES = 220
STATE_FILE = "watcher_state.json"
MAX_SEEN_IDS = 50000
try:
    LOCAL_TZ = ZoneInfo("Europe/Riga")
except ZoneInfoNotFoundError:
    # Fallback to system local timezone on Windows when tzdata package is unavailable.
    LOCAL_TZ = datetime.now().astimezone().tzinfo


@dataclass
class FilterSettings:
    selected_brands: List[str]
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
    published_at: Optional[datetime]


class SSLvHybridWatcher:
    def __init__(self) -> None:
        self.seen_ids = set()
        self.seen_order = []
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT})
        self.fields_cache = {}
        self.state_path = Path(__file__).resolve().parent / STATE_FILE
        self._load_state()

    def fetch_new_hybrid_ads(self, rss_url: str, filters: FilterSettings) -> List[CarAd]:
        entries = []
        feed = feedparser.parse(rss_url)
        if feed.bozo:
            raise RuntimeError(f"RSS parsing failed: {feed.bozo_exception}")
        entries.extend(feed.entries)

        # In date-limited mode, also crawl ss.lv "today" pages to avoid RSS recency limits.
        if filters.allow_today or filters.allow_yesterday:
            entries.extend(self._fetch_today_listing_entries(filters))

        candidates: List[CarAd] = []
        changed = False
        for entry in entries:
            ad_id = self._extract_ad_id(entry)
            if not ad_id or ad_id in self.seen_ids:
                continue

            if self._is_hybrid(entry):
                car_ad = self._build_car_ad(entry, ad_id)
                if self._passes_non_date_filters(car_ad, filters):
                    candidates.append(car_ad)

            self._mark_seen(ad_id)
            changed = True

        if changed:
            self._save_state()

        if not (filters.allow_today or filters.allow_yesterday):
            return candidates

        allowed_dates = self._resolve_allowed_dates(candidates, filters)
        if not allowed_dates:
            return []
        return [ad for ad in candidates if ad.published_date in allowed_dates]

    def _fetch_today_listing_entries(self, filters: FilterSettings) -> List[SimpleNamespace]:
        seed_pages = []
        if filters.allow_today and not filters.allow_yesterday:
            seed_pages = [urljoin(DEFAULT_CARS_URL, "today/")]
        elif filters.allow_today and filters.allow_yesterday:
            # Combine both sources so we don't miss "today" ads while also including yesterday.
            seed_pages = [
                urljoin(DEFAULT_CARS_URL, "today/"),
                urljoin(DEFAULT_CARS_URL, "today-2/"),
            ]
        else:
            # "today-2" includes ads from last two days, which covers today+yesterday
            # and also supports a strict "yesterday only" filter.
            seed_pages = [urljoin(DEFAULT_CARS_URL, "today-2/")]

        result = []
        seen_links = set()
        page_urls = []
        for seed in seed_pages:
            page_urls.extend(self._collect_listing_page_urls(seed))

        for page_url in page_urls:
            try:
                response = self.http.get(page_url, timeout=REQUEST_TIMEOUT_SECONDS)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if "/msg/lv/transport/cars/" not in href:
                        continue
                    link = urljoin("https://www.ss.lv/", href)
                    if link in seen_links:
                        continue
                    seen_links.add(link)
                    title = " ".join(a.get_text(" ", strip=True).split()) or "(No title)"
                    result.append(
                        SimpleNamespace(
                            link=link,
                            title=title,
                            summary="",
                            published="",
                            id="",
                        )
                    )
            except requests.RequestException:
                continue

        return result

    def _collect_listing_page_urls(self, start_url: str) -> List[str]:
        page_urls = []
        seen = set()
        queue = [start_url]

        normalized_start = start_url.rstrip("/")
        marker = "/today-2/" if "/today-2/" in start_url else "/today/"
        page_pattern = re.compile(r"/page\d+\.html$")

        while queue and len(page_urls) < MAX_LISTING_PAGES:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            page_urls.append(current)

            try:
                response = self.http.get(current, timeout=REQUEST_TIMEOUT_SECONDS)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
            except requests.RequestException:
                continue

            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                absolute = urljoin("https://www.ss.lv/", href)
                clean = absolute.split("?", 1)[0].rstrip("/")

                if marker not in clean:
                    continue

                is_root = clean == normalized_start
                is_paged = bool(page_pattern.search(clean))
                if not is_root and not is_paged:
                    continue

                if clean not in seen and clean not in queue:
                    queue.append(clean)

        return page_urls

    def _extract_ad_id(self, entry) -> Optional[str]:
        link = getattr(entry, "link", "")
        if link:
            match = re.search(r"/(\d+)\.html", link)
            if match:
                return match.group(1)

        guid = getattr(entry, "id", "")
        if guid:
            return guid.strip()

        # RSS entries may not expose numeric IDs; fall back to a stable link hash.
        seed = (link or "").strip()
        if not seed:
            return None
        return hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()

    def _mark_seen(self, ad_id: str) -> None:
        if ad_id in self.seen_ids:
            return
        self.seen_ids.add(ad_id)
        self.seen_order.append(ad_id)
        overflow = len(self.seen_order) - MAX_SEEN_IDS
        if overflow > 0:
            for old_id in self.seen_order[:overflow]:
                self.seen_ids.discard(old_id)
            self.seen_order = self.seen_order[overflow:]

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            ids = raw.get("seen_ids", [])
            if isinstance(ids, list):
                ids = [str(i).strip() for i in ids if str(i).strip()]
                if len(ids) > MAX_SEEN_IDS:
                    ids = ids[-MAX_SEEN_IDS:]
                self.seen_order = ids
                self.seen_ids = set(ids)
        except (OSError, json.JSONDecodeError):
            self.seen_ids = set()
            self.seen_order = []

    def _save_state(self) -> None:
        payload = {"seen_ids": self.seen_order[-MAX_SEEN_IDS:]}
        try:
            self.state_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _normalize_text(self, text: str) -> str:
        decomposed = unicodedata.normalize("NFKD", text)
        return "".join(char for char in decomposed if not unicodedata.combining(char))

    def _extract_listing_fields(self, link: str) -> dict:
        if not link:
            return {}
        if link in self.fields_cache:
            return self.fields_cache[link]

        fields = {}
        try:
            response = self.http.get(link, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for row in soup.find_all("tr"):
                tds = row.find_all("td")
                if len(tds) < 2:
                    continue
                key_raw = " ".join(tds[0].get_text(" ", strip=True).split())
                value_raw = " ".join(tds[1].get_text(" ", strip=True).split())
                if not key_raw or not value_raw:
                    continue
                key_norm = self._normalize_text(key_raw.lower()).strip(" :")
                fields[key_norm] = value_raw

            # Page footer often contains "Datums: dd.mm.yyyy hh:mm".
            page_text_norm = self._normalize_text(soup.get_text(" ", strip=True).lower())
            date_match = re.search(
                r"datums:\s*(\d{1,2}\.\d{1,2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)",
                page_text_norm,
            )
            if date_match:
                fields["datums"] = date_match.group(1)
        except requests.RequestException:
            fields = {}

        self.fields_cache[link] = fields
        return fields

    def _is_hybrid(self, entry) -> bool:
        link = getattr(entry, "link", "")
        if not link:
            return False

        fields = self._extract_listing_fields(link)
        if not fields:
            return False

        engine_value = (
            fields.get("motors")
            or fields.get("dzinejs")
            or fields.get("dzinjs")
            or ""
        )
        engine_norm = self._normalize_text(engine_value.lower())

        # Strict engine-based hybrid detection to avoid false positives from body text.
        if "hibr" in engine_norm or "phev" in engine_norm or "mhev" in engine_norm:
            return True
        if "benz" in engine_norm and "elektr" in engine_norm:
            return True
        return False

    def _build_car_ad(self, entry, ad_id: str) -> CarAd:
        title = getattr(entry, "title", "(No title)")
        summary = getattr(entry, "summary", "")
        published = getattr(entry, "published", "")
        text_blob = f"{title} {summary}".strip()
        link = getattr(entry, "link", "")
        fields = self._extract_listing_fields(link)
        model = fields.get("marka") or self._extract_model(title)
        year = self._extract_year(fields.get("izlaiduma gads") or text_blob)
        price = self._extract_price_from_field(fields.get("cena"))
        if price is None:
            price = self._extract_price_eur(text_blob)
        page_date = fields.get("datums")
        published_at = self._extract_published_datetime(entry, published, page_date)
        published_date = published_at.date() if published_at else None

        return CarAd(
            ad_id=ad_id,
            title=title,
            link=link,
            published=published,
            source="feed",
            model=model,
            year=year,
            price_eur=price,
            published_date=published_date,
            published_at=published_at,
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
        text_lower = self._normalize_text(text.lower())
        euro_match = re.search(r"(\d[\d\s]{2,})\s*(?:eur|euro)\b", text_lower)
        if not euro_match:
            euro_match = re.search(r"\bcena\s*[:\-]?\s*(\d[\d\s]{2,})\b", text_lower)

        if not euro_match:
            return None

        value = re.sub(r"\D", "", euro_match.group(1))
        if not value:
            return None

        return int(value)

    def _extract_price_from_field(self, price_field: Optional[str]) -> Optional[int]:
        if not price_field:
            return None
        # "Cena" field usually starts with the listing price, e.g. "19 400 Aprēķināt..."
        # Grab the first numeric token with optional space thousand separators.
        match = re.search(r"\b\d{1,3}(?:\s\d{3})+\b|\b\d{3,}\b", price_field)
        if not match:
            return None
        value = re.sub(r"\D", "", match.group(0))
        if not value:
            return None
        return int(value)

    def _extract_published_datetime(
        self,
        entry,
        published: str,
        page_date: Optional[str] = None,
    ) -> Optional[datetime]:
        if page_date:
            match = re.search(
                r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?\b",
                page_date,
            )
            if match:
                day = int(match.group(1))
                month = int(match.group(2))
                year = int(match.group(3))
                hour = int(match.group(4) or 0)
                minute = int(match.group(5) or 0)
                try:
                    return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
                except ValueError:
                    pass

        if getattr(entry, "published_parsed", None):
            parsed = entry.published_parsed
            try:
                parsed_dt = parsedate_to_datetime(published) if published else None
                if parsed_dt is not None:
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=LOCAL_TZ)
                    return parsed_dt.astimezone(LOCAL_TZ)
            except (TypeError, ValueError):
                pass
            try:
                return datetime(
                    parsed.tm_year,
                    parsed.tm_mon,
                    parsed.tm_mday,
                    parsed.tm_hour,
                    parsed.tm_min,
                    parsed.tm_sec,
                    tzinfo=LOCAL_TZ,
                )
            except ValueError:
                return None

        match = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", published)
        if match:
            day = int(match.group(1))
            month = int(match.group(2))
            year = int(match.group(3))
            try:
                return datetime(year, month, day, tzinfo=LOCAL_TZ)
            except ValueError:
                return None

        return None

    def _passes_non_date_filters(self, ad: CarAd, filters: FilterSettings) -> bool:
        if filters.selected_brands:
            model_lower = ad.model.lower()
            title_lower = ad.title.lower()
            if not any(
                brand.lower() in model_lower or brand.lower() in title_lower
                for brand in filters.selected_brands
            ):
                return False

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

        return True

    def _resolve_allowed_dates(self, ads: List[CarAd], filters: FilterSettings) -> set:
        dates = sorted({ad.published_date for ad in ads if ad.published_date is not None}, reverse=True)
        if not dates:
            return set()

        today = datetime.now(LOCAL_TZ).date()
        yesterday = today - timedelta(days=1)
        allowed = set()

        if filters.allow_today:
            if today in dates:
                allowed.add(today)
            else:
                # Fallback to freshest available date from ss.lv when local day boundary differs.
                allowed.add(dates[0])

        if filters.allow_yesterday:
            if yesterday in dates:
                allowed.add(yesterday)
            else:
                # Some ss.lv listing ranges may skip the exact previous day; use nearest older date.
                pivot = today
                if allowed:
                    pivot = max(allowed)
                older_dates = [d for d in dates if d < pivot]
                if older_dates:
                    allowed.add(older_dates[0])

        return allowed


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SS.lv Hybrid Car Watcher")
        self.geometry("1220x660")

        self.watcher = SSLvHybridWatcher()
        self.running = False
        self.worker_thread: Optional[threading.Thread] = None
        self.events = queue.Queue()
        self.row_meta = {}
        self.last_color_refresh = 0.0

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

        ttk.Label(config, text="Brands:").grid(row=1, column=4, sticky=tk.W, padx=(16, 8), pady=4)
        brands_row = ttk.Frame(config)
        brands_row.grid(row=1, column=5, sticky=tk.W, pady=4)
        self.brand_all_var = tk.BooleanVar(value=True)
        self.brand_toyota_var = tk.BooleanVar(value=False)
        self.brand_lexus_var = tk.BooleanVar(value=False)
        self.brand_honda_var = tk.BooleanVar(value=False)
        self.brand_all_check = ttk.Checkbutton(brands_row, text="All cars", variable=self.brand_all_var)
        self.brand_toyota_check = ttk.Checkbutton(brands_row, text="Toyota", variable=self.brand_toyota_var)
        self.brand_lexus_check = ttk.Checkbutton(brands_row, text="Lexus", variable=self.brand_lexus_var)
        self.brand_honda_check = ttk.Checkbutton(brands_row, text="Honda", variable=self.brand_honda_var)
        self.brand_all_check.pack(side=tk.LEFT)
        self.brand_toyota_check.pack(side=tk.LEFT, padx=(8, 0))
        self.brand_lexus_check.pack(side=tk.LEFT, padx=(8, 0))
        self.brand_honda_check.pack(side=tk.LEFT, padx=(8, 0))

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
        self.today_var = tk.BooleanVar(value=False)
        self.yesterday_var = tk.BooleanVar(value=False)
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
        self.tree.tag_configure("age_green", background="#e6f7ea")
        self.tree.tag_configure("age_yellow", background="#fff6cc")
        self.tree.tag_configure("age_orange", background="#ffe6cc")
        self.tree.tag_configure("opened", foreground="#8a8a8a")

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

        selected_brands: List[str] = []
        if not self.brand_all_var.get():
            if self.brand_toyota_var.get():
                selected_brands.append("Toyota")
            if self.brand_lexus_var.get():
                selected_brands.append("Lexus")
            if self.brand_honda_var.get():
                selected_brands.append("Honda")
            if not selected_brands:
                raise ValueError("Select at least one brand or enable All cars.")

        return FilterSettings(
            selected_brands=selected_brands,
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
        self.status_var.set("Running...")

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
        self.brand_all_check.config(state=state)
        self.brand_toyota_check.config(state=state)
        self.brand_lexus_check.config(state=state)
        self.brand_honda_check.config(state=state)
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
                    now_label = time.strftime("%Y-%m-%d %H:%M:%S")
                    now_dt = datetime.now(LOCAL_TZ)
                    for ad in ads:
                        price = "" if ad.price_eur is None else str(ad.price_eur)
                        year = "" if ad.year is None else str(ad.year)
                        row_id = self.tree.insert(
                            "",
                            0,
                            values=(now_label, ad.model, year, price, ad.published, ad.title, ad.link),
                        )
                        self.row_meta[row_id] = {
                            "published_at": ad.published_at,
                            "detected_at": now_dt,
                            "opened": False,
                        }
                        self._apply_row_style(row_id)
                    self.status_var.set(f"Found {len(ads)} hybrid ad(s) matching filters.")
                else:
                    self.status_var.set("No hybrid ads matching filters in latest check.")
            elif event_type == "error":
                self.status_var.set(f"Error: {payload}")

        current = time.time()
        if current - self.last_color_refresh >= 20:
            self._refresh_row_styles()
            self.last_color_refresh = current

        self.after(250, self._process_events)

    def _open_selected_link(self, _event) -> None:
        import webbrowser

        selected = self.tree.selection()
        if not selected:
            return

        row_id = selected[0]
        row = self.tree.item(row_id, "values")
        if len(row) >= 7 and row[6]:
            webbrowser.open(row[6])
            if row_id in self.row_meta:
                self.row_meta[row_id]["opened"] = True
                self._apply_row_style(row_id)

    def _refresh_row_styles(self) -> None:
        for row_id in self.tree.get_children():
            self._apply_row_style(row_id)

    def _apply_row_style(self, row_id: str) -> None:
        meta = self.row_meta.get(row_id)
        if not meta:
            return

        reference_dt = meta.get("published_at") or meta.get("detected_at")
        if reference_dt is None:
            reference_dt = datetime.now(LOCAL_TZ)

        if reference_dt.tzinfo is None:
            reference_dt = reference_dt.replace(tzinfo=LOCAL_TZ)

        now_dt = datetime.now(LOCAL_TZ)
        age_minutes = max(0.0, (now_dt - reference_dt).total_seconds() / 60.0)

        if age_minutes >= 15:
            age_tag = "age_orange"
        elif age_minutes >= 10:
            age_tag = "age_yellow"
        else:
            age_tag = "age_green"

        tags = [age_tag]
        if meta.get("opened"):
            tags.append("opened")

        self.tree.item(row_id, tags=tags)


if __name__ == "__main__":
    app = App()
    app.mainloop()

