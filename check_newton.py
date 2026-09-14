import io
import os
import re
import json
import hashlib
import smtplib
import time
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
MAX_PDFS_PER_RUN = int(os.environ.get("MAX_PDFS_PER_RUN", "2500"))

# Existing documents are not downloaded every day. A small integrity sample is
# checked approximately monthly, while new IDs and suspected revisions are
# always prioritized.
MAX_MONTHLY_RECHECKS = int(os.environ.get("MAX_MONTHLY_RECHECKS", "250"))
INTEGRITY_RECHECK_EVERY_RUNS = int(
    os.environ.get("INTEGRITY_RECHECK_EVERY_RUNS", "30")
)
MAX_FAILED_RETRIES_PER_RUN = int(
    os.environ.get("MAX_FAILED_RETRIES_PER_RUN", "100")
)
CHECKPOINT_EVERY_PDFS = int(
    os.environ.get("CHECKPOINT_EVERY_PDFS", "25")
)

# OCR is used only on pages with little/no embedded text. A per-page timeout
# prevents one pathological scan from consuming the entire Actions run.
OCR_DPI = int(os.environ.get("OCR_DPI", "170"))
OCR_NATIVE_TEXT_THRESHOLD = int(
    os.environ.get("OCR_NATIVE_TEXT_THRESHOLD", "120")
)
OCR_PAGE_TIMEOUT_SECONDS = int(
    os.environ.get("OCR_PAGE_TIMEOUT_SECONDS", "30")
)

# Stop the checker cleanly before GitHub's hard timeout. The workflow gives the
# Python step 5 hours; the script targets 4.5 hours so it has time to print a
# summary, persist state, and let the commit step run.
CHECKER_TIME_BUDGET_SECONDS = int(
    os.environ.get("CHECKER_TIME_BUDGET_SECONDS", str(4 * 60 * 60 + 30 * 60))
)

SEEN_FILE = "seen_matches.json"
CONTEXT_WINDOW = 250
MAX_CONTEXTS_IN_EMAIL = 5
SHOWPUBLISH_RE = re.compile(
    r"/home/showpublisheddocument/(?P<document_id>\d+)"
    r"(?:/(?P<suffix>\d+))?",
    re.IGNORECASE,
)

ALERT_EMAIL_TO = os.environ["ALERT_EMAIL_TO"]
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]


class TimeBudgetReached(Exception):
    """Raised internally to defer the current PDF without failing the run."""


def empty_state():
    return {
        "version": 4,
        "legacy_ids": set(),
        "matches": {},
        "last_failure_fingerprint": "",
        "run_counter": 0,
        "pdfs": {},
        "baseline_initialized": False,
        "baseline_pending": set(),
    }


def normalize_url(url):
    url, _ = urldefrag(url)
    return url.strip()


def pdf_identity(url):
    """
    Return Newton's stable document identity when a showpublisheddocument URL is
    used. The optional trailing numeric suffix is retained separately as a
    revision/cache signal rather than being treated as a second document.
    """
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    match = SHOWPUBLISH_RE.search(parsed.path)

    if match:
        document_id = match.group("document_id")
        suffix = match.group("suffix")
        canonical_url = (
            "https://www.newtonma.gov/home/showpublisheddocument/"
            f"{document_id}"
        )
        return {
            "key": f"newton-doc:{document_id}",
            "document_id": document_id,
            "suffix": suffix,
            "canonical_url": canonical_url,
            "url": normalized,
        }

    return {
        "key": f"url:{normalized}",
        "document_id": None,
        "suffix": None,
        "canonical_url": normalized,
        "url": normalized,
    }


def migrate_pdf_state(pdf_state):
    """Canonicalize any v3 URL-keyed PDF state into v4 document identities."""
    migrated = {}

    for old_key, item in (pdf_state or {}).items():
        source_url = item.get("url") or item.get("canonical_url")

        if not source_url:
            if old_key.startswith("newton-doc:") or old_key.startswith("url:"):
                new_key = old_key
                identity = {
                    "key": old_key,
                    "document_id": item.get("document_id"),
                    "suffix": item.get("suffix"),
                    "canonical_url": item.get("canonical_url", ""),
                    "url": item.get("url", ""),
                }
            else:
                source_url = old_key

        if source_url:
            identity = pdf_identity(source_url)
            new_key = identity["key"]

        merged = dict(migrated.get(new_key, {}))
        merged.update(item)
        merged["document_id"] = (
            item.get("document_id") or identity.get("document_id")
        )
        merged["canonical_url"] = (
            item.get("canonical_url") or identity.get("canonical_url")
        )
        merged["url"] = item.get("url") or identity.get("url")
        merged["suffix"] = item.get("suffix") or identity.get("suffix")
        migrated[new_key] = merged

    return migrated


