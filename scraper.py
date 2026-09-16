"""
scraper.py
Visits each university website, captures any popup/notification-modal text
before closing it, auto-discovers Programs, Fees, and Notifications
(offers/discounts/scholarships/announcements/news) pages, and extracts
STRUCTURED content -- Programs with their Specializations, Fees as clean
item/amount pairs, and Offers/Discounts/Scholarships as a clean list --
using free, rule-based parsing of the page's own HTML (no paid API):

- Fees: every <table> row and <dl> item on the page is read as a literal
  Label/Value pair. Most university fee breakups are already laid out in
  tables, so this captures them structurally for free.
- Programs/Specializations: each heading (h2/h3/h4) is treated as a
  candidate program name, and the list items directly under it (before the
  next heading) are treated as its specializations. Works well on sites
  that use real heading/list markup; sites that write everything as plain
  paragraphs won't structure as cleanly -- that content still gets
  captured in the free-text fallback, just not as a formal Program entry.
- Notifications: lines from the page's cleaned text that mention offer/
  discount/scholarship/announcement keywords.

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
from noise_filter import clean_lines, is_blocked_page, NOTIFICATION_KEYWORDS

SCRAPE_BLOCKED_SENTINEL = "__SCRAPE_BLOCKED__"
EMPTY_RESULT = {"programs": [], "fees": [], "notifications": []}

PROGRAM_KEYWORDS = [
    "program", "programme", "course", "specialization", "specialisation",
    "curriculum", "mba", "bba", "mca", "bca", "b.com", "m.com", "b.a",
    "m.a", "b.sc", "m.sc", "diploma", "degree", "elective"
]
FEE_KEYWORDS = [
    "fee", "fees", "tuition", "cost", "payment", "installment", "instalment",
    "emi", "breakup", "fee structure", "structure"
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

# JS run in-page to read <table>/<dl> rows as Label/Value fee pairs.
FEE_TABLE_JS = """
() => {
  const pairs = [];
  document.querySelectorAll('table').forEach(table => {
    table.querySelectorAll('tr').forEach(tr => {
      const cells = Array.from(tr.querySelectorAll('td,th'))
        .map(c => c.innerText.trim()).filter(Boolean);
      if (cells.length >= 2) {
        pairs.push({item: cells[0].slice(0,150), amount: cells.slice(1).join(' | ').slice(0,150)});
      }
    });
  });
  document.querySelectorAll('dl').forEach(dl => {
    const dts = Array.from(dl.querySelectorAll('dt'));
    const dds = Array.from(dl.querySelectorAll('dd'));
    for (let i = 0; i < Math.min(dts.length, dds.length); i++) {
      const item = dts[i].innerText.trim();
      const amount = dds[i].innerText.trim();
      if (item && amount) pairs.push({item: item.slice(0,150), amount: amount.slice(0,150)});
    }
  });
  return pairs;
}
"""

# JS run in-page to treat headings as Program names and the list right
# under each heading as its Specializations.
PROGRAM_HEADING_JS = """
() => {
  const scope = document.querySelector('main, article, [role="main"], #content, .content') || document.body;
  const headings = Array.from(scope.querySelectorAll('h2, h3, h4'));
  const result = [];
  for (const h of headings) {
    const name = (h.innerText || '').trim();
    if (!name || name.length < 2 || name.length > 100) continue;
    const specs = [];
    let el = h.nextElementSibling;
    let guard = 0;
    while (el && guard < 30) {
      guard++;
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      if (['h1','h2','h3','h4'].includes(tag)) break;
      if (tag === 'ul' || tag === 'ol') {
        el.querySelectorAll('li').forEach(li => {
          const t = (li.innerText || '').trim();
          if (t && t.length < 150) specs.push(t);
        });
      }
      el = el.nextElementSibling;
    }
    result.push({name, specializations: specs});
  }
  return result;
}
"""


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


def extract_image_alts(page):
    """Catches offers/announcements shown as banner IMAGES rather than text,
    by reading their alt attributes (works only if the site set meaningful
    alt text -- a pure graphic banner with no alt text can't be read this
    way without OCR, which this free pipeline doesn't do)."""
    texts = []
    seen = set()
    try:
        for img in page.query_selector_all("img[alt]"):
            alt = (img.get_attribute("alt") or "").strip()
            if alt and len(alt.split()) >= 3 and alt not in seen:
                seen.add(alt)
                texts.append(alt)
    except Exception:
        pass
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
    return clean_lines("\n".join(lines))


def settle(page):
    """Waits for dynamic content (JS-rendered dropdowns, lazy-loaded fee
    tables, 'Loading...' placeholders) to finish before we read the page,
    instead of reading it mid-render."""
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def extract_notification_lines(text):
    """Free, rule-based stand-in for AI extraction: pulls out lines that
    actually mention an offer/discount/scholarship/announcement keyword,
    instead of returning the whole page's text as 'notifications'."""
    lines = []
    seen = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        low = s.lower()
        if any(kw in low for kw in NOTIFICATION_KEYWORDS):
            seen.add(s)
            lines.append(s)
    return lines[:30]


def extract_structured(page, combined_text):
    """Free, rule-based structure extraction: tables/dl -> fees,
    headings+lists -> programs/specializations, keyword lines -> notifications."""
    try:
        fees = page.evaluate(FEE_TABLE_JS) or []
    except Exception:
        fees = []
    try:
        programs = page.evaluate(PROGRAM_HEADING_JS) or []
    except Exception:
        programs = []
    notifications = extract_notification_lines(combined_text)
    return {"programs": programs, "fees": fees, "notifications": notifications}


def store_structured(page, results_bucket, key, raw_text):
    structured = extract_structured(page, raw_text) if raw_text and len(raw_text.strip()) >= 30 else EMPTY_RESULT
    results_bucket[key] = json.dumps(structured, ensure_ascii=False)


def visit_and_extract(page, url, popup_key_prefix, results_bucket):
    """Navigates to url, captures+closes popups, captures ticker/banner-image
    text, extracts main content, runs it through free structured extraction,
    and stores the result into results_bucket.
    If the page turns out to be a bot-check / challenge page rather than
    real content, stores a sentinel instead so it's never reported as a
    'change' and never overwrites the last good snapshot."""
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        settle(page)

        popup_text = capture_and_close_popups(page)
        if popup_text and not is_blocked_page(popup_text):
            store_structured(page, results_bucket, f"{url} [popup]", clean_lines(popup_text))

        ticker_and_alts = f"{extract_ticker_text(page)}\n{extract_image_alts(page)}".strip()
        main_text = extract_text(page)
        combined = f"{ticker_and_alts}\n\n{main_text}".strip() if ticker_and_alts else main_text

        if is_blocked_page(combined):
            results_bucket[url] = SCRAPE_BLOCKED_SENTINEL
        else:
            store_structured(page, results_bucket, url, combined)
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
        settle(page)

        # Homepage: always captured as a notifications entry too (running
        # tickers / banner images / popups / "what's new" sections most
        # often live here).
        popup_text = capture_and_close_popups(page)
        if popup_text and not is_blocked_page(popup_text):
            store_structured(page, result["notifications"], f"{uni['url']} [popup]", clean_lines(popup_text))
        ticker_and_alts = f"{extract_ticker_text(page)}\n{extract_image_alts(page)}".strip()
        homepage_main = extract_text(page)
        homepage_combined = f"{ticker_and_alts}\n\n{homepage_main}".strip() if ticker_and_alts else homepage_main
        if is_blocked_page(homepage_combined):
            result["notifications"][uni["url"]] = SCRAPE_BLOCKED_SENTINEL
        else:
            store_structured(page, result["notifications"], uni["url"], homepage_combined)

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
