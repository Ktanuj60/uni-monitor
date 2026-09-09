"""
noise_filter.py
Strips known non-content noise out of scraped text before it's stored or
diffed, and detects when a page is actually a bot-protection challenge
page (Cloudflare, etc.) rather than real content -- so those never get
reported as "changes."
"""

import re

NOISE_LINE_PATTERNS = [
    r"^ray id",
    r"^select (state|city|program|course|specialization|city/town)$",
    r"^i allow .* to contact me\.?$",
    r"^loading(\.\.\.)?$",
    r"^please wait(\.\.\.)?$",
    r"^we use cookies",
    r"^accept cookies",
    r"^cookie policy",
    r"^this site uses cookies",
    r"^captcha$",
    r"^verify you are human",
    r"^enable javascript",
]

BLOCKED_PAGE_MARKERS = [
    "checking your browser",
    "attention required",
    "just a moment",
    "cloudflare ray id",
    "please verify you are human",
    "enable javascript and cookies",
    "ddos protection by",
    "sorry, you have been blocked",
]


def clean_lines(text):
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(re.match(p, low) for p in NOISE_LINE_PATTERNS):
            continue
        cleaned.append(s)
    return "\n".join(cleaned)


def is_blocked_page(text):
    low = text.lower()
    return any(marker in low for marker in BLOCKED_PAGE_MARKERS)
