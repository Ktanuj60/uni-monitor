"""
diff_and_alert.py
Compares the NEW structured snapshot (Programs+Specializations, Fees,
Notifications -- produced by scraper.py's free, rule-based extraction)
against the OLD one, and builds a clean report grouped exactly as:
University -> Program -> Specializations, University -> Fees -> item:
old -> new, University -> Offers/Discounts/Scholarships -> what's new/gone.

Each change carries the source URL(s) it was found on. Identical changes
seen on more than one page for the same university (e.g. a program listed
on both the homepage and a dedicated programs page) are merged into one
line instead of appearing as duplicates.
"""

import os
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from scraper import SCRAPE_BLOCKED_SENTINEL

CATEGORY_LABELS = {
    "programs": "Programs & Specializations",
    "fees": "Fees",
    "notifications": "Offers, Discounts, Scholarships & Announcements",
}


def _diff_programs(old_programs, new_programs):
    old_map = {
        (p.get("name") or "").strip(): set(s.strip() for s in p.get("specializations", []) if s.strip())
        for p in old_programs if (p.get("name") or "").strip()
    }
    new_map = {
        (p.get("name") or "").strip(): set(s.strip() for s in p.get("specializations", []) if s.strip())
        for p in new_programs if (p.get("name") or "").strip()
    }

    changes = []
    for name, specs in new_map.items():
        if name not in old_map:
            changes.append({"status": "new_program", "program": name, "specializations": sorted(specs)})
        else:
            added = sorted(specs - old_map[name])
            removed = sorted(old_map[name] - specs)
            if added or removed:
                changes.append({"status": "updated", "program": name,
                                 "added_specs": added, "removed_specs": removed})
    for name in old_map:
        if name not in new_map:
            changes.append({"status": "removed_program", "program": name})
    return changes


def _diff_fees(old_fees, new_fees):
    def to_map(fees):
        m = {}
        for f in fees:
            label = (f.get("item") or "").strip()
            if not label:
                continue
            m[label.lower()] = (label, (f.get("amount") or "").strip())
        return m

    old_map, new_map = to_map(old_fees), to_map(new_fees)
    changes = []
    for key, (label, amount) in new_map.items():
        if key not in old_map:
            changes.append({"status": "new", "item": label, "amount": amount})
        else:
            _, old_amount = old_map[key]
            if old_amount != amount:
                changes.append({"status": "changed", "item": label,
                                 "old_amount": old_amount, "new_amount": amount})
    for key, (label, amount) in old_map.items():
        if key not in new_map:
            changes.append({"status": "removed", "item": label, "amount": amount})
    return changes


def _diff_notifications(old_list, new_list):
    old_set = set(s.strip() for s in old_list if s.strip())
    new_set = set(s.strip() for s in new_list if s.strip())
    return sorted(new_set - old_set), sorted(old_set - new_set)


def build_report(university_name, page_type, url, old_content, new_content):
    if new_content == SCRAPE_BLOCKED_SENTINEL:
        return None
    if old_content is None or old_content == SCRAPE_BLOCKED_SENTINEL:
        return None

    try:
        old_data = json.loads(old_content)
        new_data = json.loads(new_content)
    except Exception:
        return None

    program_changes = _diff_programs(old_data.get("programs", []), new_data.get("programs", []))
    fee_changes = _diff_fees(old_data.get("fees", []), new_data.get("fees", []))
    notif_added, notif_removed = _diff_notifications(
        old_data.get("notifications", []), new_data.get("notifications", []))

    if not program_changes and not fee_changes and not notif_added and not notif_removed:
        return None

    return {
        "university": university_name,
        "page_type": page_type,
        "url": url,
        "program_changes": program_changes,
        "fee_changes": fee_changes,
        "notif_added": notif_added,
        "notif_removed": notif_removed,
    }


def _dedupe_with_urls(pairs, sig_func):
    """pairs: list of (item_dict, url). Merges items with identical content
    (regardless of which page they came from) and keeps the set of URLs
    each was seen on."""
    seen, order = {}, []
    for item, url in pairs:
        key = sig_func(item)
        if key not in seen:
            seen[key] = {"item": item, "urls": []}
            order.append(key)
        if url not in seen[key]["urls"]:
            seen[key]["urls"].append(url)
    return [seen[k] for k in order]


def _links_html(urls, max_links=2):
    shown = urls[:max_links]
    extra = len(urls) - len(shown)
    links = ", ".join(f"<a href='{u}' style='font-size:11px;color:#888;'>source</a>" for u in shown)
    if extra > 0:
        links += f" <span style='font-size:11px;color:#888;'>(+{extra} more page(s))</span>"
    return links


