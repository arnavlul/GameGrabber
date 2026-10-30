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
import argparse
import mimetypes
import urllib3
import requests
import webbrowser
import threading
import http.server
import socketserver
from datetime import datetime
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

    # 2. Search html_content if version not yet found
    if not version and html_content:
        m_var = re.search(r'(?:version|game_version|gameVersion|build_num)\s*[:=]\s*["\']v?([0-9]+(?:\.[0-9a-zA-Z_\-]+)*)["\']', html_content, re.I)
        if m_var:
            version = "v" + m_var.group(1).lstrip('vV')

        if not version:
            m_pkg = re.search(r'"version"\s*:\s*"v?([0-9]+(?:\.[0-9a-zA-Z_\-]+)*)"', html_content)
            if m_pkg:
                version = "v" + m_pkg.group(1).lstrip('vV')

    # 3. Check URL slug for version pattern: /game-name-v0.12/
    if not version:
        u_match = re.search(r'[-_](?:v|ver)?([0-9]+(?:\.[0-9a-zA-Z_\-]+)+)', url, re.I)
        if u_match:
            version = "v" + u_match.group(1).lstrip('vV')

    if not version:
        version = "v1.0"

    # If title is empty or generic, extract from URL slug
    if not clean_title or clean_title.lower() in ['untitled game', 'gamcore', 'game']:
        slug = url.rstrip('/').split('/')[-1]
        slug = re.sub(r'\.(?:html?|php)$', '', slug)
        clean_title = slug.replace('-', '_').title()

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
            chosen_url = candidates[0][1]
            print(f"[+] Found Game Frame URL: {chosen_url}")
            return chosen_url

        for frame in page.frames:
            f_url = frame.url
            if f_url and f_url != initial_url and f_url != 'about:blank':
                if not any(ad in f_url.lower() for ad in AD_DOMAINS):
                    print(f"[+] Found Frame in page tree: {f_url}")
                    return f_url

        print("[*] No separate iframe detected. Treating initial URL as game entry point.")
        return initial_url

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
    def parse_story(html_content, game_url, metadata):
        discovered_assets = []
        external_assets = {}
        unescaped = html.unescape(html_content)

        image_prefix = "images/"
        sound_prefix = "sounds/"

        setup_meta = metadata.get("metadata", {}).get("setup", {}) if isinstance(metadata, dict) else {}
        if setup_meta:
            if setup_meta.get("ImagePath"):
                image_prefix = setup_meta["ImagePath"].lstrip('/')
            if setup_meta.get("SoundPath"):
                sound_prefix = setup_meta["SoundPath"].lstrip('/')

        m_img = re.search(r'setup\.ImagePath\s*=\s*(?:setup\.Path\s*\+\s*)?[\'"]([^\'"]*)[\'"]', unescaped)
        if m_img:
            image_prefix = m_img.group(1).lstrip('/')

        m_snd = re.search(r'setup\.SoundPath\s*=\s*(?:setup\.Path\s*\+\s*)?[\'"]([^\'"]*)[\'"]', unescaped)
        if m_snd:
            sound_prefix = m_snd.group(1).lstrip('/')

        print(f"[+] SugarCube Setup paths -> imagePrefix: '{image_prefix}', soundPrefix: '{sound_prefix}'")

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
                    c = clean_relative_path(val)
                    if c:
                        if c.lower().endswith(('.mp4', '.webm', '.avi', '.mov')):
                            vids.append(c)
                        else:
                            imgs.append(c)

            # SugarCube [img[...]] syntax
            for raw in re.findall(r'\[img\[(.*?)\]\]', text):
                for part in raw.split(']['):
                    part = part.strip()
                    if '|' in part:
                        part = part.split('|', 1)[1].strip()
                    c = clean_relative_path(part)
                    if c:
                        if not c.lower().startswith(('images/', 'videos/', 'sounds/', 'audio/')) and '/' not in c:
                            c = (image_prefix + c).replace('\\', '/')
                        if c.lower().endswith(('.mp4', '.webm', '.avi', '.mov')):
                            vids.append(c)
                        else:
                            imgs.append(c)

            # SugarCube macro tags (<<video "path">>, <<audio "path">>)
            for m in re.finditer(r'<<(?:video|audio)\s+[\'"]([^\'"]+)[\'"]', text, re.I):
                c = clean_relative_path(m.group(1))
                if c:
                    if c.lower().endswith(('.mp4', '.webm', '.avi', '.mov')):
                        vids.append(c)
                    else:
                        imgs.append(c)

            # Audio macros
            for m in re.finditer(r'<<cacheaudio\s+[\'"][^\'"]+[\'"]\s+[\'"]([^\'"]+)[\'"]', text):
                c = clean_relative_path(m.group(1))
                if c:
                    if not c.lower().startswith(('sounds/', 'audio/')) and '/' not in c:
                        c = (sound_prefix + c).replace('\\', '/')
                    imgs.append(c)

            # Setup paths
            for m in re.finditer(r'setup\.ImagePath\s*\+\s*[\'"]([^\'"]+)[\'"]', text):
                c = clean_relative_path((image_prefix + m.group(1).lstrip('/')).replace('\\', '/'))
                if c:
                    imgs.append(c)
            for m in re.finditer(r'setup\.SoundPath\s*\+\s*[\'"]([^\'"]+)[\'"]', text):
                c = clean_relative_path((sound_prefix + m.group(1).lstrip('/')).replace('\\', '/'))
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

        # Step B: Fallback regex scan across global unescaped HTML/scripts
        media_regex = re.compile(r'([a-zA-Z0-9_\-./\s]+\.(?:' + '|'.join(MEDIA_EXTENSIONS) + r'))', re.I)
        for m in media_regex.finditer(unescaped):
            token = m.group(1).strip('\'" \t\r\n')
            if token.startswith(('http://', 'https://')):
                p = urlparse(token)
                external_assets[token] = f"external/{p.netloc}{p.path}"
            else:
                c = clean_relative_path(token)
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

