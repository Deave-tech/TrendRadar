# coding=utf-8
"""State-machine runner and CLI glue for publisher-to-Feishu delivery."""

from __future__ import annotations

import json
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

from .clients import (
    BPCClient,
    DownloadedImage,
    FeedClient,
    FeishuClient,
    ImageDownloader,
    block_client_token,
    build_document_plan,
    build_summary_card,
    image_patch_client_token,
    message_uuid,
    partition_summary_cards,
    _detect_image_info,
)
from .models import (
    AlreadyRunning,
    ConfigurationError,
    DeliveryConfig,
    DeliveryError,
    InitializationRequired,
    UncertainRemoteResult,
    deterministic_uuid,
)
from .outbox import Outbox, ProcessLock


@dataclass
class RunSummary:
    discovered: int = 0
    attempted: int = 0
    notified: int = 0
    manual: int = 0
    unknown: int = 0
    circuit_open: bool = False
    drain_deadline_reached: bool = False
    retention_deleted: int = 0
    retention_occupied: int = 0
    retention_excess: int = 0
    retention_capacity_blocked: bool = False
    status_counts: Optional[dict[str, int]] = None


_DOCX_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class DeliveryRunner:
    """Advance each durable article through fetch, document, share and notify."""

    def __init__(
        self,
        config: DeliveryConfig,
        outbox: Outbox,
        feed: FeedClient,
        bpc: BPCClient,
        feishu: FeishuClient,
        image_downloader: Optional[ImageDownloader] = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.outbox = outbox
        self.feed = feed
        self.bpc = bpc
        self.feishu = feishu
        self.image_downloader = image_downloader or ImageDownloader(
            timeout=config.image_timeout,
            max_bytes=config.image_max_bytes,
            max_redirects=config.image_max_redirects,
            max_pixels=config.image_max_pixels,
            allowed_hosts=config.image_allowed_hosts,
            referer=config.image_referer,
            user_agent=f"TrendRadar-{config.display_name}-Image/1.0",
        )
        self.clock = clock
        self._halt_bpc = False
        self._halt_feishu = False
        self._retention_capacity_blocked = False

    def run(
        self,
        *,
        backfill_current: bool = False,
        drain: bool = False,
        initialize_current_only: bool = False,
    ) -> RunSummary:
        if backfill_current and initialize_current_only:
            raise InitializationRequired(
                "--backfill-current 与 --initialize-current-only 不能同时使用"
            )
        if drain and initialize_current_only:
            raise InitializationRequired(
                "--initialize-current-only 不执行远程写入，不能与 --drain 同时使用"
            )
        if self.outbox.is_initialized() and initialize_current_only:
            raise InitializationRequired(
                f"{self.config.display_name} delivery state 已初始化"
            )
        if not self.outbox.is_initialized() and not (
            backfill_current or initialize_current_only
        ):
            raise InitializationRequired(
                f"{self.config.display_name} delivery state 尚未初始化；"
                "请显式运行 --backfill-current"
            )

        now = self.clock()
        if self.outbox.is_initialized():
            try:
                feed_items = self.feed.fetch()
            except DeliveryError:
                # Discovery is independent from the durable outbox. A transient
                # bridge failure must not block block/image/share/message resume.
                discovered = 0
            else:
                discovered = self.outbox.discover(feed_items, now)
        else:
            # The explicit initial backfill has no safe snapshot to fall back
            # to, so initialization still requires a successful non-empty feed.
            feed_items = self.feed.fetch()
            if initialize_current_only:
                discovered = self.outbox.initialize_current_only(feed_items, now)
            else:
                discovered = self.outbox.initialize_with_items(feed_items, now)

        summary = RunSummary(discovered=discovered)
        if initialize_current_only:
            summary.retention_occupied = self.outbox.occupied_document_count()
            summary.retention_excess = self.outbox.retention_excess(
                self.config.max_cloud_documents
            )
            summary.retention_capacity_blocked = bool(summary.retention_excess)
            summary.status_counts = self.outbox.status_counts()
            return summary
        # Resume an earlier overflow before doing new remote writes.  A second
        # pass after notification handles documents created by this run.
        summary.retention_deleted += self._enforce_document_retention()
        self._retention_capacity_blocked = bool(
            self.outbox.retention_excess(self.config.max_cloud_documents)
        )
        deadline = now + self.config.max_drain_seconds
        while True:
            if drain and self.clock() >= deadline:
                summary.drain_deadline_reached = True
                break
            attempted, manual, unknown = self._process_batch(deadline if drain else None)
            summary.attempted += attempted
            summary.manual += manual
            summary.unknown += unknown

            if not drain:
                summary.notified += self._notify_pending()
                break

            # A deterministic application-scope/auth/openchat configuration
            # failure would affect every following article.  Stop this drain
            # instead of creating a run of empty/incomplete docs.  The normal
            # one-batch path above may still flush older shared rows.
            if self._halt_feishu:
                break
            now = self.clock()
            if attempted == 0 or not self.outbox.has_eligible_work(now):
                break

        if drain:
            # A backfill may span several 20-item work batches. Notify only after
            # the whole drain so all documents from this invocation share one
            # summary card (unless the 30 KB card limit requires splitting).
            summary.notified += self._notify_pending()

        summary.retention_deleted += self._enforce_document_retention()
        summary.retention_occupied = self.outbox.occupied_document_count()
        summary.retention_excess = self.outbox.retention_excess(
            self.config.max_cloud_documents
        )
        summary.retention_capacity_blocked = bool(summary.retention_excess)

        summary.circuit_open = self.outbox.circuit_blocked(self.clock())
        summary.status_counts = self.outbox.status_counts()
        return summary

    def _process_batch(self, deadline: Optional[float] = None) -> tuple[int, int, int]:
        if self._halt_feishu:
            return 0, 0, 0
        rows = self.outbox.get_work(self.clock(), self.config.max_items_per_run)
        attempted = 0
        manual = 0
        unknown = 0
        for row in rows:
            if deadline is not None and self.clock() >= deadline:
                break
            final_status = self._process_article(row)
            latest = self.outbox.get(row["article_key"]) or row
            before = (
                row["status"],
                row["retry_count"],
                row["block_cursor"],
                row["document_id"],
            )
            after = (
                latest["status"],
                latest["retry_count"],
                latest["block_cursor"],
                latest["document_id"],
            )
            if after != before:
                attempted += 1
            if final_status == "manual":
                manual += 1
            elif final_status == "unknown":
                unknown += 1
            if self._halt_feishu:
                break
        return attempted, manual, unknown

    def _process_article(self, initial: dict) -> str:
        article_key = initial["article_key"]
        while True:
            row = self.outbox.get(article_key)
            if row is None:
                return "manual"
            status = row["status"]
            if status == "discovered":
                request_id = row.get("bpc_request_id") or deterministic_uuid(
                    f"trendradar-{self.config.publisher}-bpc:{article_key}"
                )
                self.outbox.mark_fetch_pending(article_key, request_id, self.clock())
                continue

            if status == "fetch_pending":
                if self._halt_bpc or self.outbox.circuit_blocked(self.clock()):
                    return status
                try:
                    article = self.bpc.fetch(
                        row["normalized_url"],
                        row["bpc_request_id"],
                        fallback_title=row["feed_title"],
                    )
                except DeliveryError as error:
                    if error.systemic:
                        self._handle_systemic_fetch_failure(row, error)
                        self._halt_bpc = True
                    elif error.retryable:
                        self._retry(row, error)
                    else:
                        self.outbox.mark_terminal(
                            article_key,
                            "manual",
                            error.code,
                            error.message,
                            self.clock(),
                        )
                    return self.outbox.get(article_key)["status"]
                if not self.outbox.mark_fetched(
                    article_key, article, self.clock()
                ):
                    return "manual"
                self._handle_fetch_recovery()
                continue

            if status == "fetched":
                if self._retention_capacity_blocked:
                    # A previous run is still above the configured cap.  Fetch
                    # may finish, but do not create another remote document
                    # until the oldest durable deletion has converged.
                    return status
                if not row.get("render_plan_json"):
                    plan = build_document_plan(
                        row,
                        row["canonical_url"] or row["normalized_url"],
                        include_images=self.config.include_images,
                        image_max_count=self.config.image_max_count,
                        source_name=self.config.source_name,
                    )
                    self.outbox.freeze_render_plan(
                        article_key, plan, self.clock()
                    )
                    continue
                if row.get("document_create_started_at") is not None:
                    self.outbox.mark_terminal(
                        article_key,
                        "unknown",
                        "FEISHU_CREATE_INTERRUPTED",
                        "文档创建已开始但没有持久化远程结果，需人工核对",
                        self.clock(),
                    )
                    return "unknown"
                # Write-ahead intent closes the kill window between the remote
                # create call and persisting its returned document_id.
                self.outbox.mark_doc_create_started(article_key, self.clock())
                try:
                    document_id = self.feishu.create_document(
                        f"{self.config.display_name}｜{row['title']}"
                    )
                except UncertainRemoteResult as error:
                    self.outbox.mark_terminal(
                        article_key,
                        "unknown",
                        error.code,
                        error.message,
                        self.clock(),
                    )
                    return "unknown"
                except DeliveryError as error:
                    if error.retryable or error.systemic:
                        # A rate-limit/known failure did not create a document,
                        # so a later attempt may safely start a fresh intent.
                        # The same is true for a deterministic auth/scope 4xx.
                        self.outbox.clear_doc_create_started(article_key, self.clock())
                    return self._handle_feishu_stage_error(row, error)
                document_url = f"{self.config.feishu_doc_url_prefix}/{document_id}"
                # Persist the remote ID before any block-writing request.
                self.outbox.mark_doc_created(
                    article_key, document_id, document_url, self.clock()
                )
                continue

            if status == "doc_created":
                if not row.get("render_plan_json"):
                    # Rows created by a pre-image deployment retain the exact
                    # legacy text-only semantics even if the feature flag is
                    # later enabled.
                    legacy_plan = build_document_plan(
                        row,
                        row["canonical_url"] or row["normalized_url"],
                        include_images=False,
                        source_name=self.config.source_name,
                    )
                    self.outbox.freeze_render_plan(
                        article_key, legacy_plan, self.clock()
                    )
                    continue
                try:
                    plan = json.loads(row["render_plan_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    plan = None
                if not isinstance(plan, list):
                    self.outbox.mark_terminal(
                        article_key,
                        "manual",
                        "RENDER_PLAN_INVALID",
                        "冻结的文档渲染计划无效",
                        self.clock(),
                    )
                    return "manual"
                cursor = int(row.get("block_cursor") or 0)
                document_index = int(row.get("document_block_index") or 0)
                if cursor < len(plan):
                    if plan[cursor].get("kind") == "image":
                        skip_count = 1
                        while (
                            cursor + skip_count < len(plan)
                            and plan[cursor + skip_count].get("image_caption_for")
                            == plan[cursor].get("source_url")
                        ):
                            skip_count += 1
                        outcome = self._process_image(
                            row,
                            cursor,
                            document_index,
                            plan[cursor],
                            skip_count=skip_count,
                        )
                        if outcome == "continue":
                            continue
                        return outcome

                    batch_plan = []
                    for item in plan[cursor : cursor + 50]:
                        if item.get("kind") != "block":
                            break
                        batch_plan.append(item)
                    batch = [item["block"] for item in batch_plan]
                    try:
                        self.feishu.append_blocks(
                            row["document_id"],
                            batch,
                            document_index,
                            block_client_token(article_key, cursor),
                        )
                    except UncertainRemoteResult as error:
                        self.outbox.mark_terminal(
                            article_key,
                            "unknown",
                            error.code,
                            error.message,
                            self.clock(),
                        )
                        return "unknown"
                    except DeliveryError as error:
                        return self._handle_feishu_stage_error(row, error)
                    self.outbox.advance_blocks(
                        article_key,
                        cursor + len(batch),
                        self.clock(),
                        document_index + len(batch),
                    )
                    continue
                try:
                    self.feishu.share_document(row["document_id"])
                except DeliveryError as error:
                    return self._handle_feishu_stage_error(row, error)
                self.outbox.mark_shared(article_key, self.clock())
                return "shared"

            return status

    def _process_image(
        self,
        row: dict,
        cursor: int,
        document_index: int,
        item: dict,
        *,
        skip_count: int = 1,
    ) -> str:
        """Create, upload and bind one Image Block with durable sub-stage state."""
        article_key = row["article_key"]
        try:
            states = json.loads(row.get("image_states_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            states = {}
        state = dict(states.get(str(cursor)) or {}) if isinstance(states, dict) else {}
        image = None

        if not state:
            # Validate and download before creating an empty block. A broken or
            # unsafe optional image is skipped without delaying the full text.
            try:
                image = self.image_downloader.download(item["source_url"])
            except DeliveryError as error:
                if error.retryable:
                    self._retry(row, error)
                    return row["status"]
                self.outbox.mark_image_skipped(
                    article_key,
                    cursor,
                    item["source_url"],
                    error.code,
                    self.clock(),
                    cursor_advance=skip_count,
                )
                return "continue"
            self.outbox.prepare_image(
                article_key,
                cursor,
                source_url=image.source_url,
                final_url=image.final_url,
                mime_type=image.mime_type,
                extension=image.extension,
                sha256=image.sha256,
                data=image.data,
                width=image.width,
                height=image.height,
                now=self.clock(),
            )
            state = {
                "state": "prepared",
                "source_url": image.source_url,
                "sha256": image.sha256,
            }

        if state.get("state") == "prepared":
            if image is None:
                image = self._prepared_image(article_key, cursor)
            if image is None:
                self.outbox.mark_terminal(
                    article_key,
                    "manual",
                    "IMAGE_SPOOL_MISSING",
                    "已验证图片的持久化内容缺失",
                    self.clock(),
                )
                return "manual"
            try:
                created = self.feishu.append_blocks(
                    row["document_id"],
                    [{"block_type": 27, "image": {}}],
                    document_index,
                    block_client_token(article_key, cursor),
                )
            except UncertainRemoteResult as error:
                self.outbox.mark_terminal(
                    article_key, "unknown", error.code, error.message, self.clock()
                )
                return "unknown"
            except DeliveryError as error:
                return self._handle_feishu_stage_error(row, error)
            block_ids = [
                str(value.get("block_id") or "")
                for value in created
                if isinstance(value, dict) and value.get("block_id")
            ]
            if len(block_ids) != 1:
                self.outbox.mark_terminal(
                    article_key,
                    "unknown",
                    "FEISHU_IMAGE_BLOCK_UNKNOWN",
                    "图片块创建响应缺少唯一 block_id，需人工核对",
                    self.clock(),
                )
                return "unknown"
            self.outbox.mark_image_block_created(
                article_key,
                cursor,
                item["source_url"],
                block_ids[0],
                image.sha256,
                image.mime_type,
                len(image.data),
                image.width,
                image.height,
                self.clock(),
            )
            state = {
                "state": "block_created",
                "block_id": block_ids[0],
                "sha256": image.sha256,
                "upload_started_at": None,
                "file_token": "",
            }

        if state.get("state") == "block_created":
            if state.get("upload_started_at") is not None:
                self.outbox.mark_terminal(
                    article_key,
                    "unknown",
                    "FEISHU_IMAGE_UPLOAD_INTERRUPTED",
                    "图片素材上传已开始但没有持久化远程结果，需人工核对",
                    self.clock(),
                )
                return "unknown"
            if image is None:
                image = self._prepared_image(article_key, cursor)
                if image is None:
                    self.outbox.mark_terminal(
                        article_key,
                        "manual",
                        "IMAGE_SPOOL_MISSING",
                        "图片块对应的持久化内容缺失",
                        self.clock(),
                    )
                    return "manual"
            if image.sha256 != str(state.get("sha256") or ""):
                self.outbox.mark_terminal(
                    article_key,
                    "manual",
                    "IMAGE_CONTENT_CHANGED",
                    "图片块创建后源图片内容发生变化，已停止自动绑定",
                    self.clock(),
                )
                return "manual"
            self.outbox.mark_image_upload_started(article_key, cursor, self.clock())
            try:
                file_token = self.feishu.upload_image(
                    row["document_id"], str(state["block_id"]), image
                )
            except UncertainRemoteResult as error:
                self.outbox.mark_terminal(
                    article_key, "unknown", error.code, error.message, self.clock()
                )
                return "unknown"
            except DeliveryError as error:
                # A structured response proves no material token was issued;
                # clear the intent so a known-safe retry can occur.
                self.outbox.clear_image_upload_started(article_key, cursor, self.clock())
                return self._handle_feishu_stage_error(row, error)
            self.outbox.mark_image_uploaded(article_key, cursor, file_token, self.clock())
            state["state"] = "uploaded"
            state["file_token"] = file_token

        if state.get("state") == "uploaded":
            width = image.width if image is not None else int(state.get("width") or 0)
            height = image.height if image is not None else int(state.get("height") or 0)
            try:
                self.feishu.replace_image(
                    row["document_id"],
                    str(state["block_id"]),
                    str(state["file_token"]),
                    image_patch_client_token(article_key, cursor),
                    width,
                    height,
                )
            except UncertainRemoteResult as error:
                # replace_image uses a deterministic client_token, but retain
                # the same conservative rule if a future client marks it unknown.
                self.outbox.mark_terminal(
                    article_key, "unknown", error.code, error.message, self.clock()
                )
                return "unknown"
            except DeliveryError as error:
                return self._handle_feishu_stage_error(row, error)
            self.outbox.mark_image_bound(article_key, cursor, self.clock())
            return "continue"

        self.outbox.mark_terminal(
            article_key,
            "manual",
            "IMAGE_STATE_INVALID",
            "图片 outbox 状态无效",
            self.clock(),
        )
        return "manual"

    def _prepared_image(self, article_key: str, cursor: int) -> Optional[DownloadedImage]:
        value = self.outbox.get_prepared_image(article_key, cursor)
        if not value:
            return None
        data = bytes(value.get("data") or b"")
        if not data:
            return None
        width = int(value.get("width") or 0)
        height = int(value.get("height") or 0)
        if width <= 0 or height <= 0:
            _mime, detected_width, detected_height = _detect_image_info(data)
            if detected_width > 0 and detected_height > 0:
                width, height = detected_width, detected_height
                self.outbox.backfill_prepared_image_dimensions(
                    article_key,
                    cursor,
                    width,
                    height,
                    self.clock(),
                )
        return DownloadedImage(
            source_url=str(value.get("source_url") or ""),
            final_url=str(value.get("final_url") or ""),
            data=data,
            mime_type=str(value.get("mime_type") or ""),
            extension=str(value.get("extension") or ""),
            sha256=str(value.get("sha256") or ""),
            width=width,
            height=height,
        )

    def _handle_feishu_stage_error(self, row: dict, error: DeliveryError) -> str:
        if error.systemic:
            # Scope/auth/openchat configuration may be repaired by an operator.
            # Keep the durable stage and back it off instead of marking every
            # article manual, while halting the rest of this invocation.
            self._halt_feishu = True
            self._retry(row, error)
            return row["status"]
        if isinstance(error, UncertainRemoteResult):
            status = "unknown"
        elif error.retryable:
            self._retry(row, error)
            return row["status"]
        else:
            status = "manual"
        self.outbox.mark_terminal(
            row["article_key"], status, error.code, error.message, self.clock()
        )
        return status

    def _retry(self, row: dict, error: DeliveryError) -> None:
        delay = self.outbox.retry_delay(
            row["article_key"],
            self.config.retry_base_seconds,
            self.config.retry_max_seconds,
        )
        self.outbox.schedule_retry(
            row["article_key"], error.code, error.message, self.clock(), delay
        )

    def _handle_systemic_fetch_failure(self, row: dict, error: DeliveryError) -> None:
        delay = max(
            self.config.circuit_min_seconds,
            self.outbox.retry_delay(
                row["article_key"],
                self.config.retry_base_seconds,
                self.config.retry_max_seconds,
            ),
        )
        now = self.clock()
        self.outbox.schedule_retry(
            row["article_key"], error.code, error.message, now, delay
        )
        self.outbox.activate_circuit(error.code, now, delay)
        if self.outbox.activate_alert(
            error.code, now, self.config.alert_cooldown_seconds
        ):
            alert_uuid = deterministic_uuid(
                f"trendradar-{self.config.publisher}-alert:{error.code}:"
                f"{int(now // self.config.alert_cooldown_seconds)}"
            )
            # Persist the cooldown before the remote write.  A lost response
            # must not cause the same alert to be resent outside Feishu's
            # one-hour UUID deduplication window.
            self.outbox.mark_alert_sent(error.code, now)
            try:
                self.feishu.send_alert(
                    f"{self.config.display_name} 全文抓取已暂停",
                    f"检测到系统性抓取故障：`{error.code}`。已启动退避与熔断；不会创建低质量云文档。",
                    alert_uuid,
                )
            except DeliveryError:
                return

    def _handle_fetch_recovery(self) -> None:
        now = self.clock()
        self.outbox.clear_circuit()
        for alert in self.outbox.active_alerts():
            kind = str(alert["kind"])
            started = int(float(alert["started_at"]))
            recovery_uuid = deterministic_uuid(
                f"trendradar-{self.config.publisher}-recovery:{kind}:{started}"
            )
            # Recovery notifications use the same write-ahead rule: mark the
            # incident recovered before sending so a crash cannot duplicate
            # the card after Feishu's UUID window expires.
            self.outbox.mark_alert_recovered(kind, now)
            try:
                self.feishu.send_alert(
                    f"{self.config.display_name} 全文抓取已恢复",
                    f"系统性抓取故障 `{kind}` 已恢复，文章处理已继续。",
                    recovery_uuid,
                )
            except DeliveryError:
                continue

    def _notify_pending(self) -> int:
        self.outbox.reconcile_partial_message_groups(self.clock())
        rows = self.outbox.pending_shared(self.clock())
        if not rows:
            return 0

        interrupted = {
            str(row.get("message_uuid") or "")
            for row in rows
            if row.get("notification_started_at") is not None
        }
        interrupted.discard("")
        ready_rows = [
            row for row in rows if str(row.get("message_uuid") or "") not in interrupted
        ]
        for value in interrupted:
            keys = [
                row["article_key"]
                for row in rows
                if str(row.get("message_uuid") or "") == value
            ]
            self.outbox.mark_message_terminal(
                keys,
                value,
                "unknown",
                "FEISHU_MESSAGE_INTERRUPTED",
                "消息发送已开始但没有持久化远程结果，需人工核对",
                self.clock(),
            )
        rows = ready_rows
        if not rows:
            return 0

        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        unassigned = [row for row in rows if not row.get("message_uuid")]
        for row in rows:
            value = str(row.get("message_uuid") or "")
            if value:
                groups.setdefault(value, []).append(row)

        for part in partition_summary_cards(
            unassigned,
            self.config.card_max_bytes,
            self.config.display_name,
        ):
            value = message_uuid(part, self.config.publisher)
            keys = [row["article_key"] for row in part]
            self.outbox.assign_message_uuid(keys, value, self.clock())
            for row in part:
                row["message_uuid"] = value
            groups.setdefault(value, []).extend(part)

        notified = 0
        for value, group in groups.items():
            card = build_summary_card(group, self.config.display_name)
            keys = [row["article_key"] for row in group]
            self.outbox.mark_notification_started(keys, value, self.clock())
            try:
                self.feishu.send_card(card, value)
            except UncertainRemoteResult as error:
                # Feishu message UUID deduplication lasts only one hour. A lost
                # response is therefore terminal-unknown rather than a blind
                # cross-window retry that could send a duplicate card.
                self.outbox.mark_message_terminal(
                    keys,
                    value,
                    "unknown",
                    error.code,
                    error.message,
                    self.clock(),
                )
                continue
            except DeliveryError as error:
                # Retry or terminalize the complete UUID group in one SQLite
                # transaction. A crash can never leave an eligible UUID subset.
                if error.retryable or error.systemic:
                    self.outbox.schedule_message_retry(
                        keys,
                        value,
                        error.code,
                        error.message,
                        self.clock(),
                        self.config.retry_base_seconds,
                        self.config.retry_max_seconds,
                    )
                else:
                    self.outbox.mark_message_terminal(
                        keys,
                        value,
                        "manual",
                        error.code,
                        error.message,
                        self.clock(),
                    )
                if error.systemic:
                    self._halt_feishu = True
                continue
            self.outbox.mark_notified(keys, value, self.clock())
            notified += len(keys)
        return notified

    def _enforce_document_retention(self) -> int:
        """Delete only the oldest notified Docx rows above the configured cap."""
        if self._halt_feishu:
            return 0
        deleted = 0
        candidates = self.outbox.retention_candidates(
            self.config.max_cloud_documents
        )
        for initial in candidates:
            row = self.outbox.get(initial["article_key"]) or initial
            if row.get("status") != "notified":
                break
            if row.get("document_retention_state") == "delete_pending":
                if float(row.get("document_delete_next_attempt_at") or 0) > self.clock():
                    break
            elif row.get("document_retention_state") == "active":
                if not self.outbox.begin_document_delete(
                    row["article_key"], self.clock()
                ):
                    break
                row = self.outbox.get(row["article_key"]) or row
            else:
                break

            document_id = str(row.get("document_id") or "")
            document_url = str(row.get("document_url") or "")
            if (
                not _DOCX_TOKEN_RE.fullmatch(document_id)
                or not _is_managed_feishu_doc_url(document_url, document_id)
            ):
                self.outbox.block_document_delete(
                    row["article_key"],
                    "RETENTION_DOCUMENT_ID_INVALID",
                    "云文档标识或地址未通过本地一致性校验，已停止自动删除",
                    self.clock(),
                )
                break
            try:
                self.feishu.delete_document(document_id)
            except DeliveryError as error:
                if error.retryable or error.systemic:
                    delay = self.outbox.document_delete_retry_delay(
                        row["article_key"],
                        self.config.retry_base_seconds,
                        self.config.retry_max_seconds,
                    )
                    self.outbox.schedule_document_delete_retry(
                        row["article_key"],
                        error.code,
                        error.message,
                        self.clock(),
                        delay,
                    )
                else:
                    self.outbox.block_document_delete(
                        row["article_key"],
                        error.code,
                        error.message,
                        self.clock(),
                    )
                if error.systemic:
                    self._halt_feishu = True
                # Never skip a failed oldest deletion and delete a newer row;
                # that could later over-delete when this intent recovers.
                break
            self.outbox.mark_document_deleted(row["article_key"], self.clock())
            deleted += 1
        return deleted


def _is_managed_feishu_doc_url(value: str, document_id: str) -> bool:
    """Reject corrupted/non-Feishu URLs before using their paired token."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and (host == "feishu.cn" or host.endswith(".feishu.cn"))
        and parsed.path == f"/docx/{document_id}"
        and not parsed.query
        and not parsed.fragment
    )


def run_cli(
    *,
    backfill_current: bool,
    drain: bool,
    publisher: str = "wsj",
    initialize_current_only: bool = False,
) -> int:
    """Build production dependencies from the environment and execute once."""
    outbox: Optional[Outbox] = None
    try:
        config = DeliveryConfig.from_env(publisher=publisher)
        with ProcessLock(config.db_path):
            outbox = Outbox(config.db_path, publisher=config.publisher)
            feed = FeedClient(
                config.feed_url,
                config.feed_timeout,
                publisher=config.publisher,
                display_name=config.display_name,
            )
            bpc = BPCClient(
                config.bpc_base_url,
                config.bpc_api_token,
                config.http_timeout,
                publisher=config.publisher,
                endpoint=config.bpc_endpoint,
            )
            feishu = FeishuClient(
                config.feishu_app_id,
                config.feishu_app_secret,
                config.feishu_receive_id,
                config.feishu_receive_id_type,
                timeout=min(config.http_timeout, 30),
                block_interval=config.block_interval_seconds,
            )
            summary = DeliveryRunner(
                config, outbox, feed, bpc, feishu
            ).run(
                backfill_current=backfill_current,
                drain=drain,
                initialize_current_only=initialize_current_only,
            )
            print(
                f"[{config.display_name} delivery] "
                + json.dumps(summary.__dict__, ensure_ascii=False, sort_keys=True)
            )
            # Make a stuck cap visible to systemd/monitoring while preserving
            # all durable retry state for the next hourly run.
            return 1 if summary.retention_excess else 0
    except (ConfigurationError, InitializationRequired) as exc:
        label = "WSJ" if publisher == "wsj" else "Economist"
        print(f"[{label} delivery] 配置/初始化错误: {exc}", file=sys.stderr)
        return 2
    except AlreadyRunning as exc:
        label = "WSJ" if publisher == "wsj" else "Economist"
        print(f"[{label} delivery] {exc}", file=sys.stderr)
        return 75
    except DeliveryError as exc:
        label = "WSJ" if publisher == "wsj" else "Economist"
        print(f"[{label} delivery] 远程服务错误: {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # keep service logs useful without printing secrets
        label = "WSJ" if publisher == "wsj" else "Economist"
        print(
            f"[{label} delivery] 运行失败: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if outbox is not None:
            outbox.close()