def _render_programs(parts, deduped):
    for entry in deduped:
        c, links = entry["item"], _links_html(entry["urls"])
        if c["status"] == "new_program":
            parts.append(f"<p style='margin:6px 0 2px;'><b style='color:green;'>New Program:</b> {c['program']} {links}</p>")
            if c["specializations"]:
                parts.append("<ul>" + "".join(f"<li>Specialization: {s}</li>" for s in c["specializations"]) + "</ul>")
        elif c["status"] == "removed_program":
            parts.append(f"<p style='margin:6px 0 2px;'><b style='color:crimson;'>Removed Program:</b> {c['program']} {links}</p>")
        elif c["status"] == "updated":
            parts.append(f"<p style='margin:6px 0 2px;'><b>{c['program']}</b> {links}</p><ul>")
            for s in c["added_specs"]:
                parts.append(f"<li style='color:green;'>New specialization: {s}</li>")
            for s in c["removed_specs"]:
                parts.append(f"<li style='color:crimson;'>Removed specialization: {s}</li>")
            parts.append("</ul>")


def _render_fees(parts, deduped):
    parts.append("<ul>")
    for entry in deduped:
        c, links = entry["item"], _links_html(entry["urls"])
        if c["status"] == "new":
            parts.append(f"<li style='color:green;'>New fee item: <b>{c['item']}</b> &mdash; {c['amount']} {links}</li>")
        elif c["status"] == "changed":
            parts.append(f"<li><b>{c['item']}</b>: <s style='color:#999;'>{c['old_amount']}</s> "
                         f"&rarr; <b style='color:#155;'>{c['new_amount']}</b> {links}</li>")
        elif c["status"] == "removed":
            parts.append(f"<li style='color:crimson;'>Removed fee item: <b>{c['item']}</b> (was {c['amount']}) {links}</li>")
    parts.append("</ul>")


def _render_notifications(parts, added_deduped, removed_deduped):
    if added_deduped:
        parts.append("<p style='color:green;margin:4px 0;'><b>New:</b></p><ul>")
        for entry in added_deduped:
            parts.append(f"<li>{entry['item']} {_links_html(entry['urls'])}</li>")
        parts.append("</ul>")
    if removed_deduped:
        parts.append("<p style='color:crimson;margin:4px 0;'><b>Removed:</b></p><ul>")
        for entry in removed_deduped:
            parts.append(f"<li>{entry['item']} {_links_html(entry['urls'])}</li>")
        parts.append("</ul>")


def format_email_html(reports, run_label):
    if not reports:
        return None

    by_uni = {}
    for r in reports:
        by_uni.setdefault(r["university"], []).append(r)

    parts = [f"<h2>University Change Alert &mdash; {run_label}</h2>"]
    parts.append(f"<p>{len(reports)} page(s) changed across {len(by_uni)} universit(y/ies).</p>")

    for uni, uni_reports in by_uni.items():
        parts.append(f"<h2 style='border-bottom:2px solid #333;padding-bottom:4px;'>{uni}</h2>")

        program_pairs, fee_pairs = [], []
        notif_added_pairs, notif_removed_pairs = [], []
        for r in uni_reports:
            for c in r["program_changes"]:
                program_pairs.append((c, r["url"]))
            for c in r["fee_changes"]:
                fee_pairs.append((c, r["url"]))
            for s in r["notif_added"]:
                notif_added_pairs.append((s, r["url"]))
            for s in r["notif_removed"]:
                notif_removed_pairs.append((s, r["url"]))

        deduped_programs = _dedupe_with_urls(program_pairs, lambda c: json.dumps(c, sort_keys=True))
        deduped_fees = _dedupe_with_urls(fee_pairs, lambda c: json.dumps(c, sort_keys=True))
        deduped_notif_added = _dedupe_with_urls(notif_added_pairs, lambda s: s)
        deduped_notif_removed = _dedupe_with_urls(notif_removed_pairs, lambda s: s)

        if deduped_programs:
            parts.append(f"<h3 style='color:#2a5;'>{CATEGORY_LABELS['programs']}</h3>")
            _render_programs(parts, deduped_programs)
        if deduped_fees:
            parts.append(f"<h3 style='color:#2a5;'>{CATEGORY_LABELS['fees']}</h3>")
            _render_fees(parts, deduped_fees)
        if deduped_notif_added or deduped_notif_removed:
            parts.append(f"<h3 style='color:#2a5;'>{CATEGORY_LABELS['notifications']}</h3>")
            _render_notifications(parts, deduped_notif_added, deduped_notif_removed)

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
