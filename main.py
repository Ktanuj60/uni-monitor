import json
import datetime
from scraper import scrape_all
from sheets_store import load_previous_snapshot, save_snapshot
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
    for uni_name, pages in scraped.items():
        for page_type in ("programs", "fees"):
            for url, new_content in pages.get(page_type, {}).items():
                if url == "_error":
                    continue
                key = (uni_name, page_type, url)
                old = previous.get(key)
                old_content = old["content"] if old else None

                report = build_report(uni_name, page_type, url, old_content, new_content)
                if report:
                    reports.append(report)

                save_snapshot(ws, previous, uni_name, page_type, url, new_content, timestamp)

    if reports:
        print(f"{len(reports)} change(s) detected. Sending email...")
        html = format_email_html(reports, run_label)
        send_email(html, subject=f"[Uni Monitor] {len(reports)} change(s) detected \u2014 {run_label}")
    else:
        print("No changes detected this run.")


if __name__ == "__main__":
    run()
