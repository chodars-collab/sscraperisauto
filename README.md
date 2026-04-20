# SS.lv Hybrid Car Watcher

Python GUI app that checks `ss.lv` car RSS feed and shows only newly detected hybrid ads (`Dzinējs = Hibrīds`).

## Features

- Polls RSS feed on a configurable interval
- Filters ads to hybrids only
- Shows newly detected matching ads in a GUI table
- Double-click row to open ad in browser

## Setup

1. Install Python 3.10+.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Run the app:

```powershell
python app.py
```

## Notes

- Default RSS URL is: `https://www.ss.lv/lv/transport/cars/rss/`
- Minimum polling interval in the app is 10 seconds.
- On startup, the app treats all currently seen ad IDs as already processed and then tracks newly posted items from that point onward.
