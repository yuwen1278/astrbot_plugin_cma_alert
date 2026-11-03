"""配置与文案服务层：订阅规则校验、事件过滤、消息格式化。

本模块不依赖 AstrBot，可独立单测。所有面向用户的文案均为简体中文。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any

from . import regions as region_data
from .cma_client import (
    CATEGORY_LABELS,
    CHINA_TIMEZONE,
    LEVEL_LABELS,
    LEVEL_ORDER,
    AlertEvent,
    WeatherForecast,
)

DEFAULT_POLL_INTERVAL_SECONDS = 180
DEFAULT_FORECAST_TIMES = ("07:30", "19:00")
MAX_FORECAST_TIMES = 4
MAX_MESSAGE_LENGTH = 1500
MAX_SUBSCRIPTIONS = 30
STATE_RETENTION_DAYS = 14
STATE_MAX_ENTRIES = 3000
MIN_POLL_INTERVAL = 60
MAX_POLL_INTERVAL = 1800
MAX_DETAIL_FETCH_PER_RUN = 5
MAX_CONTENT_LENGTH = 600

ALERT_TYPES = tuple(CATEGORY_LABELS)
LEVELS = ("blue", "yellow", "orange", "red")
ALERT_ACTIONS = ("issue", "change", "cancel")

_LEVEL_ALIASES = {
    "蓝": "blue",
    "蓝色": "blue",
    "黄": "yellow",
    "黄色": "yellow",
    "橙": "orange",
    "橙色": "orange",
    "红": "red",
    "红色": "red",
}
_CATEGORY_ALIASES = {key: key for key in ALERT_TYPES}
_CATEGORY_ALIASES.update({label: key for key, label in CATEGORY_LABELS.items()})
_ACTION_ALIASES = {
    "issue": "issue",
    "发布": "issue",
    "change": "change",
    "变更": "change",
    "cancel": "cancel",
    "解除": "cancel",
}
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_UMO_PATTERN = re.compile(
    r"^[^\s:]{1,128}:(?:GroupMessage|FriendMessage):[^\s:]{1,256}$"
)
_CITY_SUFFIXES = (
    "特别行政区",
    "自治区",
    "自治州",
    "地区",
    "盟",
    "省",
    "市",
    "县",
    "区",
    "旗",
)


class ValidationError(ValueError):
    """表示配置或输入不合法。"""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    """从映射、dataclass 或普通对象上按顺序取第一个存在的字段。"""
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
        return _value(obj, *names, default=default)
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def city_short_name(city: str) -> str:
    """把「广州市」这类全称压缩成「广州」，用于文本匹配。"""
    text = str(city or "").strip()
    for suffix in _CITY_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def normalize_city(value: Any) -> str:
    """校验并归一化一个城市名。

    Args:
        value: 用户输入的城市名，可带或不带「市」后缀。

    Returns:
        行政区划表中的城市全称。

    Raises:
        ValidationError: 城市不在支持范围内。
    """
    text = str(value or "").strip()
    if not text:
        raise ValidationError("城市名不能为空。")
    if text in region_data.CITY_INDEX:
        return text
    for suffix in _CITY_SUFFIXES:
        candidate = text + suffix
        if candidate in region_data.CITY_INDEX:
            return candidate
    for city in region_data.CITY_INDEX:
        if city_short_name(city) == text:
            return city
    raise ValidationError(f"暂不支持的城市：{text}")


def normalize_province(value: Any) -> str:
    """校验并归一化一个省份名。"""
    text = str(value or "").strip()
    if not text:
        raise ValidationError("省份不能为空。")
    if text in region_data.PROVINCE_CITIES:
        return text
    for suffix in ("省", "市", "自治区", "特别行政区"):
        candidate = text + suffix
        if candidate in region_data.PROVINCE_CITIES:
            return candidate
    for province in region_data.PROVINCE_CITIES:
        if city_short_name(province) == text:
            return province
    raise ValidationError(f"暂不支持的省份：{text}")


def resolve_region(city: str) -> region_data.Region:
    """按城市名取得对应的观测站信息。"""
    normalized = normalize_city(city)
    candidates = region_data.CITY_INDEX[normalized]
    return candidates[0]


def normalize_alert_type(value: Any) -> str:
    """校验并归一化灾害类型。"""
    text = str(value or "").strip()
    key = _CATEGORY_ALIASES.get(text) or _CATEGORY_ALIASES.get(text.lower())
    if key is None:
        raise ValidationError(f"暂不支持的预警类型：{text or '空值'}")
    return key


def normalize_level(value: Any) -> str:
    """校验并归一化预警等级。"""
    text = str(value or "").strip()
    key = _LEVEL_ALIASES.get(text) or (text if text in LEVELS else None)
    if key is None:
        raise ValidationError(f"暂不支持的预警等级：{text or '空值'}")
    return key


def normalize_action(value: Any) -> str:
    """校验并归一化预警动作。"""
    text = str(value or "").strip()
    key = _ACTION_ALIASES.get(text)
    if key is None:
        raise ValidationError(f"暂不支持的预警动作：{text or '空值'}")
    return key


def _normalize_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "是", "开"}:
        return True
    if text in {"0", "false", "no", "off", "否", "关", ""}:
        return False
    raise ValidationError(f"{field}必须是布尔值。")


def _normalize_str_list(value: Any, field: str, *, max_items: int = 200) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, Sequence):
        raise ValidationError(f"{field}必须是列表。")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    if len(result) > max_items:
        raise ValidationError(f"{field}最多支持 {max_items} 项。")
    return result


def normalize_subscription(raw: Mapping[str, Any]) -> dict[str, Any]:
    """校验并归一化一条订阅规则。

    Args:
        raw: 来自 KV 或管理页面的原始订阅。

    Returns:
        可直接序列化的订阅字典。

    Raises:
        ValidationError: 字段缺失或取值非法。
    """
    if not isinstance(raw, Mapping):
        raise ValidationError("订阅规则必须是对象。")

    target_umo = str(raw.get("target_umo") or "").strip()
    if not target_umo:
        raise ValidationError("订阅规则缺少会话目标。")
    if len(target_umo) > 512 or not _UMO_PATTERN.fullmatch(target_umo):
        raise ValidationError("会话目标格式不正确。")

    subscription_id = str(raw.get("id") or "").strip()
    if not subscription_id:
        digest = hashlib.sha256(target_umo.encode("utf-8")).hexdigest()[:16]
        subscription_id = f"subscription-{digest}"
    if len(subscription_id) > 128:
        raise ValidationError("订阅标识过长。")

    cities: list[str] = []
    for value in _normalize_str_list(raw.get("cities"), "城市列表"):
        city = normalize_city(value)
        if city not in cities:
            cities.append(city)

    alert_types: list[str] = []
    for value in _normalize_str_list(
        raw.get("alert_types", list(ALERT_TYPES)), "预警类型"
    ):
        alert_type = normalize_alert_type(value)
        if alert_type not in alert_types:
            alert_types.append(alert_type)

    levels: list[str] = []
    for value in _normalize_str_list(raw.get("levels", list(LEVELS)), "预警等级"):
        level = normalize_level(value)
        if level not in levels:
            levels.append(level)
    levels.sort(key=lambda item: LEVEL_ORDER[item])

    actions: list[str] = []
    for value in _normalize_str_list(
        raw.get("actions", list(ALERT_ACTIONS)), "预警动作"
    ):
        action = normalize_action(value)
        if action not in actions:
            actions.append(action)

    forecast_times: list[str] = []
    for value in _normalize_str_list(
        raw.get("forecast_times", list(DEFAULT_FORECAST_TIMES)), "播报时间"
    ):
        if not _TIME_PATTERN.fullmatch(value):
            raise ValidationError(f"播报时间格式不正确：{value}")
        if value not in forecast_times:
            forecast_times.append(value)
    forecast_times.sort()
    if len(forecast_times) > MAX_FORECAST_TIMES:
        raise ValidationError(f"每条订阅最多设置 {MAX_FORECAST_TIMES} 个播报时间。")

    forecast_enabled = _normalize_bool(
        raw.get("forecast_enabled", True), "天气播报开关"
    )
    if forecast_enabled and not cities:
        raise ValidationError("开启天气播报时至少需要选择一个城市。")

    return {
        "id": subscription_id,
        "target_umo": target_umo,
        "enabled": _normalize_bool(raw.get("enabled", True), "订阅开关"),
        "cities": cities,
        "alert_types": alert_types,
        "levels": levels,
        "actions": actions,
        "forecast_enabled": forecast_enabled,
        "forecast_times": forecast_times,
        "include_detail": _normalize_bool(
            raw.get("include_detail", True), "预警详情开关"
        ),
    }


def default_settings() -> dict[str, Any]:
    """返回默认的插件设置。"""
    return {
        "revision": 0,
        "enabled": True,
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
        "content_limit": MAX_CONTENT_LENGTH,
        "subscriptions": [],
    }


def normalize_settings(
    raw: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """校验并归一化全部插件设置。

    Args:
        raw: 原始设置。
        previous: 上一版设置，用于补齐被省略的字段。

    Returns:
        归一化后的设置字典。

    Raises:
        ValidationError: 任一字段非法。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValidationError("插件设置必须是对象。")

    base = default_settings()
    if isinstance(previous, Mapping):
        base.update(
            {key: value for key, value in previous.items() if key != "subscriptions"}
        )
    base.update(dict(raw))

    try:
        revision = int(base.get("revision", 0))
    except (TypeError, ValueError) as exc:
        raise ValidationError("设置版本号必须是整数。") from exc
    if revision < 0:
        raise ValidationError("设置版本号必须是非负整数。")

    try:
        interval = int(base.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ValidationError("轮询间隔必须是整数秒。") from exc
    if not MIN_POLL_INTERVAL <= interval <= MAX_POLL_INTERVAL:
        raise ValidationError(
            f"轮询间隔必须在 {MIN_POLL_INTERVAL} 到 {MAX_POLL_INTERVAL} 秒之间。"
        )

    try:
        content_limit = int(base.get("content_limit", MAX_CONTENT_LENGTH))
    except (TypeError, ValueError) as exc:
        raise ValidationError("正文长度上限必须是整数。") from exc
    content_limit = max(80, min(content_limit, 2000))

    subscriptions_value = base.get("subscriptions", [])
    if isinstance(subscriptions_value, (str, bytes)) or not isinstance(
        subscriptions_value, Sequence
    ):
        raise ValidationError("订阅规则必须是列表。")
    if len(subscriptions_value) > MAX_SUBSCRIPTIONS:
        raise ValidationError(f"最多支持 {MAX_SUBSCRIPTIONS} 条订阅规则。")

    subscriptions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_targets: set[str] = set()
    for item in subscriptions_value:
        subscription = normalize_subscription(item)
        if subscription["id"] in seen_ids:
            raise ValidationError("订阅标识不可重复。")
        if subscription["target_umo"] in seen_targets:
            raise ValidationError("同一个会话只能建立一条订阅规则。")
        seen_ids.add(subscription["id"])
        seen_targets.add(subscription["target_umo"])
        subscriptions.append(subscription)

    return {
        "revision": revision,
        "enabled": _normalize_bool(base.get("enabled", True), "总开关"),
        "poll_interval_seconds": interval,
        "content_limit": content_limit,
        "subscriptions": subscriptions,
    }


def subscription_accepts_event(
    subscription: Mapping[str, Any], event: AlertEvent
) -> bool:
    """判断一条订阅是否应收到该预警。

    Args:
        subscription: 归一化后的订阅规则。
        event: 预警事件。

    Returns:
        命中时返回 True。
    """
    if not bool(subscription.get("enabled", False)):
        return False
    if event.action not in set(subscription.get("actions", ALERT_ACTIONS)):
        return False
    if event.category not in set(subscription.get("alert_types", ALERT_TYPES)):
        return False
    if event.level and event.level not in set(subscription.get("levels", LEVELS)):
        return False
    cities = [str(city) for city in subscription.get("cities", [])]
    if not cities:
        # 未选城市时只接收全国性预警（如台风），避免默认淹没。
        return event.category == "typhoon"
    return any(_event_matches_city(event, city) for city in cities)


def _event_matches_city(event: AlertEvent, city: str) -> bool:
    """按行政区划码与标题文本双重匹配城市。"""
    try:
        region = resolve_region(city)
    except ValidationError:
        return False
    admin_code = event.admin_code
    if admin_code and region.city_code:
        if admin_code.startswith(region.city_code):
            return True
        # 县级市：省直辖县级市用 6 位代码，其余按前 4 位地级码匹配。
        if len(region.city_code) == 6 and admin_code.startswith(region.city_code[:4]):
            return admin_code.startswith(region.city_code)
    short = city_short_name(region.city)
    if not short:
        return False
    title = unicodedata.normalize("NFKC", event.title)
    if short in title:
        return True
    province_short = city_short_name(region.province)
    # 省级台发布的预警（如「海南省气象台发布海上雷雨大风」）视为覆盖全省。
    return bool(province_short and province_short in title and event.level)


def filter_subscriptions(
    subscriptions: Sequence[Mapping[str, Any]], event: AlertEvent
) -> list[Mapping[str, Any]]:
    """返回匹配该预警的全部订阅。"""
    return [
        subscription
        for subscription in subscriptions
        if subscription_accepts_event(subscription, event)
    ]


def format_alert_event(
    event: AlertEvent,
    *,
    content_limit: int = MAX_CONTENT_LENGTH,
) -> str:
    """把一条预警格式化成推送文本。

    Args:
        event: 预警事件。
        content_limit: 正文截断长度。

    Returns:
        简体中文纯文本。
    """
    label = CATEGORY_LABELS.get(event.category, "气象预警")
    level_text = LEVEL_LABELS.get(event.level, "")
    action_text = {"cancel": "解除", "change": "变更"}.get(event.action, "")
    heading = f"【{label}{level_text}预警{action_text}】"

    lines = [heading]
    if event.title:
        lines.append(event.title)
    issued = _display_time(event.issued_at)
    if issued:
        lines.append(f"发布时间：{issued}")
    description = _truncate(event.description, content_limit)
    if description:
        lines.append("")
        lines.append(description)
    guidance = _truncate(event.guidance, content_limit)
    if guidance:
        lines.append("")
        lines.append(guidance)
    if event.link:
        lines.append("")
        lines.append(f"详情：{event.link}")
    return "\n".join(lines).strip()


def format_weather_forecast(
    forecast: WeatherForecast,
    *,
    city: str = "",
    max_periods: int = 3,
) -> str:
    """把城市天气格式化成推送文本。

    Args:
        forecast: 天气数据。
        city: 展示用城市名，缺省时用接口返回的城市。
        max_periods: 最多展示的预报天数。

    Returns:
        简体中文纯文本。
    """
    name = str(city or forecast.city or "未知城市").strip()
    lines = [f"【{name}天气预报】"]
    if forecast.published_at:
        lines.append(f"更新时间：{forecast.published_at}")

    real_parts: list[str] = []
    if forecast.weather:
        real_parts.append(forecast.weather)
    if forecast.temperature:
        real_parts.append(f"{forecast.temperature}°C")
    if forecast.feels_like:
        real_parts.append(f"体感 {forecast.feels_like}°C")
    if forecast.humidity:
        real_parts.append(f"湿度 {forecast.humidity}%")
    if forecast.wind:
        real_parts.append(forecast.wind)
    if real_parts:
        lines.append("当前：" + " ".join(real_parts))

    air_parts: list[str] = []
    if forecast.air_quality:
        air_parts.append(forecast.air_quality)
    if forecast.aqi:
        air_parts.append(f"AQI {forecast.aqi}")
    if air_parts:
        lines.append("空气：" + " ".join(air_parts))

    for period in list(forecast.periods)[: max(1, max_periods)]:
        parts = [f"{period.label}"]
        if period.weather:
            parts.append(period.weather)
        if period.min_temperature and period.max_temperature:
            parts.append(f"{period.min_temperature}~{period.max_temperature}°C")
        elif period.min_temperature:
            parts.append(f"最低 {period.min_temperature}°C")
        elif period.max_temperature:
            parts.append(f"最高 {period.max_temperature}°C")
        if period.wind:
            parts.append(period.wind)
        lines.append("｜".join(parts))

    if forecast.warn:
        lines.append(f"当前预警：{forecast.warn}")
    if forecast.link:
        lines.append(f"详情：{forecast.link}")
    return "\n".join(lines).strip()


def split_message(
    text: str,
    max_chars: int = MAX_MESSAGE_LENGTH,
) -> list[str]:
    """把长文本按段落切分为多条消息。

    Args:
        text: 待切分文本。
        max_chars: 单条消息字符上限。

    Returns:
        非空文本块列表。
    """
    limit = max(80, int(max_chars or MAX_MESSAGE_LENGTH))
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    pieces: list[str] = []
    for paragraph in normalized.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        for line in paragraph.split("\n"):
            while len(line) > limit:
                pieces.append(line[:limit])
                line = line[limit:]
            if line:
                pieces.append(line)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(piece) <= limit:
            current += separator + piece
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks


def forecast_slot_key(target_umo: str, date: str, send_time: str) -> str:
    """生成天气播报槽位键，用于避免重复播报。"""
    raw = f"{target_umo}\x1f{date}\x1f{send_time}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def due_forecast_slots(
    subscription: Mapping[str, Any],
    *,
    now: datetime | None = None,
    grace_minutes: int = 30,
) -> list[tuple[str, str]]:
    """计算当前应触发的天气播报槽位。

    Args:
        subscription: 订阅规则。
        now: 当前时间，缺省使用中国标准时间。
        grace_minutes: 允许的迟到窗口。

    Returns:
        ``[(日期, 时间)]`` 列表。
    """
    if not bool(subscription.get("forecast_enabled", False)):
        return []
    current = now or datetime.now(CHINA_TIMEZONE)
    current = current.astimezone(CHINA_TIMEZONE)
    slots: list[tuple[str, str]] = []
    for send_time in subscription.get("forecast_times", []):
        try:
            hour, minute = (int(part) for part in str(send_time).split(":", 1))
        except (TypeError, ValueError):
            continue
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = current - scheduled
        if timedelta(0) <= delta <= timedelta(minutes=grace_minutes):
            slots.append((current.strftime("%Y-%m-%d"), str(send_time)))
    return slots


