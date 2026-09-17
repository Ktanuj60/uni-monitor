"""
scraper.py
Visits each university website, captures any popup/notification-modal text
before closing it, auto-discovers Programs, Fees, and Notifications pages,
and extracts STRUCTURED content -- Programs with Specializations, Fees as
clean item/amount pairs, Offers/Discounts/Scholarships as a clean list --
using free, rule-based parsing of the page's own HTML (no paid API).

Key correctness fixes in this version:
- Page selection is now DETERMINISTIC (sorted, not a raw set()) -- earlier
  versions picked a random subset of discovered pages every run because
  Python's set() iteration order is randomized per process, which caused
  programs to falsely look "added"/"removed" run to run even when nothing
  changed on the site.
- Every page is only ever scraped ONCE per run (shared cache), even if it's
  relevant to more than one category (e.g. a program's own page often has
  a "Fee Structure" table on it too -- very common on these sites, since
  many don't have a separate sitewide Fees page at all).
- Program page coverage raised well above the old cap of 4, since sites
  can legitimately list far more programs than that.
- Duplicate program/fee entries on a single page (e.g. carousel-cloned
  DOM nodes for infinite-scroll effects) are merged before diffing.
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
TICKER_SELECTORS = [
    "marquee", ".ticker", ".marquee", ".news-ticker", ".notification-bar",
    ".running-text", ".scroll-text", "[class*='ticker' i]",
    "[class*='marquee' i]", "[class*='announcement' i]", "[class*='notice' i]",
]

MAX_PROGRAM_PAGES = 12   # sites can legitimately list many programs
MAX_FEE_PAGES = 4        # dedicated fee-keyword nav links, if any exist
MAX_NOTIFICATION_PAGES = 4

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

PROGRAM_HEADING_JS = """
() => {
  const scope = document.querySelector('main, article, [role="main"], #content, .content') || document.body;
  const headings = Array.from(scope.querySelectorAll('h1, h2, h3, h4'));
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


def dedupe_programs(programs):
    """Collapses duplicate program entries (same name, case/whitespace
    insensitive) that come from carousel-cloned DOM nodes or a page listing
    the same program more than once, merging their specializations."""
    merged, order = {}, []
    for p in programs:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in merged:
            merged[key] = {"name": name, "specializations": set()}
            order.append(key)
        merged[key]["specializations"].update(
            s.strip() for s in p.get("specializations", []) if s.strip()
        )
    return [{"name": merged[k]["name"], "specializations": sorted(merged[k]["specializations"])}
            for k in order]


def dedupe_fees(fees):
    seen, order = {}, []
    for f in fees:
        item = (f.get("item") or "").strip()
        if not item:
            continue
        key = item.lower()
        if key not in seen:
            seen[key] = {"item": item, "amount": (f.get("amount") or "").strip()}
            order.append(key)
    return [seen[k] for k in order]


def capture_and_close_popups(page):
    captured, seen = [], set()
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
    texts, seen = [], set()
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
    texts, seen = [], set()
    try:
        for img in page.query_selector_all("img[alt]"):
            alt = (img.get_attribute("alt") or "").strip()
            if alt and len(alt.split()) >= 3 and alt not in seen:
                seen.add(alt)
                texts.append(alt)
    except Exception:
        pass
    return "\n".join(texts)


def discover_links(page, base_url, keywords, max_count):
    """Returns a DETERMINISTIC (sorted) list, capped at max_count, so the
    same pages get chosen every run instead of a random subset."""
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
    return sorted(found)[:max_count]


def extract_text(page):
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
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1200)


def extract_notification_lines(text):
    lines, seen = [], set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        if any(kw in s.lower() for kw in NOTIFICATION_KEYWORDS):
            seen.add(s)
            lines.append(s)
    return lines[:30]


def extract_structured(page, combined_text):
    try:
        fees = dedupe_fees(page.evaluate(FEE_TABLE_JS) or [])
    except Exception:
        fees = []
    try:
        programs = dedupe_programs(page.evaluate(PROGRAM_HEADING_JS) or [])
    except Exception:
        programs = []
    notifications = extract_notification_lines(combined_text)
    return {"programs": programs, "fees": fees, "notifications": notifications}


def visit_page_once(page, url):
    """Navigates to url and returns (content_json_or_sentinel, popup_json_or_None).
    Callers should cache this per-URL so the same page is never scraped
    twice in one run."""
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        settle(page)

        popup_text = capture_and_close_popups(page)
        popup_json = None
        if popup_text and not is_blocked_page(popup_text):
            structured_popup = extract_structured(page, clean_lines(popup_text))
            popup_json = json.dumps(structured_popup, ensure_ascii=False)

        ticker_and_alts = f"{extract_ticker_text(page)}\n{extract_image_alts(page)}".strip()
        main_text = extract_text(page)
        combined = f"{ticker_and_alts}\n\n{main_text}".strip() if ticker_and_alts else main_text

        if is_blocked_page(combined):
            return SCRAPE_BLOCKED_SENTINEL, popup_json

        if not combined or len(combined.strip()) < 30:
            return json.dumps(EMPTY_RESULT, ensure_ascii=False), popup_json

        structured = extract_structured(page, combined)
        return json.dumps(structured, ensure_ascii=False), popup_json
    except Exception as e:
        return f"[ERROR fetching page: {e}]", None


def scrape_university(browser, uni):
    """Returns dict: {"programs": {...}, "fees": {...}, "notifications": {...}}"""
    result = {"programs": {}, "fees": {}, "notifications": {}}
    page_cache = {}  # url -> (content, popup_json) -- ensures each URL is only ever scraped once
    context = None

    def get_or_visit(page, url):
        if url not in page_cache:
            page_cache[url] = visit_page_once(page, url)
        return page_cache[url]

    try:
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page = context.new_page()
        page.goto(uni["url"], timeout=45000, wait_until="domcontentloaded")
        settle(page)

        homepage_content, homepage_popup = get_or_visit(page, uni["url"])
        result["notifications"][uni["url"]] = homepage_content
        if homepage_popup:
            result["notifications"][f"{uni['url']} [popup]"] = homepage_popup

        program_urls = uni.get("program_urls") or discover_links(
            page, uni["url"], PROGRAM_KEYWORDS, MAX_PROGRAM_PAGES)
        fee_urls = uni.get("fee_urls") or discover_links(
            page, uni["url"], FEE_KEYWORDS, MAX_FEE_PAGES)
        notification_urls = uni.get("notification_urls") or discover_links(
            page, uni["url"], NOTIFICATION_KEYWORDS, MAX_NOTIFICATION_PAGES)

        if not program_urls:
            program_urls = [uni["url"]]

        # Fee data on these sites very often lives on each program's own
        # page (a "Fee Structure" table), not a separate sitewide Fees
        # page -- so program pages are always checked for fees too. The
        # shared cache means this doesn't cost a second page visit.
        fee_candidate_urls = list(dict.fromkeys(fee_urls + program_urls))[:MAX_PROGRAM_PAGES + MAX_FEE_PAGES]
        if not fee_candidate_urls:
            fee_candidate_urls = [uni["url"]]

        for url in program_urls:
            content, popup = get_or_visit(page, url)
            result["programs"][url] = content
            if popup:
                result["programs"][f"{url} [popup]"] = popup

        for url in fee_candidate_urls:
            content, popup = get_or_visit(page, url)
            result["fees"][url] = content
            if popup:
                result["fees"][f"{url} [popup]"] = popup

        for url in notification_urls:
            content, popup = get_or_visit(page, url)
            result["notifications"][url] = content
            if popup:
                result["notifications"][f"{url} [popup]"] = popup

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
