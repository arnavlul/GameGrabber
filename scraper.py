"""
GameGrabber - Universal Browser Game Scraper
Supports web game portals, direct iframes, Ren'Py Web, SugarCube/Twine, RPG Maker, Construct, and HTML5.
Features:
  * Automatic version detection and clean '<Game_Name>-<Version>' directory naming.
  * Corruption-safe resumability with atomic .part files and Range requests.
  * Self-healing startup: automatically purges fake captive-portal / corrupt files on resume.
  * --play-now mode: on-demand caching proxy server allowing immediate gameplay while downloading.
"""

import os
import sys
import re
import time
import json
import html
import shutil
import hashlib
import argparse
import subprocess
import mimetypes
import urllib3
import requests
import webbrowser
import threading
import http.server
import socketserver
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, formatdate
from urllib.parse import urlparse, urljoin, unquote, quote, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from tqdm import tqdm
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Standard media and web asset extensions
MEDIA_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico', 'bmp', 'avif',
    'mp3', 'ogg', 'wav', 'm4a', 'flac', 'aac', 'opus',
    'mp4', 'webm', 'ogv',
    'wasm', 'data', 'mem', 'zip', 'rpa', 'pak',
    'woff', 'woff2', 'ttf', 'otf', 'eot',
    'json', 'xml', 'txt', 'csv',
    'rpgmvp', 'rpgmvo', 'rpgmvm', 'png_', 'ogg_', 'm4a_'
}

CODE_EXTENSIONS = {'js', 'css', 'html', 'htm'}

ALL_EXTENSIONS = MEDIA_EXTENSIONS | CODE_EXTENSIONS

AUTO_AUDIO_SCRIPT = """<script id="auto-audio-manager">
/* ==========================================================
   Auto-Audio & In-Viewport Playback Manager
   - Ensures videos only play when in viewport with audio.
   - Automatically pauses and mutes videos when out of view.
   - Prevents silent background playback on mute.
   - Handles modern browser autoplay policy via user interaction unlock.
   ========================================================== */
(function () {
	let userInteracted = false;
	let activeVideo = null;

	function unlockAudio() {
		if (!userInteracted) {
			userInteracted = true;
			updateActiveVideo();
		}
	}

	window.addEventListener("click", unlockAudio, { passive: true, capture: true });
	window.addEventListener("keydown", unlockAudio, { passive: true, capture: true });
	window.addEventListener("touchstart", unlockAudio, { passive: true, capture: true });
	window.addEventListener("pointerdown", unlockAudio, { passive: true, capture: true });

	function getVisibleArea(v) {
		if (!v || v.nodeType !== 1) return 0;
		const rect = v.getBoundingClientRect();
		const vh = window.innerHeight || document.documentElement.clientHeight;
		const vw = window.innerWidth || document.documentElement.clientWidth;
		if (rect.width <= 0 || rect.height <= 0) return 0;
		const visibleWidth = Math.max(0, Math.min(rect.right, vw) - Math.max(rect.left, 0));
		const visibleHeight = Math.max(0, Math.min(rect.bottom, vh) - Math.max(rect.top, 0));
		if (visibleWidth < 40 || visibleHeight < 40) return 0;
		return visibleWidth * visibleHeight;
	}

	function updateActiveVideo() {
		const videos = Array.from(document.querySelectorAll("video"));
		if (!videos.length) {
			activeVideo = null;
			return;
		}

		let bestVideo = null;
		let maxVisibleArea = 0;

		videos.forEach(function (v) {
			if (!v._autoAudioHooked) {
				v._autoAudioHooked = true;
				v.addEventListener("play", function () {
					if (v !== activeVideo) {
						const area = getVisibleArea(v);
						if (area <= 0) {
							v.pause();
							v.muted = true;
						}
					}
				});
			}

			const visibleArea = getVisibleArea(v);
			if (visibleArea > maxVisibleArea) {
				maxVisibleArea = visibleArea;
				bestVideo = v;
			}
		});

		activeVideo = maxVisibleArea > 0 ? bestVideo : null;

		videos.forEach(function (v) {
			if (v === activeVideo) {
				// Video is in view: ensure audio is on
				v.muted = false;
				if (v.volume === 0) v.volume = 1.0;

				if (userInteracted) {
					if (v.paused) {
						var playPromise = v.play();
						if (playPromise && playPromise.catch) {
							playPromise.catch(function () {});
						}
					}
				} else {
					// Attempt unmuted play before interaction
					var p = v.play();
					if (p && p.catch) {
						p.catch(function () {
							// Browser blocked unmuted autoplay: wait paused until interaction
							if (!v.paused) v.pause();
						});
					}
				}
			} else {
				// Out of view: MUST STAY PAUSED and muted
				if (!v.paused) {
					v.pause();
				}
				v.muted = true;
			}
		});
	}

	// SugarCube passage transition hook
	if (window.jQuery) {
		$(document).on(":passagedisplay.autoVideoAudio", function () {
			setTimeout(updateActiveVideo, 50);
			setTimeout(updateActiveVideo, 250);
			setTimeout(updateActiveVideo, 700);
		});
	}

	// MutationObserver for any dynamic HTML/DOM updates
	try {
		const observer = new MutationObserver(function (mutations) {
			mutations.forEach(function (m) {
				m.addedNodes.forEach(function (node) {
					if (node.nodeType === 1) {
						const vids = node.tagName === "VIDEO" ? [node] : Array.from(node.querySelectorAll ? node.querySelectorAll("video") : []);
						vids.forEach(function (v) {
							if (getVisibleArea(v) <= 0) {
								v.removeAttribute("autoplay");
								if (!v.paused) v.pause();
								v.muted = true;
							}
						});
					}
				});
			});
			updateActiveVideo();
		});

		const targetNode = document.body || document.documentElement;
		if (targetNode) {
			observer.observe(targetNode, { childList: true, subtree: true });
		} else {
			document.addEventListener("DOMContentLoaded", function () {
				observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
			});
		}
	} catch (e) {}

	setInterval(updateActiveVideo, 500);
	window.addEventListener("scroll", updateActiveVideo, { passive: true });
	window.addEventListener("resize", updateActiveVideo, { passive: true });
}());
</script>
"""

def strip_auto_audio(html_text):
    """Strips the injected Auto-Audio manager script from HTML text for clean content comparison."""
    if not html_text:
        return ""
    return re.sub(r'<script\s+id=["\']auto-audio-manager["\']>.*?</script>\s*', '', html_text, flags=re.DOTALL)

def normalize_html_for_comparison(html_text):
    """Normalizes HTML by removing auto-audio scripts, hotlink blockers, analytics, and normalizing whitespace for pure game content comparison."""
    if not html_text:
        return ""
    text = strip_auto_audio(html_text)
    text = re.sub(r'<!--\s*starthotlinking\s*-->.*?<!--\s*endhotlinking\s*-->\s*', '', text, flags=re.DOTALL)
    text = re.sub(r'<!--\s*startAnalytics\s*-->.*?<!--\s*endAnalytics\s*-->\s*', '', text, flags=re.DOTALL)
    text = text.replace('\r\n', '\n')
    return text.strip()

AD_DOMAINS = [
    'doubleclick', 'googlesyndication', 'adtng', 'crme7srv', 'digitalclickstime',
    '69games', 'exo-right', 'yandex', 'google-analytics', 'trafficjunky',
    'magsrv', 'ad-score', 'exoclick', 'juicyads', 'erome'
]

