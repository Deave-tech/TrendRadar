# coding=utf-8
"""HTTP clients and Feishu payload builders for publisher delivery."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import feedparser
import requests

from .models import (
    DeliveryError,
    FeedArticle,
    FetchedArticle,
    UncertainRemoteResult,
    deterministic_uuid,
    is_video_candidate,
    make_article_key,
    normalize_article_url,
)


_IMAGE_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

_ECONOMIST_IMAGE_OUTPUT_FORMATS = {
    "jpg": "jpg",
    "jpeg": "jpg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
}


@dataclass(frozen=True)
class DownloadedImage:
    """A bounded, validated image held only for the immediate Docx upload."""

    source_url: str
    final_url: str
    data: bytes
    mime_type: str
    extension: str
    sha256: str
    width: int = 0
    height: int = 0


class FeedClient:
    """Read the local RSS bridge (RSS/Atom or JSON Feed) and globally dedupe it."""

    def __init__(
        self,
        url: str,
        timeout: float,
        session: Optional[requests.Session] = None,
        *,
        publisher: str = "wsj",
        display_name: str = "WSJ",
    ):
        self.url = url
        self.timeout = timeout
        self.publisher = publisher
        self.display_name = display_name
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "application/rss+xml, application/atom+xml, application/feed+json, application/json",
                "User-Agent": f"TrendRadar-{display_name}-Delivery/1.0",
            }
        )
        for sensitive_header in ("Cookie", "Authorization", "Proxy-Authorization"):
            self.session.headers.pop(sensitive_header, None)

    def fetch(self) -> list[FeedArticle]:
        try:
            response = self.session.get(self.url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DeliveryError(
                "FEED_UNAVAILABLE", f"{self.display_name} feed 请求失败", retryable=True
            ) from exc
        if response.status_code != 200:
            raise DeliveryError(
                "FEED_HTTP_ERROR",
                f"{self.display_name} feed 返回 HTTP {response.status_code}",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        content = getattr(response, "content", b"")
        if not content:
            content = str(getattr(response, "text", "")).encode("utf-8")
        content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
        try:
            if "json" in content_type or content.lstrip().startswith(b"{"):
                raw_items = self._parse_json(content)
            else:
                raw_items = self._parse_xml(content)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise DeliveryError(
                "FEED_INVALID", f"{self.display_name} feed 无法解析", retryable=True
            ) from exc

        result: list[FeedArticle] = []
        seen: set[str] = set()
        for raw in raw_items:
            source_url = str(raw.get("url", "")).strip()
            title = _clean_text(str(raw.get("title", "")))
            if not source_url or is_video_candidate(source_url, title):
                continue
            try:
                normalized = normalize_article_url(source_url, self.publisher)
            except ValueError:
                continue
            key, article_id = make_article_key(
                normalized,
                self.publisher,
                str(raw.get("id") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(
                FeedArticle(
                    article_key=key,
                    article_id=article_id,
                    normalized_url=normalized,
                    source_url=source_url,
                    title=title or normalized,
                    published_at=str(raw.get("published_at", "") or ""),
                    author=str(raw.get("author", "") or ""),
                    publisher=self.publisher,
                )
            )
        if not result:
            raise DeliveryError(
                "FEED_EMPTY",
                f"{self.display_name} feed 没有有效文章，保留现有状态",
                retryable=True,
            )
        return result

    @staticmethod
    def _parse_json(content: bytes) -> list[dict]:
        payload = json.loads(content.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON feed root must be an object")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("JSON feed items must be a list")
        result = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "url": item.get("url") or item.get("external_url") or "",
                    "id": item.get("id") or "",
                    "title": item.get("title") or "",
                    "published_at": item.get("date_published") or item.get("date_modified") or "",
                    "author": _json_feed_author(item),
                }
            )
        return result

    @staticmethod
    def _parse_xml(content: bytes) -> list[dict]:
        parsed = feedparser.parse(content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ValueError("invalid XML feed")
        result = []
        for entry in parsed.entries:
            authors = entry.get("authors") or []
            author = entry.get("author") or ""
            if not author and authors:
                author = ", ".join(str(value.get("name", "")) for value in authors if value.get("name"))
            result.append(
                {
                    "url": entry.get("link") or "",
                    "id": entry.get("id") or entry.get("guid") or "",
                    "title": entry.get("title") or "",
                    "published_at": entry.get("published") or entry.get("updated") or "",
                    "author": author,
                }
            )
        return result


class BPCClient:
    """Strict client for one publisher-specific BPC API contract."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float,
        session: Optional[requests.Session] = None,
        *,
        publisher: str = "wsj",
        endpoint: str = "/v1/fetch",
    ) -> None:
        if not endpoint.startswith("/") or "//" in endpoint:
            raise ValueError("invalid BPC endpoint")
        self.url = f"{base_url.rstrip('/')}{endpoint}"
        self.token = token
        self.timeout = timeout
        self.publisher = publisher
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False

    def fetch(self, url: str, request_id: str, fallback_title: str = "") -> FetchedArticle:
        try:
            response = self.session.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={"url": url, "requestId": request_id},
                timeout=(5, self.timeout),
            )
        except requests.Timeout as exc:
            raise DeliveryError("BPC_TIMEOUT", "BPC 正文抓取超时", retryable=True) from exc
        except requests.RequestException as exc:
            raise DeliveryError("BPC_UNAVAILABLE", "BPC 正文抓取服务不可用", retryable=True) from exc

        payload = _response_json(response)
        if response.status_code != 200 or not payload.get("ok"):
            remote_code = str(payload.get("code") or f"HTTP_{response.status_code}").upper()
            retryable = bool(payload.get("retryable"))
            if response.status_code in {429, 502, 503}:
                retryable = True
            systemic = _is_systemic_bpc_error(response.status_code, remote_code)
            message = f"BPC 抓取失败 ({remote_code})"
            raise DeliveryError(remote_code, message, retryable=retryable, systemic=systemic)

        article = payload.get("article")
        if not isinstance(article, dict):
            raise DeliveryError("BPC_INVALID_RESPONSE", "BPC 响应缺少 article", retryable=True)
        status = article.get("status", 200)
        if status not in (None, 200, "200"):
            raise DeliveryError("BPC_UPSTREAM_STATUS", "BPC 上游状态不是 200", retryable=True)

        final_url = (
            article.get("canonicalUrl")
            or article.get("canonical_url")
            or article.get("finalUrl")
            or article.get("url")
            or article.get("source")
            or url
        )
        try:
            canonical_url = normalize_article_url(str(final_url), self.publisher)
        except ValueError as exc:
            raise DeliveryError(
                "BPC_INVALID_FINAL_URL",
                "BPC 返回了不允许的最终地址",
                retryable=False,
                systemic=True,
            ) from exc

        paragraphs = article.get("paragraphs")
        if not isinstance(paragraphs, list):
            raw_text = str(article.get("text") or "")
            paragraphs = re.split(r"\n\s*\n|\r?\n", raw_text)
        clean_paragraphs = tuple(
            value for value in (_clean_text(str(part)) for part in paragraphs) if value
        )
        text = "\n\n".join(clean_paragraphs)
        # Match the BPC server's conservative allowance for complete, short
        # Chinese articles that fall just below 500 Unicode code points.
        if len(clean_paragraphs) < 3 or len(text) < 480:
            raise DeliveryError(
                "BPC_QUALITY_GATE",
                "BPC 正文未通过客户端质量门",
                retryable=False,
            )
        title = _clean_text(str(article.get("title") or fallback_title))
        if not title:
            raise DeliveryError("BPC_MISSING_TITLE", "BPC 正文缺少标题", retryable=False)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fetched_at = str(article.get("fetchedAt") or article.get("fetched_at") or "")
        if not fetched_at:
            fetched_at = datetime.now(timezone.utc).isoformat()
        body_items = _build_body_items(
            clean_paragraphs,
            article.get("images"),
            publisher=self.publisher,
        )
        return FetchedArticle(
            canonical_url=canonical_url,
            title=title,
            author=_clean_text(str(article.get("author") or "")),
            published_at=str(article.get("publishedAt") or article.get("published_at") or ""),
            paragraphs=clean_paragraphs,
            text=text,
            sha256=digest,
            fetched_at=fetched_at,
            request_id=str(payload.get("requestId") or request_id),
            body_items=body_items,
        )


