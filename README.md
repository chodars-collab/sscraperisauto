# SS.lv Hybrid Car Watcher

Python GUI app that checks `ss.lv` car RSS feed and shows only newly detected hybrid ads (`Dzinējs = Hibrīds`).

## Quick Start (Recommended)

1. Double-click `run_app.bat`
2. Click `Start` in the app

This launcher uses your installed Python path and auto-installs required packages.

## Manual Run

```powershell
& "C:\Users\choda\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
& "C:\Users\choda\AppData\Local\Programs\Python\Python312\python.exe" app.py
```

## Features

- Polls RSS feed on a configurable interval
- Filters ads to hybrids only
- Date filter for ads added today and/or yesterday
- Optional filters for model text, price range, and year range
- Shows newly detected matching ads in a GUI table
- Double-click row to open ad in browser
- Row color aging: green (fresh), yellow (10+ min), orange (15+ min)
- Opened ads are marked gray in the table

## Notes

- Default RSS URL is: `https://www.ss.lv/lv/transport/cars/rss/`
- Minimum polling interval in the app is 10 seconds.
- On startup, the app treats all currently seen ad IDs as already processed and then tracks newly posted items from that point onward.
- `Today`/`Yesterday` unchecked means no date limit (shows hybrid ads from any date).