def migrate_pending_keys(pending):
    migrated = set()
    for value in pending or []:
        if value.startswith("newton-doc:") or value.startswith("url:"):
            migrated.add(value)
        else:
            migrated.add(pdf_identity(value)["key"])
    return migrated


def load_seen():
    """Load state with backward compatibility and v4 PDF-key migration."""
    state = empty_state()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state

    # Oldest format: a JSON list of legacy match hashes.
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
        state["pdfs"] = migrate_pdf_state(data.get("pdfs", {}))
        state["baseline_initialized"] = bool(
            data.get("baseline_initialized", False)
        )
        state["baseline_pending"] = migrate_pending_keys(
            data.get("baseline_pending", [])
        )

    return state


def save_seen(state):
    serializable = {
        "version": 4,
        "legacy_ids": sorted(state["legacy_ids"]),
        "matches": state["matches"],
        "last_failure_fingerprint": state["last_failure_fingerprint"],
        "run_counter": state["run_counter"],
        "pdfs": state["pdfs"],
        "baseline_initialized": state["baseline_initialized"],
        "baseline_pending": sorted(state["baseline_pending"]),
    }

    temp_file = f"{SEEN_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())

    # Atomic replacement prevents a timeout/cancellation from leaving a
    # partially written JSON state file.
    os.replace(temp_file, SEEN_FILE)


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
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "application/pdf,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.newtonma.gov/",
    }

    response = requests.get(url, headers=headers, timeout=45)
    response.raise_for_status()
    return response


def contains_term(text, term):
    return term.lower() in text.lower()


def matching_category(text):
    """Return one category per source, with the exact property first."""
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


def make_match_key(category, identity):
    return hashlib.sha256(f"{category}|{identity}".encode()).hexdigest()


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
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else url
    page_text = soup.get_text(" ", strip=True)

    discovered_pages = []
    discovered_pdfs = []

    for anchor in soup.find_all("a", href=True):
        href = normalize_url(urljoin(url, anchor["href"]))
        text = anchor.get_text(" ", strip=True)

        if not is_allowed_url(href):
            continue

        if is_pdf_url(href):
            discovered_pdfs.append((text or "Newton PDF", href))
        elif looks_relevant(text, href):
            discovered_pages.append(href)

    return title, page_text, discovered_pages, discovered_pdfs


def candidate_from_pdf_link(title, url):
    identity = pdf_identity(url)
    return {
        "key": identity["key"],
        "title": title,
        "url": identity["url"],
        "canonical_url": identity["canonical_url"],
        "document_id": identity["document_id"],
        "suffix": identity["suffix"],
        "variants": {identity["url"]},
    }


def choose_better_variant(current, incoming):
    """
    Merge links that refer to the same Newton document ID.

    If several suffix variants are visible, retain all URLs for diagnostics and
    use the highest numeric suffix as the fetch URL. The suffix is not assumed
    to be the document identity; it is only a useful version signal.
    """
    current["variants"].update(incoming["variants"])

    current_suffix = current.get("suffix")
    incoming_suffix = incoming.get("suffix")

    use_incoming = False
    if incoming_suffix and not current_suffix:
        use_incoming = True
    elif incoming_suffix and current_suffix:
        try:
            use_incoming = int(incoming_suffix) > int(current_suffix)
        except ValueError:
            use_incoming = incoming_suffix > current_suffix

    if use_incoming:
        current["url"] = incoming["url"]
        current["suffix"] = incoming_suffix
        current["title"] = incoming["title"]

    return current


def ocr_page(page):
    """OCR one PDF page rendered to a grayscale image."""
    scale = OCR_DPI / 72.0
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(
        image,
        config="--psm 3",
        timeout=OCR_PAGE_TIMEOUT_SECONDS,
    ) or ""


