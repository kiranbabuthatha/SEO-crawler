#!/usr/bin/env python3
"""
test_crawler.py — Offline test suite for seo_crawler.py.

Runs with no network access: HTTP fetches are stubbed with canned responses.
Exercises the parsing, decode-failure handling, sitemap recursion, and
User-Agent selection logic.

    python test_crawler.py        # expect: ALL PASSED
"""

import gzip
import sys

import seo_crawler as sc


# --------------------------------------------------------------------------- #
# Tiny test harness
# --------------------------------------------------------------------------- #
_PASSED = 0
_FAILED = 0


def check(name, condition):
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        _FAILED += 1
        print(f"  FAIL  {name}")


class FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, text="", content=None, status=200, headers=None, url=""):
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.status_code = status
        self.headers = headers or {}
        self.url = url
        self.history = []


def make_crawler(**kw):
    c = sc.SEOCrawler("https://example.com", "test-ua", workers=1, **kw)
    c._robots_loaded = True          # never touch the network for robots
    c._polite_wait = lambda url: None  # no sleeping in tests
    return c


# --------------------------------------------------------------------------- #
# 1. Good page parses with no false positives
# --------------------------------------------------------------------------- #
def test_good_page():
    print("\n[1] Well-formed page extraction")
    html = (
        '<!doctype html><html lang="de"><head>'
        '<title>PDF-Konverter</title>'
        '<meta name="description" content="Kostenloser PDF-Konverter zur '
        'Konvertierung Ihrer Dateien zu und aus PDF.">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<link rel="canonical" href="https://example.com/b">'
        '<meta property="og:title" content="PDF-Konverter">'
        '<link rel="alternate" hreflang="en" href="https://example.com/en/b">'
        '<script type="application/ld+json">{"@type":"WebPage"}</script>'
        '</head><body><h1>PDF-Konverter</h1><p>' + ("wort " * 250) +
        '</p><img src="x.png" alt="ok"><img src="y.png"></body></html>'
    )
    c = make_crawler()
    r = sc.PageReport(url="https://example.com/b", final_url="https://example.com/b",
                      status_code=200)
    c.parse(r, FakeResp(html, headers={"Content-Encoding": "gzip"}))

    check("parse_ok is True", r.parse_ok is True)
    check("title extracted", r.title == "PDF-Konverter")
    check("lang extracted", r.lang == "de")
    check("meta description found", r.meta_description.startswith("Kostenloser"))
    check("canonical is self", r.canonical_is_self is True)
    check("viewport present", bool(r.viewport))
    check("one H1", r.h1_count == 1)
    check("hreflang captured", any(h["lang"] == "en" for h in r.hreflang))
    check("og:title captured", r.og_tags.get("og:title") == "PDF-Konverter")
    check("schema type captured", "WebPage" in r.schema_types)
    check("one image missing alt", r.images_missing_alt == 1)


# --------------------------------------------------------------------------- #
# 2. Undecodable (e.g. Brotli) response -> one clean warning, not a wall
# --------------------------------------------------------------------------- #
def test_undecodable():
    print("\n[2] Undecodable compressed response")
    c = make_crawler()
    r = sc.PageReport(url="https://example.com/a", final_url="https://example.com/a",
                      status_code=200)
    links = c.parse(r, FakeResp("\x1b\x2e\x00\xff\x84garbage",
                                headers={"Content-Encoding": "br"}))
    check("parse_ok is False", r.parse_ok is False)
    check("marked non-indexable", r.indexable is False)
    check("reason mentions encoding", "br" in r.indexability_reason)
    check("exactly one issue (not a wall)", len(r.issues) == 1)
    check("no links followed", links == [])


# --------------------------------------------------------------------------- #
# 3. Empty / JS-rendered shell -> distinct message
# --------------------------------------------------------------------------- #
def test_js_shell():
    print("\n[3] Empty / JS-rendered shell")
    c = make_crawler()
    r = sc.PageReport(url="https://example.com/c", final_url="https://example.com/c",
                      status_code=200)
    c.parse(r, FakeResp("\x00\xff random bytes", headers={}))
    check("parse_ok is False", r.parse_ok is False)
    check("reason mentions JS/empty",
          "JS" in r.indexability_reason or "empty" in r.indexability_reason)


