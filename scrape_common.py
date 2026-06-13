"""
WarRoom — shared scraping infrastructure for the multi-source ingestion
pipeline (proposal §5 / §7).

Two fetch layers, both with on-disk HTML caching so the pipeline is cheap to
re-run and never scrapes live during gameplay:

  fetch_static(url)   — plain HTTP via requests. For static-HTML sources
                        (WalterFootball, WordPress sites like PFN). Falls back
                        to an unverified TLS connection if this machine's CA
                        chain is unusable (the data is public HTML).

  render(url)         — full browser render via undetected-chromedriver. For
                        JavaScript single-page-app sources whose content isn't
                        in the static HTML (NFL.com) and for Cloudflare-gated
                        sites. Same technique the PFR builder uses.

A single headless-ish Chrome is launched lazily and reused across calls; call
close_driver() when a batch run finishes.
"""

import os
import re
import ssl
import time

import requests

# This machine has a broken TLS CA chain. undetected-chromedriver downloads its
# matching chromedriver over HTTPS via urllib, which fails the handshake, so we
# relax verification for that path (public binaries; same rationale as the
# requests verify=False fallback below).
ssl._create_default_https_context = ssl._create_unverified_context

CACHE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "scrape_cache")
STATIC_DELAY = 2.0   # politeness delay between live static fetches
RENDER_DELAY = 4.0   # politeness delay between live browser renders
RENDER_WAIT = 6.0    # seconds to let a page settle before reading. Kept at 6:
                     # dropping it to 3 triggered Cloudflare rate-limiting, which
                     # made the browser return stale pages (mass mis-assignment).
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WarRoomBot/0.2"}

# Detected once: local Chrome major version, for undetected-chromedriver.
CHROME_MAIN = 149

_static_verify = True   # flips to False once on the first SSLError
_driver = None


def _cache_path(url: str, sub: str) -> str:
    d = os.path.join(CACHE_ROOT, sub)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, re.sub(r"[^\w]", "_", url)[:180] + ".html")


def cache_html(url: str, html: str, sub: str, min_len: int = 2000) -> None:
    """Persist already-fetched HTML to the page cache. Used when a page came from
    render_get() (which doesn't cache) so a later cache-only re-parse can find it."""
    if html and len(html) >= min_len:
        with open(_cache_path(url, sub), "w", encoding="utf-8") as f:
            f.write(html)


def _read_cache(path: str, min_len: int) -> str | None:
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            html = f.read()
        if len(html) > min_len and "Just a moment" not in html[:600]:
            return html
    return None


def fetch_static(url: str, *, sub: str = "static", min_len: int = 2000,
                 force: bool = False) -> str | None:
    """Fetch static HTML with caching. Returns None on a bad/short response."""
    path = _cache_path(url, sub)
    if not force and (cached := _read_cache(path, min_len)) is not None:
        return cached

    global _static_verify
    time.sleep(STATIC_DELAY)
    try:
        resp = requests.get(url, headers=_UA, timeout=30, verify=_static_verify)
    except requests.exceptions.SSLError:
        import urllib3
        urllib3.disable_warnings()
        _static_verify = False
        resp = requests.get(url, headers=_UA, timeout=30, verify=False)

    if resp.status_code != 200 or len(resp.text) < min_len:
        print(f"    [warn] {url} -> HTTP {resp.status_code}, {len(resp.text)} bytes")
        return None
    with open(path, "w", encoding="utf-8") as f:
        f.write(resp.text)
    return resp.text


def _get_driver():
    global _driver
    if _driver is None:
        import undetected_chromedriver as uc
        opts = uc.ChromeOptions()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,1000")
        _driver = uc.Chrome(options=opts, version_main=CHROME_MAIN)
    return _driver


def render(url: str, *, sub: str = "render", min_len: int = 5000,
           wait: float = RENDER_WAIT, scroll: int = 0, force: bool = False) -> str | None:
    """Fetch a JS-rendered page through a real browser, with caching.

    scroll: number of scroll-to-bottom passes to trigger lazy-loaded content
    (e.g. an infinite-scroll prospect list). 0 = no scrolling."""
    path = _cache_path(url, sub)
    if not force and (cached := _read_cache(path, min_len)) is not None:
        return cached

    time.sleep(RENDER_DELAY)
    try:
        driver = _get_driver()
        driver.get(url)
        time.sleep(wait)
        if "Just a moment" in driver.page_source[:600]:   # Cloudflare interstitial
            time.sleep(10)
        for _ in range(scroll):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.0)
        html = driver.page_source
    except Exception as e:
        print(f"    [render-fail] {url} -> {e}")
        return None

    if not html or len(html) < min_len:
        print(f"    [warn] rendered {url} too small ({len(html or '')} bytes)")
        return None
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def render_get(url: str, *, wait: float = RENDER_WAIT, scroll: int = 0) -> tuple[str | None, str | None]:
    """Render a URL and return (final_url, html) — the final_url reflects any
    redirect the browser followed (e.g. a search endpoint that 302s straight to
    the unique result). Not cached here; callers that need the bytes again
    should re-render the resolved URL via render(). Used for Cloudflare-gated
    sites whose search/redirect target we must observe (Sports-Reference)."""
    time.sleep(RENDER_DELAY)
    try:
        driver = _get_driver()
        driver.get(url)
        time.sleep(wait)
        if "Just a moment" in driver.page_source[:600]:
            time.sleep(10)
        for _ in range(scroll):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.0)
        return driver.current_url, driver.page_source
    except Exception as e:
        print(f"    [render-fail] {url} -> {e}")
        return None, None


def close_driver() -> None:
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


def redact(report: str, name: str, school: str) -> str:
    """Strip identifying tokens so a report can be shown blind: removes the
    player's name parts and school (proposal §4). Shared by every source
    adapter. Not bulletproof (nicknames/paraphrases) but removes the obvious
    giveaways from the draft prompt."""
    if not report:
        return ""
    tokens = [t for t in re.split(r"\s+", name or "") if len(t) > 2]
    if school:
        tokens.append(school)
        tokens += [t for t in re.split(r"\s+", school) if len(t) > 2]
    blind = report
    for t in sorted(set(tokens), key=len, reverse=True):
        blind = re.sub(re.escape(t), "____", blind, flags=re.IGNORECASE)
    return blind
