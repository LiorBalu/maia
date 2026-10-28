"""
Scrape URL and create embedded document content.

This module extracts the scraping logic so it can run in a Celery worker
(outside of the FastAPI request lifecycle).
"""

import logging
import re
import traceback

from maia.models.knowledge_documents import EmbeddedDocument

logger = logging.getLogger(__name__)


def scrape_url_and_embed(
    document_id: str, url: str, max_depth: int = 3, max_pages: int = 500, same_domain_only: bool = True
):
    """
    Scrape a URL, save the content to the EmbeddedDocument, then trigger embedding.

    This runs synchronously inside a Celery worker.
    """
    from urllib.parse import urljoin, urlparse

    from bs4 import BeautifulSoup
    from langchain_community.document_loaders import RecursiveUrlLoader
    from langchain_community.document_loaders.sitemap import SitemapLoader

    document = EmbeddedDocument.objects(id=document_id).first()
    if not document:
        logger.error(f"scrape_url_and_embed: document {document_id} not found")
        return

    try:
        logger.info(f"Starting web scraping: {url}")
        logger.info(f"   Depth: {max_depth} | Max pages: {max_pages} | Same domain: {same_domain_only}")

        # ── helpers ──────────────────────────────────────────────

        def bs4_extractor(html: str) -> str:
            """HTML to text for RecursiveUrlLoader: skip CSS / JS blobs, strip scripts, styles, navigation and cookie
            banners, keep the main content, dedupe lines and append the image links.
            """
            html_sample = html[:2000].lower()
            if any(
                indicator in html_sample
                for indicator in [
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
            ):
                css_count = sum(1 for x in ["{", "}", ";"] if html_sample.count(x) > 20)
                if css_count >= 2 and "<html" not in html_sample and "<body" not in html_sample:
                    return ""

            try:
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                try:
                    soup = BeautifulSoup(html, "html.parser")
                except Exception as parse_err:
                    logger.error(f"   Skipping page, parser error: {parse_err}")
                    return ""
            for element in soup(["script", "style", "noscript", "iframe", "svg", "link"]):
                element.decompose()
            for element in soup.find_all(style=True):
                del element["style"]

            junk_selectors = [
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
            for selector in junk_selectors:
                for element in soup.select(selector):
                    element.decompose()

            images = []
            for img in soup.find_all("img"):
                img_src = img.get("src", "") or img.get("data-src", "")
                img_alt = img.get("alt", "")
                if img_src and not img_src.startswith("data:"):
                    if img_src.startswith("/"):
                        img_src = urljoin(url, img_src)
                    elif not img_src.startswith("http"):
                        img_src = urljoin(url, img_src)
                    images.append(f"[IMAGE: {img_alt if img_alt else 'No description'}]({img_src})")

            main_content = None
            content_selectors = [
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
            for selector in content_selectors:
                main_content = soup.select_one(selector)
                if main_content:
                    break

            content_area = main_content if main_content else soup.body if soup.body else soup
            text_content = content_area.get_text(separator="\n", strip=True)

            lines = text_content.split("\n")
            seen_lines = set()
            clean_lines = []
            for line in lines:
                line = line.strip()
                if len(line) < 3 or line in seen_lines:
                    continue
                seen_lines.add(line)
                clean_lines.append(line)
            text_content = "\n".join(clean_lines)

            if images:
                images_section = "\n\n📸 IMAGES FOUND ON THIS PAGE:\n" + "\n".join(f"  • {img}" for img in images[:10])
                text_content += images_section
            return text_content

        def sitemap_parser(content: BeautifulSoup) -> str:
            """BeautifulSoup page to text for SitemapLoader: empty for CSS / JS pages, otherwise the page text without
            navigation and junk.
            """
            raw_text = str(content)
            css_indicators = [
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
            if sum(1 for i in css_indicators if i in raw_text[:5000]) >= 3:
                return ""
            js_indicators = [
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
            if sum(1 for i in js_indicators if i in raw_text[:5000]) >= 3:
                return ""

            for element in content.find_all(["script", "style", "noscript", "iframe", "svg"]):
                element.decompose()
            junk_selectors = [
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
            for sel in junk_selectors:
                for el in content.select(sel):
                    el.decompose()
            text = content.get_text(separator="\n", strip=True)
            lines = text.split("\n")
            css_lines = sum(
                1 for line in lines if any(x in line for x in ["{", "}", ";", ":", "px", "em", "rem", "#", "rgb"])
            )
            if len(lines) > 5 and css_lines / len(lines) > 0.5:
                return ""
            return text

        def is_css_or_js_garbage(text: str, source_url: str = "") -> bool:
            """True when a scraped page is really a stylesheet or a script (by URL, by CSS markers or by brace
            density).
            """
            if not text or len(text.strip()) < 50:
                return True
            url_lower = source_url.lower()
            if any(
                x in url_lower
                for x in [
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
            ):
                return True
            text_clean = text.strip()
            first_char = text_clean[0] if text_clean else ""
            if first_char in ".#@" or text_clean.startswith("/*"):
                return True
            css_markers = [
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
            if sum(1 for m in css_markers if m in text_clean) >= 3:
                return True
            bracket_count = text_clean.count("{") + text_clean.count("}")
            semicolon_count = text_clean.count(";")
            text_len = len(text_clean)
            if text_len > 200:
                if (bracket_count / text_len) > 0.01 or (semicolon_count / text_len) > 0.02:
                    return True
            if any(x in text_clean for x in ["}{.", "}{#", ";}", ":{", "}@"]):
                return True
            return False

        # ── scraping strategies ──────────────────────────────────

        parsed_url = urlparse(url)
        base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
        documents = []

        # STRATEGY 1: SitemapLoader
        sitemap_urls = [
            f"{base_domain}/sitemap.xml",
            f"{base_domain}/wp-sitemap.xml",
            f"{base_domain}/sitemap_index.xml",
            f"{base_domain}/sitemap-index.xml",
        ]
        sitemap_found = False
        skip_patterns = [
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

        for sitemap_url in sitemap_urls:
            try:
                logger.info(f" Trying sitemap: {sitemap_url}")
                import requests

                resp = requests.head(sitemap_url, timeout=5, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                logger.info(f"Found sitemap at: {sitemap_url}")
                filter_urls = [base_domain] if same_domain_only else None
                sitemap_loader = SitemapLoader(
                    web_path=sitemap_url,
                    filter_urls=filter_urls,
                    parsing_function=sitemap_parser,
                    requests_per_second=3,
                    requests_kwargs={"timeout": 15},
                )
                loaded_count = 0
                for doc in sitemap_loader.lazy_load():
                    if loaded_count >= max_pages:
                        break
                    source = doc.metadata.get("source", "").lower()
                    if any(skip in source for skip in skip_patterns):
                        continue
                    if not doc.page_content or len(doc.page_content.strip()) < 50:
                        continue
                    documents.append(doc)
                    loaded_count += 1
                    logger.info(f"   Loaded ({loaded_count}/{max_pages}): {source[:60]}...")
                if documents:
                    sitemap_found = True
                    logger.info(f"Sitemap loaded {len(documents)} pages")
                    break
            except Exception as e:
                logger.error(f"   Sitemap {sitemap_url} failed: {str(e)[:50]}")
                continue

        # STRATEGY 2: RecursiveUrlLoader (sync, no async bombing)
        if not sitemap_found or len(documents) == 0:
            logger.warning("No sitemap found, using RecursiveUrlLoader...")
            exclude_dirs = (
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

            loader = RecursiveUrlLoader(
                url=url,
                max_depth=max_depth,
                extractor=bs4_extractor,
                prevent_outside=same_domain_only,
                use_async=False,
                timeout=30,
                exclude_dirs=exclude_dirs,
                check_response_status=True,
                continue_on_failure=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5,he;q=0.3",
                },
            )
            logger.info(f"Loading documents (depth={max_depth})...")
            documents = loader.load()
            logger.info(f"   RecursiveUrlLoader found {len(documents)} pages")

        # ── limit + filter ───────────────────────────────────────
        if len(documents) > max_pages:
            logger.warning(f" Found {len(documents)} pages, limiting to {max_pages}")
            documents = documents[:max_pages]

        if not documents:
            logger.error("No content could be extracted from the URL")
            document.content = "Error: No content could be extracted from the URL."
            document.save()
            return

        logger.info(f"Successfully scraped {len(documents)} page(s)")

        # ── combine content ──────────────────────────────────────
        combined_parts = []
        skipped_count = 0
        for idx, doc in enumerate(documents, 1):
            page_content = doc.page_content
            source_url = doc.metadata.get("source", doc.metadata.get("loc", ""))
            if is_css_or_js_garbage(page_content, source_url):
                skipped_count += 1
                continue
            page_url = doc.metadata.get("source", doc.metadata.get("loc", "unknown"))
            page_title = doc.metadata.get("title", f"Page {idx}")
            page_header = f"\n{'=' * 80}\n📄 PAGE {idx}/{len(documents)}\n📌 TITLE: {page_title}\n🔗 SOURCE URL: {page_url}\n{'=' * 80}\n\n"
            combined_parts.append(page_header + page_content)

        content = "\n\n".join(combined_parts)

        # ── final CSS cleanup ────────────────────────────────────
        content = re.sub(r"@font-face\s*\{[^}]*\}", "", content, flags=re.DOTALL)
        content = re.sub(r"@media[^{]*\{[^}]*\}", "", content, flags=re.DOTALL)
        content = re.sub(r"@keyframes[^{]*\{[^}]*\}", "", content, flags=re.DOTALL)
        content = re.sub(r"[.#][a-zA-Z_-][a-zA-Z0-9_-]*\s*\{[^}]*\}", "", content)

        lines = content.split("\n")
        clean_lines = []
        css_props = [
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
        for line in lines:
            if sum(1 for p in css_props if p in line) >= 2:
                continue
            stripped = line.strip()
            if stripped and stripped[0] in ".#" and "{" in line:
                continue
            if stripped.startswith("font-weight:") or stripped.startswith("font-style:") or stripped.startswith("src:"):
                continue
            clean_lines.append(line)
        content = "\n".join(clean_lines)
        content = re.sub(r"\n{3,}", "\n\n", content)

        logger.warning(f"   Skipped {skipped_count} CSS/JS files")

        if not content.strip():
            logger.error("The scraped content is empty after filtering CSS/JS")
            document.content = "Error: scraped content was empty after filtering."
            document.save()
            return

        logger.warning(f"Total content: {len(content)} characters from {len(documents) - skipped_count} valid page(s)")

        # ── save content to document ─────────────────────────────
        document.content = content
        document.save()

        # ── trigger embedding ────────────────────────────────────
        from background_tasks import index_document_task

        logger.info(f"Starting embedding process for document {document_id}")
        index_document_task.delay(document_id)
        logger.info(f"Embedding task scheduled for document {document_id}")

    except Exception as e:
        logger.error(f"Error in scrape_url_and_embed for document {document_id}: {e}")
        traceback.print_exc()
        # Save error info so the user can see something went wrong
        try:
            document.reload()
        except EmbeddedDocument.DoesNotExist:
            logger.error(f"Document {document_id} no longer exists, skipping error save")
            return
        document.content = f"Error during scraping: {str(e)}"
        document.save()