# --------------------------------------------------------------------------- #
# 4. Sitemap index recursion + entity decode + domain filter
# --------------------------------------------------------------------------- #
def test_sitemap_recursion():
    print("\n[4] Sitemap index recursion")
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>'
        '<sitemap><loc>https://example.com/sitemap-blog.xml</loc></sitemap>'
        '</sitemapindex>'
    )
    pages = (
        '<?xml version="1.0"?><urlset>'
        '<url><loc>https://example.com/a</loc></url>'
        '<url><loc>https://example.com/b?x=1&amp;y=2</loc></url>'
        '<url><loc>https://other.com/external</loc></url>'
        '</urlset>'
    )
    blog = (
        '<?xml version="1.0"?><urlset>'
        '<url><loc>https://example.com/blog/post-1</loc></url>'
        '</urlset>'
    )
    mock = {
        "https://example.com/sitemap.xml": index,
        "https://example.com/sitemap-pages.xml": pages,
        "https://example.com/sitemap-blog.xml": blog,
    }
    c = make_crawler()
    c.sitemaps = ["https://example.com/sitemap.xml"]
    c._fetch_sitemap_xml = lambda url: mock.get(url)
    urls = c.collect_sitemap_urls()

    check("found page from child sitemap", "https://example.com/a" in urls)
    check("XML entity decoded", "https://example.com/b?x=1&y=2" in urls)
    check("recursed into second child", "https://example.com/blog/post-1" in urls)
    check("off-domain URL filtered out",
          not any("other.com" in u for u in urls))
    check("exactly 3 same-domain URLs", len(urls) == 3)


# --------------------------------------------------------------------------- #
# 5. Gzipped sitemap is transparently decompressed
# --------------------------------------------------------------------------- #
def test_gzip_sitemap():
    print("\n[5] Gzipped sitemap decompression")
    xml = (b'<?xml version="1.0"?><urlset>'
           b'<url><loc>https://example.com/z</loc></url></urlset>')
    gz = gzip.compress(xml)
    c = make_crawler()
    c.session = type("S", (), {"get": lambda self, url, **k: FakeResp(content=gz)})()
    out = c._fetch_sitemap_xml("https://example.com/sitemap.xml.gz")
    check("decompressed gzip body", out is not None and "example.com/z" in out)


# --------------------------------------------------------------------------- #
# 6. User-Agent selection + only-decodable encodings advertised
# --------------------------------------------------------------------------- #
def test_user_agents():
    print("\n[6] User-Agent presets and encoding negotiation")
    check("googlebot preset exists", "Googlebot" in sc.USER_AGENTS["googlebot"])
    check("mobile preset differs from desktop",
          sc.USER_AGENTS["googlebot"] != sc.USER_AGENTS["googlebot-mobile"])
    check("bingbot preset exists", "bingbot" in sc.USER_AGENTS["bingbot"].lower())
    enc = sc.SEOCrawler._supported_encodings()
    check("always advertises gzip", "gzip" in enc)
    check("never advertises br without a decoder",
          ("br" in enc) == (sc._importable("brotli") or sc._importable("brotlicffi")))


# --------------------------------------------------------------------------- #
# 7. Issue detection on a deliberately broken page
# --------------------------------------------------------------------------- #
def test_issue_detection():
    print("\n[7] Issue flagging on a poor page")
    html = (
        '<!doctype html><html><head>'
        '<title>' + ("x" * 80) + '</title>'  # too long
        '</head><body><h1>one</h1><h1>two</h1><p>short</p></body></html>'
    )
    c = make_crawler()
    r = sc.PageReport(url="https://example.com/d", final_url="https://example.com/d",
                      status_code=200)
    c.parse(r, FakeResp(html, headers={}))
    joined = " | ".join(r.issues)
    check("flags long title", "Title too long" in joined)
    check("flags missing meta description", "Missing meta description" in joined)
    check("flags missing canonical", "Missing canonical" in joined)
    check("flags multiple H1s", "Multiple H1" in joined)
    check("flags thin content", "Thin content" in joined)


# --------------------------------------------------------------------------- #
def main():
    print("Running offline test suite for seo_crawler.py")
    print("=" * 55)
    test_good_page()
    test_undecodable()
    test_js_shell()
    test_sitemap_recursion()
    test_gzip_sitemap()
    test_user_agents()
    test_issue_detection()

    print("\n" + "=" * 55)
    print(f"  {_PASSED} passed, {_FAILED} failed")
    if _FAILED == 0:
        print("  ALL PASSED")
        return 0
    print("  SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
