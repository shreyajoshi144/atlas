from __future__ import annotations

import asyncio
import hashlib
import re
import ssl
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional
from urllib.parse import urlparse

import aiohttp
import certifi
from bs4 import BeautifulSoup

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.domain_models import RankedSource, ScrapedContent

logger = get_logger(__name__)


class AsyncScrapingService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.timeout = aiohttp.ClientTimeout(total=self.settings.scrape_timeout_seconds)
        self.max_retries = self.settings.scrape_max_retries
        self.max_chars = self.settings.max_content_chars_per_source
        self.concurrency = self.settings.scrape_concurrency
        self.headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def scrape_sources(self, sources: list[RankedSource]) -> list[ScrapedContent]:
        if not sources:
            return []

        semaphore = asyncio.Semaphore(self.concurrency)
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        async with aiohttp.ClientSession(
            timeout=self.timeout,
            headers=self.headers,
            raise_for_status=False,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
        ) as session:
            tasks = [
                self._scrape_with_semaphore(semaphore, session, source)
                for source in sources
            ]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        scraped_results: list[ScrapedContent] = []
        for result in raw_results:
            if isinstance(result, Exception):
                logger.exception("Unhandled scraping task error", exc_info=result)
                continue
            scraped_results.append(result)

        deduped_results = self._deduplicate_by_content(scraped_results)

        logger.info(
            "Completed async scraping",
            extra={
                "requested_sources": len(sources),
                "successful_scrapes": len([r for r in scraped_results if r.success]),
                "deduplicated_results": len(deduped_results),
            },
        )
        return deduped_results

    async def _scrape_with_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        session: aiohttp.ClientSession,
        source: RankedSource,
    ) -> ScrapedContent:
        async with semaphore:
            return await self._scrape_with_retries(session, source)

    async def _scrape_with_retries(
        self,
        session: aiohttp.ClientSession,
        source: RankedSource,
    ) -> ScrapedContent:
        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            try:
                result = await self._fetch_and_parse(session, source)
                if result.success:
                    return result

                last_error = result.error_message
            except asyncio.TimeoutError:
                last_error = f"Timeout after {self.settings.scrape_timeout_seconds}s"
            except aiohttp.ClientError as exc:
                last_error = f"HTTP client error: {str(exc)}"
            except Exception as exc:
                last_error = f"Unexpected scraping error: {str(exc)}"

            logger.warning(
                "Scrape attempt failed",
                extra={
                    "url": str(source.url),
                    "attempt": attempt + 1,
                    "max_attempts": self.max_retries + 1,
                    "error": last_error,
                },
            )

            if attempt < self.max_retries:
                await asyncio.sleep(0.75 * (attempt + 1))

        return ScrapedContent(
            source_id=source.source_id,
            url=str(source.url),
            title=source.title,
            domain=source.domain,
            publish_date=source.publish_date,
            content="",
            content_hash="",
            success=False,
            http_status=None,
            error_message=last_error or "Unknown scraping failure",
            content_length=0,
        )

    async def _fetch_and_parse(
        self,
        session: aiohttp.ClientSession,
        source: RankedSource,
    ) -> ScrapedContent:
        async with session.get(str(source.url), allow_redirects=True) as response:
            status = response.status
            html = await response.text(errors="ignore")

            if status >= 400:
                return ScrapedContent(
                    source_id=source.source_id,
                    url=str(source.url),
                    title=source.title,
                    domain=source.domain,
                    publish_date=source.publish_date,
                    content="",
                    content_hash="",
                    success=False,
                    http_status=status,
                    error_message=f"HTTP {status}",
                    content_length=0,
                )

            parsed = self._extract_content(
                html=html,
                url=str(response.url),
                fallback_title=source.title,
                fallback_domain=source.domain,
                headers=response.headers,
                fallback_publish_date=source.publish_date,
            )

            return ScrapedContent(
                source_id=source.source_id,
                url=str(source.url),
                title=parsed["title"],
                domain=parsed["domain"],
                publish_date=parsed["publish_date"],
                content=parsed["content"],
                content_hash=self._hash_content(parsed["content"]),
                success=bool(parsed["content"].strip()),
                http_status=status,
                error_message=None if parsed["content"].strip() else "Empty parsed content",
                content_length=len(parsed["content"]),
            )

    def _extract_content(
        self,
        html: str,
        url: str,
        fallback_title: str,
        fallback_domain: str,
        headers: aiohttp.typedefs.LooseHeaders,
        fallback_publish_date: Optional[date],
    ) -> dict:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(
            [
                "script",
                "style",
                "noscript",
                "svg",
                "form",
                "iframe",
                "nav",
                "footer",
                "header",
                "aside",
            ]
        ):
            tag.decompose()

        title = self._extract_title(soup, fallback_title)
        publish_date = self._extract_publish_date(soup, headers, fallback_publish_date)
        domain = self._extract_domain(url) or fallback_domain
        content = self._extract_main_text(soup)
        content = self._normalize_whitespace(content)[: self.max_chars]

        return {
            "title": title,
            "domain": domain,
            "publish_date": publish_date,
            "content": content,
        }

    def _extract_title(self, soup: BeautifulSoup, fallback_title: str) -> str:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            return og_title["content"].strip()

        twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
        if twitter_title and twitter_title.get("content"):
            return twitter_title["content"].strip()

        if soup.title and soup.title.string:
            return soup.title.string.strip()

        h1 = soup.find("h1")
        if h1:
            return h1.get_text(" ", strip=True)

        return fallback_title

    def _extract_publish_date(
        self,
        soup: BeautifulSoup,
        headers: aiohttp.typedefs.LooseHeaders,
        fallback_publish_date: Optional[date],
    ) -> Optional[date]:
        meta_candidates = [
            ("meta", {"property": "article:published_time"}, "content"),
            ("meta", {"name": "publish_date"}, "content"),
            ("meta", {"name": "pubdate"}, "content"),
            ("meta", {"name": "date"}, "content"),
            ("meta", {"itemprop": "datePublished"}, "content"),
            ("time", {"datetime": True}, "datetime"),
        ]

        for tag_name, attrs, value_attr in meta_candidates:
            tag = soup.find(tag_name, attrs=attrs)
            if tag and tag.get(value_attr):
                parsed = self._parse_date_string(tag.get(value_attr))
                if parsed:
                    return parsed

        last_modified = headers.get("Last-Modified")
        if last_modified:
            try:
                return parsedate_to_datetime(last_modified).date()
            except Exception:
                pass

        return fallback_publish_date

    def _extract_main_text(self, soup: BeautifulSoup) -> str:
        selectors = [
            "article",
            "main",
            "[role='main']",
            ".article-content",
            ".post-content",
            ".entry-content",
            ".content",
        ]

        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if len(text) > 300:
                    return text

        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        merged = " ".join(p for p in paragraphs if len(p) > 40)

        if merged.strip():
            return merged

        return soup.get_text(" ", strip=True)

    def _deduplicate_by_content(
        self,
        results: Iterable[ScrapedContent],
    ) -> list[ScrapedContent]:
        seen_hashes: set[str] = set()
        deduped: list[ScrapedContent] = []

        for result in results:
            if not result.success:
                deduped.append(result)
                continue

            if not result.content_hash:
                deduped.append(result)
                continue

            if result.content_hash in seen_hashes:
                logger.info(
                    "Dropping duplicate scraped content",
                    extra={"url": str(result.url), "source_id": result.source_id},
                )
                continue

            seen_hashes.add(result.content_hash)
            deduped.append(result)

        return deduped

    @staticmethod
    def _hash_content(content: str) -> str:
        if not content.strip():
            return ""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_domain(url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc.lower().replace("www.", "")

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _parse_date_string(value: str | None) -> Optional[date]:
        if not value:
            return None

        value = value.strip()
        candidates = [
            value,
            value.replace("Z", "+00:00"),
        ]

        for candidate in candidates:
            try:
                return datetime.fromisoformat(candidate).date()
            except ValueError:
                continue

        common_formats = [
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%d-%m-%Y",
            "%b %d, %Y",
            "%B %d, %Y",
        ]

        for fmt in common_formats:
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue

        return None