# ==============================================================================
# ATOMIC & RESUMABLE MULTI-THREADED ASSET DOWNLOADER
# ==============================================================================

class AssetDownloader:
    """Downloads files with streaming, multi-threading, Range-resuming, data-limit caps, and atomic .part renaming."""

    def __init__(self, base_url, download_folder, session, verify_ssl=True, max_workers=8, max_mb=None):
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

        # 1. Skip if already completed (and not a fake captive-portal block)
        if os.path.exists(local_path):
            size = os.path.getsize(local_path)
            if size == 715 or size == 0:
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            else:
                return True, 0, "cached"

        if self.max_bytes:
            with self._lock:
                if self.total_downloaded_bytes >= self.max_bytes:
                    self.stop_requested = True
                    return False, 0, "limit_reached"

        # 2. Use .part file for atomic download and safe resuming
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

                # Guard against captive portal intercepts
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

                    # Verify stream truncation
                    if expected_total > 0 and os.path.exists(part_path) and os.path.getsize(part_path) < expected_total:
                        raise IOError(f"Truncated stream ({os.path.getsize(part_path)}/{expected_total})")

                    os.replace(part_path, local_path)
                    return True, downloaded_bytes, "resumed"

                elif r.status_code == 200:
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

                    # Verify stream truncation
                    if expected_total > 0 and os.path.exists(part_path) and os.path.getsize(part_path) < expected_total:
                        raise IOError(f"Truncated stream ({os.path.getsize(part_path)}/{expected_total})")

                    os.replace(part_path, local_path)
                    return True, downloaded_bytes, "downloaded"

                elif r.status_code == 416:
                    if os.path.exists(part_path) and os.path.getsize(part_path) > 715:
                        os.replace(part_path, local_path)
                        return True, 0, "completed"
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

            except Exception:
                time.sleep(1)

        return False, 0, "failed"

    def download_batch(self, asset_map):
        success_count = 0
        failed_count = 0
        cached_count = 0
        resumed_count = 0
        total = len(asset_map)
        limit_hit = False

        limit_info = f" (Max download limit: {self.max_mb:.1f} MB)" if self.max_mb else ""
        print(f"[*] Starting download of {total} assets with {self.max_workers} worker threads{limit_info}...")

        items_iter = iter(asset_map.items())
        active_futures = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Prime the pool with initial tasks
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
                desc="Downloading Game Assets",
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
                                if status == "cached":
                                    cached_count += 1
                                elif status == "resumed":
                                    resumed_count += 1
                            else:
                                failed_count += 1
                        except Exception:
                            failed_count += 1

                        mb_down = self.total_downloaded_bytes / (1024 * 1024)
                        if self.max_mb:
                            status_text = f"{mb_down:.1f}/{self.max_mb:.0f} MB"
                        else:
                            status_text = f"{mb_down:.1f} MB"

                        if cached_count:
                            status_text += f", {cached_count} cached"
                        if resumed_count:
                            status_text += f", {resumed_count} resumed"
                        if failed_count:
                            status_text += f", {failed_count} skipped"
                        pbar.set_postfix_str(status_text)
                        pbar.update(1)

                        # Submit next task if limit has not been hit
                        if not self.stop_requested:
                            try:
                                nxt_rel, nxt_url = next(items_iter)
                                nxt_fut = executor.submit(self.download_file, nxt_url, os.path.join(self.download_folder, nxt_rel))
                                active_futures[nxt_fut] = nxt_rel
                            except StopIteration:
                                pass

        mb_down = self.total_downloaded_bytes / (1024 * 1024)
        print(f"[+] Download session finished: {success_count} ready ({cached_count} already present, {resumed_count} resumed), {failed_count} not found. ({mb_down:.2f} MB transferred)")

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
    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        rel_path = unquote(parsed.path.replace('+', '%2B')).lstrip('/')
        if not rel_path:
            rel_path = 'index.html'

        local_path = os.path.join(os.getcwd(), rel_path)
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
                rel_path = unquote(parsed.path.replace('+', '%2B')).lstrip('/')

                if not rel_path or rel_path == "index.html":
                    return self.serve_local_file(os.path.join(target_folder, "index.html"))

                local_path = os.path.join(target_folder, rel_path)

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
# MAIN GAME GRABBER ORCHESTRATOR
# ==============================================================================

