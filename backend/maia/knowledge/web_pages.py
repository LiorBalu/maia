"""
Scrape URL and create embedded document content.

This module extracts the scraping logic so it can run in a Celery worker
(outside of the FastAPI request lifecycle).
"""

import functools
import logging
import re
import traceback
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from langchain_community.document_loaders import RecursiveUrlLoader
from langchain_community.document_loaders.sitemap import SitemapLoader
from langchain_core.documents import Document as LangchainDocument

from maia.models.knowledge_documents import EmbeddedDocument

logger = logging.getLogger(__name__)

# Markers that, in the first 2000 characters of a crawled response, hint at a stylesheet or a script
# (_is_stylesheet_or_script_source).
_SOURCE_BLOB_INDICATORS = [
    "@font-face",
    "@keyframes",
    "@media screen",
    ".elementor-",
    ".swiper-",
    "function()",
    "var ",
    "const ",
    "export default",
    "import {",
]

# Elements removed from a crawled page before its text is taken (_remove_non_content_elements): these tags first,
# then everything the junk selectors match.
_NON_CONTENT_TAGS = ["script", "style", "noscript", "iframe", "svg", "link"]
_JUNK_SELECTORS = [
    "nav",
    "footer",
    "header",
    ".cookie-notice",
    ".cookie-consent",
    "#cookie",
    ".accessibility-menu",
    ".elementor-location-header",
    ".elementor-location-footer",
    ".woocommerce-mini-cart",
    ".mini-cart",
    ".menu-toggle",
    ".mobile-menu",
    "[class*='cookie']",
    "[class*='consent']",
    "[id*='cookie']",
    "[id*='consent']",
    ".site-footer",
    ".site-header",
    ".breadcrumb",
    ".breadcrumbs",
]

# Main content areas of a crawled page, tried in this order (_select_main_content).
_MAIN_CONTENT_SELECTORS = [
    "main",
    "article",
    ".product",
    ".single-product",
    ".entry-content",
    ".post-content",
    ".page-content",
    ".woocommerce-product-details",
    "[role='main']",
    "#content",
    "#main-content",
    "#primary",
]

# CSS and JS indicators counted in the first 5000 characters of a sitemap page: 3 or more of either kind mark it as
# a stylesheet or a script (_has_stylesheet_or_script_indicators).
_STYLESHEET_INDICATORS = [
    "@font-face",
    "@keyframes",
    "@media",
    ".swiper",
    ".elementor",
    "font-family:",
    "background-color:",
    "margin:",
    "padding:",
    "border-radius:",
    "transition:",
    "transform:",
    ":root{--",
    "var(--",
    "rgba(",
    "#fff}",
    "#000}",
]
_SCRIPT_INDICATORS = [
    "function(",
    "var ",
    "const ",
    "let ",
    "window.",
    "document.",
    "export ",
    "import ",
    "=>",
    ".prototype",
    "typeof",
    "return{",
]

# Elements removed from a sitemap page (_remove_sitemap_non_content_elements); unlike the crawl lists: no "link" tag
# and fewer selectors.
_SITEMAP_NON_CONTENT_TAGS = ["script", "style", "noscript", "iframe", "svg"]
_SITEMAP_JUNK_SELECTORS = [
    "nav",
    "footer",
    "header",
    ".cookie-notice",
    ".cookie-consent",
    "#cookie",
    ".accessibility-menu",
    ".elementor-location-header",
    ".elementor-location-footer",
    ".site-footer",
    ".site-header",
    ".breadcrumb",
]

# Symbols of a CSS line: a sitemap page whose lines mostly contain one is a stylesheet (_is_mostly_stylesheet_lines).
_STYLESHEET_LINE_SYMBOLS = ["{", "}", ";", ":", "px", "em", "rem", "#", "rgb"]

# URL fragments of stylesheets, scripts and fonts (_is_asset_url).
_ASSET_URL_FRAGMENTS = [
    ".css",
    ".js",
    "/css/",
    "/js/",
    "/fonts/",
    ".woff",
    ".ttf",
    "elementor/css",
    "elementor/js",
    "google-fonts",
]

