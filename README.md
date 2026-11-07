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
- **Lightweight Update Detection (`--update` & `--updateall`):** Check for newer versions and game patches in seconds using lightweight HTTP HEAD requests, ETag comparisons, Last-Modified timestamps, and intelligent CDN version probing without downloading heavy media files.
- **Cryptographic Differential Updates & Rename Reuse (`--diff`):** Uses RFC 7232 HTTP Conditional Requests (`If-None-Match` $\rightarrow$ `304 Not Modified`) to verify existing assets in a single network round-trip with zero bytes transferred. Automatically seeds assets via hardlinks, maintains a persistent asset manifest in `game_meta.json`, and detects renamed assets via exact MD5 ETag matches with 0% error.

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
Download a game and auto-generate an organized `downloads/<engine_type>/<Game_Name>-<Version>` directory (e.g., `downloads/html/` or `downloads/renpy/`):
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

### 4. Custom Destination Directory & Parallel Worker Threads
Specify any destination directory directly as a 2nd argument (e.g. `.`, `advancements`, or `special_downloads/here`), or via `-o` / `--out`:
```bash
# Download directly into current directory (.):
python scraper.py "https://game-portal.com/games/space-adventure" .

# Download directly into a custom relative or absolute directory:
python scraper.py "https://game-portal.com/games/space-adventure" advancements
python scraper.py "https://game-portal.com/games/space-adventure" special_downloads/here

# Using -o / --out with custom threads:
python scraper.py "https://game-host.com/game/index.html" -o "special_downloads/here" --threads 12
```

### 5. Check for Updates (`--update` & `--updateall`)
Recursively scan directories (defaults to `downloads/`) for newly released versions or CDN build changes, with interactive `[y/N]` confirmation:
```bash
# Scan all games in downloads/ and prompt before downloading updates:
python scraper.py --updateall

# Scan a specific directory tree:
python scraper.py --updateall "downloads/html"

# Scan and automatically download any detected updates without prompting:
python scraper.py --updateall -y
```

Check a specific downloaded installation or remote game URL:
```bash
# Checks if an update exists and asks [y/N] before downloading:
python scraper.py "Seven_Sins-v1.10" --update

# Automatically download if an update is found:
python scraper.py "Seven_Sins-v1.10" --update -y

# Check directly via remote URL:
python scraper.py "https://gamcore.com/games/seven_sins" --update
```

### 6. Differential Updates & Asset Verification (`--diff`)
Updates automatically seed existing assets and only download the delta. You can also explicitly trigger differential verification on any existing directory:
```bash
python scraper.py "Seven_Sins-v1.10" --diff
```

### 7. Compact & Reclaim Storage Space (`--compact`)
Recursively discovers downloaded games and compresses high-bitrate `.mp4` and `.webm` video assets in-place using FFmpeg:
* **Prerequisite:** Requires [FFmpeg](https://ffmpeg.org) installed on your system (e.g., `winget install Gyan.FFmpeg`).
* **Visually Lossless:** Re-encodes bloated raw renders (often 5,000–30,000 kbps) down to web-optimized CRF 23/31, shrinking games by 20% to 70%+ with zero perceptible quality loss in browser playback.
* **Atomic Replacement:** Safely converts videos to `.tmp` files and only replaces the original if file size is reduced and conversion succeeded.
* **Full Before & After Breakdown:** Prints per-game and total storage savings.

```bash
# Recursively scan and compact all games in downloads/:
python scraper.py --compact

# Compact a specific category or custom directory:
python scraper.py --compact "downloads/html"

# Compact a single specific game folder:
python scraper.py --compact "The_DICKtator-v2.5.1"
```

---

## 📖 CLI Reference (`--help`)

```text
usage: scraper.py [-h] [--update] [--updateall [DIR]] [-y] [--diff] [-o DIR]
                  [--play-now] [--stream-only] [--no-videos] [--headed]
                  [--no-verify] [--wait SEC] [--threads N] [--serve]
                  [--limit MB] [--proxy URL]
                  [url] [dest]

positional arguments:
  url                   Target URL (Game portal page URL or direct game iframe/CDN URL) or local game folder
  dest                  Optional custom destination directory (e.g. '.', 'advancements', 'special_downloads/here').
                        Overrides default 'downloads/<engine>/<name>' path.

options:
  -h, --help            show this help message and exit
  --update, --check-updates
                        Check for game updates via CDN headers, ETags, and version probing.
                        Scans all games if no URL is provided, or checks a specific game
  --updateall [DIR]     Recursively discovers and checks all game installations inside DIR
                        (defaults to 'downloads/')
  -y, --yes             Automatically accept update prompts and download detected updates
                        without confirmation
  --diff, --differential
                        Perform differential update check on existing assets (re-verifies
                        remote sizes/hashes and re-downloads modified files)
  -o, --folder, --out DIR
                        Custom output directory name (e.g. '.', 'advancements', 'special_downloads/here').
                        Overrides default 'downloads/<engine_type>/<Game_Name>-<Version>')
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
