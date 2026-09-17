"""
scraper.py
Visits each university website and extracts STRUCTURED content -- Programs
with Specializations, Fees as clean item/amount pairs, Offers/Discounts/
Scholarships as a clean list -- using free, rule-based parsing (no paid API).

PROGRAM DETECTION (the part that needed to get much stricter):
A heading is only treated as a real program if it is BOTH (a) a clickable
link to its own page, AND (b) has a duration nearby ("3 Years", "2 Years",
etc.) -- the same pattern real program cards use on these sites. This is
what actually distinguishes "BA (History, Economics, Politics)" from noise
headings like "Why Choose Us?" or "Awards & Recognition", which are NOT
links to their own page and have no duration attached. Earlier versions
treated every heading on the page as a candidate program when the page had
no semantic <main>/<article> wrapper, which produced a lot of garbage.

For each real program found this way, its own detail page is visited once
to look for (1) a Fee Structure table -- very common on these sites even
when there's no separate sitewide Fees page -- and (2) an explicit
"Specialization/Elective" list, rather than re-scanning that detail page's
headings generically (which would catch FAQ questions, semester headers,
etc. as if they were programs).

If a site's markup doesn't match this card pattern at all (zero programs
found this way), the pipeline falls back to the older, looser
heading-based extraction so such a site isn't left with nothing.
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
    "m.a", "b.sc", "m.sc", "diploma", "degree", "elective", "odl",
    "distance", "apprenticeship", "work integrated", "work-integrated"
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

MAX_HUB_PAGES = 6          # "Online Programs", "ODL Programs", "Degree with Apprenticeship" etc.
MAX_PROGRAM_CARDS = 24     # real distinct programs found via the card heuristic
MAX_FEE_PAGES = 4
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

# Finds real program CARDS on a listing page (homepage or a hub page like
# "Online Programs"): a heading that is a clickable link to its own page
# AND has a duration nearby. Falls back to link-only (no duration
# requirement, but with an obvious-noise stopword filter) only if the
# strict pass finds nothing on this page.
PROGRAM_CARD_JS = """
() => {
  const STOPWORDS = ['why choose us','about us','awards','recognition','testimonial',
    'learning resources','contact us','get in touch','faq','frequently asked',
    'admission process','rankings','ranking','programs offered','our programs',
    'apply now','read more','get a ugc','universally accepted','privacy policy',
    'terms','news','blog','events','gallery','welcome to','get in touch',
    'student testimonial'];
  const durationRe = /\\b\\d+(\\.\\d+)?\\s*(year|years|yr|yrs|month|months|semester|semesters)\\b/i;
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5'));
  const withDuration = [], withoutDuration = [];
  const seenD = new Set(), seenN = new Set();
  for (const h of headings) {
    const link = h.querySelector('a') || h.closest('a');
    if (!link) continue;
    const href = link.getAttribute('href') || '';
    if (!href || href.startsWith('#') || href.toLowerCase().startsWith('javascript')) continue;
    const name = (h.innerText || '').trim();
    if (!name || name.length < 2 || name.length > 100) continue;
    const lname = name.toLowerCase();
    if (STOPWORDS.some(sw => lname.includes(sw))) continue;
    let container = h.closest('div') || h.parentElement;
    let contextText = container ? container.innerText : '';
    let sib = h.nextElementSibling, guard = 0;
    while (sib && guard < 3) { contextText += ' ' + (sib.innerText || ''); sib = sib.nextElementSibling; guard++; }
    let absHref;
    try { absHref = new URL(href, window.location.href).href.split('#')[0]; } catch(e) { absHref = href; }
    if (durationRe.test(contextText)) {
      if (!seenD.has(lname)) { seenD.add(lname); withDuration.push({name, href: absHref}); }
    } else {
      if (!seenN.has(lname)) { seenN.add(lname); withoutDuration.push({name, href: absHref}); }
    }
  }
  return withDuration.length > 0 ? withDuration : withoutDuration;
}
"""

# Older, looser fallback: any heading + the list right under it. Only used
# when PROGRAM_CARD_JS finds nothing at all on any listing page for a site.
PROGRAM_HEADING_FALLBACK_JS = """
() => {
  const scope = document.querySelector('main, article, [role="main"], #content, .content') || document.body;
  const headings = Array.from(scope.querySelectorAll('h1, h2, h3, h4'));
  const result = [];
  for (const h of headings) {
    const name = (h.innerText || '').trim();
    if (!name || name.length < 2 || name.length > 100) continue;
    const specs = [];
    let el = h.nextElementSibling, guard = 0;
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

# On a program's OWN detail page: look specifically for a heading/label
# that mentions specialization/elective/stream/etc, and grab the list
# right under it -- instead of treating every heading on the detail page
# (FAQ questions, semester headers) as if it were a separate program.
SPECIALIZATION_JS = """
() => {
  const keywords = ['specialization','specialisation','elective','stream','concentration','major','track'];
  const specs = [];
  const seen = new Set();
  const candidates = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,strong,b,dt'));
  for (const el of candidates) {
    const text = (el.innerText || '').toLowerCase();
    if (!keywords.some(k => text.includes(k))) continue;
    let sib = el.nextElementSibling, guard = 0;
    while (sib && guard < 10) {
      guard++;
      const tag = sib.tagName ? sib.tagName.toLowerCase() : '';
      if (['h1','h2','h3','h4','h5'].includes(tag)) break;
      if (tag === 'ul' || tag === 'ol') {
        sib.querySelectorAll('li').forEach(li => {
          const t = (li.innerText || '').trim();
          if (t && t.length < 150 && !seen.has(t.toLowerCase())) {
            seen.add(t.toLowerCase());
            specs.push(t);
          }
        });
      }
      sib = sib.nextElementSibling;
    }
  }
  return specs;
}
"""


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


def dedupe_programs(programs):
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


def goto_and_settle(page, url):
    """Navigates + waits; returns (ok, combined_text_or_None). combined_text
    is None if the page is a bot-check page (caller should treat as failure)."""
    page.goto(url, timeout=45000, wait_until="domcontentloaded")
    settle(page)
    ticker_and_alts = f"{extract_ticker_text(page)}\n{extract_image_alts(page)}".strip()
    main_text = extract_text(page)
    combined = f"{ticker_and_alts}\n\n{main_text}".strip() if ticker_and_alts else main_text
    if is_blocked_page(combined):
        return False, None
    return True, combined


def visit_listing_page(page, url):
    """For homepage / hub pages: returns (notifications_json_or_sentinel,
    popup_json_or_None, program_cards[list of {name,href}])."""
    try:
        ok, combined = goto_and_settle(page, url)
        popup_text = capture_and_close_popups(page)
        popup_json = None
        if popup_text and not is_blocked_page(popup_text):
            popup_json = json.dumps(
                {"programs": [], "fees": [], "notifications": extract_notification_lines(clean_lines(popup_text))},
                ensure_ascii=False)

        if not ok:
            return SCRAPE_BLOCKED_SENTINEL, popup_json, []

        try:
            cards = page.evaluate(PROGRAM_CARD_JS) or []
        except Exception:
            cards = []

        notifications = extract_notification_lines(combined)
        content_json = json.dumps({"programs": [], "fees": [], "notifications": notifications}, ensure_ascii=False)
        return content_json, popup_json, cards
    except Exception as e:
        return f"[ERROR fetching page: {e}]", None, []


def visit_program_detail_page(page, url, card_name):
    """For a specific program's own page: returns (content_json_or_sentinel, popup_json_or_None)
    where content_json's "programs" is exactly ONE entry using the already-known
    card_name (not re-derived from this page's own headings), with
    specializations + fees pulled specifically from this page."""
    try:
        ok, combined = goto_and_settle(page, url)
        popup_text = capture_and_close_popups(page)
        popup_json = None
        if popup_text and not is_blocked_page(popup_text):
            popup_json = json.dumps(
                {"programs": [], "fees": [], "notifications": extract_notification_lines(clean_lines(popup_text))},
                ensure_ascii=False)

        if not ok:
            return SCRAPE_BLOCKED_SENTINEL, popup_json

        try:
            fees = dedupe_fees(page.evaluate(FEE_TABLE_JS) or [])
        except Exception:
            fees = []
        try:
            specs = page.evaluate(SPECIALIZATION_JS) or []
        except Exception:
            specs = []
        specs = sorted(set(s.strip() for s in specs if s.strip()))

        structured = {
            "programs": [{"name": card_name, "specializations": specs}],
            "fees": fees,
            "notifications": extract_notification_lines(combined),
        }
        return json.dumps(structured, ensure_ascii=False), popup_json
    except Exception as e:
        return f"[ERROR fetching page: {e}]", None


def visit_fallback_program_page(page, url):
    """Old, looser heading-based extraction -- only used when card-based
    discovery finds nothing anywhere on a site."""
    try:
        ok, combined = goto_and_settle(page, url)
        if not ok:
            return SCRAPE_BLOCKED_SENTINEL, None
        try:
            programs = dedupe_programs(page.evaluate(PROGRAM_HEADING_FALLBACK_JS) or [])
        except Exception:
            programs = []
        try:
            fees = dedupe_fees(page.evaluate(FEE_TABLE_JS) or [])
        except Exception:
            fees = []
        structured = {"programs": programs, "fees": fees, "notifications": extract_notification_lines(combined)}
        return json.dumps(structured, ensure_ascii=False), None
    except Exception as e:
        return f"[ERROR fetching page: {e}]", None


def visit_generic_page(page, url):
    """For dedicated fee/notification pages discovered by keyword (not a
    program card): generic fee-table + notification-line extraction."""
    try:
        ok, combined = goto_and_settle(page, url)
        popup_text = capture_and_close_popups(page)
        popup_json = None
        if popup_text and not is_blocked_page(popup_text):
            popup_json = json.dumps(
                {"programs": [], "fees": [], "notifications": extract_notification_lines(clean_lines(popup_text))},
                ensure_ascii=False)
        if not ok:
            return SCRAPE_BLOCKED_SENTINEL, popup_json
        try:
            fees = dedupe_fees(page.evaluate(FEE_TABLE_JS) or [])
        except Exception:
            fees = []
        structured = {"programs": [], "fees": fees, "notifications": extract_notification_lines(combined)}
        return json.dumps(structured, ensure_ascii=False), popup_json
    except Exception as e:
        return f"[ERROR fetching page: {e}]", None


def scrape_university(browser, uni):
    result = {"programs": {}, "fees": {}, "notifications": {}}
    context = None
    try:
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page = context.new_page()

        # 1) Homepage: notifications/popup + discover program cards
        home_content, home_popup, home_cards = visit_listing_page(page, uni["url"])
        result["notifications"][uni["url"]] = home_content
        if home_popup:
            result["notifications"][f"{uni['url']} [popup]"] = home_popup

        # 2) Hub pages: "Online Programs", "ODL Programs", "Degree with
        #    Apprenticeship" etc -- discover more program cards from each
        hub_urls = uni.get("hub_urls") or discover_links(page, uni["url"], PROGRAM_KEYWORDS, MAX_HUB_PAGES)
        all_cards = list(home_cards)
        for hub_url in hub_urls:
            if hub_url == uni["url"]:
                continue
            hub_content, hub_popup, hub_cards = visit_listing_page(page, hub_url)
            result["notifications"][hub_url] = hub_content
            if hub_popup:
                result["notifications"][f"{hub_url} [popup]"] = hub_popup
            all_cards.extend(hub_cards)

        # Dedupe cards by name, keep first href seen, cap total
        seen_names = {}
        for c in all_cards:
            name = (c.get("name") or "").strip()
            href = (c.get("href") or "").strip()
            if name and href and name.lower() not in seen_names:
                seen_names[name.lower()] = (name, href)
        unique_cards = list(seen_names.values())[:MAX_PROGRAM_CARDS]

        if unique_cards:
            # 3) Visit each real program's own detail page
            for name, href in unique_cards:
                content, popup = visit_program_detail_page(page, href, name)
                result["programs"][href] = content
                if popup:
                    result["programs"][f"{href} [popup]"] = popup
        else:
            # Fallback: card heuristic found nothing anywhere on this site --
            # use the looser heading-based approach so we're not left empty.
            fallback_urls = uni.get("program_urls") or (hub_urls if hub_urls else [uni["url"]])
            for url in fallback_urls:
                content, _ = visit_fallback_program_page(page, url)
                result["programs"][url] = content

        # 4) Any dedicated fee-keyword pages not already covered above
        fee_urls = uni.get("fee_urls") or discover_links(page, uni["url"], FEE_KEYWORDS, MAX_FEE_PAGES)
        already_visited = set(result["programs"].keys()) | {uni["url"]} | set(hub_urls)
        for url in fee_urls:
            if url in already_visited:
                continue
            content, popup = visit_generic_page(page, url)
            result["fees"][url] = content
            if popup:
                result["fees"][f"{url} [popup]"] = popup

        # 5) Any dedicated notification-keyword pages not already covered
        notification_urls = uni.get("notification_urls") or discover_links(
            page, uni["url"], NOTIFICATION_KEYWORDS, MAX_NOTIFICATION_PAGES)
        already_visited |= set(fee_urls)
        for url in notification_urls:
            if url in already_visited:
                continue
            content, popup = visit_generic_page(page, url)
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
