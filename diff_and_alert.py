"""
diff_and_alert.py
Compares new scrape results against the stored snapshot and builds a
clean, human-readable change report grouped exactly as:
  University -> Category (Programs & Specializations / Fees / Offers,
  Discounts & Scholarships) -> what changed.

Key fixes vs a naive line-diff:
- Content is compared as a SET of lines, not a strict sequence. This means
  a page that just reordered its content (a very common source of noisy
  false positives on JS-heavy sites) produces NO false "changes" -- only
  lines that are genuinely new or genuinely gone are reported.
- Where an added line and a removed line look like the same field with a
  different value (e.g. same label before a colon, or same first few
  words), they're paired into one "Changed: old -> new" line instead of
  being shown as two separate, confusing bullets.

First-ever run for a given (university, page) has nothing to compare
against -- that's treated as a baseline, not reported as a "change".
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from scraper import SCRAPE_BLOCKED_SENTINEL

MAX_LINES_PER_SECTION = 30  # keep emails readable; full history is always in the Sheet

CATEGORY_LABELS = {
    "programs": "Programs & Specializations",
    "fees": "Fees",
    "notifications": "Offers, Discounts, Scholarships & Announcements",
}


def _key_of(line):
    """A rough 'label' for a line, used to pair an old value with its new
    value (e.g. 'Registration Fee: 5000' and 'Registration Fee: 5500' both
    key to 'registration fee')."""
    if ":" in line:
        return line.split(":", 1)[0].strip().lower()
    return " ".join(line.split()[:5]).lower()


def diff_content(old_text, new_text):
    """Set-based diff: ignores pure reordering, only flags lines that are
    genuinely new or genuinely gone."""
    old_lines = list(dict.fromkeys(l.strip() for l in old_text.splitlines() if l.strip()))
    new_lines = list(dict.fromkeys(l.strip() for l in new_text.splitlines() if l.strip()))
    old_set, new_set = set(old_lines), set(new_lines)
    added = [l for l in new_lines if l not in old_set]
    removed = [l for l in old_lines if l not in new_set]
    return added, removed


def pair_changes(added, removed):
    """Pairs up added/removed lines that share the same label so they show
    as one clean 'Changed: old -> new' entry instead of two bullets."""
    removed_by_key = {}
    for r in removed:
        removed_by_key.setdefault(_key_of(r), []).append(r)

    pairs, leftover_added = [], []
    for a in added:
        k = _key_of(a)
        bucket = removed_by_key.get(k)
        if bucket:
            pairs.append((bucket.pop(0), a))
        else:
            leftover_added.append(a)

    leftover_removed = [r for bucket in removed_by_key.values() for r in bucket]
    return pairs, leftover_added, leftover_removed


def build_report(university_name, page_type, url, old_content, new_content):
    if new_content == SCRAPE_BLOCKED_SENTINEL:
        return None  # bot-check page hit instead of real content; not a real change
    if old_content is None or old_content == SCRAPE_BLOCKED_SENTINEL:
        return None  # baseline (nothing valid to compare against yet)
    if old_content.strip() == new_content.strip():
        return None

    added, removed = diff_content(old_content, new_content)
    if not added and not removed:
        return None  # only reordering happened -- not a real change

    pairs, only_added, only_removed = pair_changes(added, removed)
    if not pairs and not only_added and not only_removed:
        return None

    return {
        "university": university_name,
        "page_type": page_type,
        "url": url,
        "pairs": pairs[:MAX_LINES_PER_SECTION],
        "added": only_added[:MAX_LINES_PER_SECTION],
        "removed": only_removed[:MAX_LINES_PER_SECTION],
    }


def format_email_html(reports, run_label):
    if not reports:
        return None

    by_uni = {}
    for r in reports:
        by_uni.setdefault(r["university"], {}).setdefault(r["page_type"], []).append(r)

    parts = [f"<h2>University Change Alert &mdash; {run_label}</h2>"]
    parts.append(f"<p>{len(reports)} page(s) changed across {len(by_uni)} universit(y/ies).</p>")

    for uni, categories in by_uni.items():
        parts.append(f"<h2 style='border-bottom:2px solid #333;padding-bottom:4px;'>{uni}</h2>")
        for page_type in ("programs", "fees", "notifications"):
            if page_type not in categories:
                continue
            parts.append(f"<h3 style='color:#2a5;'>{CATEGORY_LABELS[page_type]}</h3>")
            for r in categories[page_type]:
                parts.append(f"<p style='margin:2px 0;'><a href='{r['url']}'>{r['url']}</a></p>")
                if r["pairs"]:
                    parts.append("<ul>")
                    for old, new in r["pairs"]:
                        parts.append(f"<li><s style='color:#999;'>{old}</s> &rarr; "
                                     f"<b style='color:#155;'>{new}</b></li>")
                    parts.append("</ul>")
                if r["added"]:
                    parts.append("<p style='color:green;margin:4px 0;'><b>New:</b></p><ul>")
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