def grab_game(url, output_folder=None, headed=False, no_verify=False, wait_idle=8, serve=False, play_now=False, max_workers=8, proxy=None, no_videos=False, stream_only=False, limit_mb=None):
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
    os.makedirs(base_download_dir, exist_ok=True)
    if output_folder:
        target_folder = output_folder if (os.path.isabs(output_folder) or output_folder.startswith(("downloads/", "downloads\\"))) else os.path.join(base_download_dir, output_folder)
    else:
        target_folder = os.path.join(base_download_dir, default_folder)

    meta_path = os.path.join(target_folder, "game_meta.json")

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
                new_folder = os.path.join(base_download_dir, f"{clean_title}-{detected_version}")
                print(f"[*] Resuming in separate folder '{new_folder}' to prevent data corruption!")
                print("=" * 75 + "\n")
                target_folder = new_folder
                meta_path = os.path.join(target_folder, "game_meta.json")
            else:
                print(f"[+] Found existing '{prev_version}' download in '{target_folder}'. Resuming missing assets...")
        except Exception:
            pass

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
        print("[*] Running generic deep scan for HTML5 assets...")
        media_regex = re.compile(r'([a-zA-Z0-9_\-./]+\.(?:' + '|'.join(ALL_EXTENSIONS) + r'))', re.I)
        for m in media_regex.finditer(page_html):
            c = clean_relative_path(m.group(1))
            if c:
                discovered_assets.add(c)

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
        if r_orig.status_code == 200 and len(r_orig.text) > 1000:
            pristine_html = r_orig.text
    except Exception:
        pass

    final_html = pristine_html if pristine_html else page_html
    for ext_url, local_rel in external_assets.items():
        final_html = final_html.replace(ext_url, local_rel)

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
    with open(py_runner, "w") as f:
        f.write('''import os
import mimetypes
import http.server
import socketserver
import webbrowser
from urllib.parse import urlparse, unquote

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        rel_path = unquote(parsed.path.replace('+', '%2B')).lstrip('/')
        if not rel_path:
            rel_path = 'index.html'

        local_path = os.path.join(os.getcwd(), rel_path)
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

import sys
import argparse

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

parser = argparse.ArgumentParser(description="Run local game server with HTTP Range support.")
parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
args = parser.parse_args()

port = args.port
try:
    httpd = ThreadingTCPServer(("", port), RangeRequestHandler)
except OSError:
    for p in range(8081, 8100):
        try:
            port = p
            httpd = ThreadingTCPServer(("", port), RangeRequestHandler)
            break
        except OSError:
            continue

print(f"Starting local game server at http://localhost:{port}/index.html ...")
if not args.no_browser:
    webbrowser.open(f"http://localhost:{port}/index.html")

try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print("\\nShutting down server...")
''')

    print("[+] Generated 'run_game.bat' and 'run_game.py' for instant local offline play.")

    # 9. Build Download Queue (Preserving Story Order and Filtering if requested)
    asset_map = {}
    video_exts = ('.mp4', '.webm', '.avi', '.mov', '.mkv', '.m4v')
    for rel_path in discovered_assets:
        if no_videos and rel_path.lower().endswith(video_exts):
            continue
        safe_rel = quote(unquote(rel_path.replace('+', '%2B')), safe="/()'!~,-_.@")
        full_url = urljoin(game_url, safe_rel)
        asset_map[rel_path] = full_url

    for ext_url, local_rel in external_assets.items():
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
        max_mb=limit_mb
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

        print("=" * 75)
        print(" [✓] 100% of prioritized game assets have been downloaded and cached locally! ")
        print(" [✓] The game is now fully offline-playable anytime via run_game.bat. ")
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

  # Resume an interrupted download (automatically purges corrupt files and skips cached ones):
  python scraper.py "https://game-portal.com/games/space-adventure"

  # Conserve bandwidth: download text & images only, skipping heavy videos:
  python scraper.py "https://game-portal.com/games/space-adventure" --no-videos

  # Stop after 250 MB downloaded (safe for limited data plans, resumable anytime):
  python scraper.py "https://game-portal.com/games/space-adventure" --limit 250

  # Scrape with custom threads and proxy:
  python scraper.py "https://game-host.com/game/index.html" --threads 12 --proxy socks5://127.0.0.1:1080
================================================================================
"""
    )

    parser.add_argument("url", help="Target URL (Game portal page URL or direct game iframe/CDN URL)")
    parser.add_argument("-o", "--folder", "--out", dest="folder", type=str, metavar="NAME", help="Custom output directory name (default: auto-named 'downloads/<Game_Name>-<Version>')")
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

    # If stream_only is selected, automatically activate play_now server
    play_now = args.play_now or args.stream_only

    grab_game(
        url=args.url,
        output_folder=args.folder,
        headed=args.headed,
        no_verify=args.no_verify,
        wait_idle=args.wait,
        serve=args.serve,
        play_now=play_now,
        max_workers=args.threads,
        proxy=args.proxy,
        no_videos=args.no_videos,
        stream_only=args.stream_only,
        limit_mb=args.limit
    )

if __name__ == "__main__":
    main()
