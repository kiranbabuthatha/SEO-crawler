#!/usr/bin/env python3
"""
seo_crawler.py — A configurable SEO audit crawler.

Crawls a website (same-domain by default), extracts SEO-relevant signals from
each page, and writes the results to CSV + JSON plus a console summary.

Key features:
  - Selectable User-Agent (Googlebot desktop/mobile, Bingbot, Chrome
    desktop/mobile, or a custom string) so you can audit how your site
    responds to different crawlers.
  - Concurrent crawling with per-domain rate limiting and automatic retry,
    so it runs fast without hammering (or getting blocked by) a server.
  - Seed from a URL list file and/or auto-discovered XML sitemaps
    (handles sitemap-index files and gzipped sitemaps).

Dependencies:
    pip install -r requirements.txt   # requests, beautifulsoup4, lxml

Usage examples:
    python seo_crawler.py https://example.com
    python seo_crawler.py https://example.com --ua googlebot-mobile --max-pages 100
    python seo_crawler.py https://example.com --from-sitemap --list-only --max-pages 0
    python seo_crawler.py --urls-file urls.txt --list-only --workers 8
    python seo_crawler.py https://example.com --ua custom --ua-string "MyBot/1.0"

Author:
    Kiran Babu Thatha — technical SEO + automation.
    Reach me at https://www.kiranbabuthatha.com to create custom
    extracts/crawls for SEO analysis.

License:
    MIT — see LICENSE.
"""

import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
import random
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependencies. Run: pip install requests beautifulsoup4 lxml")


def _importable(module_name):
    """True if a module can be imported (used to detect optional decoders)."""
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- #
# User-Agent presets
# --------------------------------------------------------------------------- #
USER_AGENTS = {
    "googlebot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "Googlebot/2.1; +http://www.google.com/bot.html) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "googlebot-mobile": (
        "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile "
        "Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "bingbot": (
        "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
    ),
    "chrome-desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "chrome-mobile": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "custom": None,  # filled from --ua-string
}


# --------------------------------------------------------------------------- #
# Per-page data model
# --------------------------------------------------------------------------- #
@dataclass
class PageReport:
    url: str
    final_url: str = ""
    status_code: int = 0
    parse_ok: bool = True
    redirect_chain: list = field(default_factory=list)
    response_time_ms: int = 0
    content_type: str = ""
    indexable: bool = True
    indexability_reason: str = ""

    # Head / meta
    title: str = ""
    title_length: int = 0
    meta_description: str = ""
    meta_description_length: int = 0
    meta_robots: str = ""
    x_robots_tag: str = ""
    canonical: str = ""
    canonical_is_self: bool = False
    lang: str = ""
    viewport: str = ""
    charset: str = ""

    # Headings & content
    h1: list = field(default_factory=list)
    h1_count: int = 0
    h2_count: int = 0
    word_count: int = 0

    # Links
    internal_links: int = 0
    external_links: int = 0
    nofollow_links: int = 0

    # Images
    images_total: int = 0
    images_missing_alt: int = 0

    # International
    hreflang: list = field(default_factory=list)

    # Social
    og_tags: dict = field(default_factory=dict)
    twitter_tags: dict = field(default_factory=dict)

    # Structured data
    schema_types: list = field(default_factory=list)

    # Security / performance headers
    is_https: bool = False
    hsts: bool = False
    content_encoding: str = ""
    cache_control: str = ""
    content_length: int = 0

    # Issues found on this page
    issues: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