# CSS markers: a page text with 3 or more of them is a stylesheet (_is_stylesheet_or_script_garbage_page).
_STYLESHEET_MARKERS = [
    ".elementor",
    ".swiper",
    ".woocommerce",
    "@font-face",
    "@keyframes",
    "@media",
    "font-family:",
    "background-color:",
    "border-radius:",
    "flex-direction:",
    "flex-shrink:",
    "justify-content:",
    "align-items:",
    "display:flex",
    "display:block",
    "display:none",
    "position:absolute",
    "position:relative",
    "unicode-range:",
    "font-weight:",
    "font-style:",
    "src: url(",
    "margin:",
    "padding:",
    "z-index:",
    "transform:",
    "transition:",
]

# Joints of minified CSS rules (_has_dense_stylesheet_punctuation).
_GLUED_RULE_PATTERNS = ["}{.", "}{#", ";}", ":{", "}@"]

# Sitemap files tried at the site root, in this order (_find_pages_via_sitemaps).
_SITEMAP_FILE_NAMES = ["sitemap.xml", "wp-sitemap.xml", "sitemap_index.xml", "sitemap-index.xml"]

# Sitemap pages whose lower-cased source URL contains one of these are skipped (_load_sitemap_pages).
_SITEMAP_SKIP_PATTERNS = [
    "wp-admin",
    "wp-login",
    "cart",
    "checkout",
    "my-account",
    "feed",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".mp3",
    ".mp4",
    ".xml",
    ".json",
    "/wp-content/plugins/",
    "/wp-content/themes/",
    "/wp-content/uploads/elementor/",
    "wp-includes",
    "elementor/css",
    "elementor/js",
    "remove_item=",
    "{{",
    "tel:",
    "mailto:",
    "javascript:",
]

# exclude_dirs and request headers of the RecursiveUrlLoader crawl (_crawl_pages_recursively).
_CRAWL_EXCLUDED_DIRS = (
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".rar",
    ".mp3",
    ".mp4",
    ".avi",
    ".xml",
    ".json",
    ".txt",
    ".map",
    "/wp-admin/",
    "/wp-login",
    "/wp-includes/",
    "/wp-content/uploads/elementor/",
    "/wp-content/plugins/",
    "/wp-content/themes/",
    "/wp-json/",
    "/cart/",
    "/checkout/",
    "/my-account/",
    "/feed/",
    "/xmlrpc",
    "add-to-cart",
    "add_to_wishlist",
    "?add_to_wishlist",
    "?add-to-cart",
    "/cdn-cgi/",
    "?add-to-cart=",
    "/comments/feed",
    "remove_item=",
    "{{{",
    "{{",
    "tel:",
    "mailto:",
    "javascript:",
    "whatsapp:",
    "/_catalogs/",
    "/_layouts/",
    "/_vti_",
    "/Forms/",
)
_CRAWL_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5,he;q=0.3",
}

# CSS properties: a line of the combined text with 2 or more of them is dropped (_scrub_stylesheet_remnants).
_STYLESHEET_PROPERTY_PREFIXES = [
    "display:",
    "flex-",
    "justify-",
    "align-",
    "margin:",
    "padding:",
    "font-family:",
    "background:",
    "border:",
    "position:",
    "transform:",
    "transition:",
    "z-index:",
    "width:",
    "height:",
    "unicode-range:",
    "src: url(",
]


def _is_stylesheet_or_script_source(page_html: str) -> bool:
    """True when the first 2000 characters of a crawled response look like a stylesheet or a script, not a page:
    one of _SOURCE_BLOB_INDICATORS, two or more of '{', '}', ';' appearing more than 20 times each, and no <html or
    <body tag. The symbol count runs only when an indicator is present."""
    html_sample = page_html[:2000].lower()
    if not any(indicator in html_sample for indicator in _SOURCE_BLOB_INDICATORS):
        return False
    frequent_symbol_count = sum(1 for symbol in ["{", "}", ";"] if html_sample.count(symbol) > 20)
    return frequent_symbol_count >= 2 and "<html" not in html_sample and "<body" not in html_sample


def _parse_html(page_html: str) -> BeautifulSoup | None:
    """Parse a crawled page with lxml, falling back to html.parser; None (logged) when both parsers fail."""
    try:
        return BeautifulSoup(page_html, "lxml")
    except Exception:
        try:
            return BeautifulSoup(page_html, "html.parser")
        except Exception as parse_error:
            logger.error(f"   Skipping page, parser error: {parse_error}")
            return None


