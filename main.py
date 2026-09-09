import json
import datetime
from scraper import scrape_all, SCRAPE_BLOCKED_SENTINEL
from sheets_store import load_previous_snapshot, save_all_snapshots
from diff_and_alert import build_report, format_email_html, send_email


def run():
    with open("universities.json") as f:
        universities = json.load(f)

    now = datetime.datetime.utcnow()
    run_label = now.strftime("%Y-%m-%d %H:%M UTC")
    timestamp = now.isoformat()

    print("Loading previous snapshot from Google Sheet...")
    previous, ws = load_previous_snapshot()

    print("Scraping all universities (this can take a while for 30 sites)...")
    scraped = scrape_all(universities)

    reports = []
    records_to_save = []

    for uni_name, pages in scraped.items():
        for page_type in ("programs", "fees", "notifications"):
            for url, new_content in pages.get(page_type, {}).items():
                if url == "_error":
                    continue
                if new_content == SCRAPE_BLOCKED_SENTINEL:
                    # Hit a bot-check/challenge page instead of real content.
                    # Don't diff it, don't overwrite the last good snapshot.
                    print(f"  Skipped (bot-check page): {uni_name} / {page_type} / {url}")
                    continue

                key = (uni_name, page_type, url)
                old = previous.get(key)
                old_content = old["content"] if old else None

                report = build_report(uni_name, page_type, url, old_content, new_content)
                if report:
                    reports.append(report)

                records_to_save.append({
                    "university": uni_name,
                    "page_type": page_type,
                    "url": url,
                    "content": new_content,
                    "timestamp": timestamp,
                })

    print(f"Saving {len(records_to_save)} page snapshot(s) to Google Sheet (batched)...")
    save_all_snapshots(ws, previous, records_to_save)

    if reports:
        print(f"{len(reports)} change(s) detected. Sending email...")
        html = format_email_html(reports, run_label)
        send_email(html, subject=f"[Uni Monitor] {len(reports)} change(s) detected \u2014 {run_label}")
    else:
        print("No changes detected this run.")


if __name__ == "__main__":
    run()
