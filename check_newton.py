import io
import os
import re
import json
import hashlib
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin, urldefrag, urlparse

import fitz  # PyMuPDF
import pytesseract
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image

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
# be recognized during migration from the original state format.
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

# Keep the webpage crawl at its existing breadth, but make PDF coverage
# incremental rather than repeatedly reading only the first 1,000 documents.
MAX_PAGES_TO_CRAWL = 1000
MAX_PDFS_PER_RUN = int(os.environ.get("MAX_PDFS_PER_RUN", "2500"))
MAX_PDF_RECHECKS_PER_RUN = int(os.environ.get("MAX_PDF_RECHECKS_PER_RUN", "500"))

# OCR is used only on pages with little/no embedded text.
OCR_DPI = int(os.environ.get("OCR_DPI", "170"))
OCR_NATIVE_TEXT_THRESHOLD = int(os.environ.get("OCR_NATIVE_TEXT_THRESHOLD", "120"))

SEEN_FILE = "seen_matches.json"
CONTEXT_WINDOW = 250
MAX_CONTEXTS_IN_EMAIL = 5

ALERT_EMAIL_TO = os.environ["ALERT_EMAIL_TO"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]


def empty_state():
    return {
        "version": 3,
        "legacy_ids": set(),
        "matches": {},
        "last_failure_fingerprint": "",
        "run_counter": 0,
        "pdfs": {},
        "baseline_initialized": False,
        "baseline_pending": set(),
    }


def load_seen():
    """Load state, including backward compatibility with older formats."""
    state = empty_state()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state

    # Oldest format: a JSON list of SHA256 IDs.
    if isinstance(data, list):
        state["legacy_ids"] = set(data)
        return state

    if isinstance(data, dict):
        state["legacy_ids"] = set(data.get("legacy_ids", []))
        state["matches"] = data.get("matches", {})
        state["last_failure_fingerprint"] = data.get(
            "last_failure_fingerprint", ""
        )
        state["run_counter"] = int(data.get("run_counter", 0))
        state["pdfs"] = data.get("pdfs", {})
        state["baseline_initialized"] = bool(
            data.get("baseline_initialized", False)
        )
        state["baseline_pending"] = set(data.get("baseline_pending", []))

    return state