def prune_state(
    entries: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_entries: int = STATE_MAX_ENTRIES,
    retention_days: int = STATE_RETENTION_DAYS,
) -> dict[str, Any]:
    """清理过期的去重记录。

    Args:
        entries: ``{指纹: ISO 时间}`` 或 ``{指纹: {"at": ISO}}``。
        now: 当前时间。
        max_entries: 保留的最大条数。
        retention_days: 保留天数。

    Returns:
        清理后的字典。
    """
    current = now or datetime.now(CHINA_TIMEZONE)
    threshold = current - timedelta(days=retention_days)
    kept: list[tuple[str, str, Any]] = []
    for key, value in entries.items():
        raw = value.get("at") if isinstance(value, Mapping) else value
        stamp = str(raw or "")
        parsed = _parse_iso(stamp)
        if parsed is not None and parsed < threshold:
            continue
        kept.append((stamp, str(key), value))
    kept.sort(reverse=True)
    result: dict[str, Any] = {}
    for _stamp, key, value in kept[:max_entries]:
        result[key] = value
    return result


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TIMEZONE)
    return parsed.astimezone(CHINA_TIMEZONE)


def _display_time(value: str) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return str(value or "").strip()
    return parsed.strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def event_is_recent(
    event: AlertEvent,
    *,
    now: datetime | None = None,
    window_minutes: int = 180,
) -> bool:
    """判断预警是否足够新，避免重启后补发历史预警。"""
    issued = _parse_iso(event.issued_at)
    if issued is None:
        return True
    current = now or datetime.now(CHINA_TIMEZONE)
    return current - issued <= timedelta(minutes=window_minutes)


def event_is_expired(
    event: AlertEvent,
    *,
    now: datetime | None = None,
    max_age_hours: int = 24,
) -> bool:
    """判断预警是否已过期。"""
    if event.is_cancellation:
        return False
    issued = _parse_iso(event.issued_at)
    if issued is None:
        return False
    current = now or datetime.now(CHINA_TIMEZONE)
    return current - issued > timedelta(hours=max_age_hours)


__all__ = [
    "ALERT_ACTIONS",
    "ALERT_TYPES",
    "LEVELS",
    "MAX_MESSAGE_LENGTH",
    "ValidationError",
    "city_short_name",
    "default_settings",
    "due_forecast_slots",
    "event_is_expired",
    "event_is_recent",
    "filter_subscriptions",
    "forecast_slot_key",
    "format_alert_event",
    "format_weather_forecast",
    "normalize_city",
    "normalize_level",
    "normalize_province",
    "normalize_settings",
    "normalize_subscription",
    "prune_state",
    "resolve_region",
    "split_message",
    "subscription_accepts_event",
]
