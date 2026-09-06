"""
diff_and_alert.py
Compares new scrape results against the stored snapshot, builds a
human-readable change report (line-level, so individual fee items or
specializations show up clearly), and emails it.

First-ever run for a given (university, page) has nothing to compare
against -- that's treated as a baseline, not reported as a "change",
to avoid a giant false-positive alert on day one.
"""

import os
import difflib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

MAX_DIFF_LINES_PER_PAGE = 40  # keep emails readable; full history is always in the Sheet


def diff_content(old_text, new_text):
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = [l[1:].strip() for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in diff if l.startswith("-") and not l.startswith("---")]
    return added, removed


def build_report(university_name, page_type, url, old_content, new_content):
    if old_content is None:
        return None  # baseline, not a change
    if old_content.strip() == new_content.strip():
        return None  # no change
    added, removed = diff_content(old_content, new_content)
    if not added and not removed:
        return None
    return {
        "university": university_name,
        "page_type": page_type,
        "url": url,
        "added": added[:MAX_DIFF_LINES_PER_PAGE],
        "removed": removed[:MAX_DIFF_LINES_PER_PAGE],
    }


def format_email_html(reports, run_label):
    if not reports:
        return None
    by_uni = {}
    for r in reports:
        by_uni.setdefault(r["university"], []).append(r)

    parts = [f"<h2>University Program/Fee Change Alert &mdash; {run_label}</h2>"]
    parts.append(f"<p>{len(reports)} page(s) changed across {len(by_uni)} universit(y/ies).</p>")

    for uni, uni_reports in by_uni.items():
        parts.append(f"<h3>{uni}</h3>")
        for r in uni_reports:
            parts.append(f"<p><b>{r['page_type'].upper()}</b> &mdash; "
                         f"<a href='{r['url']}'>{r['url']}</a></p>")
            if r["added"]:
                parts.append("<p style='color:green;margin:4px 0;'><b>Added:</b></p><ul>")
                for line in r["added"]:
                    parts.append(f"<li>{line}</li>")
                parts.append("</ul>")
            if r["removed"]:
                parts.append("<p style='color:crimson;margin:4px 0;'><b>Removed:</b></p><ul>")
                for line in r["removed"]:
                    parts.append(f"<li>{line}</li>")
                parts.append("</ul>")
    return "\n".join(parts)


def send_email(html_body, subject):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    to_addrs = [a.strip() for a in os.environ["ALERT_EMAIL_TO"].split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, to_addrs, msg.as_string())
