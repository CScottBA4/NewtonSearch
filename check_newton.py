import os
import re
import json
import hashlib
import smtplib
from io import BytesIO
from email.message import EmailMessage
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF


SEARCH_TERMS = [
    "162 Clark Street",
    "162 Clark St",
    "Clark Street",
    "Clark St",
]

START_URLS = [
    "https://www.newtonma.gov/government/electronic-posting-board",
    "https://www.newtonma.gov/government/city-clerk/city-council",
    "https://www.newtonma.gov/how-do-i/view/city-council-dockets",
    "https://www.newtonma.gov/government/city-clerk/city-council/friday-packet",
    "https://www.newtonma.gov/government/city-clerk/city-council/calendar-news/calendar",
    "https://www.newtonma.gov/government/planning",
    "https://www.newtonma.gov/government/planning/boards-commissions/planning-and-development-board",
    "https://www.newtonma.gov/government/planning/zoning-board-of-appeals",
    "https://www.newtonma.gov/government/public-works",
]

ALLOWED_DOMAIN = "www.newtonma.gov"

RELEVANT_LINK_KEYWORDS = [
    "agenda", "minutes", "meeting", "notice", "docket", "packet",
    "calendar", "hearing", "planning", "zoning", "land use",
    "public works", "traffic", "committee", "board", "commission",
    "archive", "archives", "public safety", "transportation",
    "public facilities", "engineering", "construction", "sidewalk",
    "special permit", "variance",
]

MAX_PAGES_TO_CRAWL = 1000
MAX_PDFS_TO_READ = 1000
SEEN_FILE = "seen_matches.json"

ALERT_EMAIL_TO = os.environ["ALERT_EMAIL_TO"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def normalize_url(url):
    url, _ = urldefrag(url)
    return url.strip()


def is_allowed_url(url):
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower() == ALLOWED_DOMAIN
    except Exception:
        return False


def is_pdf_url(url):
    clean = url.lower().split("?")[0]
    return (
        clean.endswith(".pdf")
        or "/home/showpublisheddocument/" in clean
    )


def looks_relevant(text, url):
    combined = f"{text} {url}".lower()
    return any(keyword in combined for keyword in RELEVANT_LINK_KEYWORDS)


def fetch(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.6 Safari/605.1.15"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.newtonma.gov/",
    }

    r = requests.get(url, headers=headers, timeout=45)
    r.raise_for_status()
    return r


def matching_terms(text):
    lower = text.lower()
    return [term for term in SEARCH_TERMS if term.lower() in lower]


def make_id(term, url):
    return hashlib.sha256(f"{term}|{url}".encode()).hexdigest()


def extract_context(text, term, window=250):
    lower = text.lower()
    idx = lower.find(term.lower())

    if idx == -1:
        return ""

    start = max(0, idx - window)
    end = min(len(text), idx + len(term) + window)

    return re.sub(r"\s+", " ", text[start:end]).strip()


def send_email(term, title, url, context=""):
    msg = EmailMessage()
    msg["Subject"] = f"Newton mention found: {term}"
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO

    body = f"""A Newton city document or page mentioned one of your search terms.

Matched term:
{term}

Title:
{title}

URL:
{url}
"""

    if context:
        body += f"""
Nearby text:
{context}
"""

    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def extract_page(url):
    r = fetch(url)
    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else url
    page_text = soup.get_text(" ", strip=True)

    discovered_pages = []
    discovered_pdfs = []

    for a in soup.find_all("a", href=True):
        href = normalize_url(urljoin(url, a["href"]))
        text = a.get_text(" ", strip=True)

        if not is_allowed_url(href):
            continue

        if is_pdf_url(href):
            discovered_pdfs.append((text or "Newton PDF", href))
        elif looks_relevant(text, href):
            discovered_pages.append(href)

    return title, page_text, discovered_pages, discovered_pdfs


def extract_pdf_text(url):
    r = fetch(url)

    text_parts = []

    with fitz.open(stream=r.content, filetype="pdf") as doc:
        for page in doc:
            text_parts.append(page.get_text() or "")

    return "\n".join(text_parts)


def handle_matches(text, title, url, seen):
    new_match_count = 0

    for term in matching_terms(text):
        mid = make_id(term, url)

        if mid not in seen:
            context = extract_context(text, term)
            send_email(term, title, url, context)
            seen.add(mid)
            new_match_count += 1

    return new_match_count


def main():
    seen = load_seen()

    pages_to_visit = [normalize_url(x) for x in START_URLS]
    visited_pages = set()
    visited_pdfs = set()
    queued_pdfs = set()
    pdf_queue = []

    page_match_count = 0
    pdf_match_count = 0
    pdf_text_success = 0
    pdf_text_empty = 0
    pdf_read_failures = 0
    discovered_pdf_count = 0

    while pages_to_visit and len(visited_pages) < MAX_PAGES_TO_CRAWL:
        url = pages_to_visit.pop(0)

        if url in visited_pages:
            continue

        visited_pages.add(url)

        print(f"Checking page: {url}", flush=True)

        try:
            title, text, pages, pdfs = extract_page(url)
        except Exception:
            continue

        page_match_count += handle_matches(text, title, url, seen)

        for page in pages:
            if page not in visited_pages and page not in pages_to_visit:
                pages_to_visit.append(page)

        for pdf_title, pdf_url in pdfs:
            if pdf_url not in queued_pdfs:
                queued_pdfs.add(pdf_url)
                discovered_pdf_count += 1
                pdf_queue.append((pdf_title, pdf_url))

    count = 0

    for title, pdf_url in pdf_queue:
        if count >= MAX_PDFS_TO_READ:
            break

        if pdf_url in visited_pdfs:
            continue

        visited_pdfs.add(pdf_url)
        count += 1

        try:
            pdf_text = extract_pdf_text(pdf_url)
        except Exception:
            pdf_read_failures += 1
            continue

        if pdf_text.strip():
            pdf_text_success += 1
        else:
            pdf_text_empty += 1

        pdf_match_count += handle_matches(pdf_text, title, pdf_url, seen)

    save_seen(seen)

    print("")
    print("----- SUMMARY -----")
    print(f"Visited pages: {len(visited_pages)}")
    print(f"Discovered unique PDF/document links: {discovered_pdf_count}")
    print(f"Visited PDFs/documents: {len(visited_pdfs)}")
    print(f"PDFs/documents with readable text: {pdf_text_success}")
    print(f"PDFs/documents with empty/unreadable text: {pdf_text_empty}")
    print(f"PDF/document read failures: {pdf_read_failures}")
    print(f"New webpage matches emailed: {page_match_count}")
    print(f"New PDF/document matches emailed: {pdf_match_count}")
    print(f"Total new matches emailed: {page_match_count + pdf_match_count}")
    print("-------------------")


if __name__ == "__main__":
    main()
