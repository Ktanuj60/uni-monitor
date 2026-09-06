"""
scraper.py
Visits each university website, captures any popup/notification-modal text
before closing it, auto-discovers Programs, Fees, and Notifications
(offers/discounts/scholarships/announcements/news) pages, and extracts
their visible text content -- including running news tickers/marquees.

Why auto-discovery instead of hardcoded page URLs:
Every one of these 30 sites has a different structure, and pages get
renamed/restructured over time. Auto-discovery by keyword is more
resilient than hardcoded URLs that quietly go stale. You can always
override a specific university's pages by adding "program_urls" /
"fee_urls" / "notification_urls" arrays directly in universities.json --
if present, the scraper uses those instead of discovering.
"""

import json
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

PROGRAM_KEYWORDS = [
    "program", "programme", "course", "specialization", "specialisation",
    "curriculum", "mba", "bba", "mca", "bca", "b.com", "m.com", "b.a",
    "m.a", "b.sc", "m.sc", "diploma", "degree", "elective"
]
FEE_KEYWORDS = [
    "fee", "fees", "tuition", "cost", "payment", "installment", "instalment",
    "emi", "breakup", "fee structure", "structure"
]
NOTIFICATION_KEYWORDS = [
    "offer", "discount", "scholarship", "announcement", "notice", "news",
    "update", "alert", "admission open", "deadline", "last date", "event",
    "webinar", "notification", "whats-new", "what's new"
]

# Common patterns for popup/modal containers -- used both to READ their
# text (so an offer/discount shown only in a popup still gets captured)
# and then to close them.
MODAL_CONTAINER_SELECTORS = [
    "[role='dialog']", ".modal", "[class*='modal' i]", "[class*='popup' i]",
    "[class*='overlay' i]", "[id*='popup' i]", "[id*='modal' i]",
]
POPUP_CLOSE_SELECTORS = [
    "button[aria-label='Close']", "button[aria-label='close']",
    ".modal-close", ".close-btn", ".popup-close", ".btn-close",
    "[class*='close' i][class*='modal' i]", "[class*='close' i][class*='popup' i]",
    "svg[class*='close' i]", ".modal .close", "[data-dismiss='modal']",
]

# Selectors for running-news tickers / marquees / notification bars that
# often carry offers, deadlines, and announcements separately from the
# main page body.
TICKER_SELECTORS = [
    "marquee", ".ticker", ".marquee", ".news-ticker", ".notification-bar",
    ".running-text", ".scroll-text", "[class*='ticker' i]",
    "[class*='marquee' i]", "[class*='announcement' i]", "[class*='notice' i]",
]

MAX_PAGES_PER_CATEGORY = 4  # cap how many discovered pages we crawl per category, per site


def capture_and_close_popups(page):
    """Reads visible text from any popup/modal BEFORE closing it (so an
    offer/discount/scholarship shown only in a popup still gets captured),
    then dismisses it."""
    captured = []
    seen = set()
    for sel in MODAL_CONTAINER_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    t = (el.inner_text() or "").strip()
                    if t and len(t) > 5 and t not in seen:
                        seen.add(t)
                        captured.append(t)
        except Exception:
            continue

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

    return "\n---\n".join(captured)


def extract_ticker_text(page):
    """Grabs text from running-news tickers/marquees/notification bars."""
    texts = []
    seen = set()
    for sel in TICKER_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                t = (el.inner_text() or "").strip()
                if t and t not in seen:
                    seen.add(t)
                    texts.append(t)
        except Exception:
            continue
    return "\n".join(texts)


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


def visit_and_extract(page, url, popup_key_prefix, results_bucket):
    """Navigates to url, captures+closes popups, captures ticker text,
    extracts main content, and stores everything into results_bucket."""
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        popup_text = capture_and_close_popups(page)
        if popup_text:
            results_bucket[f"{url} [popup]"] = popup_text

        ticker_text = extract_ticker_text(page)
        main_text = extract_text(page)
        combined = f"{ticker_text}\n\n{main_text}".strip() if ticker_text else main_text
        results_bucket[url] = combined
    except Exception as e:
        results_bucket[url] = f"[ERROR fetching page: {e}]"


def scrape_university(browser, uni):
    """Returns dict: {"programs": {...}, "fees": {...}, "notifications": {...}}"""
    result = {"programs": {}, "fees": {}, "notifications": {}}
    context = None
    try:
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page = context.new_page()
        page.goto(uni["url"], timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Homepage: always captured as a notifications entry too (running
        # tickers / popups / "what's new" banners most often live here).
        popup_text = capture_and_close_popups(page)
        if popup_text:
            result["notifications"][f"{uni['url']} [popup]"] = popup_text
        ticker_text = extract_ticker_text(page)
        homepage_main = extract_text(page)
        result["notifications"][uni["url"]] = (
            f"{ticker_text}\n\n{homepage_main}".strip() if ticker_text else homepage_main
        )

        program_urls = uni.get("program_urls") or discover_links(page, uni["url"], PROGRAM_KEYWORDS)
        fee_urls = uni.get("fee_urls") or discover_links(page, uni["url"], FEE_KEYWORDS)
        notification_urls = uni.get("notification_urls") or discover_links(page, uni["url"], NOTIFICATION_KEYWORDS)

        if not program_urls:
            program_urls = [uni["url"]]
        if not fee_urls:
            fee_urls = [uni["url"]]

        for url in program_urls:
            visit_and_extract(page, url, "programs", result["programs"])

        for url in fee_urls:
            if url in result["fees"]:
                continue
            visit_and_extract(page, url, "fees", result["fees"])

        for url in notification_urls:
            if url in result["notifications"]:
                continue
            visit_and_extract(page, url, "notifications", result["notifications"])

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
                results[uni["name"]] = {
                    "programs": {"_error": f"[Fatal error: {e}]"},
                    "fees": {},
                    "notifications": {},
                }
        browser.close()
    return results


if __name__ == "__main__":
    with open("universities.json") as f:
        unis = json.load(f)
    data = scrape_all(unis)
    with open("last_run_debug.json", "w") as f:
        json.dump(data, f, indent=2)
    print("Done. Wrote last_run_debug.json")
