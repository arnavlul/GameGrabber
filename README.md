# GameGrabber 🎮

Universal browser game downloader and offline player. Download, archive, and preserve web-based games for 100% offline play—complete with live on-demand streaming, resumable multi-threaded downloads, and self-healing startup.

---

## 🌟 Key Features

- **Universal Engine Support:**
  - **Twine / SugarCube:** Sequential story graph traversal (breadth-first from `startnode`), choice branch resolution, and macro asset extraction.
  - **Ren'Py Web:** Automated detection and archiving of WebAssembly binaries (`renpy.wasm`, `renpy.data`), engine scripts, and `game.zip` packages.
  - **RPG Maker Web (MV / MZ):** Deep extraction of `data/*.json` manifests, audio, and tile assets.
  - **Construct 2 / 3:** Dynamic asset resolution from runtime data structures (`data.js`, `c2runtime.js`).
  - **Generic HTML5:** Canvas, WebGL, and custom script assets.
- **Universal Portal & Iframe Resolver:** Seamlessly resolves games embedded in portals, iframes, and distributed CDNs.
- **Instant Live Play (`--play-now` / `--stream-only`):** Start playing immediately! Launches a multithreaded local caching proxy that streams assets live from the web while quietly caching them to disk in the background.
- **Resumable Downloads (RFC 7233):** Downloads use atomic `.part` files with HTTP Range requests (`bytes=start-end`), allowing interrupted downloads to resume exactly where they left off without restarting.
- **Self-Healing Startup:** Automatically inspects cached files on startup, detecting and purging fake 0-byte files or captive-portal network block pages before resuming.
- **Bandwidth Control:**
  - `--limit <MB>`: Caps total transferred data to a specific quota; safe for limited mobile hotspots or Wi-Fi.
  - `--no-videos`: Downloads all story dialogue, audio, and images while skipping large video files.
- **Instant Offline Runners:** Automatically writes `run_game.bat` and `run_game.py` (an RFC 7233 compliant multithreaded Range server) into the output directory for smooth seeking and playback.

---

## 🛠️ Installation

### Prerequisites
- Python 3.8 or newer
- Windows, macOS, or Linux

### Setup
```bash
# Clone the repository
git clone https://github.com/arnavlul/GameGrabber.git
cd GameGrabber

# Create and activate virtual environment
python -m venv .venv

# On Windows:
.\.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# Install dependencies and browser binaries
pip install -r requirements.txt
playwright install chromium
```

---

## 🚀 Usage & Examples

### 1. Basic Download
Download a game and auto-generate an organized `<Game_Name>-<Version>` directory:
```bash
python scraper.py "https://game-portal.com/games/space-adventure"
```

### 2. Play Immediately (`--play-now`)
Launch the game right away while missing assets download in the background:
```bash
python scraper.py "https://game-portal.com/games/space-adventure" --play-now
```

### 3. Conserve Bandwidth (`--limit` & `--no-videos`)
Stop downloading after 250 MB:
```bash
python scraper.py "https://game-portal.com/games/space-adventure" --limit 250
```
Download text, audio, and images only, skipping heavy `.mp4`/`.webm` videos:
```bash
python scraper.py "https://game-portal.com/games/space-adventure" --no-videos
```

### 4. Custom Output & Parallel Worker Threads
```bash
python scraper.py "https://game-host.com/game/index.html" -o "MyGame" --threads 12
```

---

## 📖 CLI Reference (`--help`)

```text
usage: scraper.py [-h] [-o NAME] [--play-now] [--stream-only] [--no-videos]
                  [--headed] [--no-verify] [--wait SEC] [--threads N]
                  [--serve] [--limit MB] [--proxy URL]
                  url

positional arguments:
  url                   Target URL (Game portal page URL or direct game iframe/CDN URL)

options:
  -h, --help            show this help message and exit
  -o, --folder, --out NAME
                        Custom output directory name (default: auto-named '<Game_Name>-<Version>')
  --play-now, --stream  Play immediately! Starts an on-demand caching server and streams
                        missing assets live while downloading in background
  --stream-only, --lazy Conserve bandwidth: launches game server and streams assets ONLY
                        on-demand as you play (disables background pre-downloading)
  --no-videos, --images-only
                        Conserve bandwidth: downloads all images/audio/text but skips heavy
                        video files (.mp4/.webm)
  --headed              Launch browser with GUI visible (headed mode)
  --no-verify           Disable SSL certificate verification
  --wait SEC            Seconds to wait after game boots for WASM/assets to settle (default: 8)
  --threads N           Number of parallel worker threads for asset downloads (default: 8)
  --serve               Start local server and open game in browser immediately after downloading
  --limit, --max-mb MB  Stop downloading after transferring this many megabytes (safe for
                        limited Wi-Fi; resumable anytime)
  --proxy URL           Optional HTTP/SOCKS5 proxy (e.g. http://127.0.0.1:8080 or
                        socks5://127.0.0.1:1080)
```

---

## 🕹️ Offline Playback

Once a game is downloaded, enter its folder and run:
```bash
# On Windows:
run_game.bat

# On Linux / macOS / any OS:
python run_game.py
```
This launches a lightweight, multithreaded local server with full HTTP 206 Range request support and opens your browser automatically, ensuring HTML5 `<video>` and WebAssembly assets run smoothly without browser security restrictions.

---

## 📄 License
MIT License.