def _remove_non_content_elements(soup: BeautifulSoup) -> None:
    """Remove from a crawled page (in place, in this order) the _NON_CONTENT_TAGS elements, every inline style
    attribute, then everything _JUNK_SELECTORS matches."""
    for element in soup(_NON_CONTENT_TAGS):
        element.decompose()
    for element in soup.find_all(style=True):
        del element["style"]
    for selector in _JUNK_SELECTORS:
        for element in soup.select(selector):
            element.decompose()


def _collect_image_links(soup: BeautifulSoup, site_url: str) -> list[str]:
    """'[IMAGE: alt](source)' links of the page's <img> tags (src, else data-src): inline data: images are skipped,
    relative sources are resolved against site_url, a missing alt text becomes 'No description'."""
    image_links = []
    for image_tag in soup.find_all("img"):
        image_source = image_tag.get("src", "") or image_tag.get("data-src", "")
        image_alt_text = image_tag.get("alt", "")
        if image_source and not image_source.startswith("data:"):
            if image_source.startswith("/"):
                image_source = urljoin(site_url, image_source)
            elif not image_source.startswith("http"):
                image_source = urljoin(site_url, image_source)
            image_links.append(f"[IMAGE: {image_alt_text if image_alt_text else 'No description'}]({image_source})")
    return image_links