def extract_pdf_text_from_bytes(pdf_bytes, deadline=None):
    """
    Extract PDF text page by page, OCRing only pages with little embedded text.

    If the checker time budget is reached, raise TimeBudgetReached so the
    current document is left pending for the next run rather than marked failed.

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
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeBudgetReached()

            native_text = page.get_text() or ""
            compact_native = re.sub(r"\s+", " ", native_text).strip()

            if len(compact_native) >= OCR_NATIVE_TEXT_THRESHOLD:
                native_pages += 1
                text_parts.append(native_text)
                continue

            ocr_pages += 1

            if deadline is not None and time.monotonic() >= deadline:
                raise TimeBudgetReached()

            try:
                ocr_text = ocr_page(page)
            except Exception:
                ocr_errors += 1
                ocr_text = ""

            compact_ocr = re.sub(r"\s+", " ", ocr_text).strip()
            if compact_ocr:
                ocr_pages_with_text += 1

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
    identity=None,
    suppress_new_alert=False,
):
    category = matching_category(text)
    if category is None:
        return 0

    contexts = extract_contexts(text, SEARCH_GROUPS[category])
    if not contexts:
        contexts = [category]

    identity = identity or url
    key = make_match_key(category, identity)
    fingerprint = make_content_fingerprint(category, contexts)

    # Migrate a previous URL-based match to the canonical document identity
    # without re-alerting just because the bookkeeping key changed.
    previous = state["matches"].get(key)
    old_url_key = make_match_key(category, url)
    if previous is None and old_url_key != key:
        previous = state["matches"].get(old_url_key)
        if previous is not None:
            state["matches"][key] = previous
            del state["matches"][old_url_key]

    if previous is not None:
        if previous.get("fingerprint") == fingerprint:
            previous["url"] = url
            previous["identity"] = identity
            return 0

        send_email(category, title, url, contexts, is_change=True)
        state["matches"][key] = {
            "category": category,
            "url": url,
            "identity": identity,
            "fingerprint": fingerprint,
        }
        return 1

    if was_seen_in_legacy_state(category, text, url, state["legacy_ids"]):
        state["matches"][key] = {
            "category": category,
            "url": url,
            "identity": identity,
            "fingerprint": fingerprint,
        }
        return 0

    # Existing archive documents are silently baselined during the one-time
    # backfill. Documents first appearing after that snapshot alert normally.
    if suppress_new_alert:
        state["matches"][key] = {
            "category": category,
            "url": url,
            "identity": identity,
            "fingerprint": fingerprint,
        }
        return 0

    send_email(category, title, url, contexts, is_change=False)
    state["matches"][key] = {
        "category": category,
        "url": url,
        "identity": identity,
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


def checkpoint_state(state, processed_count):
    """Persist local progress periodically so a later step can commit it."""
    if CHECKPOINT_EVERY_PDFS <= 0:
        return
    if processed_count % CHECKPOINT_EVERY_PDFS == 0:
        save_seen(state)
        print(
            f"Checkpointed monitor state after {processed_count} PDFs/documents.",
            flush=True,
        )


def suffix_changed(candidate, previous):
    """
    Treat a changed non-empty showpublisheddocument suffix as a reason to fetch
    the document immediately. The content hash still decides whether the bytes
    actually changed.
    """
    new_suffix = candidate.get("suffix")
    old_suffix = previous.get("suffix")

    return bool(
        new_suffix
        and old_suffix
        and new_suffix != old_suffix
    )


def select_pdfs_for_run(candidates, state):
    """
    Priority order:
      1. Truly new document identities discovered after baseline creation.
      2. Known document IDs whose trailing suffix changed.
      3. Previously failed documents, for a bounded retry.
      4. Historical baseline documents not yet backfilled.
      5. A small integrity sample only about once every 30 daily runs.

    This gives new Newton publications immediate attention while the historical
    archive is still being OCR-backfilled.
    """
    new_documents = []
    changed_suffix = []
    failed = []
    historical = []
    stable_known = []

    for key, candidate in candidates.items():
        previous = state["pdfs"].get(key)

        if previous is None:
            if key in state["baseline_pending"]:
                historical.append(candidate)
            else:
                new_documents.append(candidate)
            continue

        if suffix_changed(candidate, previous):
            changed_suffix.append(candidate)
            continue

        if previous.get("status") != "ok":
            failed.append(candidate)
            continue

        stable_known.append(candidate)

    failed.sort(
        key=lambda item: state["pdfs"]
        .get(item["key"], {})
        .get("last_scanned_run", 0)
    )
    failed = failed[:MAX_FAILED_RETRIES_PER_RUN]

    selected = []
    selected_keys = set()

    def add_group(group):
        for item in group:
            if len(selected) >= MAX_PDFS_PER_RUN:
                break
            if item["key"] not in selected_keys:
                selected.append(item)
                selected_keys.add(item["key"])

    add_group(new_documents)
    add_group(changed_suffix)
    add_group(failed)
    add_group(historical)

    integrity_rechecks = []
    if (
        len(selected) < MAX_PDFS_PER_RUN
        and INTEGRITY_RECHECK_EVERY_RUNS > 0
        and state["run_counter"] % INTEGRITY_RECHECK_EVERY_RUNS == 0
    ):
        stable_known.sort(
            key=lambda item: state["pdfs"]
            .get(item["key"], {})
            .get("last_scanned_run", 0)
        )
        integrity_rechecks = stable_known[:MAX_MONTHLY_RECHECKS]
        add_group(integrity_rechecks)

    return {
        "selected": selected,
        "new_count": len(new_documents),
        "suffix_change_count": len(changed_suffix),
        "failed_retry_count": len(failed),
        "historical_count": len(historical),
        "integrity_recheck_count": len(integrity_rechecks),
    }


def main():
    checker_started = time.monotonic()
    deadline = checker_started + CHECKER_TIME_BUDGET_SECONDS

    state = load_seen()
    state["run_counter"] += 1
    current_run = state["run_counter"]

    pages_to_visit = [normalize_url(x) for x in START_URLS]
    visited_pages = set()
    pdf_candidates = {}
    failures = []

    page_match_count = 0

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
            candidate = candidate_from_pdf_link(pdf_title, pdf_url)
            key = candidate["key"]

            if key not in pdf_candidates:
                pdf_candidates[key] = candidate
            else:
                pdf_candidates[key] = choose_better_variant(
                    pdf_candidates[key],
                    candidate,
                )

    raw_variant_count = sum(
        len(candidate["variants"])
        for candidate in pdf_candidates.values()
    )
    duplicate_variant_count = raw_variant_count - len(pdf_candidates)

    # Snapshot document identities, not raw URL variants. This prevents
    # /showpublisheddocument/123 and /123/<ticks> from being backfilled twice.
    if not state["baseline_initialized"]:
        state["baseline_pending"] = set(pdf_candidates)
        state["baseline_initialized"] = True
        print(
            "Initialized silent PDF baseline with "
            f"{len(state['baseline_pending'])} document identities.",
            flush=True,
        )
        # Persist the baseline immediately. This makes a partial backfill safe:
        # if the run later times out, already-existing archive documents remain
        # classified as historical rather than appearing "new" next time.
        save_seen(state)

    plan = select_pdfs_for_run(pdf_candidates, state)
    selected_pdfs = plan["selected"]

    print(
        "PDF plan: "
        f"{len(selected_pdfs)} selected; "
        f"{plan['new_count']} new IDs, "
        f"{plan['suffix_change_count']} suffix changes, "
        f"{plan['failed_retry_count']} failed retries eligible, "
        f"{plan['historical_count']} historical baseline pending, "
        f"{plan['integrity_recheck_count']} integrity rechecks.",
        flush=True,
    )

    pdf_match_count = 0
    pdf_downloaded = 0
    pdf_unchanged = 0
    pdf_parsed = 0
    pdf_text_success = 0
    pdf_text_empty = 0
    ocr_pages_attempted = 0
    ocr_pages_with_text = 0
    ocr_page_errors = 0
    time_budget_reached = False
    deferred_due_to_time = 0

    for index, candidate in enumerate(selected_pdfs, start=1):
        if time.monotonic() >= deadline:
            time_budget_reached = True
            deferred_due_to_time = len(selected_pdfs) - index + 1
            print(
                "Checker time budget reached before starting the next "
                f"document; deferring {deferred_due_to_time} selected "
                "document(s) to a future run.",
                flush=True,
            )
            break
        key = candidate["key"]
        title = candidate["title"]
        pdf_url = candidate["url"]
        previous_pdf = state["pdfs"].get(key, {})

        print(
            f"Checking PDF/document [{index}/{len(selected_pdfs)}] "
            f"{key}: {pdf_url}",
            flush=True,
        )

        try:
            response = fetch(pdf_url)
            pdf_bytes = response.content
            pdf_downloaded += 1
        except Exception as exc:
            failures.append(("PDF/document download", pdf_url, repr(exc)))
            state["pdfs"][key] = {
                **previous_pdf,
                "title": title,
                "url": pdf_url,
                "canonical_url": candidate["canonical_url"],
                "document_id": candidate["document_id"],
                "suffix": candidate["suffix"],
                "last_scanned_run": current_run,
                "status": "download_failed",
            }
            checkpoint_state(state, index)
            continue

        content_hash = hashlib.sha256(pdf_bytes).hexdigest()

        if (
            previous_pdf.get("content_hash")
            and previous_pdf.get("content_hash") == content_hash
            and previous_pdf.get("status") == "ok"
        ):
            pdf_unchanged += 1
            state["pdfs"][key] = {
                **previous_pdf,
                "title": title,
                "url": pdf_url,
                "canonical_url": candidate["canonical_url"],
                "document_id": candidate["document_id"],
                "suffix": candidate["suffix"],
                "last_scanned_run": current_run,
                "status": "ok",
            }
            checkpoint_state(state, index)
            continue

        try:
            (
                pdf_text,
                native_pages,
                ocr_pages,
                ocr_text_pages,
                ocr_errors,
            ) = extract_pdf_text_from_bytes(
                pdf_bytes,
                deadline=deadline,
            )
            pdf_parsed += 1
            ocr_pages_attempted += ocr_pages
            ocr_pages_with_text += ocr_text_pages
            ocr_page_errors += ocr_errors
        except TimeBudgetReached:
            time_budget_reached = True
            deferred_due_to_time = len(selected_pdfs) - index + 1
            print(
                "Checker time budget reached while processing "
                f"{key}; leaving it pending and deferring "
                f"{deferred_due_to_time} selected document(s) to a future run.",
                flush=True,
            )
            break
        except Exception as exc:
            failures.append(("PDF/document parse", pdf_url, repr(exc)))
            state["pdfs"][key] = {
                **previous_pdf,
                "title": title,
                "url": pdf_url,
                "canonical_url": candidate["canonical_url"],
                "document_id": candidate["document_id"],
                "suffix": candidate["suffix"],
                "last_scanned_run": current_run,
                "content_hash": content_hash,
                "status": "parse_failed",
            }
            checkpoint_state(state, index)
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

        suppress_new_alert = key in state["baseline_pending"]

        pdf_match_count += handle_match(
            pdf_text,
            title,
            pdf_url,
            state,
            identity=key,
            suppress_new_alert=suppress_new_alert,
        )

        state["pdfs"][key] = {
            **previous_pdf,
            "title": title,
            "url": pdf_url,
            "canonical_url": candidate["canonical_url"],
            "document_id": candidate["document_id"],
            "suffix": candidate["suffix"],
            "last_scanned_run": current_run,
            "content_hash": content_hash,
            "status": "ok",
            "native_pages": native_pages,
            "ocr_pages": ocr_pages,
            "ocr_pages_with_text": ocr_text_pages,
        }

        # A historical PDF remains silent until it has actually been parsed.
        state["baseline_pending"].discard(key)
        checkpoint_state(state, index)

    remaining_historical = sum(
        1
        for key in state["baseline_pending"]
        if key in pdf_candidates
    )
    never_scanned_current = sum(
        1
        for key in pdf_candidates
        if key not in state["pdfs"]
    )

    print("")
    print("----- SUMMARY -----")
    print(f"Visited pages: {len(visited_pages)}")
    print(
        "Unique Newton PDF/document identities discovered: "
        f"{len(pdf_candidates)}"
    )
    print(f"Raw URL variants represented: {raw_variant_count}")
    print(
        "Duplicate URL variants collapsed by document identity: "
        f"{duplicate_variant_count}"
    )
    print(f"PDFs/documents selected this run: {len(selected_pdfs)}")
    print(f"New document IDs awaiting processing: {plan['new_count']}")
    print(
        "Known document IDs with changed trailing suffix: "
        f"{plan['suffix_change_count']}"
    )
    print(f"PDFs/documents downloaded: {pdf_downloaded}")
    print(f"Known PDFs unchanged by content hash: {pdf_unchanged}")
    print(f"PDFs/documents parsed: {pdf_parsed}")
    print(f"PDFs/documents with readable text after OCR: {pdf_text_success}")
    print(f"PDFs/documents still empty after OCR: {pdf_text_empty}")
    print(f"OCR pages attempted: {ocr_pages_attempted}")
    print(f"OCR pages yielding text: {ocr_pages_with_text}")
    print(f"OCR page errors: {ocr_page_errors}")
    print(f"Checker time budget reached: {time_budget_reached}")
    print(f"Selected documents deferred by time budget: {deferred_due_to_time}")
    print(
        "Checker elapsed minutes: "
        f"{(time.monotonic() - checker_started) / 60:.1f}"
    )
    print(f"Never-scanned current document identities: {never_scanned_current}")
    print(f"Silent historical baseline identities remaining: {remaining_historical}")
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
