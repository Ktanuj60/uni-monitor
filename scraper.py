"""
scraper.py
Visits each university website, closes any popup/notification modal,
auto-discovers links that look like Programs/Specialization pages and
Fees pages, and extracts their visible text content.

Why auto-discovery instead of hardcoded page URLs:
Every one of these 30 sites has a different structure, and pages get
renamed/restructured over time. Auto-discovery by keyword is more
resilient than hardcoded URLs that quietly go stale. You can always
override a specific university's pages by adding "program_urls" /
"fee_urls" arrays directly in universities.json -- if present, the
scraper uses those instead of discovering.
"""

import json
import re
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

PROGRAM_KEYWORDS = [
    "program", "programme", "course", "specialization", "specialisation",
    "curriculum", "mba", "bba", "mca", "bca", "b.com", "m.com", "b.a",
    "m.a", "b.sc", "m.sc", "diploma", "degree"
]
FEE_KEYWORDS = ["fee", "fees", "tuition", "cost", "payment"]

# Common patterns for "close" buttons on lead-gen popups/modals
POPUP_CLOSE_SELECTORS = [
    "button[aria-label='Close']", "button[aria-label='close']",
    ".modal-close", ".close-btn", ".popup-close", ".btn-close",
    "[class*='close' i][class*='modal' i]", "[class*='close' i][class*='popup' i]",
    "svg[class*='close' i]", ".modal .close", "[data-dismiss='modal']",
]

MAX_PAGES_PER_CATEGORY = 4  # cap how many discovered pages we crawl per category, per site


def close_popups(page):
    """Best-effort dismissal of lead-capture popups/modals."""
    for sel in POPUP_CLOSE_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=1500)
                page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def discover_links(page, base_url, keywords):
    found = set()
    try:
        anchors = page.query_selector_all("a")
    except Exception:
        return []
    base_host = urlparse(base_url).netloc
    for a in anchors:
        try:
            href = a.get_attribute("href")
            text = (a.inner_text() or "").strip().lower()
        except Exception:
            continue
        if not href:
            continue
        haystack = f"{text} {href.lower()}"
        if any(kw in haystack for kw in keywords):
            full = urljoin(base_url, href)
            if urlparse(full).netloc == base_host and full.startswith("http"):
                found.add(full.split("#")[0])
    return list(found)[:MAX_PAGES_PER_CATEGORY]


def extract_text(page):
    """Grab visible text, preferring <main>/<article> content over full body
    (to reduce nav/footer noise), falling back to body innerText."""
    for sel in ["main", "article", "[role='main']", "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text()
                if txt and len(txt.strip()) > 200:
                    return normalize_text(txt)
        except Exception:
            continue
    try:
        return normalize_text(page.inner_text("body"))
    except Exception:
        return ""


def normalize_text(txt):
    lines = [l.strip() for l in txt.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def scrape_university(browser, uni):
    """Returns dict: {page_type: {url: text}}"""
    result = {"programs": {}, "fees": {}}
    context = None
    try:
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page = context.new_page()
        page.goto(uni["url"], timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        close_popups(page)

        program_urls = uni.get("program_urls") or discover_links(page, uni["url"], PROGRAM_KEYWORDS)
        fee_urls = uni.get("fee_urls") or discover_links(page, uni["url"], FEE_KEYWORDS)

        # If nothing discovered, fall back to homepage itself for both
        if not program_urls:
            program_urls = [uni["url"]]
        if not fee_urls:
            fee_urls = [uni["url"]]

        for url in program_urls:
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                close_popups(page)
                result["programs"][url] = extract_text(page)
            except Exception as e:
                result["programs"][url] = f"[ERROR fetching page: {e}]"

        for url in fee_urls:
            if url in result["fees"]:
                continue
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                close_popups(page)
                result["fees"][url] = extract_text(page)
            except Exception as e:
                result["fees"][url] = f"[ERROR fetching page: {e}]"

    except Exception as e:
        result["programs"]["_error"] = f"[Could not load site: {e}]"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
    return result


def scrape_all(universities):
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for uni in universities:
            print(f"Scraping: {uni['name']}")
            try:
                results[uni["name"]] = scrape_university(browser, uni)
            except Exception as e:
                # Guarantees one broken site never stops the remaining 29 from being attempted
                print(f"  -> FAILED, continuing to next university: {e}")
                results[uni["name"]] = {"programs": {"_error": f"[Fatal error: {e}]"}, "fees": {}}
        browser.close()
    return results


if __name__ == "__main__":
    with open("universities.json") as f:
        unis = json.load(f)
    data = scrape_all(unis)
    with open("last_run_debug.json", "w") as f:
        json.dump(data, f, indent=2)
    print("Done. Wrote last_run_debug.json")