def _select_main_content(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    """The first element a _MAIN_CONTENT_SELECTORS selector matches, else the <body>, else the whole page."""
    for selector in _MAIN_CONTENT_SELECTORS:
        main_content = soup.select_one(selector)
        if main_content:
            return main_content
    return soup.body if soup.body else soup


def _deduplicate_text_lines(text: str) -> str:
    """The text with every line stripped, without lines shorter than 3 characters and without repeated lines."""
    seen_lines = set()
    kept_lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if len(line) < 3 or line in seen_lines:
            continue
        seen_lines.add(line)
        kept_lines.append(line)
    return "\n".join(kept_lines)


def _extract_page_text(page_html: str, site_url: str) -> str:
    """HTML to text for RecursiveUrlLoader: skip CSS / JS blobs, strip scripts, styles, navigation and cookie
    banners, keep the main content, dedupe lines and append the image links.

    site_url resolves relative image sources; _crawl_pages_recursively binds it with functools.partial.
    """
    if _is_stylesheet_or_script_source(page_html):
        return ""
    soup = _parse_html(page_html)
    if soup is None:
        return ""
    _remove_non_content_elements(soup)
    image_links = _collect_image_links(soup, site_url)
    main_content_text = _select_main_content(soup).get_text(separator="\n", strip=True)
    page_text = _deduplicate_text_lines(main_content_text)
    if image_links:
        images_section = "\n\n📸 IMAGES FOUND ON THIS PAGE:\n" + "\n".join(f"  • {link}" for link in image_links[:10])
        page_text += images_section
    return page_text


def _has_stylesheet_or_script_indicators(raw_markup: str) -> bool:
    """True when the first 5000 characters of a sitemap page's markup hold 3 or more _STYLESHEET_INDICATORS or 3 or
    more _SCRIPT_INDICATORS."""
    markup_head = raw_markup[:5000]
    return (
        sum(1 for indicator in _STYLESHEET_INDICATORS if indicator in markup_head) >= 3
        or sum(1 for indicator in _SCRIPT_INDICATORS if indicator in markup_head) >= 3
    )


def _remove_sitemap_non_content_elements(page_soup: BeautifulSoup) -> None:
    """Remove from a sitemap page (in place) the _SITEMAP_NON_CONTENT_TAGS elements, then everything
    _SITEMAP_JUNK_SELECTORS matches."""
    for element in page_soup.find_all(_SITEMAP_NON_CONTENT_TAGS):
        element.decompose()
    for selector in _SITEMAP_JUNK_SELECTORS:
        for element in page_soup.select(selector):
            element.decompose()


def _is_mostly_stylesheet_lines(page_text: str) -> bool:
    """True when the text has more than 5 lines and more than half of them hold a _STYLESHEET_LINE_SYMBOLS symbol."""
    page_lines = page_text.split("\n")
    stylesheet_line_count = sum(1 for line in page_lines if any(symbol in line for symbol in _STYLESHEET_LINE_SYMBOLS))
    return len(page_lines) > 5 and stylesheet_line_count / len(page_lines) > 0.5


def _extract_sitemap_page_text(page_soup: BeautifulSoup) -> str:
    """BeautifulSoup page to text for SitemapLoader: empty for CSS / JS pages, otherwise the page text without
    navigation and junk.
    """
    raw_markup = str(page_soup)
    if _has_stylesheet_or_script_indicators(raw_markup):
        return ""
    _remove_sitemap_non_content_elements(page_soup)
    page_text = page_soup.get_text(separator="\n", strip=True)
    if _is_mostly_stylesheet_lines(page_text):
        return ""
    return page_text


def _is_asset_url(page_url: str) -> bool:
    """True when the URL (case-insensitively) holds a fragment of _ASSET_URL_FRAGMENTS."""
    lowercase_url = page_url.lower()
    return any(fragment in lowercase_url for fragment in _ASSET_URL_FRAGMENTS)


def _has_dense_stylesheet_punctuation(trimmed_text: str) -> bool:
    """True when a text over 200 characters is more than 1% braces or more than 2% semicolons, or when it holds a
    minified rule joint of _GLUED_RULE_PATTERNS."""
    bracket_count = trimmed_text.count("{") + trimmed_text.count("}")
    semicolon_count = trimmed_text.count(";")
    text_length = len(trimmed_text)
    if text_length > 200:
        if (bracket_count / text_length) > 0.01 or (semicolon_count / text_length) > 0.02:
            return True
    return any(pattern in trimmed_text for pattern in _GLUED_RULE_PATTERNS)


def _is_stylesheet_or_script_garbage_page(page_text: str, page_url: str = "") -> bool:
    """True when a scraped page is really a stylesheet or a script (by URL, by CSS markers or by brace
    density).
    """
    if not page_text or len(page_text.strip()) < 50:
        return True
    if _is_asset_url(page_url):
        return True
    trimmed_text = page_text.strip()
    first_character = trimmed_text[0] if trimmed_text else ""
    if first_character in ".#@" or trimmed_text.startswith("/*"):
        return True
    if sum(1 for marker in _STYLESHEET_MARKERS if marker in trimmed_text) >= 3:
        return True
    return _has_dense_stylesheet_punctuation(trimmed_text)


def _load_sitemap_pages(
    sitemap_url: str, base_domain: str, max_page_count: int, should_stay_on_domain: bool, pages: list[LangchainDocument]
) -> None:
    """Load one sitemap with SitemapLoader and append its pages to `pages`, at most max_page_count of them, skipping
    sources that match _SITEMAP_SKIP_PATTERNS and pages under 50 characters.

    `pages` is appended in place because it is the accumulator shared by every sitemap attempt of
    _find_pages_via_sitemaps: the pages of a sitemap that fails part-way stay in it and are returned once a later
    sitemap loads. Exceptions are left to the caller, which logs them and tries the next sitemap.
    """
    filter_urls = [base_domain] if should_stay_on_domain else None
    sitemap_loader = SitemapLoader(
        web_path=sitemap_url,
        filter_urls=filter_urls,
        parsing_function=_extract_sitemap_page_text,
        requests_per_second=3,
        requests_kwargs={"timeout": 15},
    )
    loaded_page_count = 0
    for page in sitemap_loader.lazy_load():
        if loaded_page_count >= max_page_count:
            break
        page_source_url = page.metadata.get("source", "").lower()
        if any(skip_pattern in page_source_url for skip_pattern in _SITEMAP_SKIP_PATTERNS):
            continue
        if not page.page_content or len(page.page_content.strip()) < 50:
            continue
        pages.append(page)
        loaded_page_count += 1
        logger.info(f"   Loaded ({loaded_page_count}/{max_page_count}): {page_source_url[:60]}...")


def _find_pages_via_sitemaps(
    base_domain: str, max_page_count: int, should_stay_on_domain: bool
) -> list[LangchainDocument]:
    """Try the _SITEMAP_FILE_NAMES sitemaps of the site in order (a HEAD probe first, a non-200 answer skips one, a
    failure is logged and skipped) until the pages accumulated so far are not empty. Returns those pages, or []
    when no attempt got there (the crawl then takes over)."""
    sitemap_urls = [f"{base_domain}/{file_name}" for file_name in _SITEMAP_FILE_NAMES]
    pages = []
    for sitemap_url in sitemap_urls:
        try:
            logger.info(f" Trying sitemap: {sitemap_url}")
            import requests

            sitemap_head_response = requests.head(sitemap_url, timeout=5, allow_redirects=True)
            if sitemap_head_response.status_code != 200:
                continue
            logger.info(f"Found sitemap at: {sitemap_url}")
            _load_sitemap_pages(sitemap_url, base_domain, max_page_count, should_stay_on_domain, pages)
            if pages:
                logger.info(f"Sitemap loaded {len(pages)} pages")
                return pages
        except Exception as error:
            logger.error(f"   Sitemap {sitemap_url} failed: {str(error)[:50]}")
            continue
    return []


def _crawl_pages_recursively(
    site_url: str, max_crawl_depth: int, should_stay_on_domain: bool
) -> list[LangchainDocument]:
    """Crawl the site from site_url with a synchronous RecursiveUrlLoader, each page turned into text by
    _extract_page_text."""
    logger.warning("No sitemap found, using RecursiveUrlLoader...")
    crawl_loader = RecursiveUrlLoader(
        url=site_url,
        max_depth=max_crawl_depth,
        extractor=functools.partial(_extract_page_text, site_url=site_url),
        prevent_outside=should_stay_on_domain,
        use_async=False,
        timeout=30,
        exclude_dirs=_CRAWL_EXCLUDED_DIRS,
        check_response_status=True,
        continue_on_failure=True,
        headers=_CRAWL_REQUEST_HEADERS,
    )
    logger.info(f"Loading documents (depth={max_crawl_depth})...")
    pages = crawl_loader.load()
    logger.info(f"   RecursiveUrlLoader found {len(pages)} pages")
    return pages


def _collect_site_pages(
    site_url: str, max_crawl_depth: int, max_page_count: int, should_stay_on_domain: bool
) -> list[LangchainDocument]:
    """The site's pages as LangChain documents: from its sitemap when one yields pages, otherwise by crawling from
    site_url; cut to max_page_count, possibly empty."""
    parsed_site_url = urlparse(site_url)
    base_domain = f"{parsed_site_url.scheme}://{parsed_site_url.netloc}"

    # STRATEGY 1: SitemapLoader
    pages = _find_pages_via_sitemaps(base_domain, max_page_count, should_stay_on_domain)

    # STRATEGY 2: RecursiveUrlLoader (sync, no async bombing)
    if not pages:
        pages = _crawl_pages_recursively(site_url, max_crawl_depth, should_stay_on_domain)

    # ── limit + filter ───────────────────────────────────────
    if len(pages) > max_page_count:
        logger.warning(f" Found {len(pages)} pages, limiting to {max_page_count}")
        pages = pages[:max_page_count]
    return pages


def _combine_page_texts(pages: list[LangchainDocument]) -> tuple[str, int]:
    """Join the pages that are not stylesheets or scripts, each under a banner with its title, its source URL and
    its position among ALL the pages (skipped pages leave gaps in the numbering). Returns the combined text and the
    number of skipped pages."""
    page_sections = []
    garbage_page_count = 0
    for page_number, page in enumerate(pages, 1):
        page_text = page.page_content
        page_url = page.metadata.get("source", page.metadata.get("loc", ""))
        if _is_stylesheet_or_script_garbage_page(page_text, page_url):
            garbage_page_count += 1
            continue
        display_url = page.metadata.get("source", page.metadata.get("loc", "unknown"))
        page_title = page.metadata.get("title", f"Page {page_number}")
        page_banner = f"\n{'=' * 80}\n📄 PAGE {page_number}/{len(pages)}\n📌 TITLE: {page_title}\n🔗 SOURCE URL: {display_url}\n{'=' * 80}\n\n"
        page_sections.append(page_banner + page_text)
    return "\n\n".join(page_sections), garbage_page_count


def _scrub_stylesheet_remnants(text: str) -> str:
    """Remove the CSS left in the combined text: @font-face / @media / @keyframes blocks and class / id rules, lines
    with 2 or more _STYLESHEET_PROPERTY_PREFIXES, rule lines starting with '.' or '#', font-weight / font-style /
    src lines; then collapse 3 or more newlines into 2."""
    scrubbed_text = re.sub(r"@font-face\s*\{[^}]*\}", "", text, flags=re.DOTALL)
    scrubbed_text = re.sub(r"@media[^{]*\{[^}]*\}", "", scrubbed_text, flags=re.DOTALL)
    scrubbed_text = re.sub(r"@keyframes[^{]*\{[^}]*\}", "", scrubbed_text, flags=re.DOTALL)
    scrubbed_text = re.sub(r"[.#][a-zA-Z_-][a-zA-Z0-9_-]*\s*\{[^}]*\}", "", scrubbed_text)

    kept_lines = []
    for line in scrubbed_text.split("\n"):
        if sum(1 for prefix in _STYLESHEET_PROPERTY_PREFIXES if prefix in line) >= 2:
            continue
        trimmed_line = line.strip()
        if trimmed_line and trimmed_line[0] in ".#" and "{" in line:
            continue
        if (
            trimmed_line.startswith("font-weight:")
            or trimmed_line.startswith("font-style:")
            or trimmed_line.startswith("src:")
        ):
            continue
        kept_lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines))


