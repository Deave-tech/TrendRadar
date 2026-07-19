# coding=utf-8
"""Configuration, models, and URL rules for WSJ document delivery."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """Raised when required delivery configuration is unavailable or unsafe."""


class InitializationRequired(RuntimeError):
    """Raised when a normal run has no initialized persistent state."""


class AlreadyRunning(RuntimeError):
    """Raised when another delivery process owns the process lock."""


class DeliveryError(RuntimeError):
    """A classified, secret-free remote operation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        systemic: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code[:100]
        self.message = message[:500]
        self.retryable = retryable
        self.systemic = systemic


class UncertainRemoteResult(DeliveryError):
    """A write may have succeeded remotely and must not be repeated automatically."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)


@dataclass(frozen=True)
class DeliveryConfig:
    """Environment-backed settings for the independent WSJ delivery command."""

    bpc_api_token: str
    feishu_app_id: str
    feishu_app_secret: str
    feishu_receive_id: str
    feishu_doc_url_prefix: str
    feed_url: str = "http://127.0.0.1:4555/wsj-cn.xml"
    db_path: Path = Path("/var/lib/trendradar/wsj-delivery.db")
    bpc_base_url: str = "http://127.0.0.1:8080"
    feishu_receive_id_type: str = "chat_id"
    max_items_per_run: int = 20
    http_timeout: float = 120.0
    feed_timeout: float = 20.0
    max_drain_seconds: int = 5400
    alert_cooldown_seconds: int = 21600
    retry_base_seconds: int = 300
    retry_max_seconds: int = 21600
    circuit_min_seconds: int = 900
    block_interval_seconds: float = 0.4
    card_max_bytes: int = 30000
    image_timeout: float = 20.0
    image_max_bytes: int = 10 * 1024 * 1024
    image_max_count: int = 8
    image_max_redirects: int = 3
    image_max_pixels: int = 40_000_000
    image_allowed_hosts: tuple[str, ...] = ("images.wsj.net",)
    include_images: bool = False
    max_cloud_documents: int = 300

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "DeliveryConfig":
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = str(env.get(name, "")).strip()
            if not value:
                raise ConfigurationError(f"缺少必需环境变量: {name}")
            return value

        def int_value(name: str, default: int) -> int:
            raw = str(env.get(name, "")).strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"环境变量 {name} 必须是整数") from exc

        def float_value(name: str, default: float) -> float:
            raw = str(env.get(name, "")).strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigurationError(f"环境变量 {name} 必须是数字") from exc

        def bool_value(name: str, default: bool) -> bool:
            raw = str(env.get(name, "")).strip().lower()
            if not raw:
                return default
            if raw in {"1", "true", "yes", "on"}:
                return True
            if raw in {"0", "false", "no", "off"}:
                return False
            raise ConfigurationError(f"环境变量 {name} 必须是 true/false")

        config = cls(
            bpc_api_token=required("BPC_API_TOKEN"),
            feishu_app_id=required("FEISHU_APP_ID"),
            feishu_app_secret=required("FEISHU_APP_SECRET"),
            feishu_receive_id=required("FEISHU_RECEIVE_ID"),
            feishu_doc_url_prefix=required("FEISHU_DOC_URL_PREFIX").rstrip("/"),
            feed_url=str(env.get("WSJ_FEED_URL", "http://127.0.0.1:4555/wsj-cn.xml")).strip(),
            db_path=Path(str(env.get("WSJ_DELIVERY_DB", "/var/lib/trendradar/wsj-delivery.db")).strip()),
            bpc_base_url=str(env.get("BPC_BASE_URL", "http://127.0.0.1:8080")).strip().rstrip("/"),
            feishu_receive_id_type=str(env.get("FEISHU_RECEIVE_ID_TYPE", "chat_id")).strip(),
            max_items_per_run=int_value("WSJ_MAX_ITEMS_PER_RUN", 20),
            http_timeout=float_value("WSJ_HTTP_TIMEOUT", 120.0),
            feed_timeout=float_value("WSJ_FEED_TIMEOUT", 20.0),
            max_drain_seconds=int_value("WSJ_MAX_DRAIN_SECONDS", 5400),
            alert_cooldown_seconds=int_value("WSJ_ALERT_COOLDOWN_SECONDS", 21600),
            retry_base_seconds=int_value("WSJ_RETRY_BASE_SECONDS", 300),
            retry_max_seconds=int_value("WSJ_RETRY_MAX_SECONDS", 21600),
            circuit_min_seconds=int_value("WSJ_CIRCUIT_MIN_SECONDS", 900),
            block_interval_seconds=float_value("FEISHU_BLOCK_INTERVAL_SECONDS", 0.4),
            card_max_bytes=int_value("FEISHU_CARD_MAX_BYTES", 30000),
            image_timeout=float_value("WSJ_IMAGE_TIMEOUT", 20.0),
            image_max_bytes=int_value("WSJ_IMAGE_MAX_BYTES", 10 * 1024 * 1024),
            image_max_count=int_value("WSJ_IMAGE_MAX_COUNT", 8),
            image_max_redirects=int_value("WSJ_IMAGE_MAX_REDIRECTS", 3),
            image_max_pixels=int_value("WSJ_IMAGE_MAX_PIXELS", 40_000_000),
            image_allowed_hosts=tuple(
                value.strip().lower().rstrip(".")
                for value in str(
                    env.get(
                        "WSJ_IMAGE_ALLOWED_HOSTS",
                        "images.wsj.net",
                    )
                ).split(",")
                if value.strip()
            ),
            include_images=bool_value("WSJ_INCLUDE_IMAGES", False),
            max_cloud_documents=int_value("WSJ_MAX_CLOUD_DOCUMENTS", 300),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.db_path.is_absolute():
            raise ConfigurationError("WSJ_DELIVERY_DB 必须是绝对路径")
        if not 1 <= self.max_items_per_run <= 20:
            raise ConfigurationError("WSJ_MAX_ITEMS_PER_RUN 必须在 1 到 20 之间")
        if self.feishu_receive_id_type != "chat_id":
            raise ConfigurationError("WSJ 云文档投递当前仅支持 FEISHU_RECEIVE_ID_TYPE=chat_id")
        if not self.feishu_receive_id.startswith("oc_"):
            raise ConfigurationError("FEISHU_RECEIVE_ID 必须是以 oc_ 开头的群 chat_id")
        if self.http_timeout <= 0 or self.feed_timeout <= 0:
            raise ConfigurationError("HTTP 超时必须大于 0")
        if self.max_drain_seconds <= 0:
            raise ConfigurationError("WSJ_MAX_DRAIN_SECONDS 必须大于 0")
        if self.alert_cooldown_seconds < 21600:
            raise ConfigurationError("WSJ_ALERT_COOLDOWN_SECONDS 不得小于 21600（6 小时）")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ConfigurationError("重试退避配置无效")
        if self.block_interval_seconds < 0.4:
            raise ConfigurationError("FEISHU_BLOCK_INTERVAL_SECONDS 不得小于 0.4")
        if self.card_max_bytes > 30000 or self.card_max_bytes < 4096:
            raise ConfigurationError("FEISHU_CARD_MAX_BYTES 必须在 4096 到 30000 之间")
        if self.image_timeout <= 0 or self.image_timeout > 120:
            raise ConfigurationError("WSJ_IMAGE_TIMEOUT 必须在 0 到 120 秒之间")
        if not 1024 <= self.image_max_bytes <= 20 * 1024 * 1024:
            raise ConfigurationError("WSJ_IMAGE_MAX_BYTES 必须在 1KB 到 20MB 之间")
        if not 0 <= self.image_max_count <= 20:
            raise ConfigurationError("WSJ_IMAGE_MAX_COUNT 必须在 0 到 20 之间")
        if not 0 <= self.image_max_redirects <= 5:
            raise ConfigurationError("WSJ_IMAGE_MAX_REDIRECTS 必须在 0 到 5 之间")
        if not 1_000_000 <= self.image_max_pixels <= 100_000_000:
            raise ConfigurationError("WSJ_IMAGE_MAX_PIXELS 必须在 100 万到 1 亿之间")
        if not self.image_allowed_hosts:
            raise ConfigurationError("WSJ_IMAGE_ALLOWED_HOSTS 不得为空")
        for host in self.image_allowed_hosts:
            if (
                not host
                or host.startswith(".")
                or "*" in host
                or "/" in host
                or ":" in host
                or host == "localhost"
            ):
                raise ConfigurationError("WSJ_IMAGE_ALLOWED_HOSTS 包含无效域名")
        if not 1 <= self.max_cloud_documents <= 10000:
            raise ConfigurationError("WSJ_MAX_CLOUD_DOCUMENTS 必须在 1 到 10000 之间")
        _validate_service_url(self.feed_url, "WSJ_FEED_URL", allow_loopback_http=True)
        _validate_service_url(self.bpc_base_url, "BPC_BASE_URL", allow_loopback_http=True)
        _validate_service_url(self.feishu_doc_url_prefix, "FEISHU_DOC_URL_PREFIX", allow_loopback_http=False)


@dataclass(frozen=True)
class FeedArticle:
    article_key: str
    article_id: str
    normalized_url: str
    source_url: str
    title: str
    published_at: str = ""
    author: str = ""


@dataclass(frozen=True)
class FetchedArticle:
    canonical_url: str
    title: str
    author: str
    published_at: str
    paragraphs: tuple[str, ...]
    text: str
    sha256: str
    fetched_at: str
    request_id: str
    body_items: tuple[dict, ...] = ()


_TRACKING_PREFIXES = ("utm_", "mod")
_TRACKING_KEYS = {
    "campaign",
    "cid",
    "gaa_at",
    "gaa_n",
    "gaa_sig",
    "gaa_ts",
    "reflink",
    "st",
}
_UUID_RE = re.compile(
    r"(?:^|-)([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
_HEX_ID_RE = re.compile(r"(?:^|-)([0-9a-f]{8,40})$", re.IGNORECASE)


def normalize_wsj_url(url: str) -> str:
    """Return a strict canonical WSJ Chinese article URL, without tracking data."""
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError as exc:
        raise ValueError("invalid URL") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or hostname != "cn.wsj.com" or parsed.port not in (None, 443):
        raise ValueError("only https://cn.wsj.com is allowed")
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/")
    if not path.lower().startswith("/articles/") or path.lower() == "/articles":
        raise ValueError("only /articles/* paths are allowed")
    if _has_video_marker(path, parsed.query):
        raise ValueError("video URLs are excluded")
    # WSJ article identity lives in the path. Drop the full query string so
    # campaign parameters (including newly introduced ones) cannot bypass the
    # persistent deduplication key.
    return urlunsplit(("https", "cn.wsj.com", path, "", ""))


def is_video_candidate(url: str, title: str = "") -> bool:
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return True
    normalized_title = re.sub(r"\s+", "", title or "").lower()
    if _has_video_marker(parsed.path, parsed.query):
        return True
    return normalized_title.startswith(("视频：", "视频:", "【视频】", "[视频]"))


def _has_video_marker(path: str, query: str) -> bool:
    parts = {part.lower() for part in path.split("/") if part}
    if parts.intersection({"video", "videos"}):
        return True
    return any(
        key.lower() in {"type", "content_type"} and value.lower() == "video"
        for key, value in parse_qsl(query, keep_blank_values=True)
    )


def extract_article_id(normalized_url: str) -> str:
    slug = urlsplit(normalized_url).path.rstrip("/").rsplit("/", 1)[-1]
    for pattern in (_UUID_RE, _HEX_ID_RE):
        match = pattern.search(slug)
        if match:
            return match.group(1).lower()
    return ""


def make_article_key(normalized_url: str) -> tuple[str, str]:
    article_id = extract_article_id(normalized_url)
    if article_id:
        return f"wsj:{article_id}", article_id
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"url:{digest}", ""


def deterministic_uuid(seed: str) -> str:
    """Return a deterministic UUID with RFC 4122 variant and version-4 bits."""
    raw = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def _validate_service_url(value: str, name: str, *, allow_loopback_http: bool) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 不是有效 URL") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return
    if parsed.scheme == "http" and allow_loopback_http and host in {"127.0.0.1", "localhost", "::1"}:
        return
    raise ConfigurationError(f"{name} 必须使用 HTTPS；仅回环地址允许 HTTP")