class ImageDownloader:
    """Download only publisher-owned HTTPS images with bounded redirects and bytes.

    Image URLs ultimately originate in page HTML.  Treat them as untrusted even
    though the local BPC extractor has already restricted them to the article
    section.  Every redirect is checked again and DNS must resolve exclusively
    to globally routable addresses before requests is allowed to connect.
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_bytes: int = 10 * 1024 * 1024,
        max_redirects: int = 3,
        max_pixels: int = 40_000_000,
        allowed_hosts: Sequence[str] = ("images.wsj.net",),
        referer: str = "https://cn.wsj.com/",
        user_agent: str = "TrendRadar-WSJ-Image/1.0",
        session: Optional[requests.Session] = None,
        resolver: Callable[..., list] = socket.getaddrinfo,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_pixels = max_pixels
        self.allowed_hosts = tuple(host.lower().rstrip(".") for host in allowed_hosts)
        self.session = session or requests.Session()
        self.resolver = resolver
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept": "image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1",
                "Referer": referer,
                "User-Agent": user_agent,
                "Accept-Encoding": "identity",
            }
        )
        for sensitive_header in ("Cookie", "Authorization", "Proxy-Authorization"):
            self.session.headers.pop(sensitive_header, None)

    def download(self, source_url: str) -> DownloadedImage:
        current = normalize_article_image_url(source_url, self.allowed_hosts)
        response = None
        for redirect_count in range(self.max_redirects + 1):
            self._validate_public_dns(current)
            cookies = getattr(self.session, "cookies", None)
            if cookies is not None and hasattr(cookies, "clear"):
                cookies.clear()
            try:
                response = self.session.get(
                    current,
                    stream=True,
                    allow_redirects=False,
                    timeout=(5, self.timeout),
                )
            except requests.Timeout as exc:
                raise DeliveryError(
                    "IMAGE_DOWNLOAD_TIMEOUT", "正文图片下载超时", retryable=True
                ) from exc
            except requests.RequestException as exc:
                raise DeliveryError(
                    "IMAGE_DOWNLOAD_UNAVAILABLE", "正文图片下载失败", retryable=True
                ) from exc

            if response.status_code in {301, 302, 303, 307, 308}:
                location = str(response.headers.get("Location") or "").strip()
                response.close()
                if not location:
                    raise DeliveryError(
                        "IMAGE_REDIRECT_INVALID", "正文图片重定向缺少地址", retryable=False
                    )
                if redirect_count >= self.max_redirects:
                    raise DeliveryError(
                        "IMAGE_TOO_MANY_REDIRECTS", "正文图片重定向次数过多", retryable=False
                    )
                current = normalize_article_image_url(
                    urljoin(current, location), self.allowed_hosts
                )
                continue
            break

        assert response is not None
        try:
            if response.status_code != 200:
                raise DeliveryError(
                    "IMAGE_HTTP_ERROR",
                    f"正文图片返回 HTTP {response.status_code}",
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            raw_length = str(response.headers.get("Content-Length") or "").strip()
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except ValueError:
                    declared_length = -1
                if declared_length == 0 or declared_length > self.max_bytes:
                    raise DeliveryError(
                        "IMAGE_SIZE_INVALID", "正文图片大小不符合限制", retryable=False
                    )

            data = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        raise DeliveryError(
                            "IMAGE_TOO_LARGE", "正文图片超过大小限制", retryable=False
                        )
            except DeliveryError:
                raise
            except (requests.Timeout, requests.ConnectionError, requests.RequestException) as exc:
                raise DeliveryError(
                    "IMAGE_DOWNLOAD_INTERRUPTED",
                    "正文图片下载中断",
                    retryable=True,
                ) from exc
            if not data:
                raise DeliveryError("IMAGE_EMPTY", "正文图片为空", retryable=False)
            if raw_length and declared_length >= 0 and len(data) != declared_length:
                raise DeliveryError(
                    "IMAGE_LENGTH_MISMATCH",
                    "正文图片声明长度与实际内容不一致",
                    retryable=True,
                )

            detected_mime, width, height = _detect_image_info(bytes(data))
            header_mime = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if (
                not detected_mime
                or header_mime not in _IMAGE_MIME_EXTENSIONS
                or header_mime != detected_mime
            ):
                raise DeliveryError(
                    "IMAGE_TYPE_INVALID", "正文图片格式或 Content-Type 不合法", retryable=False
                )
            if (
                width <= 0
                or height <= 0
                or width > 20_000
                or height > 20_000
                or width * height > self.max_pixels
            ):
                raise DeliveryError(
                    "IMAGE_DIMENSIONS_INVALID",
                    "正文图片像素尺寸不符合限制",
                    retryable=False,
                )
            digest = hashlib.sha256(data).hexdigest()
            return DownloadedImage(
                source_url=normalize_article_image_url(source_url, self.allowed_hosts),
                final_url=current,
                data=bytes(data),
                mime_type=detected_mime,
                extension=_IMAGE_MIME_EXTENSIONS[detected_mime],
                sha256=digest,
                width=width,
                height=height,
            )
        finally:
            response.close()

    def _validate_public_dns(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        try:
            answers = self.resolver(host, 443, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            raise DeliveryError(
                "IMAGE_DNS_FAILED", "正文图片域名解析失败", retryable=True
            ) from exc
        addresses = {str(answer[4][0]).split("%", 1)[0] for answer in answers if answer[4]}
        if not addresses:
            raise DeliveryError("IMAGE_DNS_EMPTY", "正文图片域名没有地址", retryable=True)
        try:
            safe = all(ipaddress.ip_address(value).is_global for value in addresses)
        except ValueError as exc:
            raise DeliveryError(
                "IMAGE_DNS_INVALID", "正文图片域名返回了无效地址", retryable=False
            ) from exc
        if not safe:
            raise DeliveryError(
                "IMAGE_SSRF_BLOCKED", "正文图片解析到非公网地址", retryable=False
            )


class FeishuClient:
    """Minimal tenant-app client for docx creation, sharing, and card delivery."""

    API_BASE = "https://open.feishu.cn/open-apis"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        receive_id_type: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        block_interval: float = 0.4,
        max_attempts: int = 4,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.session = session or requests.Session()
        self.timeout = timeout
        self.sleep = sleep
        self.clock = clock
        self.block_interval = max(0.4, block_interval)
        self.max_attempts = max(1, max_attempts)
        self._access_token = ""
        self._token_expires_at = 0.0
        self._last_block_call_at: Optional[float] = None
        self._last_delete_call_at: Optional[float] = None

    def create_document(self, title: str) -> str:
        result = self._call(
            "POST",
            "/docx/v1/documents",
            operation="CREATE_DOCUMENT",
            json_body={"title": title[:800]},
            uncertain_on_network=True,
            uncertain_on_server_error=True,
        )
        document = (result.get("data") or {}).get("document") or {}
        document_id = str(document.get("document_id") or "")
        if not document_id:
            raise UncertainRemoteResult(
                "FEISHU_CREATE_DOCUMENT_UNKNOWN",
                "飞书创建文档响应缺少 document_id，需人工核对",
            )
        return document_id

    def delete_document(self, document_id: str) -> None:
        """Delete one application-owned Docx through the Drive API.

        Deleting the same token converges on the same absent state.  The
        caller persists its intent before this call, so a lost response or
        crash is recovered by replaying DELETE and accepting the documented
        not-found/already-deleted results as success.
        """
        if self._last_delete_call_at is not None:
            wait = self.block_interval - (self.clock() - self._last_delete_call_at)
            if wait > 0:
                self.sleep(wait)
        try:
            try:
                result = self._call(
                    "DELETE",
                    f"/drive/v1/files/{quote(document_id, safe='')}",
                    operation="DELETE_DOCUMENT",
                    params={"type": "docx"},
                )
                data = result.get("data") or {}
                if isinstance(data, dict) and str(data.get("task_id") or "").strip():
                    raise DeliveryError(
                        "FEISHU_DELETE_DOCUMENT_ASYNC_PENDING",
                        "飞书删除文档仍在异步处理中",
                        retryable=True,
                    )
            except DeliveryError as error:
                # A previous attempt may have succeeded before its response was
                # lost.  Only documented absence results are accepted here.
                if error.code in {
                    "FEISHU_DELETE_DOCUMENT_1061003",
                    "FEISHU_DELETE_DOCUMENT_1061007",
                    "FEISHU_DELETE_DOCUMENT_1065200",
                }:
                    return
                raise
        finally:
            self._last_delete_call_at = self.clock()

    def append_blocks(
        self,
        document_id: str,
        blocks: Sequence[dict],
        index: int,
        client_token: str,
    ) -> list[dict]:
        if not 1 <= len(blocks) <= 50:
            raise ValueError("each Feishu block batch must contain 1..50 blocks")
        if self._last_block_call_at is not None:
            wait = self.block_interval - (self.clock() - self._last_block_call_at)
            if wait > 0:
                self.sleep(wait)
        try:
            result = self._call(
                "POST",
                f"/docx/v1/documents/{quote(document_id, safe='')}/blocks/{quote(document_id, safe='')}/children",
                operation="APPEND_BLOCKS",
                params={"document_revision_id": -1, "client_token": client_token},
                json_body={"children": list(blocks), "index": index},
            )
            children = (result.get("data") or {}).get("children") or []
            return [value for value in children if isinstance(value, dict)]
        finally:
            self._last_block_call_at = self.clock()

    def upload_image(
        self,
        document_id: str,
        image_block_id: str,
        image: DownloadedImage,
    ) -> str:
        """Upload one image as a material attached to its empty Image Block.

        Feishu's media upload endpoint has no client_token.  A network failure
        or 5xx is therefore classified as an uncertain write and must not be
        blindly repeated by the caller.
        """

        url = f"{self.API_BASE}/drive/v1/medias/upload_all"
        token_refreshed = False
        filename = f"article-{image.sha256[:20]}.{image.extension}"
        for attempt in range(self.max_attempts):
            token = self._get_token()
            try:
                response = self.session.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "file_name": filename,
                        "parent_type": "docx_image",
                        "parent_node": image_block_id,
                        "size": str(len(image.data)),
                        "extra": json.dumps(
                            {"drive_route_token": document_id},
                            separators=(",", ":"),
                        ),
                    },
                    files={"file": (filename, image.data, image.mime_type)},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise UncertainRemoteResult(
                    "FEISHU_UPLOAD_IMAGE_UNKNOWN",
                    "飞书图片素材上传结果不确定，需人工核对",
                ) from exc

            payload = _response_json(response)
            if _is_invalid_feishu_token(response.status_code, payload) and not token_refreshed:
                self._access_token = ""
                self._token_expires_at = 0
                token_refreshed = True
                continue
            if response.status_code >= 500:
                raise UncertainRemoteResult(
                    "FEISHU_UPLOAD_IMAGE_UNKNOWN",
                    "飞书图片素材上传返回服务端错误，结果不确定，需人工核对",
                )
            if _is_retryable_feishu(response.status_code, payload):
                if attempt + 1 < self.max_attempts:
                    self.sleep(_retry_delay(response, attempt))
                    continue
                raise DeliveryError(
                    "FEISHU_UPLOAD_IMAGE_RETRY_EXHAUSTED",
                    "飞书图片素材上传暂时失败",
                    retryable=True,
                )
            if response.status_code < 200 or response.status_code >= 300 or payload.get("code") != 0:
                raise _feishu_error("UPLOAD_IMAGE", response.status_code, payload)
            file_token = str((payload.get("data") or {}).get("file_token") or "")
            if not file_token:
                raise UncertainRemoteResult(
                    "FEISHU_UPLOAD_IMAGE_UNKNOWN",
                    "飞书图片素材上传响应缺少 file_token，需人工核对",
                )
            return file_token
        raise DeliveryError(
            "FEISHU_UPLOAD_IMAGE_RETRY_EXHAUSTED",
            "飞书图片素材上传暂时失败",
            retryable=True,
        )

    def replace_image(
        self,
        document_id: str,
        image_block_id: str,
        file_token: str,
        client_token: str,
        width: int = 0,
        height: int = 0,
    ) -> None:
        if self._last_block_call_at is not None:
            wait = self.block_interval - (self.clock() - self._last_block_call_at)
            if wait > 0:
                self.sleep(wait)
        replace_image = {"token": file_token}
        # Feishu otherwise applies the Image block's 100x100 default. Supplying
        # the source dimensions preserves the aspect ratio and lets the editor
        # render large images at the document's available content width.
        if width > 0 and height > 0:
            replace_image.update({"width": int(width), "height": int(height)})
        try:
            self._call(
                "PATCH",
                f"/docx/v1/documents/{quote(document_id, safe='')}/blocks/{quote(image_block_id, safe='')}",
                operation="REPLACE_IMAGE",
                params={"document_revision_id": -1, "client_token": client_token},
                json_body={"replace_image": replace_image},
            )
        finally:
            self._last_block_call_at = self.clock()

    def share_document(self, document_id: str) -> None:
        self._call(
            "POST",
            f"/drive/v1/permissions/{quote(document_id, safe='')}/members",
            operation="SHARE_DOCUMENT",
            params={"need_notification": "false", "type": "docx"},
            json_body={
                "member_type": "openchat",
                "member_id": self.receive_id,
                "perm": "view",
                "type": "chat",
            },
            # Permission-member create has no idempotency client_token. A lost
            # response or server error may already have granted access, so do
            # not retry blindly and risk an ambiguous duplicate operation.
            uncertain_on_network=True,
            uncertain_on_server_error=True,
        )

    def send_card(self, card: dict, message_uuid: str) -> None:
        self._call(
            "POST",
            "/im/v1/messages",
            operation="SEND_MESSAGE",
            params={"receive_id_type": self.receive_id_type},
            json_body={
                "receive_id": self.receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                "uuid": message_uuid,
            },
            uncertain_on_network=True,
            uncertain_on_server_error=True,
        )

    def send_alert(self, title: str, content: str, message_uuid: str) -> None:
        card = {
            "schema": "2.0",
            "header": {
                "template": "red" if "恢复" not in title else "green",
                "title": {"tag": "plain_text", "content": title[:200]},
            },
            "body": {"elements": [{"tag": "markdown", "content": content[:4000]}]},
        }
        self.send_card(card, message_uuid)

    def _get_token(self) -> str:
        now = self.clock()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token
        url = f"{self.API_BASE}/auth/v3/tenant_access_token/internal"
        last_error: Optional[DeliveryError] = None
        for attempt in range(self.max_attempts):
            try:
                response = self.session.post(
                    url,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = DeliveryError(
                    "FEISHU_TOKEN_UNAVAILABLE",
                    "飞书 tenant token 服务不可用",
                    retryable=True,
                )
                if attempt + 1 < self.max_attempts:
                    self.sleep(2**attempt)
                    continue
                raise last_error from exc
            payload = _response_json(response)
            if _is_retryable_feishu(response.status_code, payload) and attempt + 1 < self.max_attempts:
                self.sleep(_retry_delay(response, attempt))
                continue
            if response.status_code != 200 or payload.get("code") != 0:
                raise _feishu_error("TOKEN", response.status_code, payload)
            token = str(payload.get("tenant_access_token") or "")
            if not token:
                raise DeliveryError(
                    "FEISHU_TOKEN_INVALID",
                    "飞书 tenant token 响应缺少访问凭证",
                    retryable=False,
                )
            self._access_token = token
            expires = int(payload.get("expire") or 7200)
            self._token_expires_at = now + max(300, expires)
            return token
        assert last_error is not None
        raise last_error

    def _call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        uncertain_on_network: bool = False,
        uncertain_on_server_error: bool = False,
    ) -> dict:
        url = f"{self.API_BASE}{path}"
        token_refreshed = False
        for attempt in range(self.max_attempts):
            token = self._get_token()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if uncertain_on_network:
                    raise UncertainRemoteResult(
                        f"FEISHU_{operation}_UNKNOWN",
                        "飞书写入结果不确定，需人工核对",
                    ) from exc
                if attempt + 1 < self.max_attempts:
                    self.sleep(2**attempt)
                    continue
                raise DeliveryError(
                    f"FEISHU_{operation}_UNAVAILABLE",
                    f"飞书 {operation} 服务不可用",
                    retryable=True,
                ) from exc

            payload = _response_json(response)
            if _is_invalid_feishu_token(response.status_code, payload) and not token_refreshed:
                self._access_token = ""
                self._token_expires_at = 0
                token_refreshed = True
                continue
            if uncertain_on_server_error and response.status_code >= 500:
                raise UncertainRemoteResult(
                    f"FEISHU_{operation}_UNKNOWN",
                    "飞书写入返回服务端错误，结果不确定，需人工核对",
                )
            if _is_retryable_feishu_operation(
                operation, response.status_code, payload
            ):
                if attempt + 1 < self.max_attempts:
                    self.sleep(_retry_delay(response, attempt))
                    continue
                raise DeliveryError(
                    f"FEISHU_{operation}_RETRY_EXHAUSTED",
                    f"飞书 {operation} 暂时失败",
                    retryable=True,
                )
            if response.status_code < 200 or response.status_code >= 300 or payload.get("code") != 0:
                raise _feishu_error(operation, response.status_code, payload)
            return payload
        raise DeliveryError(
            f"FEISHU_{operation}_RETRY_EXHAUSTED",
            f"飞书 {operation} 暂时失败",
            retryable=True,
        )


def build_document_plan(
    article: dict,
    source_url: str,
    *,
    include_images: bool = False,
    image_max_count: int = 20,
    source_name: str = "华尔街日报中文网",
) -> list[dict]:
    """Build a stable, ordered plan of text blocks and article-only images."""
    lines = [f"来源：{source_name}"]
    if article.get("author"):
        lines.append(f"作者：{article['author']}")
    if article.get("published_at"):
        lines.append(f"发布时间：{article['published_at']}")
    lines.extend(
        [
            f"原文地址：{source_url}",
            f"抓取时间：{article.get('fetched_at', '')}",
        ]
    )
    plan = [{"kind": "block", "block": _text_block(line)} for line in lines if line]
    raw_items = article.get("body_items_json")
    try:
        body_items = json.loads(raw_items) if raw_items else []
    except (TypeError, ValueError, json.JSONDecodeError):
        body_items = []
    if not isinstance(body_items, list) or not body_items:
        paragraphs = json.loads(article.get("paragraphs_json") or "[]")
        body_items = [{"type": "paragraph", "text": value} for value in paragraphs]

    image_count = 0
    for item in body_items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "paragraph":
            for line in _split_long_text(_clean_text(str(item.get("text") or "")), 1500):
                if line:
                    plan.append({"kind": "block", "block": _text_block(line)})
        elif kind == "image" and include_images and image_count < image_max_count:
            source = str(item.get("url") or "")
            if not source:
                continue
            plan.append(
                {
                    "kind": "image",
                    "source_url": source,
                    "alt": _clean_text(str(item.get("alt") or ""))[:500],
                    "caption": _clean_text(str(item.get("caption") or ""))[:1000],
                }
            )
            image_count += 1
            caption = _clean_text(str(item.get("caption") or ""))
            if caption:
                for line in _split_long_text(f"图片说明：{caption}", 1500):
                    plan.append(
                        {
                            "kind": "block",
                            "block": _text_block(line),
                            "image_caption_for": source,
                        }
                    )
    return plan


def build_document_blocks(article: dict, source_url: str) -> list[dict]:
    """Backward-compatible text-only block builder used by existing callers/tests."""
    return [item["block"] for item in build_document_plan(article, source_url)]


def chunks(values: Sequence[dict], size: int = 50) -> Iterable[tuple[int, list[dict]]]:
    if size < 1 or size > 50:
        raise ValueError("Feishu block chunk size must be 1..50")
    for index in range(0, len(values), size):
        yield index, list(values[index : index + size])


def build_summary_card(rows: Sequence[dict], display_name: str = "WSJ") -> dict:
    lines = []
    for row in rows:
        title = _escape_markdown(
            str(row.get("title") or row.get("feed_title") or f"{display_name} 文章")[:180]
        )
        url = str(row.get("document_url") or "")
        published = str(row.get("published_at") or row.get("feed_published_at") or "")
        line = f"• [{title}]({url})"
        if published:
            line += f"\n  {published}"
        lines.append(line)
    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"{display_name} 新文章（{len(rows)} 篇）",
            },
        },
        "body": {"elements": [{"tag": "markdown", "content": "\n\n".join(lines)}]},
    }


def partition_summary_cards(
    rows: Sequence[dict],
    max_bytes: int = 30000,
    display_name: str = "WSJ",
) -> list[list[dict]]:
    """Partition rows so the serialized interactive card remains below the limit."""
    result: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        candidate = current + [row]
        if current and _card_size(build_summary_card(candidate, display_name)) > max_bytes:
            result.append(current)
            current = [row]
        else:
            current = candidate
        if _card_size(build_summary_card(current, display_name)) > max_bytes:
            # A single unusually large row is still bounded by title truncation in
            # build_summary_card; this is a defensive hard failure rather than
            # sending an invalid request.
            raise ValueError("single Feishu card entry exceeds configured byte limit")
    if current:
        result.append(current)
    return result


def message_uuid(rows: Sequence[dict], publisher: str = "wsj") -> str:
    keys = sorted(str(row["article_key"]) for row in rows)
    return deterministic_uuid(f"trendradar-{publisher}-message:" + ",".join(keys))


def block_client_token(article_key: str, cursor: int) -> str:
    return deterministic_uuid(f"trendradar-wsj-blocks:{article_key}:{cursor}")


def image_patch_client_token(article_key: str, cursor: int) -> str:
    return deterministic_uuid(f"trendradar-wsj-image-bind:{article_key}:{cursor}")


def _text_block(content: str) -> dict:
    return {
        "block_type": 2,
        "text": {
            "elements": [{"text_run": {"content": content}}],
            "style": {},
        },
    }


def _split_long_text(value: str, limit: int) -> list[str]:
    if not value:
        return [""]
    return [value[index : index + limit] for index in range(0, len(value), limit)]


def _card_size(card: dict) -> int:
    return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _json_feed_author(item: dict) -> str:
    authors = item.get("authors") or []
    if isinstance(authors, list):
        names = [str(author.get("name", "")) for author in authors if isinstance(author, dict)]
        names = [name for name in names if name]
        if names:
            return ", ".join(names)
    author = item.get("author")
    if isinstance(author, dict):
        return str(author.get("name", ""))
    return str(author or "")


def normalize_article_image_url(url: str, allowed_hosts: Sequence[str]) -> str:
    """Validate one article image URL before it reaches the network."""
    try:
        parsed = urlsplit(str(url).strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise DeliveryError(
            "IMAGE_URL_INVALID", "正文图片地址无效", retryable=False
        ) from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = host in {base.lower().rstrip(".") for base in allowed_hosts}
    if (
        parsed.scheme.lower() != "https"
        or not host
        or not allowed
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DeliveryError(
            "IMAGE_URL_NOT_ALLOWED", "正文图片地址不在允许范围", retryable=False
        )
    path = parsed.path or "/"
    query = parsed.query
    if host == "www.economist.com":
        path = _canonicalize_economist_image_path(path)
        query = ""
    return urlunsplit(("https", host, path, query, ""))


def _canonicalize_economist_image_path(path: str) -> str:
    """Make Economist Cloudflare bytes agree with the original MIME type.

    `format=auto` returns WebP to our downloader while Economist's CDN retains
    the source asset Content-Type.  Keep strict magic/header validation and
    instead request the source format explicitly.  This also repairs durable
    render plans fetched before BPC began emitting canonical image URLs.
    """
    transformed = re.fullmatch(
        r"/cdn-cgi/image/([^/]+)(/content-assets/images/.+\.([a-z0-9]+))",
        path,
        re.IGNORECASE,
    )
    direct = re.fullmatch(
        r"/content-assets/images/.+\.([a-z0-9]+)", path, re.IGNORECASE
    )
    if transformed:
        extension = transformed.group(3)
    elif direct:
        extension = direct.group(1)
    else:
        extension = ""
    output_format = _ECONOMIST_IMAGE_OUTPUT_FORMATS.get(extension.lower())
    if not output_format:
        raise DeliveryError(
            "IMAGE_URL_NOT_ALLOWED",
            "Economist 正文图片路径或源格式不受支持",
            retryable=False,
        )
    if not transformed:
        return path
    directives = [
        item.strip()
        for item in transformed.group(1).split(",")
        if item.strip()
        and not re.match(r"^format(?:=|$)", item.strip(), re.IGNORECASE)
    ]
    directives.append(f"format={output_format}")
    return f"/cdn-cgi/image/{','.join(directives)}{transformed.group(2)}"


def _build_body_items(
    paragraphs: Sequence[str],
    raw_images,
    *,
    publisher: str = "wsj",
) -> tuple[dict, ...]:
    """Merge BPC's article-scoped images into paragraphs without reordering either."""
    images_by_slot: dict[int, list[dict]] = {}
    seen_images: set[str] = set()
    if isinstance(raw_images, list):
        for raw in raw_images[:20]:
            if not isinstance(raw, dict):
                continue
            position = raw.get("afterParagraph")
            if isinstance(position, bool) or not isinstance(position, int):
                continue
            # A final-paragraph slot is accepted only with BPC's explicit,
            # publisher-specific article-tail proof. This supports WSJ photo
            # galleries while still rejecting arbitrary footer/recommendation
            # images from a compromised or older extractor.
            article_tail = raw.get("articleTail") is True
            if position < -1 or position >= len(paragraphs):
                continue
            if position == len(paragraphs) - 1 and not article_tail:
                continue
            try:
                allowed_hosts = (
                    ("images.wsj.net",)
                    if publisher == "wsj"
                    else ("www.economist.com",)
                )
                image_url = normalize_article_image_url(
                    str(raw.get("url") or ""), allowed_hosts
                )
            except DeliveryError:
                continue
            parsed_image = urlsplit(image_url)
            if publisher == "economist":
                asset_path = re.search(
                    r"(/content-assets/images/[^?#]+)$", parsed_image.path, re.I
                )
                if not asset_path:
                    continue
                image_key = f"{parsed_image.hostname}{asset_path.group(1).lower()}"
            else:
                asset = re.match(
                    r"^/(im-[a-z0-9_-]+)(?:/|$)", parsed_image.path, re.I
                )
                image_key = (
                    f"{parsed_image.hostname}/{asset.group(1).lower()}"
                    if asset
                    else image_url
                )
            if image_key in seen_images:
                continue
            seen_images.add(image_key)
            images_by_slot.setdefault(position, []).append(
                {
                    "type": "image",
                    "url": image_url,
                    "alt": _clean_text(str(raw.get("alt") or ""))[:500],
                    "caption": _clean_text(str(raw.get("caption") or ""))[:1000],
                }
            )

    items: list[dict] = []
    items.extend(images_by_slot.get(-1, []))
    for index, paragraph in enumerate(paragraphs):
        items.append({"type": "paragraph", "text": paragraph})
        items.extend(images_by_slot.get(index, []))
    return tuple(items)