def save_seen(state):
    serializable = {
        "version": 3,
        "legacy_ids": sorted(state["legacy_ids"]),
        "matches": state["matches"],
        "last_failure_fingerprint": state["last_failure_fingerprint"],
        "run_counter": state["run_counter"],
        "pdfs": state["pdfs"],
        "baseline_initialized": state["baseline_initialized"],
        "baseline_pending": sorted(state["baseline_pending"]),
    }

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
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
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf,*/*;q=0.8"
        ),
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

    body = f"""\
{description}

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


def ocr_page(page):
    """OCR one PDF page rendered to a grayscale image."""
    scale = OCR_DPI / 72.0
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(image, config="--psm 3") or ""


def extract_pdf_text_from_bytes(pdf_bytes):
    """
    Extract PDF text page by page, OCRing only pages with little embedded text.

    Returns:
        text, native_pages, ocr_pages, ocr_pages_with_text, ocr_errors
    """
    text_parts = []
    native_pages = 0
    ocr_pages = 0
    ocr_pages_with_text = 0
    ocr_errors = 0

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            native_text = page.get_text() or ""
            compact_native = re.sub(r"\s+", " ", native_text).strip()

            if len(compact_native) >= OCR_NATIVE_TEXT_THRESHOLD:
                native_pages += 1
                text_parts.append(native_text)
                continue

            # This catches scanned/image-only pages and hybrid PDFs where only
            # a tiny header/footer has embedded text.
            ocr_pages += 1
            try:
                ocr_text = ocr_page(page)
            except Exception:
                ocr_errors += 1
                ocr_text = ""

            compact_ocr = re.sub(r"\s+", " ", ocr_text).strip()
            if compact_ocr:
                ocr_pages_with_text += 1

            # Retain any native text as well, because a hybrid page may contain
            # useful text outside the rasterized scan.
            if native_text.strip() and ocr_text.strip():
                text_parts.append(native_text + "\n" + ocr_text)
            elif ocr_text.strip():
                text_parts.append(ocr_text)
            else:
                text_parts.append(native_text)

    return (
        "\n".join(text_parts),
        native_pages,
        ocr_pages,
        ocr_pages_with_text,
        ocr_errors,
    )


def handle_match(
    text,
    title,
    url,
    state,
    suppress_new_alert=False,
):
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

    # Preserve alerts already sent by the oldest script.
    if was_seen_in_legacy_state(category, text, url, state["legacy_ids"]):
        state["matches"][key] = {
            "category": category,
            "url": url,
            "fingerprint": fingerprint,
        }
        return 0

    # When the new incremental PDF index is first created, the already-existing
    # archive is treated as a silent baseline so old documents do not flood the
    # inbox. PDFs first discovered after that snapshot still alert normally.
    if suppress_new_alert:
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
    """Log crawl/read/OCR failures only. Never send warning emails."""
    print(f"Crawl/read/OCR failures: {len(failures)}")

    for kind, url, error in failures:
        print(f"  - {kind}: {url}")
        print(f"    {error}")

    state["last_failure_fingerprint"] = failure_fingerprint(failures)


def select_pdfs_for_run(pdf_queue, state):
    """
    Prioritize every never-scanned PDF before rechecking old PDFs.

    This fixes the old behavior where a fixed 1,000-document cap could repeatedly
    hit the same front of a 12k+ document queue and never reach the remainder.
    """
    unseen = [
        (title, url)
        for title, url in pdf_queue
        if url not in state["pdfs"]
    ]

    if len(unseen) >= MAX_PDFS_PER_RUN:
        return unseen[:MAX_PDFS_PER_RUN], len(unseen), 0

    chosen = list(unseen)
    remaining_capacity = MAX_PDFS_PER_RUN - len(chosen)

    known = [
        (title, url)
        for title, url in pdf_queue
        if url in state["pdfs"]
    ]
    known.sort(
        key=lambda item: (
            state["pdfs"][item[1]].get("last_scanned_run", 0),
            item[1],
        )
    )

    recheck_limit = min(
        MAX_PDF_RECHECKS_PER_RUN,
        remaining_capacity,
        len(known),
    )
    chosen.extend(known[:recheck_limit])

    return chosen, len(unseen), recheck_limit


def main():
    state = load_seen()
    state["run_counter"] += 1
    current_run = state["run_counter"]

    pages_to_visit = [normalize_url(x) for x in START_URLS]
    visited_pages = set()
    queued_pdfs = set()
    pdf_queue = []
    failures = []

    page_match_count = 0
    pdf_match_count = 0
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

    # Snapshot the existing PDF archive exactly once. Matches found while
    # backfilling this snapshot are recorded silently; genuinely new PDF URLs
    # discovered on future runs can alert immediately.
    if not state["baseline_initialized"]:
        state["baseline_pending"] = set(queued_pdfs)
        state["baseline_initialized"] = True
        print(
            "Initialized silent PDF baseline with "
            f"{len(state['baseline_pending'])} currently discovered documents.",
            flush=True,
        )

    selected_pdfs, unseen_count, recheck_count = select_pdfs_for_run(
        pdf_queue,
        state,
    )

    print(
        f"PDF plan: {len(selected_pdfs)} to inspect this run "
        f"({unseen_count} never scanned currently; "
        f"{recheck_count} scheduled rechecks).",
        flush=True,
    )

    pdf_downloaded = 0
    pdf_unchanged = 0
    pdf_parsed = 0
    pdf_text_success = 0
    pdf_text_empty = 0
    ocr_pages_attempted = 0
    ocr_pages_with_text = 0
    ocr_page_errors = 0

    for index, (title, pdf_url) in enumerate(selected_pdfs, start=1):
        print(
            f"Checking PDF/document [{index}/{len(selected_pdfs)}]: {pdf_url}",
            flush=True,
        )

        previous_pdf = state["pdfs"].get(pdf_url, {})

        try:
            response = fetch(pdf_url)
            pdf_bytes = response.content
            pdf_downloaded += 1
        except Exception as exc:
            failures.append(("PDF/document download", pdf_url, repr(exc)))
            state["pdfs"][pdf_url] = {
                **previous_pdf,
                "title": title,
                "last_scanned_run": current_run,
                "status": "download_failed",
            }
            continue

        content_hash = hashlib.sha256(pdf_bytes).hexdigest()

        # Known immutable PDFs can be rechecked cheaply without re-running OCR.
        if (
            previous_pdf.get("content_hash")
            and previous_pdf.get("content_hash") == content_hash
            and previous_pdf.get("status") == "ok"
        ):
            pdf_unchanged += 1
            state["pdfs"][pdf_url] = {
                **previous_pdf,
                "title": title,
                "last_scanned_run": current_run,
                "status": "ok",
            }
            continue

        try:
            (
                pdf_text,
                native_pages,
                ocr_pages,
                ocr_text_pages,
                ocr_errors,
            ) = extract_pdf_text_from_bytes(pdf_bytes)
            pdf_parsed += 1
            ocr_pages_attempted += ocr_pages
            ocr_pages_with_text += ocr_text_pages
            ocr_page_errors += ocr_errors
        except Exception as exc:
            failures.append(("PDF/document parse", pdf_url, repr(exc)))
            state["pdfs"][pdf_url] = {
                **previous_pdf,
                "title": title,
                "last_scanned_run": current_run,
                "content_hash": content_hash,
                "status": "parse_failed",
            }
            continue

        if ocr_errors:
            failures.append(
                (
                    "PDF OCR",
                    pdf_url,
                    f"OCR failed on {ocr_errors} page(s); other text was retained.",
                )
            )

        if pdf_text.strip():
            pdf_text_success += 1
        else:
            pdf_text_empty += 1

        suppress_new_alert = pdf_url in state["baseline_pending"]

        pdf_match_count += handle_match(
            pdf_text,
            title,
            pdf_url,
            state,
            suppress_new_alert=suppress_new_alert,
        )

        state["pdfs"][pdf_url] = {
            **previous_pdf,
            "title": title,
            "last_scanned_run": current_run,
            "content_hash": content_hash,
            "status": "ok",
            "native_pages": native_pages,
            "ocr_pages": ocr_pages,
            "ocr_pages_with_text": ocr_text_pages,
        }

        # Remove from the silent baseline only after the document has actually
        # been parsed. Failed documents remain pending so a later successful
        # retry cannot generate a false "new" historical alert.
        state["baseline_pending"].discard(pdf_url)

    remaining_unscanned = sum(
        1 for _title, url in pdf_queue if url not in state["pdfs"]
    )

    print("")
    print("----- SUMMARY -----")
    print(f"Visited pages: {len(visited_pages)}")
    print(f"Discovered unique PDF/document links: {discovered_pdf_count}")
    print(f"PDFs/documents selected this run: {len(selected_pdfs)}")
    print(f"PDFs/documents downloaded: {pdf_downloaded}")
    print(f"Known PDFs unchanged by content hash: {pdf_unchanged}")
    print(f"PDFs/documents parsed: {pdf_parsed}")
    print(f"PDFs/documents with readable text after OCR: {pdf_text_success}")
    print(f"PDFs/documents still empty after OCR: {pdf_text_empty}")
    print(f"OCR pages attempted: {ocr_pages_attempted}")
    print(f"OCR pages yielding text: {ocr_pages_with_text}")
    print(f"OCR page errors: {ocr_page_errors}")
    print(f"Never-scanned PDFs remaining in current discovery set: {remaining_unscanned}")
    print(f"Silent baseline PDFs still pending: {len(state['baseline_pending'])}")
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
