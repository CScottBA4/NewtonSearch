import os
import re
import json
import hashlib
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup
import fitz  # PyMuPDF
from dotenv import load_dotenv

load_dotenv()


SEARCH_GROUPS = {
    "162 Clark Street": [
        "162 Clark Street",
        "162 Clark St",
    ],
    "Clark Street": [
        "Clark Street",
        "Clark St",
    ],
}

# Preserve the exact old search terms so existing seen_matches.json hashes can
# be recognized during the one-time migration to the new state format.
LEGACY_SEARCH_TERMS = [
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
CONTEXT_WINDOW = 250
MAX_CONTEXTS_IN_EMAIL = 5

ALERT_EMAIL_TO = os.environ["ALERT_EMAIL_TO"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]


def empty_state():
    return {
        "version": 2,
        "legacy_ids": set(),
        "matches": {},
        "last_failure_fingerprint": "",
    }


def load_seen():
    """Load state, including backward compatibility with the old list format."""
    state = empty_state()

    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state

    if isinstance(data, list):
        state["legacy_ids"] = set(data)
        return state

    if isinstance(data, dict):
        state["legacy_ids"] = set(data.get("legacy_ids", []))
        state["matches"] = data.get("matches", {})
        state["last_failure_fingerprint"] = data.get(
            "last_failure_fingerprint", ""
        )

    return state


def save_seen(state):
    serializable = {
        "version": 2,
        "legacy_ids": sorted(state["legacy_ids"]),
        "matches": state["matches"],
        "last_failure_fingerprint": state["last_failure_fingerprint"],
    }

    with open(SEEN_FILE, "w") as f:
        json.dump(serializable, f, indent=2, sort_keys=True)


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
    return clean.endswith(".pdf") or "/home/showpublisheddocument/" in clean


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


def contains_term(text, term):
    return term.lower() in text.lower()


def matching_category(text):
    """Return one category per page/document, with the specific address first."""
    for category in ("162 Clark Street", "Clark Street"):
        if any(contains_term(text, term) for term in SEARCH_GROUPS[category]):
            return category
    return None


def legacy_make_id(term, url):
    return hashlib.sha256(f"{term}|{url}".encode()).hexdigest()


def legacy_terms_for_category(category):
    if category == "162 Clark Street":
        return SEARCH_GROUPS["162 Clark Street"]
    return SEARCH_GROUPS["Clark Street"]


def was_seen_in_legacy_state(category, text, url, legacy_ids):
    for term in legacy_terms_for_category(category):
        if contains_term(text, term):
            if legacy_make_id(term, url) in legacy_ids:
                return True
    return False


def extract_contexts(text, terms, window=CONTEXT_WINDOW):
    contexts = []
    seen_contexts = set()

    for term in terms:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        for match in pattern.finditer(text):
            start = max(0, match.start() - window)
            end = min(len(text), match.end() + window)
            context = re.sub(r"\s+", " ", text[start:end]).strip()
            if context and context not in seen_contexts:
                seen_contexts.add(context)
                contexts.append(context)

    return contexts


def make_match_key(category, url):
    return hashlib.sha256(f"{category}|{url}".encode()).hexdigest()


def make_content_fingerprint(category, contexts):
    material = category + "\n" + "\n---\n".join(sorted(contexts))
    return hashlib.sha256(material.encode()).hexdigest()


def send_email(category, title, url, contexts, is_change=False):
    msg = EmailMessage()

    if category == "162 Clark Street":
        msg["Subject"] = f"162 Clark Street update: {title}"
        description = (
            "A Newton city document or page specifically mentions "
            "162 Clark Street."
        )
    else:
        msg["Subject"] = f"Clark Street update: {title}"
        description = (
            "A Newton city document or page mentions Clark Street "
            "(not specifically 162 Clark Street)."
        )

    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO

    update_type = (
        "The relevant Clark Street text has changed since the last check."
        if is_change
        else "This is a newly detected mention."
    )

    body = f"""{description}

{update_type}

Category:
{category}

Title:
{title}

URL:
{url}
"""

    if contexts:
        body += "\nNearby text:\n"
        for i, context in enumerate(contexts[:MAX_CONTEXTS_IN_EMAIL], start=1):
            if len(contexts) > 1:
                body += f"\n[{i}] {context}\n"
            else:
                body += f"\n{context}\n"

        if len(contexts) > MAX_CONTEXTS_IN_EMAIL:
            body += (
                f"\n({len(contexts) - MAX_CONTEXTS_IN_EMAIL} additional "
                "matching context(s) omitted from this email.)\n"
            )

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


def handle_match(text, title, url, state):
    category = matching_category(text)

    if category is None:
        return 0

    contexts = extract_contexts(text, SEARCH_GROUPS[category])
    if not contexts:
        contexts = [category]

    key = make_match_key(category, url)
    fingerprint = make_content_fingerprint(category, contexts)
    previous = state["matches"].get(key)

    if previous is not None:
        if previous.get("fingerprint") == fingerprint:
            return 0

        send_email(category, title, url, contexts, is_change=True)
        state["matches"][key] = {
            "category": category,
            "url": url,
            "fingerprint": fingerprint,
        }
        return 1

    if was_seen_in_legacy_state(category, text, url, state["legacy_ids"]):
        state["matches"][key] = {
            "category": category,
            "url": url,
            "fingerprint": fingerprint,
        }
        return 0

    send_email(category, title, url, contexts, is_change=False)
    state["matches"][key] = {
        "category": category,
        "url": url,
        "fingerprint": fingerprint,
    }
    return 1


def failure_fingerprint(failures):
    if not failures:
        return ""
    normalized = sorted(f"{kind}|{url}|{error}" for kind, url, error in failures)
    return hashlib.sha256("\n".join(normalized).encode()).hexdigest()


def report_failures(failures, state):
    """Log crawl/read failures only. Never send warning emails."""
    print(f"Crawl failures: {len(failures)}")

    for kind, url, error in failures:
        print(f"  - {kind}: {url}")
        print(f"    {error}")

    state["last_failure_fingerprint"] = failure_fingerprint(failures)


def main():
    state = load_seen()

    pages_to_visit = [normalize_url(x) for x in START_URLS]
    visited_pages = set()
    visited_pdfs = set()
    queued_pdfs = set()
    pdf_queue = []
    failures = []

    page_match_count = 0
    pdf_match_count = 0
    pdf_text_success = 0
    pdf_text_empty = 0
    discovered_pdf_count = 0

    while pages_to_visit and len(visited_pages) < MAX_PAGES_TO_CRAWL:
        url = pages_to_visit.pop(0)

        if url in visited_pages:
            continue

        visited_pages.add(url)
        print(f"Checking page: {url}", flush=True)

        try:
            title, text, pages, pdfs = extract_page(url)
        except Exception as exc:
            failures.append(("Webpage", url, repr(exc)))
            continue

        page_match_count += handle_match(text, title, url, state)

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
        except Exception as exc:
            failures.append(("PDF/document", pdf_url, repr(exc)))
            continue

        if pdf_text.strip():
            pdf_text_success += 1
        else:
            pdf_text_empty += 1
            failures.append(
                (
                    "PDF/document unreadable",
                    pdf_url,
                    "No extractable text was found; this may be a scanned "
                    "or image-only PDF that would require OCR.",
                )
            )

        pdf_match_count += handle_match(pdf_text, title, pdf_url, state)

    print("")
    print("----- SUMMARY -----")
    print(f"Visited pages: {len(visited_pages)}")
    print(f"Discovered unique PDF/document links: {discovered_pdf_count}")
    print(f"Visited PDFs/documents: {len(visited_pdfs)}")
    print(f"PDFs/documents with readable text: {pdf_text_success}")
    print("PDFs/documents with empty/unreadable text: " f"{pdf_text_empty}")
    print(f"New webpage matches emailed: {page_match_count}")
    print(f"New PDF/document matches emailed: {pdf_match_count}")
    print(
        "Total new/changed matches emailed: "
        f"{page_match_count + pdf_match_count}"
    )
    report_failures(failures, state)
    print("-------------------")

    save_seen(state)


if __name__ == "__main__":
    main()