def _detect_image_info(data: bytes) -> tuple[str, int, int]:
    """Read bounded image dimensions without invoking a decompression codec."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return (
                "image/png",
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
        return "image/png", 0, 0
    if data.startswith((b"GIF87a", b"GIF89a")):
        if len(data) >= 10:
            return (
                "image/gif",
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        return "image/gif", 0, 0
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", *_jpeg_dimensions(data)
    if len(data) >= 20 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return "image/webp", width, height
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            b0, b1, b2, b3 = data[21:25]
            width = 1 + b0 + ((b1 & 0x3F) << 8)
            height = 1 + (b1 >> 6) + (b2 << 2) + ((b3 & 0x0F) << 10)
            return "image/webp", width, height
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return "image/webp", width, height
        return "image/webp", 0, 0
    return "", 0, 0


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        if marker == 0xDA:  # Start of Scan: dimensions must have appeared already.
            break
        index += segment_length
    return 0, 0


def _response_json(response) -> dict:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_systemic_bpc_error(status: int, code: str) -> bool:
    if status == 401:
        return True
    # The BPC service has already stopped its serialized WSJ queue because the
    # browser's rotated DataDome value could not be durably saved. Continuing
    # this batch would only produce the same fail-closed 503 until restart.
    if code == "SESSION_PERSIST_FAILED":
        return True
    # HTTP 403 is also used for URL_NOT_ALLOWED/FORBIDDEN_URL, which is an
    # article-specific policy error and must not stop the whole queue.
    markers = (
        "AUTH",
        "INVALID_TOKEN",
        "COOKIE",
        "CHALLENGE",
        "DATADOME",
        "CAPTCHA",
        "PAYWALL",
        "BPC_FAILURE",
        "BPC_NOT_READY",
        "SERVICE_NOT_READY",
        "BROWSER",
    )
    return any(marker in code for marker in markers)


def _is_invalid_feishu_token(status: int, payload: dict) -> bool:
    return status == 401 or payload.get("code") in {99991661, 99991663, 99991664, 99991668}


def _is_retryable_feishu(status: int, payload: dict) -> bool:
    if status == 429 or status >= 500:
        return True
    code = payload.get("code")
    if code in {99991400, 230020, 11232, 11233, 1061045}:
        return True
    message = str(payload.get("msg") or "").lower()
    return "rate limit" in message or "too many requests" in message


def _is_retryable_feishu_operation(operation: str, status: int, payload: dict) -> bool:
    """Keep Drive delete's legacy internal-error retry local to DELETE."""
    if _is_retryable_feishu(status, payload):
        return True
    return operation == "DELETE_DOCUMENT" and payload.get("code") == 1061001