def normalize_url(raw_url):
    """Cleans up typos in URL strings, such as thttps:// or missing scheme, and sanitizes invalid domain characters."""
    raw = raw_url.strip('\'" \t\r\n')
    if re.match(r'^[a-z]+https?://', raw, re.I):
        raw = re.sub(r'^[a-z]+(?=https?://)', '', raw, flags=re.I)
    elif re.match(r'^https?:/([^/])', raw, re.I):
        raw = re.sub(r'^(https?:)/', r'\1//', raw, flags=re.I)
    elif raw.startswith('//'):
        raw = 'https:' + raw
    elif not raw.startswith(('http://', 'https://')):
        raw = 'https://' + raw

    try:
        parsed = urlparse(raw)
        host = parsed.netloc or ""
        lower_host = host.lower()
        new_host = host
        if lower_host.endswith(".fgn.cdn.serverable.com") or lower_host.endswith(".serverable.com"):
            new_host = "fgn.cdn.serverable.com"
        elif lower_host.endswith(".discretehosting.com"):
            new_host = "discretehosting.com"
        elif "_" in host:
            new_host = host.replace("_", "-")
        if new_host != host:
            raw = urlunparse((parsed.scheme, new_host, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass

    return raw

def clean_mojibake(text: str) -> str:
    """
    Detects and fixes UTF-8 -> Latin-1 mojibake (e.g. 'â€™' -> '’', 'Î¡' -> '\ue821', etc.)
    caused when web servers omit charset headers or decode UTF-8 as ISO-8859-1.
    """
    if not text:
        return text

    signatures = ('\u00e2\u0080', '\u00ee\u00a0', '\u00c3\u0082', '\u00c3\u0083', '\u00c2\u00a0\u00c2\u00a0', 'â\x80')
    if any(sig in text for sig in signatures):
        # 1. Try full string reversal
        try:
            return text.encode('latin1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

        # 2. Fallback: regex replace high-byte sequences in mixed content
        def repl(m):
            chunk = m.group(0)
            try:
                return chunk.encode('latin1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                return chunk

        return re.sub(r'[\u0080-\u00ff]{2,}', repl, text)

    return text

def sanitize_name(name):
    clean = re.sub(r'[<>:"/\\|?*]+', '_', name).strip('. _')
    clean = re.sub(r'\s+', '_', clean)
    return clean if clean else "Game"

def extract_game_identity(title, url, html_content=""):
    """
    Extracts (clean_title, version, folder_name).
    Folder name format: <Game_Name>-<Version>
    """
    raw_title = title.strip() if title else ""

    # Strip marketing fluff
    fluff_patterns = [
        r'[-|–]\s*(?:Play\s+)?(?:Free\s+)?Online(?:\s+Game)?.*$',
        r'[-|–]\s*Gamcore.*$',
        r'[-|–]\s*Play\s+on\s+Gamcore.*$',
        r'[-|–]\s*Download.*$',
        r'\|.*$'
    ]
    for pat in fluff_patterns:
        raw_title = re.sub(pat, '', raw_title, flags=re.I).strip()

    version = None

    # 1. Search title for version pattern: v0.12, ver 1.4, (v2.0), [build 102]
    v_match = re.search(r'[\(\[\{]?(?:v(?:er)?\.?\s*|build\s*)([0-9]+(?:\.[0-9a-zA-Z_\-]+)*)[\)\]\}]?', raw_title, re.I)
    if v_match:
        version = "v" + v_match.group(1).lstrip('vV')
        clean_title = raw_title[:v_match.start()] + raw_title[v_match.end():]
        clean_title = re.sub(r'[\(\[\{\)\]\}]', '', clean_title).strip(' -_')
    else:
        clean_title = raw_title

    # 2. Check URL path FIRST for explicit game version segments (e.g. /v_0.79/, /v1.08/, /v_ch.3/, /episode_1/)
    # This prevents catching internal framework library versions (e.g. SugarCube 2.36.1 / 2.6.2) from HTML text
    if not version and url:
        parsed_url = urlparse(url)
        path_segs = [s for s in parsed_url.path.strip('/').split('/') if s]
        for seg in reversed(path_segs):
            m_seg = re.match(r'^(?:v_?|ver_?|build_?)([0-9]+(?:\.[0-9a-zA-Z_\-]+)*)$', seg, re.I)
            if m_seg:
                version = "v" + m_seg.group(1)
                break
            m_ch = re.match(r'^(?:v_?)?(?:ch|chapter|ep|episode)[-_\.]?([0-9a-zA-Z_\-]+)$', seg, re.I)
            if m_ch:
                version = "v_ch." + m_ch.group(1)
                break

    # 3. Check URL slug for version pattern: /game-name-v0.12/
    if not version and url:
        u_match = re.search(r'[-_](?:v|ver)?([0-9]+(?:\.[0-9a-zA-Z_\-]+)+)', url, re.I)
        if u_match:
            version = "v" + u_match.group(1).lstrip('vV')

    # 4. Search html_content for explicit author game version variables
    if not version and html_content:
        m_var = re.search(r'(?:game_version|gameVersion|build_num)\s*[:=]\s*["\']v?([0-9]+(?:\.[0-9a-zA-Z_\-]+)*)["\']', html_content, re.I)
        if m_var:
            version = "v" + m_var.group(1).lstrip('vV')

    if not version:
        version = "v1.0"

    # If title is empty or generic, extract from URL slug (avoiding filenames like index.html or version folders)
    generic_titles = {
        'untitled game', 'gamcore', 'game', 'index', 'home', 'player',
        'play', 'default', 'fullscreen', 'adult_game', 'framework', 'webgl'
    }
    if not clean_title or clean_title.lower().strip() in generic_titles:
        parsed_url = urlparse(url)
        path_segs = [s for s in parsed_url.path.strip('/').split('/') if s]
        slug = ""
        # Find the segment that is the game slug (skip index.html, version folders, nf, flashes, etc.)
        for seg in reversed(path_segs):
            seg_clean = re.sub(r'\.(?:html?|php|aspx?)$', '', seg, flags=re.I)
            if seg_clean.lower() in generic_titles:
                continue
            if re.match(r'^(?:v_?|ver_?|ch|chapter|ep|episode)[0-9_\.]*$', seg_clean, re.I):
                continue
            if seg_clean.lower() in ['nf', 'flashes', 'common', 'games']:
                continue
            slug = seg_clean
            break
        if slug:
            clean_title = slug.replace('-', '_').title()
        else:
            clean_title = "Game"

    clean_title = sanitize_name(clean_title)
    folder_name = f"{clean_title}-{version}"
    return clean_title, version, folder_name

def clean_relative_path(path):
    if not path:
        return None
    path = html.unescape(str(path)).strip('\'" \t\r\n')
    if not path:
        return None

    lower_start = path.lower()
    if lower_start.startswith(('data:', 'blob:', 'javascript:', 'mailto:', '#', 'about:', 'http://', 'https://', '//')):
        return None

    if any(c in path for c in ['<', '>', '$', '{', '}', ';', '*', '\n', '\r', '|', '"']):
        return None

    path = path.split('?')[0].split('#')[0].strip()
    path = path.replace('\\', '/')
    if not path:
        return None

    # Safe percent-decoding preserving '+'
    path = unquote(path.replace('+', '%2B')).replace('\\', '/').strip()
    norm_segs = []
    for seg in path.split('/'):
        if not seg or seg == '.':
            continue
        if seg == '..':
            if norm_segs:
                norm_segs.pop()
        else:
            norm_segs.append(seg)
    if not norm_segs:
        return None

    # Protect against Windows reserved device names
    base_name = os.path.splitext(norm_segs[-1])[0].upper()
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$', base_name):
        norm_segs[-1] = '_' + norm_segs[-1]

    path = '/'.join(norm_segs)

    # Filter dummy placeholders
    lower = path.lower()
    if lower in ['movie.ogg', 'movie.mp4', 'dummy.mp4', 'video.ogg'] or lower.endswith(('/movie.ogg', '/movie.mp4')):
        return None

    if '.' not in lower:
        return None
    ext = lower.rsplit('.', 1)[-1]
    if ext not in ALL_EXTENSIONS:
        return None

    return path

def auto_purge_corrupted_files(target_folder):
    """
    Scans the download folder and automatically purges fake captive portal HTML block pages
    or truncated 0-byte media files so they get cleanly re-downloaded on resume.
    """
    if not os.path.exists(target_folder):
        return 0

    purged = 0
    portal_signatures = [b"httpclient.html", b"portal_url", b"blocked site", b"<html", b"<!doctype html>"]

    for root, dirs, files in os.walk(target_folder):
        for f in files:
            if f in ["index.html", "game_meta.json", "run_game.bat", "run_game.py"]:
                continue
            path = os.path.join(root, f)
            ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''

            try:
                size = os.path.getsize(path)
                if size == 0 and f.endswith(".part"):
                    os.remove(path)
                    continue

                if ext in MEDIA_EXTENSIONS and ext not in ['json', 'xml', 'txt', 'csv']:
                    if size == 0 or size == 715:
                        os.remove(path)
                        purged += 1
                        continue
                    if size < 4096:
                        with open(path, "rb") as fp:
                            header = fp.read(1024).lower()
                        if any(sig in header for sig in portal_signatures):
                            os.remove(path)
                            purged += 1
            except Exception:
                pass

    if purged > 0:
        print(f"[+] Self-Healing: Automatically purged {purged} corrupt/captive-portal files for clean re-download.")
    return purged

def file_md5(file_path, chunk_size=65536):
    """Computes the MD5 hex digest of a local file."""
    try:
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest().lower()
    except Exception:
        return None

def load_asset_manifest(folder):
    """
    Loads persistent asset manifest from game_meta.json if present.
    Returns:
        dict: {rel_path: {"etag": str, "size": int, "mtime": float}}
    """
    if not folder:
        return {}
    meta_path = os.path.join(folder, "game_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                return meta.get("assets_manifest", {})
        except Exception:
            pass
    return {}

def save_asset_manifest(folder, manifest):
    """
    Saves/updates persistent asset manifest inside game_meta.json.
    """
    if not folder or not manifest:
        return
    meta_path = os.path.join(folder, "game_meta.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
    meta["assets_manifest"] = manifest
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass

def build_asset_index(folder, manifest=None, max_workers=8):
    """
    Builds in-memory lookup indexes for fast differential updates:
      - etag_index: mapping from etag (hex MD5) -> local_abs_path
      - size_index: mapping from file_size -> [local_abs_path, ...]
      - manifest: {rel_path: {"etag": ..., "size": ..., "mtime": ...}}
    Reuses existing manifest data for instant zero-overhead lookups,
    and parallel-hashes un-indexed files across worker threads.
    """
    if manifest is None:
        manifest = load_asset_manifest(folder)

    etag_index = {}
    size_index = {}
    skip_files = {"index.html", "game_meta.json", "run_game.bat", "run_game.py"}

    if not folder or not os.path.isdir(folder):
        return etag_index, size_index, manifest

    unindexed_files = []

    for root, dirs, files in os.walk(folder):
        for f in files:
            if f in skip_files or f.endswith(".part"):
                continue
            abs_p = os.path.join(root, f)
            rel_p = os.path.relpath(abs_p, folder).replace("\\", "/")
            try:
                st = os.stat(abs_p)
                sz = st.st_size
                mtime = st.st_mtime
            except Exception:
                continue

            if sz > 0:
                if sz > 10240:
                    size_index.setdefault(sz, []).append(abs_p)

                cached_entry = manifest.get(rel_p)
                if cached_entry and cached_entry.get("size") == sz and abs(cached_entry.get("mtime", 0) - mtime) < 1.0:
                    etag = cached_entry.get("etag")
                    if etag:
                        etag_index[etag.strip('"').lower()] = abs_p
                else:
                    if sz < 100 * 1024 * 1024:
                        unindexed_files.append((abs_p, rel_p, sz, mtime))

    if unindexed_files:
        def worker(item):
            ap, rp, s, mt = item
            h = file_md5(ap)
            return ap, rp, s, mt, h

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for ap, rp, s, mt, h in ex.map(worker, unindexed_files):
                if h:
                    manifest[rp] = {"etag": h, "size": s, "mtime": mt}
                    etag_index[h.strip('"').lower()] = ap

        save_asset_manifest(folder, manifest)

    return etag_index, size_index, manifest

def seed_existing_assets(source_folder, dest_folder):
    """
    Seeds dest_folder with media assets from source_folder using fast hardlinks (or copy fallback).
    Copies source manifest to dest folder for instant zero-overhead ETag lookups.
    Returns:
        seeded_count (int): number of seeded files
        etag_index (dict): mapping from etag -> local_abs_path
        size_index (dict): mapping from file_size -> [local_abs_path, ...]
        manifest (dict): asset manifest
    """
    if not source_folder or not os.path.isdir(source_folder) or os.path.abspath(source_folder) == os.path.abspath(dest_folder):
        return 0, {}, {}, {}

    os.makedirs(dest_folder, exist_ok=True)
    seeded_count = 0
    skip_files = {"index.html", "game_meta.json", "run_game.bat", "run_game.py"}

    src_manifest = load_asset_manifest(source_folder)
    dest_manifest = {}

    for root, dirs, files in os.walk(source_folder):
        for f in files:
            if f in skip_files or f.endswith(".part"):
                continue
            src_path = os.path.join(root, f)
            rel_path = os.path.relpath(src_path, source_folder)
            rel_posix = rel_path.replace("\\", "/")
            dst_path = os.path.join(dest_folder, rel_path)

            if not os.path.exists(dst_path):
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                try:
                    os.link(src_path, dst_path)
                except Exception:
                    try:
                        shutil.copy2(src_path, dst_path)
                    except Exception:
                        continue
                seeded_count += 1

            if rel_posix in src_manifest:
                dest_manifest[rel_posix] = dict(src_manifest[rel_posix])

    etag_index, size_index, manifest = build_asset_index(dest_folder, manifest=dest_manifest)
    return seeded_count, etag_index, size_index, manifest

def find_existing_game_folder(clean_title, current_target_folder, base_dir="downloads"):
    """
    Finds an existing downloaded folder for clean_title to seed from.
    Searches downloads/html, downloads/renpy, and downloads root. Excludes current_target_folder.
    """
    candidates = []
    norm_title = re.sub(r'[^a-zA-Z0-9]', '', clean_title).lower()
    abs_target = os.path.abspath(current_target_folder) if current_target_folder else ""

    search_roots = [
        os.path.join(base_dir, "html"),
        os.path.join(base_dir, "renpy"),
        base_dir
    ]

    for s_root in search_roots:
        if not os.path.isdir(s_root):
            continue
        try:
            for entry in os.listdir(s_root):
                full_p = os.path.join(s_root, entry)
                if not os.path.isdir(full_p) or os.path.abspath(full_p) == abs_target:
                    continue
                if entry in ["html", "renpy", "temp"]:
                    continue

                norm_entry = re.sub(r'[^a-zA-Z0-9]', '', entry).lower()
                is_match = False
                if norm_entry.startswith(norm_title):
                    is_match = True

                meta_p = os.path.join(full_p, "game_meta.json")
                if not is_match and os.path.exists(meta_p):
                    try:
                        with open(meta_p, "r", encoding="utf-8") as f:
                            m = json.load(f)
                            m_name = m.get("game_name", "")
                            if re.sub(r'[^a-zA-Z0-9]', '', m_name).lower() == norm_title:
                                is_match = True
                    except Exception:
                        pass

                if is_match and GameUpdateChecker.is_game_directory(full_p):
                    candidates.append(full_p)
        except Exception:
            pass

    if not candidates:
        return None

    candidates = list(set(candidates))
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]

def get_remote_file_info(session, url, referer=None, verify_ssl=True):
    """
    Queries remote asset metadata (status_code, content_length, etag, last_modified)
    using HTTP HEAD with fallback to GET Range bytes=0-0.
    """
    headers = {"Referer": referer} if referer else {}
    parsed_url = urlparse(url)
    safe_path = quote(unquote(parsed_url.path.replace('+', '%2B')), safe="/()'!~,-_.@")
    safe_url = urlunparse((parsed_url.scheme, parsed_url.netloc, safe_path, parsed_url.params, parsed_url.query, parsed_url.fragment))

    try:
        r = session.head(safe_url, headers=headers, verify=verify_ssl, timeout=8, allow_redirects=True)
        if r.status_code in [200, 206]:
            cl = int(r.headers.get("Content-Length", 0) or 0)
            etag = r.headers.get("ETag", "").strip('"')
            lm = r.headers.get("Last-Modified")
            return r.status_code, cl, etag, lm
        elif r.status_code in [404, 403]:
            return r.status_code, 0, None, None
    except requests.exceptions.SSLError:
        try:
            r = session.head(safe_url, headers=headers, verify=False, timeout=8, allow_redirects=True)
            if r.status_code in [200, 206]:
                cl = int(r.headers.get("Content-Length", 0) or 0)
                etag = r.headers.get("ETag", "").strip('"')
                lm = r.headers.get("Last-Modified")
                return r.status_code, cl, etag, lm
            elif r.status_code in [404, 403]:
                return r.status_code, 0, None, None
        except Exception:
            pass
    except Exception:
        pass

    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        r = session.get(safe_url, headers=h, verify=verify_ssl, timeout=8, stream=True)
        if r.status_code == 206:
            cr = r.headers.get("Content-Range", "")
            cl = 0
            if "/" in cr:
                try:
                    cl = int(cr.rsplit("/", 1)[1].strip())
                except Exception:
                    pass
            etag = r.headers.get("ETag", "").strip('"')
            lm = r.headers.get("Last-Modified")
            return 200, cl, etag, lm
        elif r.status_code == 200:
            cl = int(r.headers.get("Content-Length", 0) or 0)
            etag = r.headers.get("ETag", "").strip('"')
            lm = r.headers.get("Last-Modified")
            return 200, cl, etag, lm
        return r.status_code, 0, None, None
    except requests.exceptions.SSLError:
        try:
            h = dict(headers)
            h["Range"] = "bytes=0-0"
            r = session.get(safe_url, headers=h, verify=False, timeout=8, stream=True)
            if r.status_code in [200, 206]:
                cl = int(r.headers.get("Content-Length", 0) or 0)
                etag = r.headers.get("ETag", "").strip('"')
                lm = r.headers.get("Last-Modified")
                return 200, cl, etag, lm
        except Exception:
            pass
        return 0, 0, None, None
    except Exception:
        return 0, 0, None, None

# ==============================================================================
# IFRAME & GAME URL EXTRACTOR
# ==============================================================================

class GameURLExtractor:
    """Extracts game iframe URL from Gamcore or other game portal pages."""

    @staticmethod
    def is_portal_url(url):
        parsed = urlparse(url)
        return 'gamcore.com' in parsed.netloc or 'gam-core.com' in parsed.netloc

    @staticmethod
    def extract_game_url(page, initial_url):
        print(f"[*] Connecting to portal: {initial_url} ...")
        try:
            page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
        except Exception as e:
            print(f"[!] Note while loading portal page: {e}")

        # Check for Sophos or Network block page
        title = page.title() or ""
        body_text = ""
        try:
            body_text = page.inner_text("body")[:300]
        except Exception:
            pass

        if "Blocked site" in title or "website is blocked" in body_text:
            print("\n" + "!" * 70)
            print("[!] ALERT: This network's firewall is blocking access to this website!")
            print("[!] Please switch to your unblocked Wi-Fi network or mobile hotspot.")
            print("!" * 70 + "\n")
            raise ConnectionError("Network firewall blocked access to the target website.")

        play_selectors = [
            "button:has-text('Play')", "a:has-text('Play')",
            "button:has-text('Start')", "a:has-text('Start')",
            "#play-btn", ".play-button", ".start-button",
            "button:has-text('I agree')", "button:has-text('Continue')"
        ]
        for sel in play_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    print(f"[*] Clicking prompt button ({sel})...")
                    el.click(timeout=3000)
                    time.sleep(2)
                    break
            except Exception:
                pass

        iframes = page.query_selector_all("iframe")
        candidates = []
        for ifr in iframes:
            try:
                src = ifr.get_attribute("src")
                if not src:
                    continue
                full_src = urljoin(initial_url, src)
                if any(ad in full_src.lower() for ad in AD_DOMAINS):
                    continue
                score = 0
                box = ifr.bounding_box()
                if box and box['width'] > 200 and box['height'] > 200:
                    score += 10
                if any(kw in full_src.lower() for kw in ['game', 'play', 'embed', 'discretehosting', 'content', 'html5', 'index.html']):
                    score += 10
                candidates.append((score, full_src))
            except Exception:
                continue

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            chosen_url = normalize_url(candidates[0][1])
            print(f"[+] Found Game Frame URL: {chosen_url}")
            return chosen_url

        for frame in page.frames:
            f_url = frame.url
            if f_url and f_url != initial_url and f_url != 'about:blank':
                if not any(ad in f_url.lower() for ad in AD_DOMAINS):
                    norm_f = normalize_url(f_url)
                    print(f"[+] Found Frame in page tree: {norm_f}")
                    return norm_f

        print("[*] No separate iframe detected. Treating initial URL as game entry point.")
        return normalize_url(initial_url)

# ==============================================================================
# ENGINE DETECTOR & INTROSPECTOR
# ==============================================================================

class EngineIntrospector:
    """Introspects the live browser context to identify game engine and metadata."""

    @staticmethod
    def inspect(page, captured_urls):
        result = {
            "engine": "unknown",
            "metadata": {}
        }

        renpy_match = any(
            re.search(r'(renpy|game\.zip|renpy\.wasm|renpy-pre\.js)', u, re.I)
            for u in captured_urls
        )
        if renpy_match:
            result["engine"] = "renpy"
            result["metadata"]["has_wasm"] = any('wasm' in u for u in captured_urls)
            result["metadata"]["game_zip"] = next((u for u in captured_urls if 'game.zip' in u.lower()), None)
            return result

        try:
            js_info = page.evaluate("""() => {
                const info = {};
                if (window.SugarCube) {
                    info.engine = "sugarcube";
                    info.version = window.SugarCube.version ? String(window.SugarCube.version) : null;
                    info.title = document.title || "";
                    info.hasStory = !!window.SugarCube.Story;
                    info.setup = window.setup ? {
                        ImagePath: window.setup.ImagePath ? String(window.setup.ImagePath) : null,
                        SoundPath: window.setup.SoundPath ? String(window.setup.SoundPath) : null,
                        Path: window.setup.Path ? String(window.setup.Path) : null
                    } : null;
                    return info;
                }
                if (document.querySelector('tw-storydata')) {
                    info.engine = "twine";
                    info.format = document.querySelector('tw-storydata').getAttribute('format') || "";
                    info.title = document.querySelector('tw-storydata').getAttribute('name') || document.title || "";
                    return info;
                }
                if (window.DataManager && (window.$dataSystem !== undefined || window.SceneManager)) {
                    info.engine = "rpgmaker";
                    info.type = window.rmmz_core ? "MZ" : "MV";
                    return info;
                }
                if (window.c2runtime || window.c3_runtime || window.cr_setSuspended) {
                    info.engine = "construct";
                    return info;
                }
                if (window.Phaser) {
                    info.engine = "phaser";
                    return info;
                }
                if (window.Engine && window.Godot) {
                    info.engine = "godot";
                    return info;
                }
                return { engine: "html5" };
            }""")
            result["engine"] = js_info.get("engine", "html5")
            result["metadata"] = js_info
            return result
        except Exception as e:
            print(f"[!] Warning during browser introspection: {e}")
            result["engine"] = "html5"
            return result

# ==============================================================================
# ENGINE HANDLERS
# ==============================================================================

class RenPyHandler:
    """Handles Ren'Py Web games: ensures game.zip, wasm, and engine scripts are grabbed."""

    @staticmethod
    def discover_assets(game_url, captured_urls, session, verify_ssl):
        assets = set()
        print("[*] Ren'Py Web detected. Probing for Ren'Py web package files...")

        standard_renpy_files = [
            'game.zip', 'game.zip.001', 'game.zip.002',
            'renpy.js', 'renpy-pre.js', 'renpy.wasm', 'renpy.data',
            'pyapp-data.js', 'pythonhome-data.js',
            'package.json', 'progressive_download.txt',
            'web-presplash.png', 'web-presplash.jpg',
            'renpy.png', 'icons/icon-192x192.png', 'icons/icon-512x512.png'
        ]

        base_parsed = urlparse(game_url)
        base_dir = os.path.dirname(base_parsed.path)
        if not base_dir.endswith('/'):
            base_dir += '/'

        for url in captured_urls:
            p = urlparse(url)
            if p.netloc == base_parsed.netloc and p.path.startswith(base_dir):
                rel = p.path[len(base_dir):].lstrip('/')
                if rel:
                    assets.add(rel)

        for fname in standard_renpy_files:
            if fname not in assets:
                probe_url = urljoin(game_url, fname)
                try:
                    r = session.head(probe_url, verify=verify_ssl, timeout=5)
                    if r.status_code == 200:
                        print(f"  [+] Found Ren'Py asset: {fname}")
                        assets.add(fname)
                except Exception:
                    pass

        pkg_url = urljoin(game_url, "package.json")
        try:
            r = session.get(pkg_url, verify=verify_ssl, timeout=5)
            if r.status_code == 200:
                data = r.json()
                for item in data.get("files", []):
                    assets.add(item.lstrip('/'))
        except Exception:
            pass

        return assets

class SugarCubeHandler:
    """Handles SugarCube / Twine games: resolves passages, story graph, choices, and macros."""

    @staticmethod
    def extract_js_object(text, var_name):
        """Finds `var_name = { ... }` or `setup.var_name = { ... }` and parses it into a Python dict."""
        m = re.search(r'(?:(?:var|let|const|setup\.)\s*)?' + re.escape(var_name) + r'\s*=\s*(\{)', text)
        if not m:
            return None
        start = m.start(1)
        depth = 0
        in_string = False
        quote_char = ''
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if ch == quote_char and text[i-1] != '\\':
                    in_string = False
            else:
                if ch in ('"', "'", '`'):
                    in_string = True
                    quote_char = ch
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end == -1:
            return None
        obj_str = text[start:end]
        obj_str = re.sub(r'//[^\n]*', '', obj_str)
        obj_str = re.sub(r'/\*.*?\*/', '', obj_str, flags=re.DOTALL)
        obj_str = re.sub(r'(?<=[\{,\n\r\t ])([a-zA-Z0-9_$]+)\s*:', r'"\1":', obj_str)
        obj_str = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', obj_str)
        obj_str = re.sub(r',\s*([\}\]])', r'\1', obj_str)
        try:
            return json.loads(obj_str)
        except Exception:
            return None

    @staticmethod
    def discover_dynamic_assets(html_content):
        dynamic_assets = set()
        unescaped = html.unescape(html_content)

        # 1. Look for pools (e.g. tribePools, slavePools, npcPools, etc.)
        pool_vars = re.findall(r'(?:setup\.)?(\w*Pools?)\s*=\s*\{', unescaped)
        for p_var in set(pool_vars):
            pool_dict = SugarCubeHandler.extract_js_object(unescaped, p_var)
            if isinstance(pool_dict, dict):
                base_category = "Tribe" if "tribe" in p_var.lower() else ("Slaves" if "slave" in p_var.lower() else p_var.replace("Pools", "").replace("pools", "").capitalize())
                for origin, data in pool_dict.items():
                    if isinstance(data, dict):
                        o_lower = origin.lower()
                        m_count = data.get("maleImgCount", 0) or data.get("maleCount", 0)
                        f_count = data.get("femaleImgCount", 0) or data.get("femaleCount", 0)
                        gen_count = data.get("imgCount", 0) or data.get("count", 0)
                        for i in range(1, int(m_count) + 1):
                            dynamic_assets.add(f"images/NPC/{base_category}/male/{o_lower}/{i}.webp")
                        for i in range(1, int(f_count) + 1):
                            dynamic_assets.add(f"images/NPC/{base_category}/female/{o_lower}/{i}.webp")
                        for i in range(1, int(gen_count) + 1):
                            dynamic_assets.add(f"images/NPC/{base_category}/{o_lower}/{i}.webp")

        # 2. Look for action / video count tables (e.g. genericCounts, specificCounts)
        gc = SugarCubeHandler.extract_js_object(unescaped, "genericCounts")
        if isinstance(gc, dict):
            for combo, actions in gc.items():
                if isinstance(actions, dict) and "_" in combo:
                    p_gen, n_gen = combo.split("_", 1)
                    for prefix, count in actions.items():
                        if isinstance(count, (int, float)):
                            for v in range(1, int(count) + 1):
                                dynamic_assets.add(f"images/intimacy/{p_gen}_player/with_{n_gen}/generic/{prefix}_{v}.webm")

        sc = SugarCubeHandler.extract_js_object(unescaped, "specificCounts")
        if isinstance(sc, dict):
            for scene_key, actions in sc.items():
                if isinstance(actions, dict) and "_" in scene_key:
                    parts = scene_key.split("_")
                    if len(parts) >= 3:
                        p_gen, n_gen = parts[0], parts[1]
                        origin = "_".join(parts[2:])
                        for prefix, count in actions.items():
                            if isinstance(count, (int, float)):
                                for v in range(1, int(count) + 1):
                                    dynamic_assets.add(f"images/intimacy/{p_gen}_player/with_{n_gen}/{origin}/{prefix}_{v}.webm")

        # 3. Look for explicit img/video properties in setup or object presets
        for m in re.finditer(r'(?:img|video|profile|fight|image|bg|sound|audio)\s*:\s*[\'"]([^\'"\r\n\t<>]+)[\'"]', unescaped):
            val = m.group(1).strip()
            if "." in val and ("/" in val or any(val.lower().endswith(ext) for ext in ['.webp', '.png', '.jpg', '.webm', '.mp4', '.ogg', '.mp3'])):
                dynamic_assets.add(val.replace('\\', '/').lstrip('/'))

        return dynamic_assets

    @staticmethod
    def parse_story(html_content, game_url, metadata):
        discovered_assets = []
        external_assets = {}
        unescaped = html.unescape(html_content)

        image_prefix = ""
        sound_prefix = ""
        video_prefix = ""

        setup_meta = metadata.get("metadata", {}).get("setup", {}) if isinstance(metadata, dict) else {}
        if setup_meta:
            if setup_meta.get("ImagePath"):
                image_prefix = setup_meta["ImagePath"].lstrip('/')
            if setup_meta.get("SoundPath"):
                sound_prefix = setup_meta["SoundPath"].lstrip('/')

        m_img = re.search(r'(?:setup\.ImagePath|window\.imageBaseUrl|imageBaseUrl)\s*=\s*(?:setup\.Path\s*\+\s*)?[\'"]([^\'"]*)[\'"]', unescaped)
        if m_img:
            image_prefix = m_img.group(1).lstrip('/')

        m_vid = re.search(r'(?:setup\.VideoPath|window\.videoBaseUrl|videoBaseUrl)\s*=\s*(?:setup\.Path\s*\+\s*)?[\'"]([^\'"]*)[\'"]', unescaped)
        if m_vid:
            video_prefix = m_vid.group(1).lstrip('/')

        m_snd = re.search(r'(?:setup\.SoundPath|window\.soundBaseUrl|window\.audioBaseUrl|soundBaseUrl|audioBaseUrl)\s*=\s*(?:setup\.Path\s*\+\s*)?[\'"]([^\'"]*)[\'"]', unescaped)
        if m_snd:
            sound_prefix = m_snd.group(1).lstrip('/')

        print(f"[+] SugarCube Setup paths -> imagePrefix: '{image_prefix}', videoPrefix: '{video_prefix}', soundPrefix: '{sound_prefix}'")

        # 1. Parse passages with PID and Name
        passages_raw = re.findall(r'<tw-passagedata[^>]*pid="([^"]*)"[^>]*name="([^"]*)"[^>]*>(.*?)</tw-passagedata>', html_content, re.DOTALL)
        if not passages_raw:
            passages_raw = [(str(i+1), n, b) for i, (n, b) in enumerate(re.findall(r'<tw-passagedata[^>]*name="([^"]*)"[^>]*>(.*?)</tw-passagedata>', html_content, re.DOTALL))]
        if not passages_raw:
            # Twine 1 / Sugarcane / Jonah / Responsive tiddler support
            tiddlers = re.findall(r'<div[^>]*tiddler="([^"]*)"[^>]*>(.*?)</div>', html_content, re.DOTALL)
            passages_raw = [(str(i+1), n, b) for i, (n, b) in enumerate(tiddlers)]
        print(f"[+] Extracted {len(passages_raw)} passages from story data.")

        pid_to_name = {pid: name for pid, name, _ in passages_raw}
        name_to_passage = {name: (pid, p_body) for pid, name, p_body in passages_raw}

        # 2. Build Story Graph
        story_graph = {}
        for pid, name, p_text in passages_raw:
            p_unescaped = html.unescape(p_text)
            links = []
            for l_match in re.finditer(r'\[\[(.*?)\]\]', p_unescaped):
                link_content = l_match.group(1).strip()
                if '->' in link_content:
                    target = link_content.split('->', 1)[1].strip()
                elif '<-' in link_content:
                    target = link_content.split('<-', 1)[0].strip()
                elif '|' in link_content:
                    target = link_content.split('|', 1)[1].strip()
                else:
                    target = link_content
                if target:
                    links.append(target)
            for m_link in re.finditer(r'<<(?:link|button)\s+[\'"][^\'"]+[\'"](?:\s+[\'"]([^\'"]+)[\'"])?', p_unescaped):
                if m_link.group(1):
                    links.append(m_link.group(1).strip())
            for m_goto in re.finditer(r'<<(?:goto|include|display)\s+[\'"]([^\'"]+)[\'"]', p_unescaped):
                links.append(m_goto.group(1).strip())
            for m_data in re.finditer(r'data-passage=[\'"]([^\'"]+)[\'"]', p_unescaped):
                links.append(m_data.group(1).strip())
            story_graph[name] = links

        # 3. Determine story start node
        start_m = re.search(r'startnode="(\d+)"', html_content)
        start_pid = start_m.group(1) if start_m else "1"
        start_name = pid_to_name.get(start_pid, "Lets Go")

        # 4. BFS story progression traversal
        ordered_passages = []
        visited_passages = set()

        # Special UI passages first (StoryBanner, StoryInit, StoryCaption, StoryMenu, etc.)
        special_passages = ["StoryInit", "StoryBanner", "StoryCaption", "StoryMenu", "PassageHeader", "PassageFooter"]
        for sp in special_passages:
            if sp in name_to_passage and sp not in visited_passages:
                visited_passages.add(sp)
                ordered_passages.append(sp)

        # BFS queue from start
        queue = [start_name]
        if start_name in name_to_passage:
            visited_passages.add(start_name)

        while queue:
            curr = queue.pop(0)
            ordered_passages.append(curr)
            for nxt in story_graph.get(curr, []):
                if nxt in name_to_passage and nxt not in visited_passages:
                    visited_passages.add(nxt)
                    queue.append(nxt)

        # Append any unreached / dynamic passages
        for pid, name, _ in passages_raw:
            if name not in visited_passages:
                visited_passages.add(name)
                ordered_passages.append(name)

        vid_exts = ('.mp4', '.webm', '.avi', '.mov', '.mkv', '.m4v')
        img_exts = ('.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.bmp', '.ico', '.avif')
        snd_exts = ('.mp3', '.ogg', '.wav', '.flac', '.m4a', '.aac', '.opus')

        def resolve_media_path(raw_path):
            c = clean_relative_path(raw_path)
            if not c:
                return None
            c_lower = c.lower()
            if c_lower.startswith(('images/', 'img/', 'videos/', 'video/', 'sounds/', 'sound/', 'audio/', 'media/', 'assets/')):
                return c
            if c_lower.endswith(vid_exts):
                if video_prefix and not c_lower.startswith(video_prefix.lower()):
                    c = (video_prefix + c).replace('\\', '/')
            elif c_lower.endswith(img_exts):
                if image_prefix and not c_lower.startswith(image_prefix.lower()):
                    c = (image_prefix + c).replace('\\', '/')
            elif c_lower.endswith(snd_exts):
                if sound_prefix and not c_lower.startswith(sound_prefix.lower()):
                    c = (sound_prefix + c).replace('\\', '/')
            return c

        # 5. Extract assets per passage in story progression order
        def extract_passage_media(text):
            imgs = []
            vids = []

            # HTML tags (img, video, audio, source, etc.)
            for m in re.finditer(r'<(?:video|audio|source|img|script|link)[^>]+(?:src|href)=[\'"]([^\'"]+)[\'"]', text, re.I):
                val = m.group(1).strip()
                if val.startswith(('http://', 'https://')):
                    p = urlparse(val)
                    external_assets[val] = f"external/{p.netloc}{p.path}"
                else:
                    c = resolve_media_path(val)
                    if c:
                        if c.lower().endswith(vid_exts):
                            vids.append(c)
                        else:
                            imgs.append(c)

            # SugarCube [img[...]] syntax
            for raw in re.findall(r'\[img\[(.*?)\]\]', text):
                for part in raw.split(']['):
                    part = part.strip()
                    if '|' in part:
                        part = part.split('|', 1)[1].strip()
                    c = resolve_media_path(part)
                    if c:
                        if c.lower().endswith(vid_exts):
                            vids.append(c)
                        else:
                            imgs.append(c)

            # SugarCube macro tags (<<video>>, <<imgbg>>, <<videobg>>, <<char>>, etc.)
            for m in re.finditer(r'<<(?:video|audio|vid|videobg|img|imgbg|char|instapost)\s+[\'"]([^\'"]+)[\'"]', text, re.I):
                c = resolve_media_path(m.group(1))
                if c:
                    if c.lower().endswith(vid_exts):
                        vids.append(c)
                    else:
                        imgs.append(c)

            # Quoted strings with media extensions inside the passage
            ext_pattern = '|'.join(MEDIA_EXTENSIONS)
            for m in re.finditer(r'[\'"]([^\'"\r\n\t<>]+\.(?:' + ext_pattern + r'))[\'"]', text, re.I):
                val = m.group(1).strip()
                if val.startswith(('http://', 'https://')):
                    p = urlparse(val)
                    external_assets[val] = f"external/{p.netloc}{p.path}"
                else:
                    c = resolve_media_path(val)
                    if c:
                        if c.lower().endswith(vid_exts):
                            vids.append(c)
                        else:
                            imgs.append(c)

            # Audio macros
            for m in re.finditer(r'<<cacheaudio\s+[\'"][^\'"]+[\'"]\s+[\'"]([^\'"]+)[\'"]', text):
                c = resolve_media_path(m.group(1))
                if c:
                    imgs.append(c)

            # Setup paths
            for m in re.finditer(r'setup\.ImagePath\s*\+\s*[\'"]([^\'"]+)[\'"]', text):
                c = resolve_media_path(m.group(1).lstrip('/'))
                if c:
                    imgs.append(c)
            for m in re.finditer(r'setup\.SoundPath\s*\+\s*[\'"]([^\'"]+)[\'"]', text):
                c = resolve_media_path(m.group(1).lstrip('/'))
                if c:
                    imgs.append(c)

            return imgs, vids

        seen_assets = set()

        # Step A: Collect story passages in priority order (images/audio first, then videos)
        for p_name in ordered_passages:
            _, p_body = name_to_passage[p_name]
            p_text = html.unescape(p_body)
            p_imgs, p_vids = extract_passage_media(p_text)
            for img in p_imgs:
                if img not in seen_assets:
                    seen_assets.add(img)
                    discovered_assets.append(img)
            for vid in p_vids:
                if vid not in seen_assets:
                    seen_assets.add(vid)
                    discovered_assets.append(vid)

        # Step A2: Dynamic asset templates (pools, intimacy count objects, setup dictionaries)
        dynamic_candidates = SugarCubeHandler.discover_dynamic_assets(html_content)
        for dyn in dynamic_candidates:
            c = resolve_media_path(dyn)
            if c and c not in seen_assets:
                seen_assets.add(c)
                discovered_assets.append(c)

        # Step B: Fallback regex scan across global unescaped HTML/scripts
        ext_pattern = '|'.join(MEDIA_EXTENSIONS)
        for m in re.finditer(r'[\'"]([^\'"\r\n\t<>]+\.(?:' + ext_pattern + r'))[\'"]', unescaped, re.I):
            token = m.group(1).strip('\'" \t\r\n')
            if token.startswith(('http://', 'https://')):
                p = urlparse(token)
                external_assets[token] = f"external/{p.netloc}{p.path}"
            else:
                c = resolve_media_path(token)
                if c and c not in seen_assets:
                    seen_assets.add(c)
                    discovered_assets.append(c)

        print(f"[+] SugarCube story traversal prioritized {len(discovered_assets)} assets sequentially.")
        return discovered_assets, external_assets

class RPGMakerHandler:
    """Handles RPG Maker MV/MZ games: parses data/*.json for assets."""

    @staticmethod
    def discover_assets(game_url, session, verify_ssl):
        assets = set()
        print("[*] RPG Maker detected. Fetching data/*.json database manifests...")
        data_files = [
            'System.json', 'Actors.json', 'Classes.json', 'Skills.json',
            'Items.json', 'Weapons.json', 'Armors.json', 'Enemies.json',
            'Troops.json', 'States.json', 'Animations.json', 'Tilesets.json',
            'CommonEvents.json', 'MapInfos.json'
        ]

        # Check for encrypted assets
        encrypted = False
        sys_url = urljoin(game_url, "data/System.json")
        try:
            r = session.get(sys_url, verify=verify_ssl, timeout=5)
            if r.status_code == 200:
                sys_data = r.json()
                if sys_data.get("hasEncryptedImages") or sys_data.get("hasEncryptedAudio") or "encryptionKey" in sys_data:
                    encrypted = True
                    print("  [+] RPG Maker encrypted asset container detected.")
        except Exception:
            pass

        for df in data_files:
            rel = f"data/{df}"
            assets.add(rel)
            url = urljoin(game_url, rel)
            try:
                r = session.get(url, verify=verify_ssl, timeout=5)
                if r.status_code == 200:
                    text = r.text
                    for m in re.finditer(r'[\'"]([a-zA-Z0-9_\-]+)\.(?:png|rpgmvp)[\'"]', text, re.I):
                        name = m.group(1)
                        for sub in ['characters', 'faces', 'pictures', 'sv_actors', 'sv_enemies', 'system', 'tilesets', 'titles1', 'titles2']:
                            assets.add(f"img/{sub}/{name}.png")
                            if encrypted:
                                assets.add(f"img/{sub}/{name}.rpgmvp")
                                assets.add(f"img/{sub}/{name}.png_")
                    for m in re.finditer(r'[\'"]name[\'"]\s*:\s*[\'"]([a-zA-Z0-9_\-]+)[\'"]', text):
                        name = m.group(1)
                        if name:
                            for sub in ['bgm', 'bgs', 'me', 'se']:
                                assets.add(f"audio/{sub}/{name}.ogg")
                                assets.add(f"audio/{sub}/{name}.m4a")
                                if encrypted:
                                    assets.add(f"audio/{sub}/{name}.rpgmvo")
                                    assets.add(f"audio/{sub}/{name}.rpgmvm")
                                    assets.add(f"audio/{sub}/{name}.ogg_")
                                    assets.add(f"audio/{sub}/{name}.m4a_")
            except Exception:
                pass

        return assets

class ConstructHandler:
    """Handles Construct 2/3 games: parses data.json manifest."""

    @staticmethod
    def discover_assets(game_url, session, verify_ssl):
        assets = set()
        print("[*] Construct engine detected. Checking project manifests...")
        manifest_urls = [urljoin(game_url, "data.json"), urljoin(game_url, "scripts/c3runtime.js")]
        for u in manifest_urls:
            try:
                r = session.get(u, verify=verify_ssl, timeout=5)
                if r.status_code == 200:
                    for m in re.finditer(r'[\'"]([a-zA-Z0-9_\-./]+\.(?:' + '|'.join(MEDIA_EXTENSIONS) + r'))[\'"]', r.text, re.I):
                        c = clean_relative_path(m.group(1))
                        if c:
                            assets.add(c)
            except Exception:
                pass
        return assets

class HTML5Handler:
    """Handles generic HTML5, PixiJS, Phaser, custom WebGL, and Visual Novel engines:
    Recursively discovers scripts, stylesheets, and JSON databases, and extracts static and dynamic media assets."""

    @staticmethod
    def discover_assets(game_url, page_html, session, verify_ssl, captured_urls=None, captured_responses=None):
        discovered = set()
        scanned_files = set()
        queue = []
        captured_responses = captured_responses or {}
        captured_urls = captured_urls or set()

        print("[*] HTML5 engine: Performing deep recursive script and database inspection...")

        # 1. Start with the root HTML page
        queue.append(("index.html", page_html))

        # 2. Add any script, json, or css files already captured in runtime memory
        parsed_game_url = urlparse(game_url)
        base_game_dir = os.path.dirname(parsed_game_url.path)
        if not base_game_dir.endswith('/'):
            base_game_dir += '/'

        for u, body in captured_responses.items():
            pu = urlparse(u)
            if pu.netloc == parsed_game_url.netloc and pu.path.startswith(base_game_dir):
                rel = pu.path[len(base_game_dir):].lstrip('/')
                clean_rel = clean_relative_path(rel)
                ext = clean_rel.rsplit('.', 1)[-1].lower() if '.' in clean_rel else ''
                if ext in ['js', 'json', 'css', 'html', 'txt']:
                    try:
                        text = body.decode('utf-8', errors='ignore')
                        queue.append((clean_rel, text))
                    except Exception:
                        pass

        # Also find all scripts and stylesheets in the HTML
        scripts = re.findall(r'<script[^>]+src=[\'"]([^\'"]+)[\'"]', page_html, re.I)
        styles = re.findall(r'<link[^>]+(?:href|src)=[\'"]([^\'"]+)[\'"]', page_html, re.I)
        for s in scripts + styles:
            c = clean_relative_path(s)
            if c:
                discovered.add(c)
                if c not in scanned_files:
                    queue.append((c, None))

        img_vid_exts = ('.gif', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.webm', '.mov', '.m4v')

        while queue:
            file_name, text = queue.pop(0)
            if file_name in scanned_files:
                continue
            scanned_files.add(file_name)

            # If text is None, attempt to fetch the file content
            if text is None:
                matched_body = None
                for u, body in captured_responses.items():
                    if u.split('?')[0].endswith(file_name.lstrip('/')):
                        matched_body = body
                        break
                if matched_body:
                    try:
                        text = matched_body.decode('utf-8', errors='ignore')
                    except Exception:
                        text = None
                elif session:
                    try:
                        full_cand_url = urljoin(game_url, file_name)
                        r = session.get(full_cand_url, verify=verify_ssl, timeout=5)
                        if r.status_code == 200:
                            text = r.text
                    except Exception:
                        pass

            if not text:
                continue

            # A. Scan for quoted paths with known extensions
            ext_pattern = '|'.join(ALL_EXTENSIONS)
            for m in re.finditer(r'[\'"]([a-zA-Z0-9_\-/\\]+\.(?:' + ext_pattern + r'))[\'"]', text, re.I):
                val = m.group(1).replace('\\', '/').lstrip('/')
                if not val.startswith(('http://', 'https://', 'data:')):
                    c = clean_relative_path(val)
                    if c:
                        discovered.add(c)
                        c_ext = c.rsplit('.', 1)[-1].lower() if '.' in c else ''
                        if c_ext in ['json', 'js', 'css'] and c not in scanned_files and len(scanned_files) < 150:
                            queue.append((c, None))

            # B. Extract extensionless or dynamic media object references (common in VN engines)
            vid_matches = re.findall(r'\b(?:video|clip|anim|media|scene|image|img|sound|audio)\s*:\s*[\'"]([^\'"\r\n\t<>]+)[\'"]', text, re.I)
            vid_assigns = re.findall(r'(?:\.video|\.image|\.src)\s*=\s*[\'"]([^\'"\r\n\t<>]+)[\'"]', text, re.I)
            all_media_ids = set(vid_matches + vid_assigns)

            # Detect potential base media folders
            base_folders = set(re.findall(r'[\'"]([a-zA-Z0-9_\-]+/(?:adult|videos|media|scenes|movies|images|audio|sounds)/)[\'"]', text, re.I))
            if not base_folders:
                base_folders = {"assets/adult/", "assets/videos/", "videos/", "assets/", "images/"}

            for v_id in all_media_ids:
                v_clean = v_id.replace('\\', '/').strip('/')
                if not v_clean or v_clean.startswith(('http://', 'https://', 'data:')) or len(v_clean) > 120:
                    continue
                # If it already has a media extension
                if v_clean.lower().endswith(img_vid_exts):
                    discovered.add(v_clean)
                    for bf in base_folders:
                        discovered.add(f"{bf}{v_clean}")
                else:
                    # Extensionless ID: candidate for .mp4, .webm, .webp, .png
                    for ext in ['.mp4', '.webm', '.webp', '.png']:
                        cand = f"{v_clean}{ext}"
                        discovered.add(cand)
                        for bf in base_folders:
                            discovered.add(f"{bf}{cand}")
                            # Also check character subdirectories if not qualified
                            if "/" not in v_clean:
                                for char_name in ["lily", "maya", "noelle", "sabrina", "claire", "vivian", "chloe", "zoe", "jade", "amber", "kira", "harper", "serena"]:
                                    if v_clean.lower().startswith(char_name):
                                        discovered.add(f"{bf}{char_name}/{cand}")
                                        break

        print(f"[+] HTML5 deep inspection discovered {len(discovered)} assets across {len(scanned_files)} files.")
        return discovered

# ==============================================================================
# ATOMIC & RESUMABLE MULTI-THREADED ASSET DOWNLOADER
# ==============================================================================

class AssetDownloader:
    """Downloads files with streaming, multi-threading, Range-resuming, data-limit caps, atomic .part renaming, RFC 7232 conditional updates, and cryptographic ETag reuse."""

    def __init__(self, base_url, download_folder, session, verify_ssl=True, max_workers=8, max_mb=None, diff_check=False, etag_index=None, size_index=None, manifest=None):
        self.base_url = base_url
        self.download_folder = download_folder
        self.session = session
        self.verify_ssl = verify_ssl
        self.max_workers = max_workers
        self.max_mb = max_mb
        self.max_bytes = int(max_mb * 1024 * 1024) if (max_mb and max_mb > 0) else None
        self.total_downloaded_bytes = 0
        self.stop_requested = False
        self._lock = threading.Lock()
        self.diff_check = diff_check
        self.etag_index = etag_index if etag_index is not None else {}
        self.size_index = size_index if size_index is not None else {}
        self.manifest = manifest if manifest is not None else {}
        self.cached_count = 0
        self.reused_count = 0
        self.updated_count = 0
        self.downloaded_count = 0

    def _handle_index_html_update(self, local_path, r, ret_etag, rel_p):
        """Prevents false modification detections on index.html due to injected Auto-Audio manager,
        and re-applies Auto-Audio manager if index.html was genuinely updated on the server."""
        try:
            with open(local_path, "r", encoding="utf-8", errors="replace") as lf:
                local_text = lf.read()
            remote_text = r.content.decode('utf-8', errors='replace')
            if normalize_html_for_comparison(local_text) == normalize_html_for_comparison(remote_text):
                with self._lock:
                    self.cached_count += 1
                return True, 0, "cached"
            had_auto_audio = "auto-audio-manager" in local_text
            if had_auto_audio and "auto-audio-manager" not in remote_text:
                if "</body>" in remote_text:
                    remote_text = remote_text.replace("</body>", AUTO_AUDIO_SCRIPT + "\n</body>", 1)
                elif "</html>" in remote_text:
                    remote_text = remote_text.replace("</html>", AUTO_AUDIO_SCRIPT + "\n</html>", 1)
                else:
                    remote_text += "\n" + AUTO_AUDIO_SCRIPT
            part_path = local_path + ".part"
            with open(part_path, "w", encoding="utf-8") as f:
                f.write(remote_text)
            os.replace(part_path, local_path)
            with self._lock:
                self.updated_count += 1
                self.manifest[rel_p] = {"etag": ret_etag, "size": os.path.getsize(local_path), "mtime": time.time()}
            return True, len(remote_text.encode('utf-8')), "updated"
        except Exception:
            return None

    def download_file(self, full_url, local_path):
        if self.stop_requested:
            return False, 0, "limit_reached"

        # Security: Prevent path traversal outside download_folder
        try:
            canonical_target = os.path.abspath(self.download_folder)
            canonical_local = os.path.abspath(local_path)
            if not canonical_local.startswith(canonical_target + os.sep):
                return False, 0, "path_traversal_rejected"
        except Exception:
            return False, 0, "path_traversal_rejected"

        folder = os.path.dirname(local_path)
        if folder:
            os.makedirs(folder, exist_ok=True)

        rel_p = os.path.relpath(local_path, self.download_folder).replace("\\", "/")
        is_modified = False

        # 1. Existing local file handling
        if os.path.exists(local_path):
            size = os.path.getsize(local_path)
            if size == 715 or size == 0:
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            elif self.diff_check:
                # RFC 7232 HTTP Conditional Request (1 network round trip)
                entry = self.manifest.get(rel_p, {})
                known_etag = entry.get("etag")
                mtime = entry.get("mtime")

                if not known_etag:
                    try:
                        st = os.stat(local_path)
                        mtime = st.st_mtime
                        if st.st_size < 100 * 1024 * 1024:
                            known_etag = file_md5(local_path)
                            if known_etag:
                                with self._lock:
                                    self.manifest[rel_p] = {"etag": known_etag, "size": st.st_size, "mtime": mtime}
                                    self.etag_index[known_etag.strip('"').lower()] = local_path
                    except Exception:
                        pass

                headers = {}
                if self.base_url:
                    headers["Referer"] = self.base_url
                if known_etag:
                    headers["If-None-Match"] = f'"{known_etag.strip(chr(34))}"'
                if mtime:
                    headers["If-Modified-Since"] = formatdate(mtime, usegmt=True)

                parsed_url = urlparse(full_url)
                safe_path = quote(unquote(parsed_url.path.replace('+', '%2B')), safe="/()'!~,-_.@")
                safe_request_url = urlunparse((parsed_url.scheme, parsed_url.netloc, safe_path, parsed_url.params, parsed_url.query, parsed_url.fragment))

                try:
                    r = self.session.get(safe_request_url, headers=headers, verify=self.verify_ssl, stream=True, timeout=25)
                    if r.status_code == 304:
                        # 100% verified unchanged by server
                        with self._lock:
                            self.cached_count += 1
                        return True, 0, "cached"
                    elif r.status_code == 200:
                        ret_etag = r.headers.get("ETag", "").strip('"').lower()
                        ret_len = int(r.headers.get("Content-Length", 0) or 0)
                        if known_etag and ret_etag == known_etag.lower() and ret_len == os.path.getsize(local_path):
                            with self._lock:
                                self.cached_count += 1
                            return True, 0, "cached"

                        if rel_p.lower() == "index.html":
                            html_res = self._handle_index_html_update(local_path, r, ret_etag, rel_p)
                            if html_res is not None:
                                return html_res

                        # File was modified! Stream new content to .part file
                        is_modified = True
                        part_path = local_path + ".part"
                        downloaded_bytes = 0
                        with open(part_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                                    downloaded_bytes += len(chunk)
                                    with self._lock:
                                        self.total_downloaded_bytes += len(chunk)
                        os.replace(part_path, local_path)
                        new_etag = ret_etag or (file_md5(local_path) if downloaded_bytes < 100 * 1024 * 1024 else None)
                        with self._lock:
                            self.updated_count += 1
                            self.manifest[rel_p] = {"etag": new_etag, "size": downloaded_bytes, "mtime": time.time()}
                        return True, downloaded_bytes, "updated"
                    elif r.status_code in [404, 403]:
                        return False, 0, f"http_{r.status_code}"
                except requests.exceptions.SSLError:
                    self.verify_ssl = False
                    try:
                        r = self.session.get(safe_request_url, headers=headers, verify=False, stream=True, timeout=25)
                        if r.status_code == 304:
                            with self._lock:
                                self.cached_count += 1
                            return True, 0, "cached"
                        elif r.status_code == 200:
                            ret_etag = r.headers.get("ETag", "").strip('"').lower()
                            ret_len = int(r.headers.get("Content-Length", 0) or 0)
                            if known_etag and ret_etag == known_etag.lower() and ret_len == os.path.getsize(local_path):
                                with self._lock:
                                    self.cached_count += 1
                                return True, 0, "cached"

                            if rel_p.lower() == "index.html":
                                html_res = self._handle_index_html_update(local_path, r, ret_etag, rel_p)
                                if html_res is not None:
                                    return html_res
                            is_modified = True
                            part_path = local_path + ".part"
                            downloaded_bytes = 0
                            with open(part_path, "wb") as f:
                                for chunk in r.iter_content(chunk_size=65536):
                                    if chunk:
                                        f.write(chunk)
                                        downloaded_bytes += len(chunk)
                                        with self._lock:
                                            self.total_downloaded_bytes += len(chunk)
                            os.replace(part_path, local_path)
                            new_etag = ret_etag or (file_md5(local_path) if downloaded_bytes < 100 * 1024 * 1024 else None)
                            with self._lock:
                                self.updated_count += 1
                                self.manifest[rel_p] = {"etag": new_etag, "size": downloaded_bytes, "mtime": time.time()}
                            return True, downloaded_bytes, "updated"
                        elif r.status_code in [404, 403]:
                            return False, 0, f"http_{r.status_code}"
                    except Exception:
                        with self._lock:
                            self.cached_count += 1
                        return True, 0, "cached"
                except Exception:
                    with self._lock:
                        self.cached_count += 1
                    return True, 0, "cached"
            else:
                with self._lock:
                    self.cached_count += 1
                return True, 0, "cached"

        # 2. Rename & relocation detection (Cryptographic MD5 Matching)
        if not os.path.exists(local_path) and not is_modified:
            status_code, remote_len, remote_etag, remote_lm = get_remote_file_info(
                self.session, full_url, referer=self.base_url, verify_ssl=self.verify_ssl
            )
            clean_etag = (remote_etag or "").strip('"').lower()

            # Strategy 1: Direct cryptographic MD5 ETag match (0% error)
            if clean_etag and len(clean_etag) == 32 and clean_etag in self.etag_index:
                cand_path = self.etag_index[clean_etag]
                if os.path.exists(cand_path):
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    try:
                        os.link(cand_path, local_path)
                    except Exception:
                        shutil.copy2(cand_path, local_path)
                    with self._lock:
                        self.reused_count += 1
                        self.manifest[rel_p] = {"etag": clean_etag, "size": os.path.getsize(local_path), "mtime": time.time()}
                    return True, 0, "reused_rename"

            # Strategy 2: Size match + 8KB byte prefix match (Fallback for non-MD5 ETags)
            if remote_len > 10240 and remote_len in self.size_index:
                cand_paths = self.size_index[remote_len]
                for cand_path in cand_paths:
                    if os.path.exists(cand_path) and os.path.getsize(cand_path) == remote_len:
                        try:
                            with open(cand_path, 'rb') as cf:
                                cand_prefix = cf.read(8192)
                            parsed_u = urlparse(full_url)
                            sp = quote(unquote(parsed_u.path.replace('+', '%2B')), safe="/()'!~,-_.@")
                            su = urlunparse((parsed_u.scheme, parsed_u.netloc, sp, parsed_u.params, parsed_u.query, parsed_u.fragment))
                            rh = {"Range": "bytes=0-8191"}
                            if self.base_url:
                                rh["Referer"] = self.base_url
                            rr = self.session.get(su, headers=rh, verify=self.verify_ssl, timeout=8)
                            if rr.status_code in [200, 206] and len(rr.content) > 0 and rr.content[:len(cand_prefix)] == cand_prefix:
                                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                                try:
                                    os.link(cand_path, local_path)
                                except Exception:
                                    shutil.copy2(cand_path, local_path)
                                with self._lock:
                                    self.reused_count += 1
                                    self.manifest[rel_p] = {"etag": clean_etag, "size": remote_len, "mtime": time.time()}
                                return True, 0, "reused_rename"
                        except Exception:
                            pass

        if self.max_bytes:
            with self._lock:
                if self.total_downloaded_bytes >= self.max_bytes:
                    self.stop_requested = True
                    return False, 0, "limit_reached"

        # 3. Use .part file for atomic download and safe resuming
        part_path = local_path + ".part"
        downloaded_bytes = 0
        resume_offset = 0

        if os.path.exists(part_path):
            resume_offset = os.path.getsize(part_path)

        ext = local_path.rsplit('.', 1)[-1].lower() if '.' in local_path else ''
        is_media = ext in MEDIA_EXTENSIONS and ext not in ['json', 'xml', 'txt', 'csv']

        parsed_url = urlparse(full_url)
        safe_path = quote(unquote(parsed_url.path.replace('+', '%2B')), safe="/()'!~,-_.@")
        safe_request_url = urlunparse((parsed_url.scheme, parsed_url.netloc, safe_path, parsed_url.params, parsed_url.query, parsed_url.fragment))

        for attempt in range(5):
            if self.stop_requested:
                return False, 0, "limit_reached"

            try:
                headers = {}
                if self.base_url:
                    headers["Referer"] = self.base_url
                if resume_offset > 0:
                    headers["Range"] = f"bytes={resume_offset}-"

                r = self.session.get(safe_request_url, headers=headers, verify=self.verify_ssl, stream=True, timeout=25)

                content_type = r.headers.get("Content-Type", "").lower()
                if is_media and "text/html" in content_type:
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                    return False, 0, "captive_portal_intercept"

                if r.status_code == 206:
                    expected_total = -1
                    cr = r.headers.get("Content-Range", "")
                    if "/" in cr:
                        try:
                            cr_total = int(cr.rsplit("/", 1)[1].strip())
                            if cr_total > 0:
                                expected_total = cr_total
                        except Exception:
                            pass
                    cl = int(r.headers.get("Content-Length", 0) or 0)
                    if expected_total <= 0 and cl > 0:
                        expected_total = resume_offset + cl

                    with open(part_path, 'ab') as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                downloaded_bytes += len(chunk)
                                with self._lock:
                                    self.total_downloaded_bytes += len(chunk)
                                    if self.max_bytes and self.total_downloaded_bytes >= self.max_bytes:
                                        self.stop_requested = True
                                        break
                    if self.stop_requested:
                        return False, downloaded_bytes, "limit_reached"

                    if expected_total > 0 and os.path.exists(part_path) and os.path.getsize(part_path) < expected_total:
                        raise IOError(f"Truncated stream ({os.path.getsize(part_path)}/{expected_total})")

                    os.replace(part_path, local_path)
                    status_name = "updated" if is_modified else "resumed"
                    new_etag = r.headers.get("ETag", "").strip('"').lower() or (file_md5(local_path) if os.path.getsize(local_path) < 100 * 1024 * 1024 else None)
                    with self._lock:
                        if is_modified:
                            self.updated_count += 1
                        self.manifest[rel_p] = {"etag": new_etag, "size": os.path.getsize(local_path), "mtime": time.time()}
                    return True, downloaded_bytes, status_name

                elif r.status_code == 200:
                    if rel_p.lower() == "index.html" and os.path.exists(local_path):
                        html_res = self._handle_index_html_update(local_path, r, r.headers.get("ETag", "").strip('"').lower(), rel_p)
                        if html_res is not None:
                            return html_res

                    cl = int(r.headers.get("Content-Length", 0) or 0)
                    expected_total = cl if cl > 0 else -1

                    with open(part_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                                downloaded_bytes += len(chunk)
                                with self._lock:
                                    self.total_downloaded_bytes += len(chunk)
                                    if self.max_bytes and self.total_downloaded_bytes >= self.max_bytes:
                                        self.stop_requested = True
                                        break
                    if self.stop_requested:
                        return False, downloaded_bytes, "limit_reached"

                    if expected_total > 0 and os.path.exists(part_path) and os.path.getsize(part_path) < expected_total:
                        raise IOError(f"Truncated stream ({os.path.getsize(part_path)}/{expected_total})")

                    os.replace(part_path, local_path)
                    status_name = "updated" if is_modified else "downloaded"
                    new_etag = r.headers.get("ETag", "").strip('"').lower() or (file_md5(local_path) if os.path.getsize(local_path) < 100 * 1024 * 1024 else None)
                    with self._lock:
                        if is_modified:
                            self.updated_count += 1
                        else:
                            self.downloaded_count += 1
                        self.manifest[rel_p] = {"etag": new_etag, "size": os.path.getsize(local_path), "mtime": time.time()}
                    return True, downloaded_bytes, status_name

                elif r.status_code == 416:
                    if os.path.exists(part_path) and os.path.getsize(part_path) > 715:
                        os.replace(part_path, local_path)
                        status_name = "updated" if is_modified else "completed"
                        new_etag = file_md5(local_path) if os.path.getsize(local_path) < 100 * 1024 * 1024 else None
                        with self._lock:
                            if is_modified:
                                self.updated_count += 1
                            self.manifest[rel_p] = {"etag": new_etag, "size": os.path.getsize(local_path), "mtime": time.time()}
                        return True, 0, status_name
                    elif os.path.exists(part_path):
                        os.remove(part_path)
                        resume_offset = 0
                        continue

                elif r.status_code in [404, 403]:
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                    return False, 0, f"http_{r.status_code}"

            except requests.exceptions.SSLError:
                self.verify_ssl = False
                continue
            except Exception:
                time.sleep(1)

        return False, 0, "failed"

    def download_batch(self, asset_map):
        success_count = 0
        failed_count = 0
        resumed_count = 0
        total = len(asset_map)
        limit_hit = False

        limit_info = f" (Max download limit: {self.max_mb:.1f} MB)" if self.max_mb else ""
        print(f"[*] Starting download of {total} assets with {self.max_workers} worker threads{limit_info}...")

        sorted_items = sorted(
            asset_map.items(),
            key=lambda item: 0 if os.path.exists(os.path.join(self.download_folder, item[0]) + ".part") else 1
        )
        items_iter = iter(sorted_items)
        active_futures = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for _ in range(self.max_workers * 2):
                try:
                    rel_path, url = next(items_iter)
                    fut = executor.submit(self.download_file, url, os.path.join(self.download_folder, rel_path))
                    active_futures[fut] = rel_path
                except StopIteration:
                    break

            with tqdm(
                total=total,
                unit="file",
                desc="Processing Game Assets",
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n}/{total} files [{elapsed}<{remaining}, {rate_fmt}, {postfix}]"
            ) as pbar:
                while active_futures:
                    done, _ = wait(active_futures.keys(), return_when=FIRST_COMPLETED)
                    for future in done:
                        rel = active_futures.pop(future)
                        try:
                            ok, bytes_down, status = future.result()
                            if status == "limit_reached":
                                limit_hit = True
                            elif ok:
                                success_count += 1
                                if status == "resumed":
                                    resumed_count += 1
                            else:
                                failed_count += 1
                        except Exception:
                            failed_count += 1

                        mb_down = self.total_downloaded_bytes / (1024 * 1024)
                        if self.max_mb:
                            status_parts = [f"{mb_down:.1f}/{self.max_mb:.0f} MB"]
                        else:
                            status_parts = [f"{mb_down:.1f} MB"]

                        if self.cached_count:
                            status_parts.append(f"{self.cached_count} cached")
                        if self.reused_count:
                            status_parts.append(f"{self.reused_count} reused")
                        if self.updated_count:
                            status_parts.append(f"{self.updated_count} updated")
                        if self.downloaded_count:
                            status_parts.append(f"{self.downloaded_count} new")
                        if failed_count:
                            status_parts.append(f"{failed_count} skipped")

                        pbar.set_postfix_str(", ".join(status_parts))
                        pbar.update(1)

                        if not self.stop_requested:
                            try:
                                nxt_rel, nxt_url = next(items_iter)
                                nxt_fut = executor.submit(self.download_file, nxt_url, os.path.join(self.download_folder, nxt_rel))
                                active_futures[nxt_fut] = nxt_rel
                            except StopIteration:
                                pass

        # Persist updated asset manifest to game_meta.json
        save_asset_manifest(self.download_folder, self.manifest)

        mb_down = self.total_downloaded_bytes / (1024 * 1024)
        print("\n" + "=" * 75)
        print(" [+] Asset Transfer & Update Summary:")
        print(f"     * Total Assets Processed : {total}")
        print(f"     * Unchanged / Cached     : {self.cached_count}")
        if self.reused_count:
            print(f"     * Reused (Name Changes)  : {self.reused_count}")
        if self.updated_count:
            print(f"     * Modified / Updated     : {self.updated_count}")
        print(f"     * Newly Downloaded       : {self.downloaded_count}")
        if failed_count:
            print(f"     * Skipped / 404 Missing  : {failed_count}")
        print(f"     * Total Data Transferred : {mb_down:.2f} MB")
        print("=" * 75 + "\n")

        # Post-download disk verification audit
        verified_count = 0
        missing_count = 0
        for rel in asset_map:
            lp = os.path.join(self.download_folder, rel)
            if os.path.exists(lp) and os.path.getsize(lp) > 715:
                verified_count += 1
            else:
                missing_count += 1

        audit_msg = f"[AUDIT] Verified on disk: {verified_count}/{total} files intact."
        if missing_count > 0:
            audit_msg += f" ({missing_count} files not present on disk)"
        print(audit_msg)

        return success_count, failed_count, (limit_hit or self.stop_requested)

# ==============================================================================
# LOCAL SERVERS & ON-DEMAND PROXY (PLAY-NOW MODE)
# ==============================================================================

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def guess_type(self, path):
        ctype = super().guess_type(path)
        if ctype and (ctype.startswith('text/') or ctype in ('application/javascript', 'application/json')):
            if 'charset=' not in ctype:
                ctype += '; charset=utf-8'
        return ctype

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        rel_path = unquote(parsed.path.replace('+', '%2B')).replace('\\', '/').lstrip('/')
        if not rel_path:
            rel_path = 'index.html'

        local_path = os.path.join(os.getcwd(), *rel_path.split('/'))
        canonical_cwd = os.path.abspath(os.getcwd())
        canonical_local = os.path.abspath(local_path)
        if not (canonical_local == canonical_cwd or canonical_local.startswith(canonical_cwd + os.sep)):
            self.send_error(403, "Access denied")
            return

        if not os.path.isfile(local_path):
            return super().do_GET()

        file_size = os.path.getsize(local_path)
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = 'application/octet-stream'
        elif content_type.startswith('text/') or content_type in ('application/javascript', 'application/json'):
            if 'charset=' not in content_type:
                content_type += '; charset=utf-8'

        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            try:
                ranges = range_header[6:].split('-')
                start = int(ranges[0]) if ranges[0] else 0
                end = int(ranges[1]) if ranges[1] else file_size - 1
                if end >= file_size:
                    end = file_size - 1
                length = end - start + 1

                self.send_response(206)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()

                with open(local_path, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(remaining, 65536))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass

        return super().do_GET()

class OnDemandCachingServer:
    """
    HTTP server that serves local files instantly and intercepts missing requests
    to fetch them on-demand from the CDN and save them to disk.
    """
    @staticmethod
    def create_handler(target_folder, game_url, session, verify_ssl):
        canonical_target = os.path.abspath(target_folder)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=target_folder, **kwargs)

            def guess_type(self, path):
                ctype = super().guess_type(path)
                if ctype and (ctype.startswith('text/') or ctype in ('application/javascript', 'application/json')):
                    if 'charset=' not in ctype:
                        ctype += '; charset=utf-8'
                return ctype

            def log_message(self, format, *args):
                pass  # Suppress routine GET logging

            def serve_local_file(self, local_path):
                if not os.path.isfile(local_path):
                    self.send_error(404, "File not found")
                    return

                try:
                    file_size = os.path.getsize(local_path)
                except OSError:
                    self.send_error(404, "File not accessible")
                    return

                content_type, _ = mimetypes.guess_type(local_path)
                if not content_type:
                    content_type = "application/octet-stream"
                elif content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
                    if "charset=" not in content_type:
                        content_type += "; charset=utf-8"

                range_header = self.headers.get("Range")
                if range_header and range_header.startswith("bytes="):
                    try:
                        ranges = range_header[6:].split("-")
                        start = int(ranges[0]) if ranges[0] else 0
                        end = int(ranges[1]) if ranges[1] else file_size - 1
                        if end >= file_size:
                            end = file_size - 1
                        length = end - start + 1

                        self.send_response(206)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                        self.send_header("Content-Length", str(length))
                        self.send_header("Accept-Ranges", "bytes")
                        self.end_headers()

                        with open(local_path, "rb") as f:
                            f.seek(start)
                            remaining = length
                            while remaining > 0:
                                chunk = f.read(min(remaining, 65536))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        return
                    except Exception:
                        return

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                try:
                    with open(local_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except Exception:
                    pass

            def do_GET(self):
                parsed = urlparse(self.path)
                rel_path = unquote(parsed.path.replace('+', '%2B')).replace('\\', '/').lstrip('/')

                if not rel_path or rel_path == "index.html":
                    return self.serve_local_file(os.path.join(target_folder, "index.html"))

                local_path = os.path.join(target_folder, *rel_path.split('/'))

                # Security: Canonical path traversal check
                try:
                    canonical_local = os.path.abspath(local_path)
                    if not (canonical_local == canonical_target or canonical_local.startswith(canonical_target + os.sep)):
                        self.send_error(403, "Access denied")
                        return
                except Exception:
                    self.send_error(403, "Access denied")
                    return

                # 1. Serve directly if valid and complete
                if os.path.exists(local_path) and os.path.getsize(local_path) > 715:
                    return self.serve_local_file(local_path)

                # 2. On-demand fetch from remote CDN
                safe_rel = quote(unquote(rel_path.replace('+', '%2B')), safe="/()'!~,-_.@")
                full_url = urljoin(game_url, safe_rel)
                try:
                    headers = {"Referer": game_url} if game_url else {}
                    r = session.get(full_url, headers=headers, verify=verify_ssl, stream=True, timeout=25)
                    content_type = r.headers.get("Content-Type", "").lower()
                    if r.status_code == 200 and "text/html" not in content_type:
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        part_path = local_path + ".part"
                        cl = int(r.headers.get("Content-Length", 0) or 0)
                        expected_total = cl if cl > 0 else -1

                        with open(part_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)

                        # Truncation verification
                        if expected_total > 0 and os.path.exists(part_path) and os.path.getsize(part_path) < expected_total:
                            try:
                                os.remove(part_path)
                            except Exception:
                                pass
                            self.send_error(502, f"Incomplete transfer from CDN for {rel_path}")
                            return

                        os.replace(part_path, local_path)
                        return self.serve_local_file(local_path)
                except Exception:
                    pass

                self.send_error(404, f"File not found: {rel_path}")

        return Handler

# ==============================================================================
# GAME CATALOG & ARCHIVE MANAGER
# ==============================================================================

class GameCatalog:
    """
    Manages persistent game registry in 'game_catalog.json' and syncs with 'DOWNLOADED_GAMES.md'.
    Tracks games whether active on disk, archived (files deleted to save space), or discarded.
    Enables update checking, version detection, and re-downloading without local files.
    """
    CATALOG_FILE = "game_catalog.json"
    MARKDOWN_FILE = "DOWNLOADED_GAMES.md"

    @classmethod
    def load_catalog(cls):
        if os.path.exists(cls.CATALOG_FILE):
            try:
                with open(cls.CATALOG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @classmethod
    def save_catalog(cls, catalog):
        try:
            with open(cls.CATALOG_FILE, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[!] Warning: Could not save catalog: {e}")

    @classmethod
    def detect_version_from_folder(cls, folder_path):
        """
        Multi-layered version detection inspecting internal game files rather than just folder name:
        1. Explicit 'game_version' / 'version' inside game_meta.json (filtering out Twine/SugarCube engine strings)
        2. Remote CDN / Source URLs in game_meta.json (e.g. /v_1.10/, /v_0.79/, /v_ch.3/, /episode_1/)
        3. <title> tag in index.html (e.g. 'RISEN CITY 0.3' -> 'v0.3', 'Dragon Lewd Z v1.0.3.1' -> 'v1.0.3.1')
        4. Folder name suffix (-v1.10, -v0.79)
        """
        folder_name = os.path.basename(folder_path)

        # 1. Inspect game_meta.json
        meta_path = os.path.join(folder_path, "game_meta.json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass

        # Check URLs in meta for CDN version segments
        for u_key in ["cdn_url", "source_url", "game_url"]:
            u = meta.get(u_key)
            if u:
                m_v = re.search(r'/(?:v_|v|ver_?)([\d]+(?:\.[\d\w\-]+)+)/', u, re.I)
                if m_v:
                    return "v" + m_v.group(1).lstrip('vV')
                m_ch = re.search(r'/(v_ch\.?[\d\w]+)/', u, re.I)
                if m_ch:
                    return m_ch.group(1).replace('_', '')
                m_ep = re.search(r'/(episode_[\d\w]+)/', u, re.I)
                if m_ep:
                    return m_ep.group(1)

        # 2. Inspect index.html <title>
        index_path = os.path.join(folder_path, "index.html")
        if os.path.exists(index_path):
            try:
                with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(30000)
                m_title = re.search(r'<title>([^<]+)</title>', head, re.I)
                if m_title:
                    t = m_title.group(1).strip()
                    m_v = re.search(r'[\(\[\{]?(?:v(?:er)?\.?\s*|build\s*|\b)([0-9]+(?:\.[0-9a-zA-Z_\-]+)+)[\)\]\}]?', t, re.I)
                    if m_v:
                        val = m_v.group(1)
                        if val not in ["2.37.3", "2.36.1", "2.9.2"]:
                            return "v" + val.lstrip('vV')
            except Exception:
                pass

        # 3. Check folder name suffix
        m_fv = re.search(r'-(v[0-9]+(?:\.[0-9a-zA-Z_\-]+)*)$', folder_name, re.I)
        if m_fv:
            return m_fv.group(1)

        # 4. Check if meta had an unflagged valid version
        v = meta.get("version")
        if v and v not in ["2.37.3", "2.36.1", "2.9.2", "v2.9.2", "v2.10.0", "v2.12.0"]:
            return v if v.startswith("v") else f"v{v}"

        return "v1.0"

    @classmethod
    def parse_markdown_metadata(cls):
        """Extracts existing human-entered tracking info from DOWNLOADED_GAMES.md."""
        if not os.path.exists(cls.MARKDOWN_FILE):
            return {}
        try:
            with open(cls.MARKDOWN_FILE, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return {}

        entries = {}
        # HTML Games table: | Game Title | Play Status | Last Downloaded | Directory | Gamcore URL | CDN Base URL |
        html_sec = re.search(r'### HTML Games.*?(?=###|\Z)', text, re.DOTALL)
        if html_sec:
            for line in html_sec.group(0).splitlines():
                if not line.startswith('|') or 'Game Title' in line or ':---' in line:
                    continue
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 6:
                    title = re.sub(r'[*_]', '', parts[0]).strip()
                    entries[title] = {
                        "engine": "html",
                        "status": parts[1].strip(),
                        "last_downloaded": parts[2].strip(),
                        "directory": parts[3].strip('` '),
                        "gamcore_url": parts[4].strip('` '),
                        "cdn_url": parts[5].strip('` ')
                    }

        # Ren'Py Games table: | Game Title | Last Downloaded | Directory | Gamcore URL | CDN Base URL |
        renpy_sec = re.search(r'### Ren\'Py Games.*?(?=##|\Z)', text, re.DOTALL)
        if renpy_sec:
            for line in renpy_sec.group(0).splitlines():
                if not line.startswith('|') or 'Game Title' in line or ':---' in line:
                    continue
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 5:
                    title = re.sub(r'[*_]', '', parts[0]).strip()
                    is_deleted = "deleted" in parts[1].lower()
                    entries[title] = {
                        "engine": "renpy",
                        "status": "🚫 Deleted / Discarded" if is_deleted else "⏳ Unplayed",
                        "last_downloaded": parts[1].strip(),
                        "directory": parts[2].strip('` '),
                        "gamcore_url": parts[3].strip('` '),
                        "cdn_url": parts[4].strip('` ')
                    }

        # Discarded games section
        disc_sec = re.search(r'## 🚫 Deleted / Discarded Games.*?(?=##|\Z)', text, re.DOTALL)
        if disc_sec:
            for line in disc_sec.group(0).splitlines():
                m = re.match(r'\d+\.\s+\*\*([^*]+)\*\*(?:\s*\(([^)]+)\))?\s*—\s*(.*)', line)
                if m:
                    t = m.group(1).strip()
                    v = m.group(2) or "v1.0"
                    reason = m.group(3).strip()
                    if t not in entries:
                        entries[t] = {
                            "status": "🚫 Deleted / Discarded",
                            "version": v,
                            "notes": reason
                        }
                    else:
                        entries[t]["status"] = "🚫 Deleted / Discarded"
                        entries[t]["notes"] = reason

        return entries

    @classmethod
    def sync_catalog(cls, scan_dir="downloads"):
        """
        Scans disk and markdown to populate or refresh 'game_catalog.json'.
        Keeps all local files completely untouched.
        """
        print("\n" + "=" * 80)
        print(" GameGrabber - Catalog Synchronization")
        print("=" * 80)
        catalog = cls.load_catalog()
        md_data = cls.parse_markdown_metadata()

        game_paths = GameUpdateChecker.find_game_directories(scan_dir) if os.path.exists(scan_dir) else []
        print(f"[*] Found {len(game_paths)} game folder(s) on disk.")
        print(f"[*] Found {len(md_data)} tracked entry/entries in {cls.MARKDOWN_FILE}.\n")

        # 1. Ingest active disk folders
        for gp in game_paths:
            folder_name = os.path.basename(gp)
            parent = os.path.basename(os.path.dirname(gp))
            engine = parent if parent in ["html", "renpy"] else "game"

            meta_file = os.path.join(gp, "game_meta.json")
            meta = {}
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass

            version = cls.detect_version_from_folder(gp)
            raw_title = meta.get("game_name") or re.sub(r'-(v[0-9]+(?:\.[0-9a-zA-Z_\-]+)*)$', '', folder_name, flags=re.I)
            clean_title = raw_title.replace('_', ' ').strip()
            # If folder is Risen_City or title has numbers like RISEN CITY 0.3, normalize title
            if "risen" in folder_name.lower():
                clean_title = "Risen City"

            # Find matching md entry
            md_match = None
            for md_t, md_entry in md_data.items():
                if md_t.lower() == clean_title.lower() or folder_name.lower().startswith(md_t.lower().replace(' ', '_')) or md_t.lower().replace(' ', '_') in folder_name.lower():
                    md_match = md_entry
                    clean_title = md_t
                    break

            gamcore_url = (md_match.get("gamcore_url") if md_match else None) or ""
            cdn_url = meta.get("cdn_url") or meta.get("game_url") or meta.get("source_url") or (md_match.get("cdn_url") if md_match else "") or ""
            status = (md_match.get("status") if md_match else None) or "🎮 Active on Disk"

            # Check if this folder already exists in catalog
            key = folder_name
            for k, ex in catalog.items():
                if ex.get("title", "").lower() == clean_title.lower() or k.lower() == folder_name.lower():
                    key = k
                    break

            catalog[key] = {
                "title": clean_title,
                "version": version,
                "engine": engine,
                "directory": gp.replace('\\', '/'),
                "gamcore_url": gamcore_url,
                "cdn_url": cdn_url,
                "status": status if "Archived" not in status else "🎮 Active on Disk",
                "on_disk": True,
                "last_downloaded": (md_match.get("last_downloaded") if md_match else None) or datetime.fromtimestamp(os.path.getmtime(gp), tz=timezone.utc).strftime('%Y-%m-%d'),
                "last_checked": datetime.now(timezone.utc).strftime('%Y-%m-%d')
            }
            print(f"  [+] Synced: {clean_title:<32} ({version}) -> {gp}")

        # 2. Ingest markdown-only or discarded entries not on disk
        for md_t, md_entry in md_data.items():
            already = False
            for k, ex in catalog.items():
                if ex.get("title", "").lower() == md_t.lower():
                    already = True
                    break
            if not already:
                key = md_t.replace(' ', '_')
                catalog[key] = {
                    "title": md_t,
                    "version": md_entry.get("version", "v1.0"),
                    "engine": md_entry.get("engine", "html"),
                    "directory": md_entry.get("directory", ""),
                    "gamcore_url": md_entry.get("gamcore_url", ""),
                    "cdn_url": md_entry.get("cdn_url", ""),
                    "status": md_entry.get("status", "📦 Archived (Deleted Locally)"),
                    "on_disk": False,
                    "last_downloaded": md_entry.get("last_downloaded", "Unknown"),
                    "last_checked": datetime.now(timezone.utc).strftime('%Y-%m-%d')
                }
                print(f"  [+] Added from log: {md_t:<32} (Archived / Not on disk)")

        cls.save_catalog(catalog)
        cls.sync_markdown(catalog)
        print("=" * 80)
        print(f"[+] Catalog successfully synchronized! Total tracked games: {len(catalog)}")
        print("=" * 80 + "\n")
        return catalog

    @classmethod
    def register_game(cls, target, gamcore_url=None, cdn_url=None):
        """
        Explicitly registers a game into the catalog without deleting local files.
        Can register an existing folder, or an online URL.
        """
        catalog = cls.load_catalog()

        # Check if target is a local directory
        local_dir = None
        for cand in [target, os.path.join("downloads", "html", target), os.path.join("downloads", "renpy", target), os.path.join("downloads", target)]:
            if os.path.isdir(cand):
                local_dir = cand
                break

        if local_dir:
            folder_name = os.path.basename(local_dir)
            parent = os.path.basename(os.path.dirname(local_dir))
            engine = parent if parent in ["html", "renpy"] else "html"
            version = cls.detect_version_from_folder(local_dir)

            meta_file = os.path.join(local_dir, "game_meta.json")
            meta = {}
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass

            raw_title = meta.get("game_name") or re.sub(r'-(v[0-9]+(?:\.[0-9a-zA-Z_\-]+)*)$', '', folder_name, flags=re.I)
            clean_title = raw_title.replace('_', ' ').strip()
            g_url = gamcore_url or ""
            c_url = cdn_url or meta.get("cdn_url") or meta.get("game_url") or meta.get("source_url") or ""

            catalog[folder_name] = {
                "title": clean_title,
                "version": version,
                "engine": engine,
                "directory": local_dir.replace('\\', '/'),
                "gamcore_url": g_url,
                "cdn_url": c_url,
                "status": "🎮 Active on Disk",
                "on_disk": True,
                "last_downloaded": datetime.fromtimestamp(os.path.getmtime(local_dir), tz=timezone.utc).strftime('%Y-%m-%d'),
                "last_checked": datetime.now(timezone.utc).strftime('%Y-%m-%d')
            }
            cls.save_catalog(catalog)
            cls.sync_markdown(catalog)
            print("\n" + "=" * 80)
            print(f"[+] Successfully registered '{clean_title}' ({version}) into catalog!")
            print(f"[*] Directory : {local_dir}")
            print(f"[*] Status    : 🎮 Active on Disk (Files completely preserved)")
            print("=" * 80 + "\n")
            return

        # Target is a URL
        if target.startswith(("http://", "https://")):
            clean_title, version, folder_name = extract_game_identity("", target)
            g_url = target if "gamcore.com" in target.lower() else ""
            c_url = target if "fgn.cdn" in target.lower() or "serverable" in target.lower() else ""

            catalog[folder_name] = {
                "title": clean_title.replace('_', ' '),
                "version": version,
                "engine": "html",
                "directory": "",
                "gamcore_url": g_url,
                "cdn_url": c_url,
                "status": "📋 Tracked (Not Downloaded)",
                "on_disk": False,
                "last_downloaded": "Never",
                "last_checked": datetime.now(timezone.utc).strftime('%Y-%m-%d')
            }
            cls.save_catalog(catalog)
            cls.sync_markdown(catalog)
            print("\n" + "=" * 80)
            print(f"[+] Successfully registered online game '{clean_title}' ({version}) into catalog!")
            print(f"[*] Tracked URL : {target}")
            print(f"[*] Status      : 📋 Tracked (Not Downloaded)")
            print("=" * 80 + "\n")
            return

        print(f"[X] Error: Could not resolve target '{target}' to a local game folder or valid URL.")

    @classmethod
    def archive_game(cls, target, auto_yes=False):
        """
        Saves metadata & version into catalog as Archived, then safely deletes local files to reclaim disk space.
        """
        catalog = cls.load_catalog()

        local_dir = None
        for cand in [target, os.path.join("downloads", "html", target), os.path.join("downloads", "renpy", target), os.path.join("downloads", target)]:
            if os.path.isdir(cand):
                local_dir = cand
                break

        if not local_dir:
            print(f"[X] Error: Local game folder '{target}' not found.")
            return

        folder_name = os.path.basename(local_dir)
        version = cls.detect_version_from_folder(local_dir)
        dir_size = GameCompactor.get_directory_size(local_dir)
        size_str = f"{dir_size / (1024 * 1024):.1f} MB" if dir_size < 1024 * 1024 * 1024 else f"{dir_size / (1024 * 1024 * 1024):.2f} GB"

        meta_file = os.path.join(local_dir, "game_meta.json")
        meta = {}
        if os.path.exists(meta_file):
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass

        raw_title = meta.get("game_name") or re.sub(r'-(v[0-9]+(?:\.[0-9a-zA-Z_\-]+)*)$', '', folder_name, flags=re.I)
        clean_title = raw_title.replace('_', ' ').strip()

        # Find existing catalog entry or create
        cat_entry = catalog.get(folder_name, {})
        gamcore_url = cat_entry.get("gamcore_url") or ""
        cdn_url = meta.get("cdn_url") or meta.get("game_url") or meta.get("source_url") or cat_entry.get("cdn_url") or ""

        # If gamcore_url is empty, check markdown
        if not gamcore_url:
            md_data = cls.parse_markdown_metadata()
            for md_t, md_e in md_data.items():
                if md_t.lower() == clean_title.lower():
                    gamcore_url = md_e.get("gamcore_url", "")
                    if not cdn_url:
                        cdn_url = md_e.get("cdn_url", "")
                    break

        print("\n" + "=" * 80)
        print(f" GameGrabber - Archive & Reclaim Storage")
        print("=" * 80)
        print(f"[*] Game Title       : {clean_title}")
        print(f"[*] Detected Version : {version}")
        print(f"[*] Local Directory  : {os.path.abspath(local_dir)}")
        print(f"[*] Disk Space Freed : {size_str}")
        print(f"[*] Gamcore URL      : {gamcore_url or 'N/A'}")
        print(f"[*] Direct CDN URL   : {cdn_url or 'N/A'}")
        print("-" * 80)
        print("[!] All version and URL metadata will be preserved in 'game_catalog.json' & 'DOWNLOADED_GAMES.md'.")
        print("[!] You can check updates and re-download fresh anytime with `scraper.py --updateall`.")
        print("=" * 80)

        if not auto_yes:
            try:
                ans = input(f"\n[?] Are you sure you want to archive '{clean_title}' and delete this folder? [y/N]: ").strip().lower()
                if ans not in ['y', 'yes']:
                    print("[*] Archiving cancelled by user.")
                    return
            except (KeyboardInterrupt, EOFError):
                print("\n[*] Aborted.")
                return

        # Update catalog entry
        catalog[folder_name] = {
            "title": clean_title,
            "version": version,
            "engine": cat_entry.get("engine", "html"),
            "directory": local_dir.replace('\\', '/'),
            "gamcore_url": gamcore_url,
            "cdn_url": cdn_url,
            "status": "📦 Archived (Deleted Locally)",
            "on_disk": False,
            "last_downloaded": cat_entry.get("last_downloaded") or datetime.fromtimestamp(os.path.getmtime(local_dir), tz=timezone.utc).strftime('%Y-%m-%d'),
            "last_checked": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            "archived_date": datetime.now(timezone.utc).strftime('%Y-%m-%d')
        }
        cls.save_catalog(catalog)

        # Safely remove folder
        try:
            shutil.rmtree(local_dir)
            print(f"\n[+] Successfully deleted '{local_dir}'! Freed {size_str} of disk space.")
        except Exception as e:
            print(f"[!] Warning: Error while deleting directory: {e}")

        cls.sync_markdown(catalog)
        print(f"[+] Catalog and {cls.MARKDOWN_FILE} updated to '📦 Archived (Deleted Locally)'.\n")

    @classmethod
    def list_catalog(cls):
        """Displays formatted catalog table."""
        catalog = cls.load_catalog()
        if not catalog:
            catalog = cls.sync_catalog()

        print("\n" + "=" * 90)
        print(f" GameGrabber - Persistent Game Catalog ({len(catalog)} Tracked Games)")
        print("=" * 90)
        print(f"{'Game Title':<34} | {'Version':<10} | {'Status':<28} | {'On Disk':<7}")
        print("-" * 90)
        for key, entry in sorted(catalog.items(), key=lambda x: (not x[1].get('on_disk', False), x[1].get('title', ''))):
            title = entry.get("title", key)
            ver = entry.get("version", "v1.0")
            st = entry.get("status", "Unknown")
            on_d = "YES" if entry.get("on_disk", False) else "NO"
            print(f"{title[:34]:<34} | {ver:<10} | {st[:28]:<28} | {on_d:<7}")
        print("=" * 90 + "\n")

    @classmethod
    def sync_markdown(cls, catalog=None):
        """Regenerates or updates DOWNLOADED_GAMES.md from catalog."""
        if catalog is None:
            catalog = cls.load_catalog()
        if not catalog:
            return

        lines = [
            "# Game Library & Download Tracking\n",
            "This document maintains a permanent log of all downloaded, archived, and tracked games in `GameGrabber`.\n",
            f"**Last Updated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}  ",
            "**Strict Preference:** Male MC (Protagonist) Only.\n",
            "---\n",
            "## 🚫 Deleted / Discarded Games (DO NOT RE-DOWNLOAD)\n"
        ]

        # 1. Discarded
        disc_count = 1
        for k, e in sorted(catalog.items(), key=lambda x: x[1].get('title', '')):
            if "Discarded" in e.get("status", "") or "DO NOT RE-DOWNLOAD" in e.get("notes", ""):
                reason = e.get("notes") or "Deleted by user (DO NOT RE-DOWNLOAD)"
                lines.append(f"{disc_count}. **{e.get('title')} ({e.get('version', 'v1.0')})** — *{reason}*\n")
                disc_count += 1

        lines.extend([
            "---\n",
            "## 📦 Archived Games (Files Deleted to Free Space - Monitored for Updates)\n",
            "| Game Title | Last Version | Gamcore URL | CDN Base URL | Archived Date |\n",
            "| :--- | :---: | :--- | :--- | :---: |\n"
        ])
        for k, e in sorted(catalog.items(), key=lambda x: x[1].get('title', '')):
            if "Archived" in e.get("status", ""):
                lines.append(f"| **{e.get('title')}** | {e.get('version', 'v1.0')} | {e.get('gamcore_url') or 'N/A'} | {e.get('cdn_url') or 'N/A'} | {e.get('archived_date', e.get('last_checked', ''))} |\n")

        lines.extend([
            "---\n",
            "## 🎮 Active Games on Disk\n",
            "| Game Title | Version | Play Status | Directory | Gamcore URL | CDN Base URL |\n",
            "| :--- | :---: | :---: | :--- | :--- | :--- |\n"
        ])
        for k, e in sorted(catalog.items(), key=lambda x: x[1].get('title', '')):
            if e.get("on_disk", False):
                lines.append(f"| **{e.get('title')}** | {e.get('version', 'v1.0')} | {e.get('status', '🎮 Active')} | `{e.get('directory', '')}` | {e.get('gamcore_url') or 'N/A'} | {e.get('cdn_url') or 'N/A'} |\n")

        lines.append("\n---\n")
        try:
            with open(cls.MARKDOWN_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as ex:
            print(f"[!] Warning: Could not update {cls.MARKDOWN_FILE}: {ex}")


# ==============================================================================
# GAME UPDATE CHECKER & CDN INTROSPECTOR
# ==============================================================================

class GameUpdateChecker:
    """Checks for game updates using lightweight HTTP HEAD requests, ETag/Last-Modified comparisons,
    and version path inspection without downloading heavy game files."""

    @staticmethod
    def generate_version_candidates(version_str):
        """Generates likely next version strings given an existing version."""
        candidates = []
        m_ch = re.match(r'v_ch\.?(\d+)', version_str, re.IGNORECASE)
        if m_ch:
            curr = int(m_ch.group(1))
            candidates.extend([f"v_ch.{curr+1}", f"v_ch.{curr+2}", f"v_ch{curr+1}"])
            return candidates

        m = re.match(r'v_?(\d+)(?:\.(\d+))?(?:\.(\d+))?', version_str, re.IGNORECASE)
        if m:
            major = int(m.group(1))
            minor = int(m.group(2)) if m.group(2) is not None else None
            patch = int(m.group(3)) if m.group(3) is not None else None
            prefix = "v_" if version_str.startswith("v_") else "v"

            if patch is not None:
                candidates.append(f"{prefix}{major}.{minor}.{patch+1}")
                candidates.append(f"{prefix}{major}.{minor+1}.0")
            elif minor is not None:
                m_str = m.group(2)
                if len(m_str) == 2 and m_str.startswith('0'):
                    candidates.append(f"{prefix}{major}.{minor+1:02d}")
                    candidates.append(f"{prefix}{major}.{minor+2:02d}")
                    candidates.append(f"{prefix}{major}.10")
                else:
                    candidates.append(f"{prefix}{major}.{minor+1}")
                    candidates.append(f"{prefix}{major}.{minor+2}")
                candidates.append(f"{prefix}{major+1}.0")
                candidates.append(f"{prefix}{major+1}.00")
            else:
                candidates.append(f"{prefix}{major+1}")
                candidates.append(f"{prefix}{major+1}.0")
        return candidates

    @classmethod
    def check_single_url(cls, target_url, local_mtime=None, local_etag=None, local_index_path=None, verify_ssl=True):
        """Checks if a remote game URL or CDN build has an update available."""
        target_url = normalize_url(target_url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        portal_iframe_url = None
        if "gamcore.com" in target_url.lower():
            try:
                r = requests.get(target_url, headers=headers, timeout=8, verify=verify_ssl)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'html.parser')
                    iframe = soup.find('iframe')
                    if iframe and iframe.get('src'):
                        src = iframe.get('src')
                        if src.startswith('//'):
                            src = 'https:' + src
                        portal_iframe_url = normalize_url(src)
            except Exception:
                pass

        resolved_url = portal_iframe_url or target_url
        resolved_url = normalize_url(resolved_url)
        clean_url = resolved_url.split('?')[0]
        if clean_url.endswith('/'):
            clean_url += 'index.html'

        try:
            r = requests.head(clean_url, headers=headers, timeout=6, verify=verify_ssl)
            if r.status_code not in [200, 206, 301, 302] and clean_url.endswith('index.html'):
                zip_url = clean_url[:-10] + 'game.zip'
                r2 = requests.head(zip_url, headers=headers, timeout=6, verify=verify_ssl)
                if r2.status_code in [200, 206, 301, 302]:
                    r = r2
                    clean_url = zip_url

            if r.status_code not in [200, 206, 301, 302]:
                return {
                    "has_update": False,
                    "update_type": "error",
                    "status_code": r.status_code,
                    "message": f"Server returned HTTP {r.status_code}"
                }

            last_mod_str = r.headers.get("Last-Modified")
            remote_mtime = None
            if last_mod_str:
                try:
                    remote_mtime = parsedate_to_datetime(last_mod_str)
                except Exception:
                    pass
            remote_etag = r.headers.get("ETag")

            # Check for newer version folder on CDN
            v_match = re.search(r'/(v_[\d\.\w]+)/', clean_url, re.IGNORECASE)
            if v_match:
                current_v = v_match.group(1)
                candidates = cls.generate_version_candidates(current_v)
                for cand in candidates:
                    test_url = clean_url.replace(f"/{current_v}/", f"/{cand}/")
                    try:
                        probe = requests.head(test_url, headers=headers, timeout=4, verify=verify_ssl)
                        if probe.status_code in [200, 206]:
                            return {
                                "has_update": True,
                                "update_type": "version_bump",
                                "remote_url": test_url,
                                "new_version": cand,
                                "remote_mtime": remote_mtime,
                                "remote_etag": remote_etag,
                                "message": f"New version '{cand}' detected on CDN!"
                            }
                    except Exception:
                        pass

            # If timestamp or ETag changed on an HTML entry point, verify whether content actually changed (ignoring auto-audio)
            is_html_entry = clean_url.endswith('.html') or clean_url.endswith('/')
            if local_index_path and os.path.exists(local_index_path) and is_html_entry:
                if (remote_mtime and local_mtime and (remote_mtime - local_mtime).total_seconds() > 86400) or (remote_etag and local_etag and remote_etag != local_etag):
                    try:
                        r_content = requests.get(clean_url, headers=headers, timeout=6, verify=verify_ssl)
                        if r_content.status_code == 200:
                            with open(local_index_path, "r", encoding="utf-8", errors="replace") as lf:
                                loc_html = lf.read()
                            if normalize_html_for_comparison(loc_html) == normalize_html_for_comparison(r_content.text):
                                return {
                                    "has_update": False,
                                    "update_type": "none",
                                    "remote_url": clean_url,
                                    "new_version": None,
                                    "remote_mtime": remote_mtime,
                                    "remote_etag": remote_etag,
                                    "message": "Game is up to date (code verified identical; auto-audio manager preserved)."
                                }
                    except Exception:
                        pass

            # Check if remote build timestamp is newer than local download
            if remote_mtime and local_mtime:
                diff_seconds = (remote_mtime - local_mtime).total_seconds()
                if diff_seconds > 86400:  # Over 1 day newer
                    return {
                        "has_update": True,
                        "update_type": "timestamp_newer",
                        "remote_url": clean_url,
                        "new_version": None,
                        "remote_mtime": remote_mtime,
                        "remote_etag": remote_etag,
                        "message": f"Remote build updated on {remote_mtime.strftime('%Y-%m-%d')} (newer than local build)"
                    }

            # Check ETag changes
            if remote_etag and local_etag and remote_etag != local_etag:
                return {
                    "has_update": True,
                    "update_type": "etag_changed",
                    "remote_url": clean_url,
                    "new_version": None,
                    "remote_mtime": remote_mtime,
                    "remote_etag": remote_etag,
                    "message": "Remote ETag checksum changed (assets refreshed on CDN)"
                }

            return {
                "has_update": False,
                "update_type": "none",
                "remote_url": clean_url,
                "new_version": None,
                "remote_mtime": remote_mtime,
                "remote_etag": remote_etag,
                "message": "Game is up to date."
            }

        except Exception as e:
            return {
                "has_update": False,
                "update_type": "error",
                "message": f"Connection error: {e}"
            }

    @staticmethod
    def is_game_directory(path):
        """Determines if a directory contains a game (Ren'Py, Twine, HTML5, etc.)."""
        if not os.path.isdir(path):
            return False
        try:
            entries = set(os.listdir(path))
        except Exception:
            return False
        if "game_meta.json" in entries:
            return True
        if any(f in entries for f in ["game.zip", "renpy.wasm", "renpy.js", "renpy.data"]):
            return True
        if "index.html" in entries:
            return True
        if any(f in entries for f in ["data.js", "c2runtime.js", "c3runtime.js"]):
            return True
        return False

    @classmethod
    def find_game_directories(cls, root_dir):
        """Recursively walks root_dir to discover all game directories without descending into discovered games."""
        game_dirs = []
        if not os.path.exists(root_dir):
            return game_dirs

        if cls.is_game_directory(root_dir):
            return [root_dir]

        for root, dirs, files in os.walk(root_dir):
            keep_dirs = []
            for d in list(dirs):
                full_d = os.path.join(root, d)
                if cls.is_game_directory(full_d):
                    game_dirs.append(full_d)
                else:
                    keep_dirs.append(d)
            dirs[:] = keep_dirs

        return sorted(list(set(game_dirs)))

    @classmethod
    def check_all_downloaded_games(cls, target_dir="downloads", verify_ssl=True, max_workers=8):
        """Recursively discovers all games inside target_dir and checks for updates, integrating with catalog."""
        catalog = GameCatalog.load_catalog()

        game_paths = cls.find_game_directories(target_dir) if os.path.exists(target_dir) else []
        seen_folders = set()

        games_to_check = []
        for folder_path in game_paths:
            folder_name = os.path.basename(folder_path)
            seen_folders.add(folder_name.lower())
            parent_name = os.path.basename(os.path.dirname(folder_path))
            cat = parent_name if parent_name in ["html", "renpy"] else "game"

            meta_file = os.path.join(folder_path, "game_meta.json")
            meta = {}
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass

            local_mtime = None
            index_file = os.path.join(folder_path, "index.html")
            if os.path.exists(index_file):
                local_mtime = datetime.fromtimestamp(os.path.getmtime(index_file), tz=timezone.utc)
            elif os.path.exists(meta_file):
                local_mtime = datetime.fromtimestamp(os.path.getmtime(meta_file), tz=timezone.utc)

            # Robust internal version detection
            local_ver = GameCatalog.detect_version_from_folder(folder_path)

            url = meta.get("game_url") or meta.get("cdn_url") or meta.get("source_url")
            # If no URL in folder meta, check catalog
            if not url and folder_name in catalog:
                url = catalog[folder_name].get("gamcore_url") or catalog[folder_name].get("cdn_url")

            games_to_check.append({
                "folder": folder_name,
                "title": catalog.get(folder_name, {}).get("title", folder_name),
                "category": cat,
                "path": folder_path,
                "url": url,
                "local_version": local_ver,
                "local_etag": meta.get("etag"),
                "local_mtime": local_mtime,
                "archived": False
            })

        # Also check archived games from catalog that aren't on disk
        for cat_k, cat_e in catalog.items():
            if cat_e.get("status") and "Archived" in cat_e.get("status") and cat_k.lower() not in seen_folders:
                c_url = cat_e.get("gamcore_url") or cat_e.get("cdn_url")
                if c_url:
                    games_to_check.append({
                        "folder": cat_k,
                        "title": cat_e.get("title", cat_k),
                        "category": cat_e.get("engine", "archived"),
                        "path": None,
                        "url": c_url,
                        "local_version": cat_e.get("version", "v1.0"),
                        "local_etag": cat_e.get("etag"),
                        "local_mtime": None,
                        "archived": True
                    })

        if not games_to_check:
            print(f"[*] No game installations found inside '{target_dir}' or catalog.")
            return []

        print("=" * 95)
        print(f" Checking updates for {len(games_to_check)} game(s) ({len(game_paths)} active, {len(games_to_check)-len(game_paths)} archived)...")
        print("=" * 95)

        def worker(game):
            index_p = os.path.join(game["path"], "index.html") if game.get("path") else None
            res = cls.check_single_url(
                game["url"],
                local_mtime=game["local_mtime"],
                local_etag=game["local_etag"],
                local_index_path=index_p,
                verify_ssl=verify_ssl
            ) if game["url"] else {"has_update": False, "update_type": "no_url", "message": "No remote URL configured"}
            return game, res

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(worker, games_to_check))

        updates_found = []
        print(f"{'Game Title':<32} | {'Type':<8} | {'Version':<9} | {'Status':<16} | {'Details'}")
        print("-" * 95)
        for game, res in results:
            ver_str = game["local_version"] or "Unknown"
            type_str = f"📦 {game['category']}" if game["archived"] else game["category"]

            if res.get("has_update"):
                status_str = "[UPDATE AVAIL]"
                updates_found.append((game, res))
            elif res.get("update_type") == "none":
                status_str = "[UP TO DATE]"
            elif res.get("update_type") == "error":
                status_str = "[CHECK ERROR]"
            else:
                status_str = f"[{res.get('update_type', 'UNKNOWN').upper()}]"

            disp_title = game["title"][:30] + ".." if len(game["title"]) > 32 else game["title"]
            print(f"{disp_title:<32} | {type_str:<8} | {ver_str:<9} | {status_str:<16} | {res.get('message', '')}")

        print("=" * 95)
        if updates_found:
            print(f"\n[+] {len(updates_found)} game(s) have updates available:")
            for g, r in updates_found:
                new_tgt = r.get("remote_url", g["url"])
                arch_tag = " (Archived)" if g["archived"] else ""
                print(f"    * {g['title']}{arch_tag} [{g['local_version']}] -> {r.get('message')}")
        else:
            print("\n[+] All checked games are up to date!")
        print("=" * 95 + "\n")
        return updates_found

# ==============================================================================
# MAIN GAME GRABBER ORCHESTRATOR
# ==============================================================================

def grab_game(url, output_folder=None, headed=False, no_verify=False, wait_idle=8, serve=False, play_now=False, max_workers=8, proxy=None, no_videos=False, stream_only=False, limit_mb=None, diff_check=False, seed_folder=None):
    verify_ssl = not no_verify
    url = normalize_url(url)

    print("=" * 75)
    print(" GameGrabber - Universal Browser Game Scraper ")
    print("=" * 75)
    print(f"[*] Target URL : {url}")
    print(f"[*] Headless   : {not headed}")
    print(f"[*] SSL Verify : {verify_ssl}")
    if play_now:
        print("[*] Mode       : PLAY-NOW (Live On-Demand Streaming + Background Caching)")

    captured_responses = {}
    captured_urls = set()

    with sync_playwright() as p:
        launch_args = ["--disable-web-security", "--allow-running-insecure-content"]
        proxy_settings = {"server": proxy} if proxy else None
        browser = p.chromium.launch(
            headless=not headed,
            args=launch_args,
            proxy=proxy_settings
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=no_verify
        )
        page = context.new_page()

        def on_response(response):
            try:
                r_url = response.url
                if any(ad in r_url.lower() for ad in AD_DOMAINS):
                    return
                captured_urls.add(r_url)
                if response.status in [200, 206]:
                    content_len = response.headers.get("content-length")
                    if content_len and int(content_len) < 50 * 1024 * 1024:
                        body = response.body()
                        captured_responses[r_url] = body
            except Exception:
                pass

        page.on("response", on_response)

        # 1. Resolve Game URL if Portal
        game_url = url
        if GameURLExtractor.is_portal_url(url):
            try:
                game_url = GameURLExtractor.extract_game_url(page, url)
            except ConnectionError:
                browser.close()
                return

        game_url = normalize_url(game_url)
        print(f"[+] Final Game Target: {game_url}")

        # 2. Navigate to Game URL and sniff runtime network
        print("[*] Booting game in browser and intercepting network traffic...")
        try:
            page.goto(game_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[!] Warning on initial load: {e}")

        # Loading progress bar with ETA countdown and live network stats
        with tqdm(
            total=wait_idle,
            desc="Booting & Loading Assets",
            unit="s",
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n:.1f}/{total:.1f}s [{elapsed}<{remaining}, {postfix}]"
        ) as pbar:
            start_time = time.time()
            last_update = start_time
            while True:
                elapsed = time.time() - start_time
                if elapsed >= wait_idle:
                    pbar.n = wait_idle
                    pbar.refresh()
                    break
                step = time.time() - last_update
                if step >= 0.2:
                    pbar.n = min(elapsed, wait_idle)
                    cached_mb = sum(len(b) for b in captured_responses.values()) / (1024 * 1024)
                    pbar.set_postfix_str(f"{len(captured_urls)} requests, {cached_mb:.1f} MB cached")
                    pbar.refresh()
                    last_update = time.time()
                time.sleep(0.1)

        # Grab DOM content & title
        page_html = page.content()
        title = page.title() or ""

        # 3. Determine Game Title & Version for folder naming
        clean_title, detected_version, default_folder = extract_game_identity(title, url, page_html)
        print(f"[+] Game Title : {clean_title}")
        print(f"[+] Detected Version : {detected_version}")

        # 4. Introspect Game Engine
        engine_info = EngineIntrospector.inspect(page, captured_urls)
        detected_engine = engine_info.get("engine", "html5")
        print(f"[+] Engine Detected: {detected_engine.upper()}")

        # Extract Cookies and Headers for Python requests session
        cookies = context.cookies()
        session = requests.Session()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        for c in cookies:
            session.cookies.set(c['name'], c['value'], domain=c['domain'], path=c['path'])
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": game_url
        })

        browser.close()

    # 5. Determine output folder and check for version compatibility
    base_download_dir = "downloads"
    category = "renpy" if detected_engine == "renpy" else "html"
    category_dir = os.path.join(base_download_dir, category)
    os.makedirs(category_dir, exist_ok=True)

    if output_folder:
        target_folder = os.path.normpath(output_folder)
    else:
        cat_target = os.path.join(category_dir, default_folder)
        legacy_target = os.path.join(base_download_dir, default_folder)
        if not os.path.exists(cat_target) and os.path.exists(legacy_target) and os.path.isdir(legacy_target):
            target_folder = legacy_target
        else:
            target_folder = cat_target

    meta_path = os.path.join(target_folder, "game_meta.json")

    detected_prior_folder = None
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                prev_meta = json.load(f)
            prev_version = prev_meta.get("version", "v1.0")

            if prev_version != detected_version:
                print("\n" + "=" * 75)
                print(f"[!] VERSION MISMATCH DETECTED:")
                print(f"    Existing download in '{target_folder}' is version: {prev_version}")
                print(f"    Remote game is newer version: {detected_version}")
                if output_folder:
                    new_folder = target_folder
                else:
                    new_folder = os.path.join(category_dir, f"{clean_title}-{detected_version}")
                print(f"[*] Resuming in folder '{new_folder}' to prevent data corruption!")
                print("=" * 75 + "\n")
                detected_prior_folder = target_folder
                target_folder = new_folder
                meta_path = os.path.join(target_folder, "game_meta.json")
            else:
                print(f"[+] Found existing '{prev_version}' download in '{target_folder}'. Resuming missing assets...")
        except Exception:
            pass

    # Look for an existing version folder to seed from if not supplied
    if not seed_folder:
        seed_folder = detected_prior_folder or find_existing_game_folder(clean_title, target_folder, base_dir=base_download_dir)

    etag_index = {}
    size_index = {}
    manifest = {}

    if seed_folder and os.path.abspath(seed_folder) != os.path.abspath(target_folder):
        print(f"[*] Previous version detected at: {seed_folder}")
        print(f"[*] Seeding assets into '{target_folder}' via hardlinks for instant differential update...")
        seeded_count, etag_index, size_index, manifest = seed_existing_assets(seed_folder, target_folder)
        print(f"[+] Fast-seeded {seeded_count} assets from previous version (0 extra disk space).")
        diff_check = True
    elif os.path.exists(target_folder):
        etag_index, size_index, manifest = build_asset_index(target_folder)

    os.makedirs(target_folder, exist_ok=True)
    print(f"[+] Output Directory: {os.path.abspath(target_folder)}")

    # Self-healing: automatically purge any corrupt / captive-portal files from previous dropped sessions
    auto_purge_corrupted_files(target_folder)

    # Initialize/update metadata
    meta = {
        "game_name": clean_title,
        "version": detected_version,
        "source_url": url,
        "game_url": game_url,
        "engine": detected_engine,
        "auto_audio": True,
        "status": "in_progress",
        "last_started": datetime.now().isoformat()
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    # 6. Engine-Specific Asset Discovery
    discovered_assets = set()
    external_assets = {}

    if detected_engine == "renpy":
        renpy_assets = RenPyHandler.discover_assets(game_url, captured_urls, session, verify_ssl)
        discovered_assets.update(renpy_assets)

    elif detected_engine in ["sugarcube", "twine"]:
        sc_assets, sc_external = SugarCubeHandler.parse_story(page_html, game_url, engine_info)
        discovered_assets.update(sc_assets)
        external_assets.update(sc_external)

    elif detected_engine == "rpgmaker":
        rpg_assets = RPGMakerHandler.discover_assets(game_url, session, verify_ssl)
        discovered_assets.update(rpg_assets)

    elif detected_engine == "construct":
        c_assets = ConstructHandler.discover_assets(game_url, session, verify_ssl)
        discovered_assets.update(c_assets)

    else:
        html5_assets = HTML5Handler.discover_assets(
            game_url=game_url,
            page_html=page_html,
            session=session,
            verify_ssl=verify_ssl,
            captured_urls=captured_urls,
            captured_responses=captured_responses
        )
        discovered_assets.update(html5_assets)

    # Include all captured same-origin assets from Playwright
    parsed_game_url = urlparse(game_url)
    base_game_dir = os.path.dirname(parsed_game_url.path)
    if not base_game_dir.endswith('/'):
        base_game_dir += '/'

    for cap_url in captured_urls:
        p = urlparse(cap_url)
        if p.netloc == parsed_game_url.netloc and p.path.startswith(base_game_dir):
            rel = p.path[len(base_game_dir):].lstrip('/')
            c = clean_relative_path(rel)
            if c:
                discovered_assets.add(c)

    # Save captured response buffers directly if already downloaded by browser
    saved_from_cache = 0
    for cap_url, body in captured_responses.items():
        p = urlparse(cap_url)
        if p.netloc == parsed_game_url.netloc and p.path.startswith(base_game_dir):
            rel = p.path[len(base_game_dir):].lstrip('/')
            clean = clean_relative_path(rel)
            if clean:
                local_dest = os.path.join(target_folder, clean)
                if not os.path.exists(local_dest) or os.path.getsize(local_dest) <= 715:
                    os.makedirs(os.path.dirname(local_dest), exist_ok=True)
                    with open(local_dest, "wb") as f:
                        f.write(body)
                    saved_from_cache += 1

    if saved_from_cache > 0:
        print(f"[+] Saved {saved_from_cache} runtime assets directly from browser cache.")

    # 7. Save Main index.html and rewrite external CDN URLs for offline use
    # Fetch pristine source HTML to prevent saving mutated runtime DOM (which breaks SugarCube UIBar)
    pristine_html = None
    try:
        r_orig = session.get(game_url, verify=verify_ssl, timeout=10)
        if r_orig.status_code == 200 and len(r_orig.content) > 1000:
            # Modern HTML5 and SugarCube web games are encoded in UTF-8.
            # Avoid r_orig.text as requests defaults to ISO-8859-1 when headers omit charset.
            try:
                pristine_html = r_orig.content.decode('utf-8')
            except UnicodeDecodeError:
                pristine_html = r_orig.content.decode(r_orig.apparent_encoding or 'utf-8', errors='replace')
    except Exception:
        pass

    final_html = pristine_html if pristine_html else page_html
    final_html = clean_mojibake(final_html)

    # Strip any injected CDN anti-hotlinking locks and analytics
    final_html = re.sub(r'<!--\s*starthotlinking\s*-->.*?<!--\s*endhotlinking\s*-->\s*', '', final_html, flags=re.DOTALL)
    final_html = re.sub(r'<!--\s*startAnalytics\s*-->.*?<!--\s*endAnalytics\s*-->\s*', '', final_html, flags=re.DOTALL)

    # Ensure <meta charset="utf-8"> is in <head>
    if "<head" in final_html.lower() and "charset" not in final_html[:3000].lower():
        final_html = re.sub(r'(<head[^>]*>)', r'\1\n  <meta charset="utf-8">', final_html, count=1, flags=re.I)

    for ext_url, local_rel in external_assets.items():
        final_html = final_html.replace(ext_url, local_rel)

    # Normalize backslashes in Twine/SugarCube image and media references
    if detected_engine in ["sugarcube", "twine"]:
        final_html = re.sub(r'\[img\[(.*?)\]\]', lambda m: m.group(0).replace('\\', '/'), final_html)
        final_html = re.sub(r'((?:src|href)=[\'"][^\'"]*?)\\(.*?[\'"])', lambda m: m.group(0).replace('\\', '/'), final_html)

    # Automatically inject or update Auto-Audio in-viewport focus manager
    if "auto-audio-manager" in final_html:
        final_html = re.sub(r'<script\s+id=["\']auto-audio-manager["\']>.*?</script>', AUTO_AUDIO_SCRIPT.strip(), final_html, flags=re.DOTALL)
        print("  [+] Updated Auto-Audio in-viewport focus manager in index.html")
    else:
        if "</body>" in final_html:
            final_html = final_html.replace("</body>", AUTO_AUDIO_SCRIPT + "\n</body>", 1)
        elif "</html>" in final_html:
            final_html = final_html.replace("</html>", AUTO_AUDIO_SCRIPT + "\n</html>", 1)
        else:
            final_html += "\n" + AUTO_AUDIO_SCRIPT
        print("  [+] Injected Auto-Audio in-viewport focus manager into index.html")

    index_path = os.path.join(target_folder, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(final_html)
    print(f"[+] Saved offline entry point: {index_path}")

    # 8. Create Offline Runner Scripts
    bat_path = os.path.join(target_folder, "run_game.bat")
    with open(bat_path, "w") as f:
        f.write("@echo off\n")
        f.write("echo Starting local game server with HTML5 video streaming support...\n")
        f.write("python run_game.py\n")

    py_runner = os.path.join(target_folder, "run_game.py")
    with open(py_runner, "w", encoding="utf-8") as f:
        f.write('''import os
import sys
import json
import mimetypes
import http.server
import socketserver
import webbrowser
from urllib.parse import urlparse, unquote

# Register essential MIME types for WebAssembly & modern engines
mimetypes.add_type('application/wasm', '.wasm')
mimetypes.add_type('application/octet-stream', '.data')
mimetypes.add_type('application/zip', '.zip')
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('image/webp', '.webp')
mimetypes.add_type('video/webm', '.webm')
mimetypes.add_type('video/mp4', '.mp4')

CDN_BASE = None
if os.path.exists('game_meta.json'):
    try:
        with open('game_meta.json', 'r', encoding='utf-8') as f:
            meta = json.load(f)
            CDN_BASE = meta.get('cdn_url') or meta.get('source_url') or meta.get('game_url')
            if CDN_BASE and not CDN_BASE.endswith('/'):
                if CDN_BASE.endswith('.html') or CDN_BASE.endswith('.jpg'):
                    CDN_BASE = CDN_BASE.rsplit('/', 1)[0] + '/'
                else:
                    CDN_BASE += '/'
    except Exception:
        pass

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def guess_type(self, path):
        ctype = super().guess_type(path)
        if ctype and (ctype.startswith('text/') or ctype in ('application/javascript', 'application/json')):
            if 'charset=' not in ctype:
                ctype += '; charset=utf-8'
        return ctype

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        # Crucial for Ren'Py Web SharedArrayBuffer and WASM multi-threading
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'credentialless')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        rel_path = unquote(parsed.path.replace('+', '%2B')).replace('\\\\', '/').lstrip('/')
        if not rel_path:
            rel_path = 'index.html'

        local_path = os.path.join(os.getcwd(), *rel_path.split('/'))
        canonical_cwd = os.path.abspath(os.getcwd())
        canonical_local = os.path.abspath(local_path)
        if not (canonical_local == canonical_cwd or canonical_local.startswith(canonical_cwd + os.sep)):
            self.send_error(403, "Access denied")
            return

        # Auto-fetch missing progressive asset from CDN if available
        if not os.path.isfile(local_path) and CDN_BASE and not rel_path.endswith('.html'):
            try:
                import requests
                r = requests.get(CDN_BASE + rel_path, headers={'User-Agent': 'Mozilla/5.0'}, verify=False, timeout=8)
                if r.status_code == 200:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, 'wb') as f:
                        f.write(r.content)
            except Exception:
                pass

        if not os.path.isfile(local_path):
            return super().do_GET()

        file_size = os.path.getsize(local_path)
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = 'application/octet-stream'
        elif content_type.startswith('text/') or content_type in ('application/javascript', 'application/json'):
            if 'charset=' not in content_type:
                content_type += '; charset=utf-8'

        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            try:
                ranges = range_header[6:].split('-')
                start = int(ranges[0]) if ranges[0] else 0
                end = int(ranges[1]) if ranges[1] else file_size - 1
                if end >= file_size:
                    end = file_size - 1
                length = end - start + 1

                self.send_response(206)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()

                with open(local_path, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(remaining, 65536))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass

        return super().do_GET()

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run local game server with HTTP Range & WebAssembly support.")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    port = args.port
    httpd = None
    for p in range(port, port + 50):
        try:
            httpd = ThreadingTCPServer(("", p), RangeRequestHandler)
            port = p
            break
        except OSError:
            continue

    if not httpd:
        print("Error: Could not find an open port.")
        sys.exit(1)

    url = f"http://localhost:{port}/index.html"
    print("=" * 60)
    print(f"Local Game Server running at: {url}")
    if CDN_BASE:
        print(f"Auto-caching enabled from CDN: {CDN_BASE}")
    print("Keep this window open while playing!")
    print("=" * 60)

    if not args.no_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down server...")

if __name__ == "__main__":
    main()
''')

    print("[+] Generated 'run_game.bat' and 'run_game.py' for instant local offline play.")

    # 9. Build Download Queue (Preserving Story Order and Filtering if requested)
    asset_map = {}
    video_exts = ('.mp4', '.webm', '.avi', '.mov', '.mkv', '.m4v')
    skip_download_assets = {"index.html", "game_meta.json", "run_game.bat", "run_game.py"}
    for rel_path in discovered_assets:
        if rel_path.lower() in skip_download_assets:
            continue
        if no_videos and rel_path.lower().endswith(video_exts):
            continue
        safe_rel = quote(unquote(rel_path.replace('+', '%2B')), safe="/()'!~,-_.@")
        full_url = urljoin(game_url, safe_rel)
        asset_map[rel_path] = full_url

    for ext_url, local_rel in external_assets.items():
        if local_rel.lower() in skip_download_assets:
            continue
        if no_videos and local_rel.lower().endswith(video_exts):
            continue
        asset_map[local_rel] = ext_url

    # 10. If PLAY-NOW mode is enabled, launch On-Demand Caching Server immediately!
    httpd = None
    if play_now:
        port = 8080
        handler_cls = OnDemandCachingServer.create_handler(target_folder, game_url, session, verify_ssl)
        for p in range(8080, 8100):
            try:
                httpd = ThreadingTCPServer(("", p), handler_cls)
                port = p
                break
            except OSError:
                continue

        if not httpd:
            print("[!] Could not bind to any port between 8080 and 8100.")
            return

        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        print("\n" + "=" * 75)
        print(f"[+] PLAY-NOW SERVER STARTED at http://localhost:{port}/index.html")
        print("[+] Opening game in your browser! Any missing assets will stream live on-demand.")
        print("=" * 75 + "\n")
        webbrowser.open(f"http://localhost:{port}/index.html")

        if stream_only:
            print("[*] Stream-only mode active: background pre-downloading disabled to save Wi-Fi quota.")
            print("[*] Assets stream purely on-demand as you play. Press Ctrl+C in terminal when finished.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n[*] Stopping stream server...")
                httpd.shutdown()
            return

    # 11. Execute Background Downloads with Safe Resumability & Limit Caps
    downloader = AssetDownloader(
        base_url=game_url,
        download_folder=target_folder,
        session=session,
        verify_ssl=verify_ssl,
        max_workers=max_workers,
        max_mb=limit_mb,
        diff_check=diff_check,
        etag_index=etag_index,
        size_index=size_index,
        manifest=manifest
    )
    success_count, failed_count, limit_reached = downloader.download_batch(asset_map)

    mb_transferred = downloader.total_downloaded_bytes / (1024 * 1024)
    if limit_reached:
        meta["status"] = "paused_limit"
        meta["downloaded_mb"] = mb_transferred
        meta["last_paused"] = datetime.now().isoformat()
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        print("\n" + "=" * 75)
        print(f" [!] DOWNLOAD LIMIT REACHED: {mb_transferred:.2f} MB transferred (limit: {limit_mb:.1f} MB).")
        print(" [!] Download safely paused to protect your Wi-Fi quota.")
        print(" [+] All finished files and partial chunk .part files are preserved.")
        print(" [+] Run the command again anytime to resume where you left off,")
        print("     or launch with --play-now to play immediately with live on-demand streaming!")
        print("=" * 75)
    else:
        # Mark metadata completed
        meta["status"] = "completed"
        meta["completed_at"] = datetime.now().isoformat()
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # Register completed download into persistent catalog
        try:
            GameCatalog.register_game(target_folder, gamcore_url=url if "gamcore.com" in url.lower() else None, cdn_url=game_url)
        except Exception:
            pass

        print("=" * 75)
        print(" [+] 100% of prioritized game assets have been downloaded and cached locally! ")
        print(" [+] The game is now fully offline-playable anytime via run_game.bat. ")
        print("=" * 75)

    if play_now and httpd:
        print("[*] Game server is actively running. Press Ctrl+C in terminal when finished playing.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[*] Stopping game server...")
            httpd.shutdown()
    elif serve:
        os.chdir(target_folder)
        port = 8080
        serve_httpd = None
        for p in range(8080, 8100):
            try:
                serve_httpd = ThreadingTCPServer(("", p), RangeRequestHandler)
                port = p
                break
            except OSError:
                continue
        if serve_httpd:
            print(f"[*] Launching game locally at http://localhost:{port}/index.html ...")
            webbrowser.open(f"http://localhost:{port}/index.html")
            try:
                serve_httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n[*] Stopping server...")
                serve_httpd.shutdown()

# ==============================================================================
# CLI INTERFACE WITH CUSTOM FORMATTED HELP
# ==============================================================================

class GameCompactor:
    """Recursively discovers game directories and optimizes high-bitrate video assets via FFmpeg."""

    @staticmethod
    def is_ffmpeg_available():
        try:
            res = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
            return res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def get_directory_size(dir_path):
        total = 0
        for root, dirs, files in os.walk(dir_path):
            for f in files:
                p = os.path.join(root, f)
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
        return total

    @classmethod
    def get_video_info(cls, file_path):
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            str(file_path)
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                return None
            data = json.loads(res.stdout)
            v_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
            if not v_stream:
                return None

            bitrate = int(data.get('format', {}).get('bit_rate', 0)) // 1000
            if bitrate == 0 and v_stream.get('bit_rate'):
                bitrate = int(v_stream['bit_rate']) // 1000

            duration = float(data.get('format', {}).get('duration', 0))
            if bitrate == 0 and duration > 0:
                sz_bits = os.path.getsize(file_path) * 8
                bitrate = int(sz_bits / duration / 1000)

            return {
                'codec': v_stream.get('codec_name', '').lower(),
                'width': int(v_stream.get('width', 0)),
                'height': int(v_stream.get('height', 0)),
                'bitrate_kbps': bitrate,
                'duration': duration
            }
        except Exception:
            return None

    @classmethod
    def optimize_video(cls, file_path, min_bitrate=2500):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ('.mp4', '.webm'):
            return None

        try:
            orig_size = os.path.getsize(file_path)
        except OSError:
            return None

        if orig_size < 500 * 1024:
            return None

        info = cls.get_video_info(file_path)
        if not info:
            return None

        bitrate = info['bitrate_kbps']
        codec = info['codec']

        effective_thresh = min_bitrate
        if info['height'] > 0 and info['height'] <= 720:
            effective_thresh = int(min_bitrate * 0.7)

        needs_reencode = False
        if bitrate > effective_thresh:
            needs_reencode = True
        elif codec in ('vp8', 'mpeg4', 'msmpeg4v3'):
            needs_reencode = True

        if not needs_reencode:
            return None

        tmp_path = file_path + ".tmp" + ext

        if ext == '.mp4':
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-v', 'error',
                '-i', str(file_path),
                '-c:v', 'libx264',
                '-crf', '23',
                '-preset', 'veryfast',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-c:a', 'copy',
                str(tmp_path)
            ]
        else:
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-v', 'error',
                '-i', str(file_path),
                '-c:v', 'libvpx-vp9',
                '-crf', '31',
                '-b:v', '0',
                '-row-mt', '1',
                '-cpu-used', '4',
                '-threads', '2',
                '-c:a', 'copy',
                str(tmp_path)
            ]

        try:
            ret = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=180)
            if ret.returncode == 0 and os.path.exists(tmp_path):
                new_size = os.path.getsize(tmp_path)
                if new_size < (orig_size * 0.95):
                    os.replace(tmp_path, file_path)
                    return orig_size - new_size
                else:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    return 0
            else:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                return None
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None

    @classmethod
    def compact_directory(cls, target_dir="downloads", max_workers=4, min_bitrate=2500):
        if not cls.is_ffmpeg_available():
            print("\n" + "=" * 75)
            print(" [X] ERROR: FFmpeg is not installed or not found in system PATH.")
            print(" [*] FFmpeg is required for the --compact feature.")
            print(" [*] Please install FFmpeg (https://ffmpeg.org or winget install Gyan.FFmpeg) and try again.")
            print("=" * 75)
            return

        print("\n" + "=" * 75, flush=True)
        print(f"[*] GameGrabber Compactor: Scanning '{target_dir}' for games...", flush=True)
        print("=" * 75, flush=True)

        game_paths = GameUpdateChecker.find_game_directories(target_dir)
        if not game_paths and GameUpdateChecker.is_game_directory(target_dir):
            game_paths = [target_dir]

        if not game_paths:
            print(f"[*] No game installations found in '{target_dir}'.", flush=True)
            return

        print(f"[*] Discovered {len(game_paths)} game installation(s) to inspect.\n", flush=True)

        total_orig_bytes = 0
        total_saved_bytes = 0
        total_vids_optimized = 0
        game_summaries = []

        for g_idx, game_dir in enumerate(game_paths, 1):
            folder_name = os.path.basename(game_dir)
            size_before = cls.get_directory_size(game_dir)
            total_orig_bytes += size_before

            # Find all videos
            vids = []
            for root, dirs, files in os.walk(game_dir):
                for f in files:
                    if f.lower().endswith(('.mp4', '.webm')):
                        vids.append(os.path.join(root, f))

            if not vids:
                game_summaries.append({
                    "name": folder_name,
                    "before": size_before,
                    "after": size_before,
                    "saved": 0,
                    "vids": 0
                })
                continue

            print(f"[{g_idx}/{len(game_paths)}] Checking '{folder_name}' ({len(vids)} videos, {size_before / (1024*1024):.1f} MB)...", flush=True)
            game_saved = 0
            game_vids_opt = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(cls.optimize_video, v, min_bitrate): v for v in vids}
                for f in as_completed(futures):
                    saved = f.result()
                    if saved and saved > 0:
                        game_saved += saved
                        game_vids_opt += 1

            size_after = cls.get_directory_size(game_dir)
            total_saved_bytes += game_saved
            total_vids_optimized += game_vids_opt

            if game_saved > 0:
                pct = (game_saved / size_before) * 100 if size_before else 0
                print(f"  [+] Compressed: {size_before / (1024*1024):.1f} MB -> {size_after / (1024*1024):.1f} MB (Saved: {game_saved / (1024*1024):.1f} MB / -{pct:.1f}% | {game_vids_opt} videos)", flush=True)
            else:
                print(f"  [*] Already optimal ({size_after / (1024*1024):.1f} MB).", flush=True)

            game_summaries.append({
                "name": folder_name,
                "before": size_before,
                "after": size_after,
                "saved": game_saved,
                "vids": game_vids_opt
            })

        # Final Report
        print("\n" + "=" * 80)
        print("                  COMPACTION SUMMARY & STORAGE SAVINGS")
        print("=" * 80)
        print(f"{'Game Name':<38} | {'Before':>10} | {'After':>10} | {'Saved':>10} | {'Reduction':>9}")
        print("-" * 80)
        for s in game_summaries:
            b_mb = s['before'] / (1024 * 1024)
            a_mb = s['after'] / (1024 * 1024)
            s_mb = s['saved'] / (1024 * 1024)
            pct = (s['saved'] / s['before'] * 100) if s['before'] > 0 else 0
            b_str = f"{b_mb:.1f} MB" if b_mb < 1024 else f"{b_mb/1024:.2f} GB"
            a_str = f"{a_mb:.1f} MB" if a_mb < 1024 else f"{a_mb/1024:.2f} GB"
            s_str = f"{s_mb:.1f} MB" if s_mb < 1024 else f"{s_mb/1024:.2f} GB"
            print(f"{s['name'][:38]:<38} | {b_str:>10} | {a_str:>10} | {s_str:>10} | {pct:>8.1f}%")
        print("-" * 80)

        tot_b = total_orig_bytes / (1024 * 1024)
        tot_a = (total_orig_bytes - total_saved_bytes) / (1024 * 1024)
        tot_s = total_saved_bytes / (1024 * 1024)
        tot_pct = (total_saved_bytes / total_orig_bytes * 100) if total_orig_bytes else 0
        tot_b_str = f"{tot_b:.1f} MB" if tot_b < 1024 else f"{tot_b/1024:.2f} GB"
        tot_a_str = f"{tot_a:.1f} MB" if tot_a < 1024 else f"{tot_a/1024:.2f} GB"
        tot_s_str = f"{tot_s:.1f} MB" if tot_s < 1024 else f"{tot_s/1024:.2f} GB"
        print(f"{'TOTAL':<38} | {tot_b_str:>10} | {tot_a_str:>10} | {tot_s_str:>10} | {tot_pct:>8.1f}%")
        print(f"[*] Videos optimized: {total_vids_optimized}")
        print("=" * 80 + "\n")

class GameAudioFixer:
    """Recursively discovers game directories, inspects index.html, and injects/upgrades Auto-Audio Focus Manager."""

    @classmethod
    def fix_game(cls, game_dir):
        """
        Inspects index.html in game_dir.
        Returns:
            ("injected", message) if injected
            ("updated", message) if upgraded from previous version
            ("skipped", message) if already present
            ("no_html", message) if index.html is missing
            ("error", message) if read/write failed
        """
        index_path = os.path.join(game_dir, "index.html")
        if not os.path.exists(index_path):
            return "no_html", "No index.html found"

        try:
            st = os.stat(index_path)
            orig_atime = st.st_atime
            orig_mtime = st.st_mtime
            with open(index_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return "error", f"Read error: {e}"

        # If auto-audio-manager is already present in content:
        if "auto-audio-manager" in content:
            updated = re.sub(
                r'<script\s+id=["\']auto-audio-manager["\']>.*?</script>',
                AUTO_AUDIO_SCRIPT.strip(),
                content,
                flags=re.DOTALL
            )
            if updated != content:
                try:
                    with open(index_path, "w", encoding="utf-8") as f:
                        f.write(updated)
                    os.utime(index_path, (orig_atime, orig_mtime))
                    cls._mark_meta(game_dir)
                    return "updated", "Upgraded to In-Viewport Pause & Audio Manager"
                except Exception as e:
                    return "error", f"Write error: {e}"
            else:
                return "skipped", "Already up to date"

        # Not present: inject before </body>, </html>, or at EOF
        if "</body>" in content:
            updated = content.replace("</body>", AUTO_AUDIO_SCRIPT + "\n</body>", 1)
        elif "</html>" in content:
            updated = content.replace("</html>", AUTO_AUDIO_SCRIPT + "\n</html>", 1)
        else:
            updated = content + "\n" + AUTO_AUDIO_SCRIPT

        try:
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(updated)
            os.utime(index_path, (orig_atime, orig_mtime))
            cls._mark_meta(game_dir)
            return "injected", "Injected successfully"
        except Exception as e:
            return "error", f"Write error: {e}"

    @classmethod
    def _mark_meta(cls, game_dir):
        meta_path = os.path.join(game_dir, "game_meta.json")
        if os.path.exists(meta_path):
            try:
                st = os.stat(meta_path)
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta["auto_audio"] = True
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2)
                os.utime(meta_path, (st.st_atime, st.st_mtime))
            except Exception:
                pass

    @classmethod
    def fix_directory(cls, target_dir="downloads"):
        print("\n" + "=" * 80)
        print(" GameGrabber - Auto-Audio & In-Viewport Playback Manager Injector")
        print("=" * 80)
        print(f"[*] Scanning '{target_dir}' for downloaded games...\n")

        # Resolve target if single folder passed
        for cand in [
            target_dir,
            os.path.join("downloads", "html", target_dir),
            os.path.join("downloads", "renpy", target_dir),
            os.path.join("downloads", target_dir)
        ]:
            if os.path.isdir(cand):
                target_dir = cand
                break

        game_paths = GameUpdateChecker.find_game_directories(target_dir)
        if not game_paths and GameUpdateChecker.is_game_directory(target_dir):
            game_paths = [target_dir]

        if not game_paths:
            print(f"[*] No game installations found in '{target_dir}'.")
            return

        print(f"[*] Discovered {len(game_paths)} game installation(s) to inspect.\n")

        counts = {"injected": 0, "updated": 0, "skipped": 0, "no_html": 0, "error": 0}

        for p in game_paths:
            name = os.path.basename(p)
            status, msg = cls.fix_game(p)
            counts[status] = counts.get(status, 0) + 1
            if status == "injected":
                print(f"  [+] {name:<45} -> Injected In-Viewport Audio Manager")
            elif status == "updated":
                print(f"  [+] {name:<45} -> Upgraded to In-Viewport Pause & Audio Manager")
            elif status == "skipped":
                print(f"  [=] {name:<45} -> Already Up to Date (Skipped)")
            elif status == "no_html":
                print(f"  [-] {name:<45} -> No index.html found (Skipped)")
            else:
                print(f"  [!] {name:<45} -> Error: {msg}")

        print("\n" + "=" * 80)
        print("                   AUTO-AUDIO INJECTION SUMMARY")
        print("=" * 80)
        print(f"[*] Total Games Inspected : {len(game_paths)}")
        print(f"[+] Newly Injected        : {counts['injected']}")
        print(f"[^] Upgraded              : {counts['updated']}")
        print(f"[=] Already Up to Date    : {counts['skipped']}")
        if counts['no_html'] > 0:
            print(f"[-] Missing index.html    : {counts['no_html']}")
        if counts['error'] > 0:
            print(f"[!] Errors Encountered    : {counts['error']}")
        print("=" * 80 + "\n")
        print("=" * 80 + "\n")

class CustomHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def _format_action_invocation(self, action):
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)
        return ', '.join(action.option_strings) + ' ' + args_string

def main():
    parser = argparse.ArgumentParser(
        prog="scraper.py",
        formatter_class=CustomHelpFormatter,
        description="""
================================================================================
 GameGrabber - Universal Browser & Web Game Downloader
================================================================================
Download and preserve browser games for complete offline play:
  * Automatically detects game engines (Ren'Py Web, SugarCube/Twine, RPG Maker, Construct, HTML5).
  * Automatically unpacks / inspects portal iframe embeds and resolves direct CDN/game URLs.
  * Captures runtime WebAssembly/WASM assets and game.zip packages.
  * Resolves hidden choice branches, dynamic asset paths, and localizes external CDN assets.
  * Formats folder names cleanly as '<Game_Name>-<Version>'.
  * Safe resumability with Range requests and atomic .part renaming (prevents corruption).
  * Self-healing: automatically detects and purges corrupt/captive-portal block pages on resume.
  * --play-now mode: on-demand caching proxy server allowing instant play while streaming.
  * Generates instant offline runners (run_game.bat / run_game.py).
""",
        epilog="""
Examples:
  # Play immediately while downloading in the background (on-demand streaming):
  python scraper.py "https://game-portal.com/games/space-adventure" --play-now

  # Scrape a web game directly (saved into <Game_Name>-<Version>):
  python scraper.py "https://game-portal.com/games/space-adventure"

  # Download to a specific custom directory (e.g. current directory '.', 'advancements', or 'special_downloads/here'):
  python scraper.py "https://game-portal.com/games/space-adventure" .
  python scraper.py "https://game-portal.com/games/space-adventure" advancements
  python scraper.py "https://game-portal.com/games/space-adventure" special_downloads/here

  # Resume an interrupted download (automatically purges corrupt files and skips cached ones):
  python scraper.py "https://game-portal.com/games/space-adventure"

  # Conserve bandwidth: download text & images only, skipping heavy videos:
  python scraper.py "https://game-portal.com/games/space-adventure" --no-videos

  # Check for updates across all downloaded games (or specific directory):
  python scraper.py --updateall
  python scraper.py --updateall "downloads/html"
  python scraper.py --updateall -y

  # Check if a specific game has an update:
  python scraper.py "Seven_Sins-v1.10" --update
  python scraper.py "Seven_Sins-v1.10" --update -y
  python scraper.py "https://gamcore.com/games/seven_sins" --update

  # Force differential verification / check on existing assets:
  python scraper.py "Seven_Sins-v1.10" --diff

  # Stop after 250 MB downloaded (safe for limited data plans, resumable anytime):
  python scraper.py "https://game-portal.com/games/space-adventure" --limit 250

  # Compress videos with FFmpeg across all downloaded games (or specific directory):
  python scraper.py --compact
  python scraper.py --compact "downloads/html"
  python scraper.py --compact "The_DICKtator-v2.5.1"

  # Detect and inject Auto-Audio focus manager across all games (or specific game/dir):
  python scraper.py --audio-fix
  python scraper.py --audio-fix "downloads/html"
  python scraper.py --audio-fix "Candid_Urban_Muffs-v0.78"

  # Archive a game to reclaim disk space (keeps version and URLs in catalog, deletes local folder):
  python scraper.py --archive "Fairytale-v1.0"
  python scraper.py --archive "Fairytale-v1.0" -y

  # Register a game in catalog WITHOUT deleting files on disk:
  python scraper.py --register "Seven_Sins-v1.10"
  python scraper.py --register "https://gamcore.com/games/college_daze"

  # Sync all existing downloads & tracking log into game_catalog.json:
  python scraper.py --sync-catalog

  # View all tracked games and statuses in catalog:
  python scraper.py --catalog

  # Scrape with custom threads and proxy:
  python scraper.py "https://game-host.com/game/index.html" --threads 12 --proxy socks5://127.0.0.1:1080
================================================================================
"""
    )

    parser.add_argument("url", nargs="?", default=None, help="Target URL (Game portal page URL or direct game iframe/CDN URL) or local downloaded game directory")
    parser.add_argument("dest", nargs="?", default=None, help="Optional custom destination directory (e.g. '.', 'advancements', 'special_downloads/here'). Overrides default 'downloads/<engine>/<name>' path.")
    parser.add_argument("--register", "--track", dest="register", type=str, metavar="TARGET", default=None, help="Explicitly register a game folder or online URL into the persistent catalog WITHOUT deleting local files.")
    parser.add_argument("--archive", dest="archive", type=str, metavar="TARGET", default=None, help="Archive a game: records versions and URLs into catalog/DOWNLOADED_GAMES.md, then safely deletes local files to reclaim disk space.")
    parser.add_argument("--sync-catalog", dest="sync_catalog", action="store_true", help="Scans existing downloads and tracking log to synchronize 'game_catalog.json' and 'DOWNLOADED_GAMES.md'.")
    parser.add_argument("--catalog", "--list-catalog", dest="list_catalog", action="store_true", help="Display all games tracked in the persistent catalog with version, status, and disk presence.")
    parser.add_argument("--audio-fix", "--fix-audio", dest="audio_fix", nargs="?", const="downloads", default=None, metavar="DIR", help="Detect and inject Auto-Audio Focus Manager into downloaded games. Checks index.html; skips games that already have it. Scans DIR (defaults to 'downloads/'), or can be pointed to a specific game directory.")
    parser.add_argument("--compact", "--optimize", dest="compact", nargs="?", const="downloads", default=None, metavar="DIR", help="Recursively discovers downloaded games in DIR (defaults to 'downloads/'), re-encodes high-bitrate videos via FFmpeg, and reports before/after storage sizes and savings.")
    parser.add_argument("--update", "--check-updates", "--check-update", dest="update", action="store_true", help="Check for game updates via CDN headers, ETags, and version probing. Scans all games if no URL is given, or checks a specific game.")
    parser.add_argument("--updateall", dest="updateall", nargs="?", const="downloads", default=None, metavar="DIR", help="Recursively discovers and checks all game installations inside DIR (defaults to 'downloads/').")
    parser.add_argument("-y", "--yes", dest="auto_yes", action="store_true", help="Automatically accept update prompts and download detected updates without confirmation.")
    parser.add_argument("--diff", "--differential", action="store_true", help="Perform differential update check on existing assets (re-verifies remote sizes/hashes and re-downloads modified files).")
    parser.add_argument("-o", "--folder", "--out", dest="folder", type=str, metavar="DIR", help="Custom output directory name (e.g. '.', 'advancements', 'special_downloads/here'). Overrides default 'downloads/<engine>/<name>' path.")
    parser.add_argument("--play-now", "--stream", dest="play_now", action="store_true", help="Play immediately! Starts an on-demand caching server and streams missing assets live while downloading in background")
    parser.add_argument("--stream-only", "--lazy", dest="stream_only", action="store_true", help="Conserve bandwidth: launches game server and streams assets ONLY on-demand as you play (disables background pre-downloading)")
    parser.add_argument("--no-videos", "--images-only", dest="no_videos", action="store_true", help="Conserve bandwidth: downloads all images/audio/text but skips heavy video files (.mp4/.webm)")
    parser.add_argument("--headed", action="store_true", help="Launch browser with GUI visible (headed mode)")
    parser.add_argument("--no-verify", action="store_true", help="Disable SSL certificate verification")
    parser.add_argument("--wait", type=int, default=8, metavar="SEC", help="Seconds to wait after game boots for WASM/assets to settle (default: 8)")
    parser.add_argument("--threads", type=int, default=8, metavar="N", help="Number of parallel worker threads for asset downloads (default: 8)")
    parser.add_argument("--serve", action="store_true", help="Start local server and open game in browser immediately after downloading")
    parser.add_argument("--limit", "--max-mb", dest="limit", type=float, default=None, metavar="MB", help="Stop downloading after transferring this many megabytes (safe for limited Wi-Fi; resumable anytime)")
    parser.add_argument("--proxy", type=str, metavar="URL", help="Optional HTTP/SOCKS5 proxy (e.g. http://127.0.0.1:8080 or socks5://127.0.0.1:1080)")

    args = parser.parse_args()
    custom_dest = args.dest if args.dest is not None else args.folder

    if not args.url and not args.update and args.updateall is None and args.compact is None and args.audio_fix is None and not args.sync_catalog and not args.list_catalog and args.archive is None and args.register is None:
        parser.print_help()
        sys.exit(0)

    # Handle --sync-catalog
    if args.sync_catalog:
        GameCatalog.sync_catalog()
        return

    # Handle --catalog / --list-catalog
    if args.list_catalog:
        GameCatalog.list_catalog()
        return

    # Handle --register
    if args.register:
        GameCatalog.register_game(args.register)
        return

    # Handle --archive
    if args.archive:
        GameCatalog.archive_game(args.archive, auto_yes=args.auto_yes)
        return

    # Handle --audio-fix
    if args.audio_fix is not None:
        GameAudioFixer.fix_directory(target_dir=args.audio_fix)
        return

    # Handle --compact
    if args.compact is not None:
        target = args.compact
        # If user passed a game name without full path
        for cand in [target, os.path.join("downloads", "html", target), os.path.join("downloads", "renpy", target), os.path.join("downloads", target)]:
            if os.path.isdir(cand):
                target = cand
                break
        GameCompactor.compact_directory(target_dir=target, max_workers=args.threads)
        return

    play_now = args.play_now or args.stream_only

    # Handle --updateall or --update without URL
    if args.updateall is not None or (args.update and not args.url):
        scan_dir = args.updateall if args.updateall is not None else "downloads"
        updates_found = GameUpdateChecker.check_all_downloaded_games(
            target_dir=scan_dir,
            verify_ssl=not args.no_verify,
            max_workers=args.threads
        )
        if updates_found:
            proceed = False
            if args.auto_yes:
                print(f"[*] -y / --yes provided: automatically downloading {len(updates_found)} update(s)...")
                proceed = True
            else:
                try:
                    ans = input(f"[?] {len(updates_found)} update(s) available. Download updates now? [y/N]: ").strip().lower()
                    if ans in ['y', 'yes']:
                        proceed = True
                    else:
                        print("[*] Update download skipped by user.")
                except (KeyboardInterrupt, EOFError):
                    print("\n[*] Aborted.")
                    return

            if proceed:
                for g, r in updates_found:
                    new_tgt = r.get("remote_url", g["url"])
                    print("\n" + "=" * 80)
                    print(f"[*] Updating '{g['folder']}' from: {new_tgt}")
                    print("=" * 80)
                    grab_game(
                        url=new_tgt,
                        output_folder=custom_dest,
                        headed=args.headed,
                        no_verify=args.no_verify,
                        wait_idle=args.wait,
                        serve=False,
                        play_now=False,
                        max_workers=args.threads,
                        proxy=args.proxy,
                        no_videos=args.no_videos,
                        stream_only=args.stream_only,
                        limit_mb=args.limit,
                        diff_check=True,
                        seed_folder=g["path"]
                    )
        return

    # Handle --update on a specific game or URL
    if args.update:
        target = args.url
        local_path = None
        for candidate in [
            target,
            os.path.join("downloads", "html", target),
            os.path.join("downloads", "renpy", target),
            os.path.join("downloads", target)
        ]:
            if os.path.isdir(candidate):
                local_path = candidate
                break

        remote_url = target
        local_mtime = None
        local_etag = None
        if local_path:
            meta_file = os.path.join(local_path, "game_meta.json")
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    remote_url = meta.get("game_url") or meta.get("cdn_url") or meta.get("source_url") or target
                    local_etag = meta.get("etag")
                except Exception:
                    pass
            index_file = os.path.join(local_path, "index.html")
            if os.path.exists(index_file):
                local_mtime = datetime.fromtimestamp(os.path.getmtime(index_file), tz=timezone.utc)
            elif os.path.exists(meta_file):
                local_mtime = datetime.fromtimestamp(os.path.getmtime(meta_file), tz=timezone.utc)

        # If not found on disk, check if it's an archived game in catalog
        catalog = GameCatalog.load_catalog()
        cat_match = None
        if not local_path:
            for k, e in catalog.items():
                if k.lower() == target.lower() or e.get("title", "").lower() == target.lower():
                    cat_match = e
                    remote_url = e.get("cdn_url") or e.get("gamcore_url") or target
                    break

        print("=" * 80)
        print(" GameGrabber - Update Verification")
        print("=" * 80)
        print(f"[*] Target             : {target}")
        if local_path:
            print(f"[*] Local Installation : {os.path.abspath(local_path)}")
        elif cat_match:
            print(f"[*] Catalog Status     : {cat_match.get('status')} [Version: {cat_match.get('version')}]")
        print(f"[*] Remote Origin URL  : {remote_url}")

        res = GameUpdateChecker.check_single_url(
            remote_url,
            local_mtime=local_mtime,
            local_etag=local_etag,
            local_index_path=index_file if (local_path and os.path.exists(index_file)) else None,
            verify_ssl=not args.no_verify
        )

        if res.get("has_update"):
            print("\n" + "=" * 80)
            print(f"[!] UPDATE DETECTED: {res.get('message')}")
            download_url = res.get("remote_url", remote_url)
            print(f"[*] Update Origin URL  : {download_url}")
            print("=" * 80)

            proceed = False
            if args.auto_yes:
                print("[*] -y / --yes provided: proceeding with download automatically.")
                proceed = True
            else:
                try:
                    ans = input("\n[?] An update is available. Download this update now? [y/N]: ").strip().lower()
                    if ans in ['y', 'yes']:
                        proceed = True
                    else:
                        print("[*] Update download skipped by user.")
                except (KeyboardInterrupt, EOFError):
                    print("\n[*] Aborted.")
                    return

            if proceed:
                print(f"[*] Proceeding to download updated version from: {download_url}\n")
                grab_game(
                    url=download_url,
                    output_folder=custom_dest,
                    headed=args.headed,
                    no_verify=args.no_verify,
                    wait_idle=args.wait,
                    serve=args.serve,
                    play_now=play_now,
                    max_workers=args.threads,
                    proxy=args.proxy,
                    no_videos=args.no_videos,
                    stream_only=args.stream_only,
                    limit_mb=args.limit,
                    diff_check=True,
                    seed_folder=local_path
                )
        else:
            print("\n" + "-" * 80)
            print(f"[+] Status: {res.get('message', 'Game is up to date.')}")
            if res.get("remote_mtime"):
                print(f"[+] Remote Last-Modified: {res['remote_mtime'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
            if res.get("remote_etag"):
                print(f"[+] Remote ETag: {res['remote_etag']}")
            print("-" * 80 + "\n")
        return

    grab_game(
        url=args.url,
        output_folder=custom_dest,
        headed=args.headed,
        no_verify=args.no_verify,
        wait_idle=args.wait,
        serve=args.serve,
        play_now=play_now,
        max_workers=args.threads,
        proxy=args.proxy,
        no_videos=args.no_videos,
        stream_only=args.stream_only,
        limit_mb=args.limit,
        diff_check=args.diff
    )

if __name__ == "__main__":
    main()