class SEOCrawler:
    SKIP_EXT = re.compile(
        r"\.(pdf|jpe?g|png|gif|webp|svg|ico|css|js|zip|gz|mp4|mp3|woff2?|ttf|"
        r"eot|xml|json|csv|doc|docx|xls|xlsx|ppt|pptx)$",
        re.IGNORECASE,
    )

    def __init__(self, start_url, user_agent, max_pages=50, max_depth=3,
                 delay=0.3, timeout=15, respect_robots=True, same_domain=True,
                 workers=1, seed_urls=None, list_only=False, retries=3):
        self.start_url = start_url.rstrip("/")
        self.user_agent = user_agent
        self.max_pages = max_pages if max_pages and max_pages > 0 else float("inf")
        self.max_depth = max_depth
        self.delay = delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.same_domain = same_domain
        self.workers = max(1, workers)
        self.seed_urls = seed_urls or []
        self.list_only = list_only

        self.base_domain = urlparse(self.start_url).netloc
        self.session = self._build_session(retries)

        self.visited = set()
        self.reports = []
        self.robots = None
        self.sitemaps = []
        self.crawl_delay = None  # from robots.txt Crawl-delay, if present

        # thread-safety + politeness
        self._lock = threading.Lock()          # guards visited / reports
        self._domain_locks = {}                # one lock per host
        self._domain_next_ok = {}              # earliest next-request time per host
        self._throttle_lock = threading.Lock() # guards the two dicts above

    @staticmethod
    def _supported_encodings():
        """Only advertise compression we can actually decode.

        requests/urllib3 transparently handle gzip & deflate. Brotli (br) and
        zstd need extra packages; advertising them without the decoder yields
        undecodable bytes -> garbage HTML -> false 'missing tag' reports.
        """
        encodings = ["gzip", "deflate"]
        if any(_importable(m) for m in ("brotli", "brotlicffi")):
            encodings.append("br")
        if _importable("zstandard"):
            encodings.append("zstd")
        return ", ".join(encodings)

    def _build_session(self, retries):
        """Session with connection pooling + automatic retry/backoff."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.user_agent,
            # look less like a bare scraper; keep connections alive
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": self._supported_encodings(),
            "Connection": "keep-alive",
        })
        retry = Retry(
            total=retries,
            backoff_factor=1.0,                 # 1s, 2s, 4s between retries
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            respect_retry_after_header=True,    # honor server's Retry-After
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=self.workers * 2,
            pool_maxsize=self.workers * 2,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _polite_wait(self, url):
        """Per-domain spacing so concurrent workers never hammer one host."""
        host = urlparse(url).netloc
        gap = max(self.delay, self.crawl_delay or 0)
        # add jitter (±30%) so requests don't arrive in a robotic lockstep
        gap *= random.uniform(0.85, 1.3)
        with self._throttle_lock:
            lock = self._domain_locks.setdefault(host, threading.Lock())
        with lock:
            now = time.monotonic()
            with self._throttle_lock:
                next_ok = self._domain_next_ok.get(host, 0)
            wait = next_ok - now
            if wait > 0:
                time.sleep(wait)
            with self._throttle_lock:
                self._domain_next_ok[host] = time.monotonic() + gap

    # ----------------------------- helpers --------------------------------- #
    def _same_site(self, url):
        return urlparse(url).netloc == self.base_domain

    def _normalize(self, url):
        url, _ = urldefrag(url)  # drop #fragment
        return url.rstrip("/") if url != self.start_url else url

    def _allowed_by_robots(self, url):
        if not self.respect_robots or self.robots is None:
            return True
        return self.robots.can_fetch(self.user_agent, url)

    # ------------------------- robots & sitemap ---------------------------- #
    def load_robots(self):
        if getattr(self, "_robots_loaded", False):
            return
        self._robots_loaded = True
        robots_url = urljoin(self.start_url, "/robots.txt")
        rp = RobotFileParser()
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                self.robots = rp
                # collect sitemap references
                for line in resp.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        self.sitemaps.append(line.split(":", 1)[1].strip())
                print(f"[robots] Loaded /robots.txt ({len(self.sitemaps)} sitemap refs)")
                # honor Crawl-delay if the host specifies one for our UA
                try:
                    cd = rp.crawl_delay(self.user_agent)
                    if cd:
                        self.crawl_delay = float(cd)
                        print(f"[robots] Crawl-delay honored: {cd}s")
                except Exception:
                    pass
            else:
                print(f"[robots] /robots.txt returned {resp.status_code} — crawling all")
        except requests.RequestException as e:
            print(f"[robots] Could not fetch robots.txt: {e}")
        if not self.sitemaps:
            # guess the conventional location
            self.sitemaps.append(urljoin(self.start_url, "/sitemap.xml"))

    # --------------------------- sitemap parsing --------------------------- #
    def _fetch_sitemap_xml(self, url):
        """Fetch one sitemap URL and return its decoded XML text (handles .gz)."""
        self._polite_wait(url)
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as e:
            print(f"[sitemap] Could not fetch {url}: {e}")
            return None
        if resp.status_code != 200:
            print(f"[sitemap] {url} returned {resp.status_code}")
            return None

        content = resp.content
        # Decompress if it's gzipped — either by extension or by magic bytes,
        # since some servers don't set Content-Encoding for .xml.gz files.
        is_gz = url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b"
        if is_gz:
            try:
                content = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
            except OSError:
                pass  # not actually gzipped; use as-is
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return None

    def collect_sitemap_urls(self, max_urls=None, max_sitemaps=200):
        """Walk sitemaps (and nested sitemap indexes) and return page URLs.

        Handles both <sitemapindex> (links to more sitemaps) and <urlset>
        (actual page URLs), gzip-compressed sitemaps, and avoids loops.
        """
        if not self.sitemaps:
            self.load_robots()

        to_process = deque(self.sitemaps)
        seen_sitemaps = set()
        page_urls = []
        seen_pages = set()
        processed = 0

        # Loc extractor that works regardless of XML namespace prefixes.
        loc_re = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)

        while to_process and processed < max_sitemaps:
            sm_url = to_process.popleft().strip()
            if sm_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sm_url)
            processed += 1

            xml = self._fetch_sitemap_xml(sm_url)
            if not xml:
                continue

            is_index = "<sitemapindex" in xml.lower()
            locs = loc_re.findall(xml)
            # XML-unescape the handful of entities that appear in URLs
            locs = [
                loc.replace("&amp;", "&").replace("&lt;", "<")
                   .replace("&gt;", ">").replace("&#39;", "'")
                   .replace("&quot;", '"')
                for loc in locs
            ]

            if is_index:
                print(f"[sitemap] Index {sm_url} -> {len(locs)} child sitemap(s)")
                for loc in locs:
                    if loc not in seen_sitemaps:
                        to_process.append(loc)
            else:
                added = 0
                for loc in locs:
                    if not loc.startswith(("http://", "https://")):
                        continue
                    if self.same_domain and not self._same_site(loc):
                        continue
                    nu = self._normalize(loc)
                    if nu in seen_pages:
                        continue
                    seen_pages.add(nu)
                    page_urls.append(nu)
                    added += 1
                    if max_urls and len(page_urls) >= max_urls:
                        print(f"[sitemap] Reached max-urls cap ({max_urls})")
                        return page_urls
                print(f"[sitemap] {sm_url} -> {added} page URL(s)")

        print(f"[sitemap] Collected {len(page_urls)} URL(s) "
              f"from {processed} sitemap file(s)")
        return page_urls

    # ----------------------------- fetch ----------------------------------- #
    def fetch(self, url):
        report = PageReport(url=url)
        self._polite_wait(url)
        try:
            start = time.perf_counter()
            resp = self.session.get(
                url, timeout=self.timeout, allow_redirects=True
            )
            report.response_time_ms = int((time.perf_counter() - start) * 1000)
        except requests.RequestException as e:
            report.status_code = -1
            report.indexable = False
            report.indexability_reason = f"request failed: {e}"
            report.issues.append(f"Request failed: {e}")
            return report, None

        report.status_code = resp.status_code
        report.final_url = resp.url
        report.redirect_chain = [r.status_code for r in resp.history]
        report.content_type = resp.headers.get("Content-Type", "")
        report.content_encoding = resp.headers.get("Content-Encoding", "")
        report.cache_control = resp.headers.get("Cache-Control", "")
        report.x_robots_tag = resp.headers.get("X-Robots-Tag", "")
        report.hsts = "Strict-Transport-Security" in resp.headers
        report.is_https = resp.url.startswith("https://")
        try:
            report.content_length = int(resp.headers.get("Content-Length", 0))
        except ValueError:
            report.content_length = len(resp.content)

        if report.redirect_chain:
            report.issues.append(
                f"Redirect chain: {report.redirect_chain} -> {resp.url}"
            )
        if resp.status_code >= 400:
            report.indexable = False
            report.indexability_reason = f"HTTP {resp.status_code}"
            report.issues.append(f"Error status: {resp.status_code}")
            return report, None
        if "html" not in report.content_type.lower():
            report.indexable = False
            report.indexability_reason = "non-HTML content"
            return report, None

        return report, resp

    # --------------------------- parse page -------------------------------- #
    def parse(self, report, resp):
        soup = BeautifulSoup(resp.text, "lxml")

        # --- sanity guard: did we actually get parseable HTML? ---
        # A 200 response with no <head>, <title>, AND no <meta> almost always
        # means the bytes weren't decoded (e.g. server sent br/zstd we can't
        # decompress) or the page is a JS-only shell. (We don't test for <body>:
        # lxml synthesizes one around any stray text, even garbage.) Either way
        # the per-tag "missing" checks below would produce a wall of false
        # positives, so we flag the real problem once and skip them.
        has_structure = bool(
            soup.find("head") or soup.find("title") or soup.find("meta")
        )
        enc = resp.headers.get("Content-Encoding", "").lower()
        if not has_structure:
            report.parse_ok = False
            report.indexable = False
            if enc and enc not in ("gzip", "deflate", "identity", ""):
                report.indexability_reason = f"undecodable content ({enc})"
                report.issues.append(
                    f"Response sent as '{enc}' but no decoder installed — "
                    f"content unreadable. Install it (e.g. pip install brotli) "
                    f"or the crawler advertises only gzip/deflate."
                )
            else:
                report.indexability_reason = "empty/JS-rendered HTML"
                report.issues.append(
                    "No HTML structure found (likely JS-rendered; needs a "
                    "headless browser to audit)."
                )
            return []  # nothing more to extract; no links to follow

        # --- title ---
        if soup.title and soup.title.string:
            report.title = soup.title.string.strip()
            report.title_length = len(report.title)
        if not report.title:
            report.issues.append("Missing <title>")
        elif report.title_length > 60:
            report.issues.append(f"Title too long ({report.title_length} chars)")
        elif report.title_length < 10:
            report.issues.append(f"Title very short ({report.title_length} chars)")

        # --- meta description ---
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            report.meta_description = md["content"].strip()
            report.meta_description_length = len(report.meta_description)
        if not report.meta_description:
            report.issues.append("Missing meta description")
        elif report.meta_description_length > 160:
            report.issues.append(
                f"Meta description too long ({report.meta_description_length} chars)"
            )

        # --- meta robots ---
        mr = soup.find("meta", attrs={"name": re.compile("robots", re.I)})
        if mr and mr.get("content"):
            report.meta_robots = mr["content"].strip()

        # indexability via robots directives
        directives = f"{report.meta_robots} {report.x_robots_tag}".lower()
        if "noindex" in directives:
            report.indexable = False
            report.indexability_reason = "noindex directive"
            report.issues.append("Page is noindex")

        # --- canonical ---
        canon = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
        if canon and canon.get("href"):
            report.canonical = urljoin(report.final_url, canon["href"])
            report.canonical_is_self = (
                self._normalize(report.canonical)
                == self._normalize(report.final_url)
            )
            if not report.canonical_is_self:
                report.issues.append(
                    f"Canonical points elsewhere: {report.canonical}"
                )
        else:
            report.issues.append("Missing canonical tag")

        # conflicting directives
        if not report.indexable and report.canonical and report.canonical_is_self:
            report.issues.append("Conflict: noindex + self-canonical")

        # --- lang / viewport / charset ---
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            report.lang = html_tag["lang"]
        else:
            report.issues.append("Missing lang attribute on <html>")

        vp = soup.find("meta", attrs={"name": "viewport"})
        if vp and vp.get("content"):
            report.viewport = vp["content"]
        else:
            report.issues.append("Missing viewport meta (mobile)")

        cs = soup.find("meta", attrs={"charset": True})
        if cs:
            report.charset = cs.get("charset", "")

        # --- headings ---
        h1s = [h.get_text(strip=True) for h in soup.find_all("h1")]
        report.h1 = h1s
        report.h1_count = len(h1s)
        report.h2_count = len(soup.find_all("h2"))
        if report.h1_count == 0:
            report.issues.append("No H1 found")
        elif report.h1_count > 1:
            report.issues.append(f"Multiple H1s ({report.h1_count})")

        # --- structured data (JSON-LD) ---
        # Extract BEFORE the word-count step below decomposes <script> tags.
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "{}")
                report.schema_types.extend(self._extract_schema_types(data))
            except (json.JSONDecodeError, TypeError):
                report.issues.append("Invalid JSON-LD structured data")

        # --- word count (visible text) ---
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        report.word_count = len(text.split())
        if report.word_count < 200:
            report.issues.append(f"Thin content ({report.word_count} words)")

        # --- hreflang ---
        for link in soup.find_all("link", attrs={"rel": re.compile("alternate", re.I)}):
            if link.get("hreflang"):
                report.hreflang.append({
                    "lang": link["hreflang"],
                    "href": urljoin(report.final_url, link.get("href", "")),
                })

        # --- Open Graph / Twitter ---
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "")
            name = meta.get("name", "")
            if prop.startswith("og:") and meta.get("content"):
                report.og_tags[prop] = meta["content"]
            if name.startswith("twitter:") and meta.get("content"):
                report.twitter_tags[name] = meta["content"]

        # --- images ---
        imgs = soup.find_all("img")
        report.images_total = len(imgs)
        report.images_missing_alt = sum(
            1 for img in imgs if not img.get("alt", "").strip()
        )
        if report.images_missing_alt:
            report.issues.append(
                f"{report.images_missing_alt} image(s) missing alt text"
            )

        # --- links + collect for crawl frontier ---
        new_links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(report.final_url, a["href"])
            href = self._normalize(href)
            rel = " ".join(a.get("rel", [])).lower()
            if "nofollow" in rel:
                report.nofollow_links += 1
            if self._same_site(href):
                report.internal_links += 1
                if href.startswith("http"):
                    new_links.append(href)
            elif href.startswith("http"):
                report.external_links += 1

        return new_links

    @staticmethod
    def _extract_schema_types(data):
        types = []
        if isinstance(data, dict):
            if "@type" in data:
                t = data["@type"]
                types.extend(t if isinstance(t, list) else [t])
            for v in data.values():
                types.extend(SEOCrawler._extract_schema_types(v))
        elif isinstance(data, list):
            for item in data:
                types.extend(SEOCrawler._extract_schema_types(item))
        return types

    # ----------------------------- crawl ----------------------------------- #
    def _eligible(self, url, depth):
        """Filter a candidate URL; returns True if it should be fetched."""
        if url in self.visited or depth > self.max_depth:
            return False
        if self.SKIP_EXT.search(urlparse(url).path):
            return False
        if self.same_domain and not self._same_site(url):
            return False
        if not self._allowed_by_robots(url):
            print(f"[skip] Blocked by robots.txt: {url}")
            return False
        return True

    def _process(self, url, depth):
        """Fetch + parse one URL. Runs inside a worker thread."""
        report, resp = self.fetch(url)
        new_links = []
        if resp is not None:
            new_links = self.parse(report, resp) or []
        return report, new_links, depth

    def crawl(self):
        self.load_robots()

        # ---- list-only mode: crawl exactly the provided URLs, no link-following ----
        if self.list_only:
            targets = [self._normalize(u) for u in self.seed_urls]
            print(f"[mode] List-only: {len(targets)} URL(s), no link discovery\n")
            self._run_wave([(u, 0) for u in targets], follow_links=False)
            return self.reports

        # ---- normal crawl: seed from start_url + any provided seed URLs ----
        seeds = [self.start_url] + [self._normalize(u) for u in self.seed_urls]
        # de-dupe while preserving order
        seen = set()
        frontier = []
        for u in seeds:
            nu = self._normalize(u)
            if nu not in seen:
                seen.add(nu)
                frontier.append((nu, 0))

        depth = 0
        while frontier and len(self.reports) < self.max_pages:
            # process this depth level concurrently, gather next level's links
            next_frontier = self._run_wave(frontier, follow_links=True)
            depth += 1
            if depth > self.max_depth:
                break
            # dedupe next wave
            batch, seen_b = [], set()
            for link in next_frontier:
                nl = self._normalize(link)
                if nl not in seen_b and nl not in self.visited:
                    seen_b.add(nl)
                    batch.append((nl, depth))
            frontier = batch

        return self.reports

    def _run_wave(self, items, follow_links):
        """Fetch a batch of (url, depth) tuples concurrently. Returns new links."""
        # pre-filter and reserve slots under lock to respect max_pages & dedupe
        to_fetch = []
        with self._lock:
            for url, depth in items:
                if len(self.reports) + len(to_fetch) >= self.max_pages:
                    break
                if self.list_only:
                    if url in self.visited:
                        continue
                    self.visited.add(url)
                    to_fetch.append((url, depth))
                elif self._eligible(url, depth):
                    self.visited.add(url)
                    to_fetch.append((url, depth))

        discovered = []
        if not to_fetch:
            return discovered

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._process, url, depth): url
                for url, depth in to_fetch
            }
            for fut in as_completed(futures):
                report, new_links, depth = fut.result()
                with self._lock:
                    if len(self.reports) >= self.max_pages:
                        continue
                    self.reports.append(report)
                    n = len(self.reports)
                status = report.status_code
                cap = self.max_pages if self.max_pages != float("inf") else "∞"
                print(f"[{n}/{cap}] (d{depth}) {status} {report.url}")
                if follow_links:
                    discovered.extend(new_links)
        return discovered


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(reports, prefix):
    # JSON (full detail)
    json_path = f"{prefix}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in reports], f, indent=2, ensure_ascii=False)

    # CSV (flat summary)
    csv_path = f"{prefix}.csv"
    fields = [
        "url", "final_url", "status_code", "parse_ok", "response_time_ms", "indexable",
        "indexability_reason", "title", "title_length", "meta_description_length",
        "h1_count", "h2_count", "word_count", "canonical_is_self",
        "internal_links", "external_links", "images_total", "images_missing_alt",
        "hreflang_count", "schema_types", "is_https", "hsts", "issue_count",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in reports:
            row = asdict(r)
            row["hreflang_count"] = len(r.hreflang)
            row["schema_types"] = "|".join(sorted(set(r.schema_types)))
            row["issue_count"] = len(r.issues)
            writer.writerow(row)

    return json_path, csv_path


def print_summary(reports, user_agent_label):
    print("\n" + "=" * 70)
    print("SEO CRAWL SUMMARY")
    print("=" * 70)
    print(f"User-Agent profile : {user_agent_label}")
    print(f"Pages crawled      : {len(reports)}")

    statuses = {}
    indexable = 0
    issue_tally = {}
    for r in reports:
        statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
        if r.indexable:
            indexable += 1
        for issue in r.issues:
            key = re.sub(r"\d+", "N", issue).split(":")[0].strip()
            issue_tally[key] = issue_tally.get(key, 0) + 1

    print(f"Indexable pages    : {indexable}/{len(reports)}")
    print(f"Status codes       : {dict(sorted(statuses.items()))}")

    if issue_tally:
        print("\nTop issues across the site:")
        for issue, count in sorted(issue_tally.items(), key=lambda x: -x[1])[:12]:
            print(f"  {count:>3}x  {issue}")
    else:
        print("\nNo issues detected. ")
    print("=" * 70)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Configurable SEO audit crawler with selectable User-Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", default=None,
                   help="Start URL, e.g. https://example.com "
                        "(optional if --urls-file is given)")
    p.add_argument(
        "--ua", default="googlebot",
        choices=list(USER_AGENTS.keys()),
        help="User-Agent profile to crawl as (default: googlebot)",
    )
    p.add_argument(
        "--ua-string", default=None,
        help="Custom User-Agent string (required when --ua custom)",
    )
    p.add_argument("--urls-file", default=None,
                   help="Path to a text file with one URL per line "
                        "(used as seeds, or as the exact list with --list-only)")
    p.add_argument("--from-sitemap", nargs="?", const="auto", default=None,
                   metavar="SITEMAP_URL",
                   help="Seed the crawl from the site's sitemap(s). Give a "
                        "sitemap URL, or pass the flag alone to auto-discover "
                        "via robots.txt / sitemap.xml. Combine with --list-only "
                        "to audit exactly the sitemap URLs.")
    p.add_argument("--max-sitemap-urls", type=int, default=0,
                   help="Cap URLs pulled from sitemaps; 0 = no cap (default: 0)")
    p.add_argument("--list-only", action="store_true",
                   help="Crawl only the URLs given (no link discovery/recursion)")
    p.add_argument("--workers", type=int, default=5,
                   help="Concurrent fetch workers (default: 5)")
    p.add_argument("--max-pages", type=int, default=50,
                   help="Max pages to crawl; use 0 for unlimited (default: 50)")
    p.add_argument("--depth", type=int, default=3,
                   help="Max crawl depth from start URL (default: 3)")
    p.add_argument("--delay", type=float, default=0.3,
                   help="Min delay per domain between requests, seconds (default: 0.3)")
    p.add_argument("--retries", type=int, default=3,
                   help="Auto-retries on 429/5xx with backoff (default: 3)")
    p.add_argument("--timeout", type=int, default=15,
                   help="Per-request timeout in seconds (default: 15)")
    p.add_argument("--ignore-robots", action="store_true",
                   help="Do not respect robots.txt")
    p.add_argument("--allow-external", action="store_true",
                   help="Allow crawling beyond the start domain")
    p.add_argument("--out", default="seo_audit",
                   help="Output filename prefix (default: seo_audit)")
    return p


def load_urls_file(path):
    urls = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if not line.startswith(("http://", "https://")):
                        print(f"[urls-file] Skipping (no scheme): {line}")
                        continue
                    urls.append(line)
    except OSError as e:
        sys.exit(f"Error reading --urls-file: {e}")
    return urls


def main():
    args = build_parser().parse_args()

    if args.ua == "custom":
        if not args.ua_string:
            sys.exit("Error: --ua custom requires --ua-string \"YourBot/1.0\"")
        user_agent = args.ua_string
        ua_label = f"custom ({args.ua_string})"
    else:
        user_agent = USER_AGENTS[args.ua]
        ua_label = args.ua

    # resolve seed URLs from file
    file_urls = load_urls_file(args.urls_file) if args.urls_file else []

    if not args.url and not file_urls and not args.from_sitemap:
        sys.exit("Error: provide a start URL, --urls-file, or --from-sitemap")

    # determine a start URL (needed to resolve domain/robots/sitemap location)
    start_url = args.url or (file_urls[0] if file_urls else None)
    if args.from_sitemap and args.from_sitemap != "auto" and not start_url:
        start_url = args.from_sitemap  # derive domain from the sitemap URL
    if not start_url:
        sys.exit("Error: could not determine a start URL")
    if not start_url.startswith(("http://", "https://")):
        sys.exit("Error: URL must start with http:// or https://")

    print(f"User-Agent: {ua_label} | workers: {args.workers}")

    crawler = SEOCrawler(
        start_url=start_url,
        user_agent=user_agent,
        max_pages=args.max_pages,
        max_depth=args.depth,
        delay=args.delay,
        timeout=args.timeout,
        respect_robots=not args.ignore_robots,
        same_domain=not args.allow_external,
        workers=args.workers,
        seed_urls=[],            # set below once all sources are merged
        list_only=args.list_only,
        retries=args.retries,
    )

    # ---- gather URLs from the sitemap, if requested ----
    sitemap_urls = []
    if args.from_sitemap:
        crawler.load_robots()
        if args.from_sitemap != "auto":
            # explicit sitemap URL takes priority over discovered ones
            crawler.sitemaps = [args.from_sitemap]
        print(f"[sitemap] Discovering URLs from: "
              f"{', '.join(crawler.sitemaps)}")
        cap = args.max_sitemap_urls or None
        sitemap_urls = crawler.collect_sitemap_urls(max_urls=cap)
        if not sitemap_urls:
            print("[sitemap] No URLs found in sitemap(s).")

    # ---- merge all URL sources (dedupe, keep order) ----
    merged, seen = [], set()
    for u in ([args.url] if args.url else []) + file_urls + sitemap_urls:
        if not u:
            continue
        nu = crawler._normalize(u)
        if nu not in seen:
            seen.add(nu)
            merged.append(nu)

    if args.list_only:
        crawler.seed_urls = merged
        print(f"\nStarting list-only crawl of {len(merged)} URL(s)\n")
    else:
        # first URL is the start; the rest are extra seeds for discovery
        crawler.start_url = merged[0].rstrip("/")
        crawler.base_domain = urlparse(crawler.start_url).netloc
        crawler.seed_urls = merged[1:]
        print(f"\nStarting crawl of {crawler.start_url}")
        if len(merged) > 1:
            print(f"Plus {len(merged) - 1} seed URL(s)")
        print()

    reports = crawler.crawl()

    if not reports:
        print("No pages crawled.")
        return

    json_path, csv_path = write_outputs(reports, args.out)
    print_summary(reports, ua_label)
    print(f"\nDetailed JSON : {json_path}")
    print(f"Summary CSV   : {csv_path}")
    if crawler.sitemaps and not args.list_only:
        print(f"Sitemaps seen : {', '.join(crawler.sitemaps)}")


if __name__ == "__main__":
    main()