def _schedule_indexing(knowledge_document_id: str) -> None:
    """Queue the Celery task that indexes the scraped knowledge document. The import stays lazy: background_tasks
    imports this module (circular import)."""
    from background_tasks import index_knowledge_document_task

    logger.info(f"Starting embedding process for document {knowledge_document_id}")
    index_knowledge_document_task.delay(knowledge_document_id)
    logger.info(f"Embedding task scheduled for document {knowledge_document_id}")


def _record_scrape_error(knowledge_doc: EmbeddedDocument, knowledge_document_id: str, error: Exception) -> None:
    """Log a scraping failure with its traceback and write it into the knowledge document, unless the document was
    deleted meanwhile. Called from inside the caller's except block, so traceback.print_exc() still sees the error."""
    logger.error(f"Error in scrape_url_and_schedule_indexing for document {knowledge_document_id}: {error}")
    traceback.print_exc()
    # Save error info so the user can see something went wrong
    try:
        knowledge_doc.reload()
    except EmbeddedDocument.DoesNotExist:
        logger.error(f"Document {knowledge_document_id} no longer exists, skipping error save")
        return
    knowledge_doc.content = f"Error during scraping: {str(error)}"
    knowledge_doc.save()


def scrape_url_and_schedule_indexing(
    knowledge_document_id: str, start_url: str, max_crawl_depth: int = 3, max_page_count: int = 500, should_stay_on_same_domain: bool = True
):
    """
    Scrape a URL, save the content to the EmbeddedDocument, then trigger embedding.

    This runs synchronously inside a Celery worker.
    """
    knowledge_doc = EmbeddedDocument.objects(id=knowledge_document_id).first()
    if not knowledge_doc:
        logger.error(f"scrape_url_and_schedule_indexing: document {knowledge_document_id} not found")
        return

    try:
        logger.info(f"Starting web scraping: {start_url}")
        logger.info(f"   Depth: {max_crawl_depth} | Max pages: {max_page_count} | Same domain: {should_stay_on_same_domain}")

        pages = _collect_site_pages(start_url, max_crawl_depth, max_page_count, should_stay_on_same_domain)
        if not pages:
            logger.error("No content could be extracted from the URL")
            knowledge_doc.content = "Error: No content could be extracted from the URL."
            knowledge_doc.save()
            return

        logger.info(f"Successfully scraped {len(pages)} page(s)")

        # ── combine content ──────────────────────────────────────
        combined_text, garbage_page_count = _combine_page_texts(pages)

        # ── final CSS cleanup ────────────────────────────────────
        combined_text = _scrub_stylesheet_remnants(combined_text)

        logger.warning(f"   Skipped {garbage_page_count} CSS/JS files")

        if not combined_text.strip():
            logger.error("The scraped content is empty after filtering CSS/JS")
            knowledge_doc.content = "Error: scraped content was empty after filtering."
            knowledge_doc.save()
            return

        logger.warning(f"Total content: {len(combined_text)} characters from {len(pages) - garbage_page_count} valid page(s)")

        # ── save content to document ─────────────────────────────
        knowledge_doc.content = combined_text
        knowledge_doc.save()

        # ── trigger embedding ────────────────────────────────────
        _schedule_indexing(knowledge_document_id)

    except Exception as error:
        _record_scrape_error(knowledge_doc, knowledge_document_id, error)