def _retry_delay(response, attempt: int) -> float:
    retry_after = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
    if retry_after:
        try:
            return max(0.4, min(float(retry_after), 30.0))
        except ValueError:
            pass
    return float(min(2**attempt, 8))


def _feishu_error(operation: str, status: int, payload: dict) -> DeliveryError:
    remote_code = str(payload.get("code") or f"HTTP_{status}")
    retryable = _is_retryable_feishu(status, payload)
    return DeliveryError(
        f"FEISHU_{operation}_{remote_code}",
        f"飞书 {operation} 失败 (HTTP {status}, code {remote_code})",
        retryable=retryable,
        systemic=_is_systemic_feishu_error(operation, status, payload),
    )


def _is_systemic_feishu_error(operation: str, status: int, payload: dict) -> bool:
    """Classify only global auth/scope/target configuration failures.

    Do not infer this from every 4xx: Docx also uses 400 for article-specific
    content limits and malformed blocks.  The explicit codes below are the
    auth/scope errors documented by Feishu, plus the permission-member errors
    whose inputs (the application-owned document and configured openchat) are
    global to this delivery pipeline.
    """
    raw_code = payload.get("code")
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = None

    if status in {401, 403}:
        return True
    if operation == "TOKEN" and 400 <= status < 500:
        return True
    if code in {
        99991672,
        99991679,
        1770032,
        1061004,  # Drive resource permission denied
        1062501,  # Drive permission denied
        1062502,  # Drive permission denied
    }:
        return True
    if operation == "SHARE_DOCUMENT" and code in {
        1063001,  # configured member/type mismatch
        1063002,  # permission denied
        1063003,  # openchat/app visibility or tenant policy
        1063004,  # no share permission
    }:
        return True
    return False
