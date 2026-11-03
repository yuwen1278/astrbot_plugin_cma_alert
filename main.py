"""中央气象台预警与天气播报插件。

把中央气象台（nmc.cn）发布的全国气象灾害预警信号，以及指定城市的天气预报，
主动推送到 AstrBot 的群聊或私聊会话。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession

from . import regions as region_data
from . import service
from .cma_client import (
    CATEGORY_LABELS,
    CHINA_TIMEZONE,
    LEVEL_LABELS,
    AlertEvent,
    CMAClient,
    CMAClientError,
    alert_fingerprint,
)

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_cma_alert"
SETTINGS_KEY = "settings_v1"
SEEN_EVENTS_KEY = "seen_events_v1"
DELIVERY_STATUS_KEY = "delivery_status_v1"
FORECAST_SLOTS_KEY = "forecast_slots_v1"

LOOP_TICK_SECONDS = 15
MAX_DELIVERY_RECORDS = 200
MAX_SEND_CHUNK_DELAY = 0.2


class CMAAlertPlugin(star.Star):
    """把中央气象台的预警与天气预报推送到 AstrBot 会话。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self._settings: dict[str, Any] = service.default_settings()
        self._seen_events: dict[str, Any] = {}
        self._delivery_status: dict[str, Any] = {}
        self._forecast_slots: dict[str, Any] = {}
        self._client: CMAClient | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._next_alert_poll_at = 0.0
        self._started_at = datetime.now(CHINA_TIMEZONE)
        self._last_loop_error: str | None = None
        self._last_alert_poll_at: str | None = None
        self._last_alert_count = 0
        self._register_web_apis()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _register_web_apis(self) -> None:
        """注册管理页面使用的 Web API。"""
        routes = (
            ("state", self.page_state, ["GET"], "读取气象播报状态"),
            ("settings", self.page_save_settings, ["POST", "PUT"], "保存气象播报设置"),
            ("regions", self.page_regions, ["GET"], "读取省市对照表"),
            ("source/test", self.page_test_source, ["POST"], "测试中央气象台数据源"),
            ("preview", self.page_preview, ["POST"], "预览播报内容"),
            ("test-send", self.page_test_send, ["POST"], "发送测试消息"),
            (
                "subscriptions/delete",
                self.page_delete_subscription,
                ["POST"],
                "删除订阅",
            ),
        )
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{suffix}", handler, methods, description
            )

    async def initialize(self) -> None:
        """载入持久化状态并启动唯一的调度协程。"""
        if self._poll_task and not self._poll_task.done():
            logger.warning("气象播报排程已在执行，跳过重复启动。")
            return

        self._settings = self._load_settings(await self.get_kv_data(SETTINGS_KEY, {}))
        seen_events = await self.get_kv_data(SEEN_EVENTS_KEY, {})
        delivery_status = await self.get_kv_data(DELIVERY_STATUS_KEY, {})
        forecast_slots = await self.get_kv_data(FORECAST_SLOTS_KEY, {})
        self._seen_events = service.prune_state(
            seen_events if isinstance(seen_events, Mapping) else {}
        )
        self._delivery_status = _prune_delivery_status(
            delivery_status if isinstance(delivery_status, Mapping) else {}
        )
        self._forecast_slots = service.prune_state(
            forecast_slots if isinstance(forecast_slots, Mapping) else {}
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"{PLUGIN_NAME}:poller"
        )
        logger.info(
            "[气象播报] 服务已启动，订阅会话 %d 个，轮询间隔 %d 秒",
            len(self._settings.get("subscriptions", [])),
            int(self._settings.get("poll_interval_seconds", 180)),
        )

    async def terminate(self) -> None:
        """停止后台任务并持久化状态。"""
        task = self._poll_task
        self._poll_task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            await self._persist_runtime_state()
        except Exception as exc:  # noqa: BLE001 - 持久化失败不应阻断卸载
            logger.error("保存气象播报运行状态失败：%s", self._safe_error(exc))
        client = self._client
        self._client = None
        if client is not None:
            with suppress(Exception):
                await client.close()

    # ------------------------------------------------------------------
    # 状态存取
    # ------------------------------------------------------------------
    def _load_settings(self, value: Any) -> dict[str, Any]:
        try:
            return service.normalize_settings(
                value if isinstance(value, Mapping) else {}
            )
        except service.ValidationError as exc:
            logger.error("气象播报配置无效，已回退到默认值：%s", exc)
            return service.default_settings()

    async def _persist_runtime_state(self) -> None:
        await self.put_kv_data(SEEN_EVENTS_KEY, self._seen_events)
        await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)
        await self.put_kv_data(FORECAST_SLOTS_KEY, self._forecast_slots)

    def _config_value(self, key: str, default: Any) -> Any:
        """从插件配置（_conf_schema.json）中读取一项，缺失时回退默认值。"""
        config = getattr(self, "config", None)
        if isinstance(config, Mapping):
            value = config.get(key)
            if value is not None:
                return value
        return default

    def _client_instance(self) -> CMAClient:
        if self._client is None:
            self._client = CMAClient(
                timeout=self._config_value("timeout", 20),
                retries=self._config_value("retries", 2),
                proxy=str(self._config_value("proxy", "") or ""),
                user_agent=str(self._config_value("user_agent", "") or ""),
            )
        return self._client

    def _subscription_for(self, umo: str) -> dict[str, Any] | None:
        for subscription in self._settings.get("subscriptions", []):
            if subscription.get("target_umo") == umo:
                return subscription
        return None

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            try:
                if self._settings.get("enabled"):
                    monotonic_now = time.monotonic()
                    if monotonic_now >= self._next_alert_poll_at:
                        interval = int(
                            self._settings.get(
                                "poll_interval_seconds",
                                service.DEFAULT_POLL_INTERVAL_SECONDS,
                            )
                        )
                        self._next_alert_poll_at = monotonic_now + interval
                        await self._poll_alerts()
                    await self._send_due_forecasts()
                self._last_loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 轮询协程必须存活
                self._last_loop_error = self._safe_error(exc)
                logger.error("气象播报排程失败：%s", self._last_loop_error)
            await asyncio.sleep(LOOP_TICK_SECONDS)

    async def _poll_alerts(self) -> None:
        subscriptions = [
            item
            for item in self._settings.get("subscriptions", [])
            if item.get("enabled")
        ]
        self._last_alert_poll_at = datetime.now(CHINA_TIMEZONE).isoformat(
            timespec="seconds"
        )
        if not subscriptions:
            self._last_alert_count = 0
            return

        try:
            events = await self._client_instance().fetch_alarms()
        except CMAClientError as exc:
            logger.warning("拉取中央气象台预警失败：%s", self._safe_error(exc))
            return

        self._last_alert_count = len(events)
        fresh = [event for event in events if not service.event_is_expired(event)]
        detail_budget = service.MAX_DETAIL_FETCH_PER_RUN
        pushed = 0

        for event in fresh:
            fingerprint = alert_fingerprint(event)
            if fingerprint in self._seen_events:
                continue
            targets = service.filter_subscriptions(subscriptions, event)
            if not targets:
                self._mark_seen(fingerprint)
                continue
            if not service.event_is_recent(event, window_minutes=180):
                self._mark_seen(fingerprint)
                continue

            detailed = event
            if detail_budget > 0 and any(
                target.get("include_detail", True) for target in targets
            ):
                detail_budget -= 1
                detailed = await self._enrich_event(event)

            for subscription in targets:
                limit = int(self._settings.get("content_limit", 600))
                text = service.format_alert_event(detailed, content_limit=limit)
                succeeded, error = await self._send_text(
                    subscription["target_umo"], text
                )
                self._record_delivery(
                    subscription["target_umo"], "alert", succeeded, error
                )
                if succeeded:
                    pushed += 1
                else:
                    logger.error(
                        "[气象播报] 预警推送到 %s 失败：%s",
                        subscription["target_umo"],
                        error,
                    )
            self._mark_seen(fingerprint)

        self._seen_events = service.prune_state(self._seen_events)
        if pushed:
            logger.info("[气象播报] 本轮推送预警 %d 条", pushed)
        await self._persist_runtime_state()

    async def _enrich_event(self, event: AlertEvent) -> AlertEvent:
        """补全一条预警的正文与防御指南，失败时原样返回。"""
        try:
            description, guidance = await self._client_instance().fetch_alarm_detail(
                event.official_id
            )
        except CMAClientError as exc:
            logger.debug("拉取预警详情失败（%s）：%s", event.official_id, exc)
            return event
        if not description and not guidance:
            return event
        return AlertEvent(
            source=event.source,
            official_id=event.official_id,
            title=event.title,
            signal=event.signal,
            level=event.level,
            category=event.category,
            action=event.action,
            admin_code=event.admin_code,
            issued_at=event.issued_at,
            link=event.link,
            description=description or event.description,
            guidance=guidance or event.guidance,
        )

    def _mark_seen(self, fingerprint: str) -> None:
        self._seen_events[fingerprint] = datetime.now(CHINA_TIMEZONE).isoformat(
            timespec="seconds"
        )

    def _record_delivery(
        self, target_umo: str, kind: str, succeeded: bool, error: str | None
    ) -> None:
        now = datetime.now(CHINA_TIMEZONE).isoformat(timespec="seconds")
        record = dict(self._delivery_status.get(target_umo) or {})
        record.update(
            {
                "target_umo": target_umo,
                "kind": kind,
                "last_attempt_at": now,
                "last_status": "ok" if succeeded else "failed",
                "last_error": error or "",
            }
        )
        if succeeded:
            record["delivered_at"] = now
        self._delivery_status[target_umo] = record
        self._delivery_status = _prune_delivery_status(self._delivery_status)

    async def _send_due_forecasts(self) -> None:
        subscriptions = [
            item
            for item in self._settings.get("subscriptions", [])
            if item.get("enabled") and item.get("forecast_enabled")
        ]
        if not subscriptions:
            return

        for subscription in subscriptions:
            slots = service.due_forecast_slots(subscription)
            if not slots:
                continue
            for date_text, send_time in slots:
                slot_key = service.forecast_slot_key(
                    subscription["target_umo"], date_text, send_time
                )
                if slot_key in self._forecast_slots:
                    continue
                succeeded, error = await self._send_forecast(
                    subscription, date_text, send_time
                )
                self._forecast_slots[slot_key] = datetime.now(CHINA_TIMEZONE).isoformat(
                    timespec="seconds"
                )
                self._record_delivery(
                    subscription["target_umo"], "forecast", succeeded, error
                )
                if not succeeded:
                    logger.error(
                        "[气象播报] 天气推送到 %s 失败：%s",
                        subscription["target_umo"],
                        error,
                    )
        self._forecast_slots = service.prune_state(self._forecast_slots)
        await self._persist_runtime_state()

    async def _send_forecast(
        self, subscription: Mapping[str, Any], date_text: str, send_time: str
    ) -> tuple[bool, str | None]:
        cities = list(subscription.get("cities", []))
        if not cities:
            return False, "该订阅没有配置城市。"
        stations: list[str] = []
        for city in cities:
            try:
                stations.append(service.resolve_region(city).station)
            except service.ValidationError:
                continue
        if not stations:
            return False, "该订阅的城市没有对应的观测站。"

        try:
            forecasts, errors = await self._client_instance().fetch_weather_many(
                stations
            )
        except CMAClientError as exc:
            return False, self._safe_error(exc)

        blocks: list[str] = []
        for city in cities:
            try:
                region = service.resolve_region(city)
            except service.ValidationError:
                continue
            forecast = forecasts.get(region.station)
            if forecast is None:
                reason = errors.get(region.station, "没有返回数据")
                blocks.append(f"【{city}天气预报】\n暂时无法获取：{reason}")
                continue
            blocks.append(service.format_weather_forecast(forecast, city=city))

        if not blocks:
            return False, "没有取到任何城市的天气数据。"
        text = "\n\n".join(blocks)
        return await self._send_text(subscription["target_umo"], text)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def _send_text(self, target_umo: str, text: str) -> tuple[bool, str | None]:
        validation_error = self._validate_target_umo(target_umo)
        if validation_error:
            return False, validation_error
        self._restore_qq_scenes([target_umo])
        try:
            chunks = service.split_message(text)
            for index, chunk in enumerate(chunks):
                sent = await self.context.send_message(
                    target_umo, MessageChain().message(chunk).use_t2i(False)
                )
                if not sent:
                    return False, "找不到目标会话所属的平台实例。"
                if index < len(chunks) - 1:
                    await asyncio.sleep(MAX_SEND_CHUNK_DELAY)
            return True, None
        except Exception as exc:  # noqa: BLE001 - 平台适配器可能抛出任意异常
            return False, self._safe_error(exc)

    def _validate_target_umo(
        self, target_umo: str, *, require_available: bool = True
    ) -> str | None:
        try:
            session = MessageSession.from_str(target_umo)
        except Exception:  # noqa: BLE001 - from_str 可能抛出任意解析异常
            return "会话目标格式无效。"
        if not session.platform_id.strip() or not session.session_id.strip():
            return "会话目标格式无效。"
        if session.message_type not in (
            MessageType.GROUP_MESSAGE,
            MessageType.FRIEND_MESSAGE,
        ):
            return "只支持推送到群聊或私聊。"
        if not require_available:
            return None
        if self.context.get_platform_inst(session.platform_id) is None:
            return "找不到目标会话所属的平台实例。"
        return None

    def _restore_qq_scenes(self, umos: list[str] | None = None) -> int:
        """为 QQ 官方平台补登记会话场景，使主动推送在重启后依然可用。"""
        platform_manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_manager, "platform_insts", None) or []
        if not platforms:
            return 0
        targets = umos
        if targets is None:
            targets = [
                str(item.get("target_umo"))
                for item in self._settings.get("subscriptions", [])
            ]
        restored = 0
        for umo in targets:
            if not umo:
                continue
            try:
                session = MessageSession.from_str(umo)
            except Exception:  # noqa: BLE001 - 跳过无法解析的历史目标
                logger.debug("跳过无法解析的会话目标：%s", umo)
                continue
            scene = {
                MessageType.GROUP_MESSAGE: "group",
                MessageType.FRIEND_MESSAGE: "friend",
            }.get(session.message_type)
            if scene is None:
                continue
            for platform in platforms:
                try:
                    meta = platform.meta()
                except Exception:  # noqa: BLE001 - 第三方平台实现可能抛异常
                    logger.debug("读取平台元数据失败：%s", type(platform).__name__)
                    continue
                if getattr(meta, "id", None) != session.platform_id:
                    continue
                remember = getattr(platform, "remember_session_scene", None)
                if not callable(remember):
                    continue
                session_ids = {session.session_id}
                tail = session.session_id.rsplit("_", 1)[-1]
                if tail and tail != session.session_id:
                    session_ids.add(tail)
                for session_id in session_ids:
                    try:
                        remember(session_id, scene)
                    except Exception:  # noqa: BLE001 - 场景登记失败不影响发送
                        logger.debug("补登记会话场景失败：%s", session_id)
                        continue
                restored += 1
                break
        return restored

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cma_register")
    async def cma_register(self, event: AstrMessageEvent, cities: str = "") -> None:
        """把当前会话登记为气象播报目标。"""
        target_umo = event.unified_msg_origin
        validation_error = self._validate_target_umo(target_umo)
        if validation_error:
            await event.send(MessageChain().message(validation_error))
            return

        raw_cities = [
            item
            for item in str(cities or "").replace("，", ",").split(",")
            if item.strip()
        ]
        normalized: list[str] = []
        for value in raw_cities:
            try:
                city = service.normalize_city(value)
            except service.ValidationError as exc:
                await event.send(MessageChain().message(str(exc)))
                return
            if city not in normalized:
                normalized.append(city)
        if not normalized:
            await event.send(
                MessageChain().message("请指定城市，例如：/cma_register 广州,深圳")
            )
            return

        async with self._state_lock:
            existing = self._subscription_for(target_umo)
            raw_subscription = dict(existing or {})
            raw_subscription.update(
                {
                    "target_umo": target_umo,
                    "cities": normalized,
                    "enabled": True,
                }
            )
            raw_subscription.setdefault("forecast_enabled", True)
            try:
                subscription = service.normalize_subscription(raw_subscription)
            except service.ValidationError as exc:
                await event.send(MessageChain().message(str(exc)))
                return
            subscriptions = [
                item
                for item in self._settings.get("subscriptions", [])
                if item.get("target_umo") != target_umo
            ]
            subscriptions.append(subscription)
            try:
                self._settings = service.normalize_settings(
                    {**self._settings, "subscriptions": subscriptions},
                    previous=self._settings,
                )
            except service.ValidationError as exc:
                await event.send(MessageChain().message(str(exc)))
                return
            self._settings["revision"] = int(self._settings.get("revision", 0)) + 1
            await self.put_kv_data(SETTINGS_KEY, self._settings)

        self._restore_qq_scenes([target_umo])
        scope = (
            "群聊" if event.get_message_type() == MessageType.GROUP_MESSAGE else "私聊"
        )
        await event.send(
            MessageChain().message(
                f"已登记本{scope}的气象播报，城市：{'、'.join(normalized)}。\n"
                f"预警每 {int(self._settings.get('poll_interval_seconds', 180))} 秒检查一次，"
                f"天气播报时间：{'、'.join(subscription.get('forecast_times', []))}。\n"
                "可用 /cma_status 查看，/cma_test 立即测试。"
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cma_unregister")
    async def cma_unregister(self, event: AstrMessageEvent) -> None:
        """取消当前会话的气象播报登记。"""
        target_umo = event.unified_msg_origin
        async with self._state_lock:
            original = self._settings.get("subscriptions", [])
            remaining = [
                item for item in original if item.get("target_umo") != target_umo
            ]
            if len(remaining) == len(original):
                await event.send(MessageChain().message("当前会话尚未登记气象播报。"))
                return
            self._settings["subscriptions"] = remaining
            self._settings["revision"] = int(self._settings.get("revision", 0)) + 1
            await self.put_kv_data(SETTINGS_KEY, self._settings)
        await event.send(MessageChain().message("已取消当前会话的气象播报登记。"))

    @filter.command("cma_weather")
    async def cma_weather(self, event: AstrMessageEvent, city: str = "") -> None:
        """查询城市天气，缺省使用当前会话订阅的城市。"""
        cities: list[str] = []
        if str(city or "").strip():
            try:
                cities = [service.normalize_city(city)]
            except service.ValidationError as exc:
                await event.send(MessageChain().message(str(exc)))
                return
        else:
            subscription = self._subscription_for(event.unified_msg_origin)
            if subscription:
                cities = list(subscription.get("cities", []))
        if not cities:
            await event.send(
                MessageChain().message(
                    "请指定城市，例如：/cma_weather 广州；或先用 /cma_register 广州 登记。"
                )
            )
            return

        stations = [service.resolve_region(item).station for item in cities]
        try:
            forecasts, errors = await self._client_instance().fetch_weather_many(
                stations
            )
        except CMAClientError as exc:
            await event.send(MessageChain().message(f"获取天气失败：{exc}"))
            return

        blocks: list[str] = []
        for item in cities:
            region = service.resolve_region(item)
            forecast = forecasts.get(region.station)
            if forecast is None:
                blocks.append(
                    f"【{item}天气预报】\n暂时无法获取："
                    f"{errors.get(region.station, '没有返回数据')}"
                )
            else:
                blocks.append(service.format_weather_forecast(forecast, city=item))
        for chunk in service.split_message("\n\n".join(blocks)):
            await event.send(MessageChain().message(chunk).use_t2i(False))

    @filter.command("cma_status")
    async def cma_status(self, event: AstrMessageEvent) -> None:
        """查看当前会话的订阅状态。"""
        subscription = self._subscription_for(event.unified_msg_origin)
        lines = ["【气象播报状态】"]
        lines.append(f"总开关：{'开启' if self._settings.get('enabled') else '关闭'}")
        lines.append(
            f"轮询间隔：{int(self._settings.get('poll_interval_seconds', 180))} 秒"
        )
        if not subscription:
            lines.append("当前会话：尚未登记（使用 /cma_register 城市 登记）")
        else:
            lines.append(
                f"城市：{'、'.join(subscription.get('cities', [])) or '未设置'}"
            )
            types = [
                CATEGORY_LABELS.get(item, item)
                for item in subscription.get("alert_types", [])
            ]
            lines.append(f"预警类型：{'、'.join(types) or '全部'}")
            levels = [
                LEVEL_LABELS.get(item, item) for item in subscription.get("levels", [])
            ]
            lines.append(f"预警等级：{'、'.join(levels) or '全部'}")
            if subscription.get("forecast_enabled"):
                times = "、".join(subscription.get("forecast_times", [])) or "未设置"
                lines.append(f"天气播报：{times}")
            else:
                lines.append("天气播报：关闭")
            lines.append(
                f"订阅开关：{'开启' if subscription.get('enabled') else '关闭'}"
            )
        lines.append(f"全国当前预警：{self._last_alert_count} 条")
        if self._last_alert_poll_at:
            lines.append(f"最近检查：{self._last_alert_poll_at}")
        if self._last_loop_error:
            lines.append(f"最近错误：{self._last_loop_error}")
        await event.send(MessageChain().message("\n".join(lines)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cma_test")
    async def cma_test(self, event: AstrMessageEvent) -> None:
        """立即向当前会话推送一条测试消息。"""
        target_umo = event.unified_msg_origin
        succeeded, error = await self._send_text(
            target_umo,
            "【中央气象台播报测试】\n主动消息发送已交由 AstrBot 平台接口处理。",
        )
        if not succeeded:
            await event.send(MessageChain().message(f"测试失败：{error}"))

    @filter.command("cma_help")
    async def cma_help(self, event: AstrMessageEvent) -> None:
        """显示指令帮助。"""
        await event.send(
            MessageChain().message(
                "【中央气象台播报插件】\n"
                "/cma_register 城市1,城市2 — 把当前会话登记为播报目标\n"
                "/cma_unregister — 取消当前会话的登记\n"
                "/cma_weather [城市] — 查询实时天气\n"
                "/cma_status — 查看当前会话的订阅状态\n"
                "/cma_test — 立即发送一条测试消息\n"
                "更多设置（预警类型、等级、播报时间）请到插件管理页调整。"
            )
        )

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------
    async def page_state(self):
        deliveries = sorted(
            (
                dict(record)
                for record in self._delivery_status.values()
                if isinstance(record, Mapping)
            ),
            key=lambda item: str(item.get("last_attempt_at") or ""),
            reverse=True,
        )[:MAX_DELIVERY_RECORDS]
        return json_response(
            {
                "revision": int(self._settings.get("revision", 0)),
                "settings": self._settings,
                "delivery_status": deliveries,
                "alert_types": dict(CATEGORY_LABELS),
                "levels": dict(LEVEL_LABELS),
                "provinces": list(region_data.PROVINCES),
                "province_cities": {
                    key: list(value)
                    for key, value in region_data.PROVINCE_CITIES.items()
                },
                "started_at": self._started_at.isoformat(timespec="seconds"),
                "last_alert_poll_at": self._last_alert_poll_at,
                "last_alert_count": self._last_alert_count,
                "last_loop_error": self._last_loop_error,
            }
        )

    async def page_regions(self):
        return json_response(
            {
                "provinces": list(region_data.PROVINCES),
                "province_cities": {
                    key: list(value)
                    for key, value in region_data.PROVINCE_CITIES.items()
                },
                "cities": [
                    {
                        "province": region.province,
                        "city": region.city,
                        "station": region.station,
                    }
                    for region in region_data.REGIONS
                ],
            }
        )

    async def page_save_settings(self):
        payload = await request.json(default={})
        if not isinstance(payload, Mapping):
            return error_response("设置内容必须是 JSON 对象。")
        try:
            supplied_revision = int(payload.get("revision", -1))
        except (TypeError, ValueError):
            return error_response("设置版本格式无效。")
        async with self._state_lock:
            current_revision = int(self._settings.get("revision", 0))
            if supplied_revision != current_revision:
                return error_response(
                    "设置已被其他操作更新，请刷新后重试。", status_code=409
                )
            try:
                normalized = service.normalize_settings(
                    payload, previous=self._settings
                )
                for subscription in normalized["subscriptions"]:
                    target_error = self._validate_target_umo(
                        subscription["target_umo"],
                        require_available=bool(subscription["enabled"]),
                    )
                    if target_error:
                        raise service.ValidationError(target_error)
            except service.ValidationError as exc:
                return error_response(str(exc))
            normalized["revision"] = current_revision + 1
            self._settings = normalized
            await self.put_kv_data(SETTINGS_KEY, normalized)
        return json_response(
            {
                "ok": True,
                "revision": normalized["revision"],
                "message": "设置已保存。",
            }
        )

    async def page_test_source(self):
        started = time.perf_counter()
        try:
            result = await self._client_instance().test_connection()
            return json_response(
                {
                    "ok": True,
                    "alarm_count": result.get("alarm_count", 0),
                    "weather_city": result.get("weather_city", ""),
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "message": "中央气象台接口可用。",
                }
            )
        except Exception as exc:  # noqa: BLE001 - 管理页需要把错误回显给用户
            return json_response(
                {
                    "ok": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "message": self._safe_error(exc),
                }
            )

    async def page_preview(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        kind = str(payload.get("kind") or "alerts").lower()
        generated_at = datetime.now(CHINA_TIMEZONE).isoformat(timespec="seconds")
        try:
            if kind == "forecast":
                subscription = self._subscription_for(
                    str(payload.get("target_umo") or "")
                )
                cities = (
                    list(subscription.get("cities", []))
                    if subscription
                    else list(payload.get("cities") or [])
                )
                normalized: list[str] = []
                for value in cities:
                    try:
                        normalized.append(service.normalize_city(value))
                    except service.ValidationError:
                        continue
                if not normalized:
                    return json_response(
                        {
                            "ok": False,
                            "generated_at": generated_at,
                            "message": "没有可用城市。",
                        }
                    )
                stations = [service.resolve_region(item).station for item in normalized]
                forecasts, _errors = await self._client_instance().fetch_weather_many(
                    stations
                )
                blocks = [
                    service.format_weather_forecast(
                        forecasts[service.resolve_region(item).station], city=item
                    )
                    for item in normalized
                    if service.resolve_region(item).station in forecasts
                ]
                return json_response(
                    {
                        "ok": bool(blocks),
                        "kind": "forecast",
                        "generated_at": generated_at,
                        "text": "\n\n".join(blocks),
                        "message": "" if blocks else "没有取到天气数据。",
                    }
                )

            events = await self._client_instance().fetch_alarms()
            limit = int(self._settings.get("content_limit", 600))
            preview_events = [
                event for event in events if not service.event_is_expired(event)
            ][:5]
            texts = [
                service.format_alert_event(event, content_limit=limit)
                for event in preview_events
            ]
            return json_response(
                {
                    "ok": True,
                    "kind": "alerts",
                    "generated_at": generated_at,
                    "total": len(events),
                    "text": "\n\n".join(texts),
                    "message": "" if texts else "当前没有有效预警。",
                }
            )
        except Exception as exc:  # noqa: BLE001 - 预览失败需要回显原因
            return json_response(
                {
                    "ok": False,
                    "generated_at": generated_at,
                    "message": self._safe_error(exc),
                }
            )

    async def page_test_send(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        target_umo = str(payload.get("target_umo") or "").strip()
        if not target_umo:
            return json_response({"ok": False, "message": "缺少目标会话。"})
        text = str(payload.get("text") or "").strip() or (
            "【中央气象台播报测试】\n这是一条来自插件管理页的测试消息。"
        )
        succeeded, error = await self._send_text(target_umo, text)
        self._record_delivery(target_umo, "test", succeeded, error)
        return json_response(
            {
                "ok": succeeded,
                "message": "测试消息已发送。" if succeeded else f"发送失败：{error}",
            }
        )

    async def page_delete_subscription(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        target_umo = str(payload.get("target_umo") or "").strip()
        subscription_id = str(payload.get("id") or "").strip()
        async with self._state_lock:
            original = self._settings.get("subscriptions", [])
            remaining = [
                item
                for item in original
                if not (
                    (target_umo and item.get("target_umo") == target_umo)
                    or (subscription_id and item.get("id") == subscription_id)
                )
            ]
            if len(remaining) == len(original):
                return json_response({"ok": False, "message": "没有找到对应的订阅。"})
            self._settings["subscriptions"] = remaining
            self._settings["revision"] = int(self._settings.get("revision", 0)) + 1
            await self.put_kv_data(SETTINGS_KEY, self._settings)
        return json_response(
            {
                "ok": True,
                "revision": self._settings["revision"],
                "message": "订阅已删除。",
            }
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        return message[:300]


def _prune_delivery_status(entries: Mapping[str, Any]) -> dict[str, Any]:
    """按最近尝试时间裁剪投递记录。"""
    ordered = sorted(
        (
            (str(item.get("last_attempt_at") or ""), key, dict(item))
            for key, item in entries.items()
            if isinstance(item, Mapping)
        ),
        reverse=True,
    )
    return {key: value for _stamp, key, value in ordered[:MAX_DELIVERY_RECORDS]}
