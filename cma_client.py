"""中央气象台（nmc.cn）数据客户端：预警信号与城市天气。

本模块只负责网络请求与原始数据解析，不依赖 AstrBot，便于离线单测。
所有对外抛出的异常消息均为简体中文，且不会带出请求 URL 中的敏感参数。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

BASE_URL = "https://www.nmc.cn"
ALARM_LIST_URL = f"{BASE_URL}/rest/findAlarm"
ALARM_DETAIL_URL = BASE_URL + "/publish/alarm/{alert_id}.html"
WEATHER_URL = f"{BASE_URL}/rest/weather"
PROVINCE_URL = f"{BASE_URL}/rest/province"

DEFAULT_TIMEOUT_SECONDS = 20
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 3 * 1024 * 1024
ALARM_PAGE_SIZE = 500
MAX_ALARM_PAGES = 8
MISSING_VALUE = "9999"
MISSING_VALUES = ("", "-", "--", "—", "9999", "9998", "9997")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

LEVEL_ORDER = {"blue": 1, "yellow": 2, "orange": 3, "red": 4}
LEVEL_LABELS = {
    "blue": "蓝色",
    "yellow": "黄色",
    "orange": "橙色",
    "red": "红色",
}

CATEGORY_LABELS = {
    "rain": "暴雨",
    "wind": "大风",
    "fog": "大雾",
    "thunder": "雷电",
    "snow": "暴雪",
    "road_ice": "道路结冰",
    "cold": "寒潮低温",
    "heat": "高温",
    "typhoon": "台风",
    "dust": "沙尘",
    "hail": "冰雹",
    "drought": "干旱",
    "haze": "霾",
    "geological": "地质灾害",
    "flood": "山洪渍涝",
    "other": "其他预警",
}

# 关键词按优先级排列：先匹配到的分类生效，避免「雷雨大风」被「大风」以外的规则误判。
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("typhoon", ("台风",)),
    ("rain", ("特大暴雨", "大暴雨", "暴雨", "强降水", "短时强降雨")),
    ("road_ice", ("道路结冰", "道路冰雪", "路面结冰")),
    ("snow", ("暴雪", "大雪", "中雪", "小雪", "雨雪", "积雪")),
    ("cold", ("寒潮", "低温", "霜冻", "冰冻", "严寒")),
    ("heat", ("高温", "炎热", "中暑")),
    ("wind", ("雷雨大风", "雷暴大风", "海上大风", "大风", "强风", "飑线")),
    ("thunder", ("雷电", "雷暴", "雷击")),
    ("fog", ("大雾", "浓雾", "强浓雾", "雾")),
    ("dust", ("沙尘暴", "扬沙", "浮尘")),
    ("hail", ("冰雹",)),
    ("haze", ("霾",)),
    ("drought", ("干旱",)),
    ("geological", ("地质灾害", "滑坡", "泥石流", "崩塌")),
    ("flood", ("山洪", "中小河流洪水", "渍涝", "内涝", "洪水")),
)

_LEVEL_KEYWORDS = (
    ("red", "红色"),
    ("orange", "橙色"),
    ("yellow", "黄色"),
    ("blue", "蓝色"),
)

_ACTION_KEYWORDS = (
    ("cancel", ("解除", "取消", "撤销", "终止")),
    ("change", ("变更", "升级", "降级", "更新", "确认")),
    ("issue", ("发布", "继续发布")),
)

_TITLE_ACTION_PATTERN = re.compile(
    r"(发布|解除|取消|撤销|终止|变更|升级|降级|继续发布)"
)
_ADMIN_CODE_PATTERN = re.compile(r"^(\d{6})")
_ALARM_BLOCK_PATTERN = re.compile(
    r'<div[^>]*\bid\s*=\s*["\']?alarmtext["\']?[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_BLOCK_PATTERN = re.compile(
    r'<div[^>]*\bid\s*=\s*["\']?title["\']?[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_PUBTIME_BLOCK_PATTERN = re.compile(
    r'<div[^>]*\bid\s*=\s*["\']?pubtime["\']?[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)
_PUBTIME_PATTERN = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*时\s*(\d{1,2})\s*分"
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"[\s\u00a0]+")


class CMAClientError(RuntimeError):
    """客户端基础异常，消息为可直接展示给用户的简体中文。"""


class CMAParseError(CMAClientError):
    """上游返回了无法解析的数据。"""


class CMAHTTPError(CMAClientError):
    """上游返回了非预期的状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AlertEvent:
    """一条已归一化的气象灾害预警信号。"""

    source: str
    official_id: str
    title: str
    signal: str
    level: str
    category: str
    action: str
    admin_code: str
    issued_at: str
    link: str
    description: str = ""
    guidance: str = ""

    @property
    def is_cancellation(self) -> bool:
        """该条记录是否为解除/取消类通知。"""
        return self.action == "cancel"


@dataclass(frozen=True)
class ForecastPeriod:
    """一个天气预报表中的时段。"""

    label: str
    date: str
    weather: str
    min_temperature: str
    max_temperature: str
    wind: str


@dataclass(frozen=True)
class WeatherForecast:
    """一个城市实况 + 未来若干天的预报。"""

    station: str
    province: str
    city: str
    published_at: str
    temperature: str
    humidity: str
    feels_like: str
    weather: str
    wind: str
    air_quality: str
    aqi: str
    warn: str
    periods: tuple[ForecastPeriod, ...]
    link: str


def clean_text(value: Any) -> str:
    """把 HTML 片段转成紧凑的纯文本。"""
    text = str(value or "")
    text = _TAG_PATTERN.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\u3000", " ")
    return _SPACE_PATTERN.sub(" ", text).strip()


def detect_level(text: str) -> str:
    """从预警标题中识别颜色等级，识别不到时返回空串。"""
    for level, keyword in _LEVEL_KEYWORDS:
        if keyword in text:
            return level
    return ""


def detect_category(text: str) -> str:
    """从预警标题中识别灾害分类。"""
    for category, keywords in _CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword in text:
                return category
    return "other"


def detect_action(text: str) -> str:
    """从预警标题中识别发布/变更/解除等动作。"""
    for action, keywords in _ACTION_KEYWORDS:
        for keyword in keywords:
            if keyword in text:
                return action
    return "issue"


def parse_admin_code(alert_id: str) -> str:
    """从预警编号中取出 6 位行政区划码。"""
    match = _ADMIN_CODE_PATTERN.match(str(alert_id or "").strip())
    return match.group(1) if match else ""


def parse_issue_time(value: str) -> str:
    """把 ``2026/11/03 19:11`` 这类时间统一成 ISO 格式。"""
    text = str(value or "").strip()
    for pattern in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.replace(tzinfo=CHINA_TIMEZONE).isoformat()
    return ""


def build_alert_event(raw: dict[str, Any]) -> AlertEvent:
    """把 ``findAlarm`` 列表中的一项转换为 :class:`AlertEvent`。

    Args:
        raw: 列表接口返回的单条记录。

    Returns:
        归一化后的预警事件。

    Raises:
        CMAParseError: 缺少必要字段。
    """
    if not isinstance(raw, dict):
        raise CMAParseError("预警记录格式不正确。")
    alert_id = str(raw.get("alertid") or "").strip()
    title = clean_text(raw.get("title"))
    if not alert_id or not title:
        raise CMAParseError("预警记录缺少编号或标题。")

    action = detect_action(title)
    signal = title
    match = _TITLE_ACTION_PATTERN.search(title)
    if match:
        signal = title[match.end() :].strip() or title

    return AlertEvent(
        source="nmc",
        official_id=alert_id,
        title=title,
        signal=signal,
        level=detect_level(title),
        category=detect_category(title),
        action=action,
        admin_code=parse_admin_code(alert_id),
        issued_at=parse_issue_time(str(raw.get("issuetime") or "")),
        link=BASE_URL + str(raw.get("url") or "").strip()
        if str(raw.get("url") or "").startswith("/")
        else str(raw.get("url") or "").strip(),
    )


def parse_alarm_detail(page: str) -> tuple[str, str]:
    """从预警详情页 HTML 中提取正文与防御指南。

    Args:
        page: 详情页 HTML 文本。

    Returns:
        ``(正文, 防御指南)``，缺失时对应项为空串。
    """
    blocks = [clean_text(item) for item in _ALARM_BLOCK_PATTERN.findall(page)]
    blocks = [block for block in blocks if block]
    if not blocks:
        return "", ""

    description = blocks[0]
    guidance = ""
    for block in blocks[1:]:
        if block.startswith("防御指南") or "防御指南" in block[:12]:
            guidance = block
            break
    return description, guidance


def parse_alarm_page_title(page: str) -> tuple[str, str]:
    """从详情页提取标题与发布时间（用于校验与兜底）。"""
    title_match = _TITLE_BLOCK_PATTERN.search(page)
    title = clean_text(title_match.group(1)) if title_match else ""
    pub_match = _PUBTIME_BLOCK_PATTERN.search(page)
    issued_at = ""
    if pub_match:
        parsed = _PUBTIME_PATTERN.search(clean_text(pub_match.group(1)))
        if parsed:
            year, month, day, hour, minute = (int(part) for part in parsed.groups())
            try:
                issued_at = datetime(
                    year, month, day, hour, minute, tzinfo=CHINA_TIMEZONE
                ).isoformat()
            except ValueError:
                issued_at = ""
    return title, issued_at


def _text_or_missing(value: Any) -> str:
    """把 ``9999``、``-`` 之类的占位值统一成空串。"""
    text = str(value if value is not None else "").strip()
    if text in MISSING_VALUES:
        return ""
    return text


def _temperature(value: Any) -> str:
    text = _text_or_missing(value)
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def build_weather_forecast(raw: dict[str, Any]) -> WeatherForecast:
    """把 ``/rest/weather`` 的返回转换为 :class:`WeatherForecast`。

    Args:
        raw: 接口返回的完整 JSON。

    Returns:
        归一化后的天气数据。

    Raises:
        CMAParseError: 返回结构缺失。
    """
    if not isinstance(raw, dict) or raw.get("code") not in (0, "0"):
        raise CMAParseError("天气接口返回异常。")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise CMAParseError("天气接口没有返回数据。")
    real = data.get("real") or {}
    if not isinstance(real, dict):
        raise CMAParseError("天气接口缺少实况数据。")

    station = real.get("station") or {}
    if not isinstance(station, dict):
        station = {}
    weather = real.get("weather") or {}
    if not isinstance(weather, dict):
        weather = {}
    wind = real.get("wind") or {}
    if not isinstance(wind, dict):
        wind = {}
    warn = real.get("warn") or {}
    if not isinstance(warn, dict):
        warn = {}
    air = data.get("air") or {}
    if not isinstance(air, dict):
        air = {}

    periods: list[ForecastPeriod] = []
    predict = data.get("predict") or {}
    details = predict.get("detail") if isinstance(predict, dict) else None
    if isinstance(details, Sequence) and not isinstance(details, (str, bytes)):
        for index, item in enumerate(details):
            if not isinstance(item, dict):
                continue
            day = item.get("day") or {}
            night = item.get("night") or {}
            day_weather = (day.get("weather") or {}) if isinstance(day, dict) else {}
            night_weather = (
                (night.get("weather") or {}) if isinstance(night, dict) else {}
            )
            day_wind = (day.get("wind") or {}) if isinstance(day, dict) else {}
            night_wind = (night.get("wind") or {}) if isinstance(night, dict) else {}

            day_text = _text_or_missing(day_weather.get("info"))
            night_text = _text_or_missing(night_weather.get("info"))
            weather_text = day_text
            if day_text and night_text and day_text != night_text:
                weather_text = f"{day_text}转{night_text}"
            elif not day_text:
                weather_text = night_text

            temperatures = [
                _temperature(day_weather.get("temperature")),
                _temperature(night_weather.get("temperature")),
            ]
            numbers = []
            for item_text in temperatures:
                try:
                    numbers.append(float(item_text))
                except (TypeError, ValueError):
                    continue
            max_temperature = _temperature(day_weather.get("temperature"))
            min_temperature = _temperature(night_weather.get("temperature"))
            if len(numbers) >= 2 and numbers[0] < numbers[1]:
                max_temperature, min_temperature = min_temperature, max_temperature

            wind_direct = _text_or_missing(day_wind.get("direct")) or _text_or_missing(
                night_wind.get("direct")
            )
            wind_power = _text_or_missing(day_wind.get("power")) or _text_or_missing(
                night_wind.get("power")
            )
            wind_text = " ".join(part for part in (wind_direct, wind_power) if part)

            date_text = str(item.get("date") or "").strip()
            if index == 0:
                label = "今天"
            elif index == 1:
                label = "明天"
            elif index == 2:
                label = "后天"
            else:
                label = _weekday_label(date_text) or f"第 {index + 1} 天"

            if not weather_text and not max_temperature and not min_temperature:
                continue
            periods.append(
                ForecastPeriod(
                    label=label,
                    date=date_text,
                    weather=weather_text,
                    min_temperature=min_temperature,
                    max_temperature=max_temperature,
                    wind=wind_text,
                )
            )

    warn_text = _text_or_missing(warn.get("issuecontent")) or _text_or_missing(
        warn.get("alert")
    )
    url = str(station.get("url") or "").strip()
    return WeatherForecast(
        station=str(station.get("code") or "").strip(),
        province=str(station.get("province") or "").strip(),
        city=str(station.get("city") or "").strip(),
        published_at=str(real.get("publish_time") or "").strip(),
        temperature=_temperature(weather.get("temperature")),
        humidity=_temperature(weather.get("humidity")),
        feels_like=_temperature(weather.get("feelst")),
        weather=_text_or_missing(weather.get("info")),
        wind=" ".join(
            part
            for part in (
                _text_or_missing(wind.get("direct")),
                _text_or_missing(wind.get("power")),
            )
            if part
        ),
        air_quality=_text_or_missing(air.get("text")),
        aqi=_text_or_missing(air.get("aqi")),
        warn=warn_text,
        periods=tuple(periods),
        link=BASE_URL + url if url.startswith("/") else url,
    )


_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _weekday_label(date_text: str) -> str:
    """把 ``2026-11-05`` 转成「周四」。"""
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return _WEEKDAY_NAMES[parsed.weekday()]


def alert_fingerprint(event: AlertEvent) -> str:
    """生成与来源无关的去重指纹。"""
    parts = [
        event.official_id,
        event.signal,
        event.level,
        event.action,
        event.issued_at,
    ]
    canonical = "\x1f".join(part.strip() for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CMAClient:
    """中央气象台 HTTP 客户端。

    Args:
        timeout: 单次请求超时时间（秒）。
        retries: 失败重试次数。
        proxy: 可选 HTTP 代理地址。
        user_agent: 可选自定义 User-Agent。
    """

    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        retries: int = 2,
        proxy: str = "",
        user_agent: str = "",
    ) -> None:
        self._timeout = max(5, min(int(timeout or DEFAULT_TIMEOUT_SECONDS), 120))
        self._retries = max(0, min(int(retries or 0), 5))
        self._proxy = str(proxy or "").strip() or None
        self._user_agent = str(user_agent or "").strip() or USER_AGENT
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> CMAClient:  # noqa: PYI034
        await self._ensure_session()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._session

    async def close(self) -> None:
        """关闭底层连接池。"""
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def _get(self, url: str, *, limit: int, referer: str = "") -> bytes:
        """执行一次带重试的 GET 请求并返回原始字节。"""
        session = await self._ensure_session()
        headers = {"Referer": referer} if referer else {}
        last_error: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                async with session.get(url, headers=headers, proxy=self._proxy) as resp:
                    if resp.status != 200:
                        raise CMAHTTPError(
                            resp.status, f"中央气象台接口返回状态码 {resp.status}。"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > limit:
                            raise CMAClientError("中央气象台返回内容过大，已中止解析。")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError) as exc:
                last_error = exc
            except CMAHTTPError as exc:
                last_error = exc
                if 400 <= exc.status < 500:
                    break
            if attempt < self._retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        message = str(last_error) if last_error else "未知错误"
        raise CMAClientError(f"访问中央气象台失败：{message}")

    async def fetch_alarm_page(
        self, *, page_no: int = 1, page_size: int = ALARM_PAGE_SIZE
    ) -> dict[str, Any]:
        """请求一页预警列表。

        Args:
            page_no: 页码，从 1 开始。
            page_size: 每页条数。

        Returns:
            解析后的 ``data`` 字典。

        Raises:
            CMAParseError: 返回不是预期 JSON。
        """
        url = (
            f"{ALARM_LIST_URL}?pageNo={int(page_no)}&pageSize={int(page_size)}"
            "&signaltype=&signallevel=&province="
        )
        payload = await self._get(url, limit=MAX_JSON_BYTES)
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CMAParseError("中央气象台预警列表不是有效 JSON。") from exc
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, dict):
            raise CMAParseError("中央气象台预警列表结构异常。")
        return data

    async def fetch_alarms(self) -> tuple[AlertEvent, ...]:
        """拉取全国当前全部预警信号。

        Returns:
            归一化后的预警事件元组，顺序与上游一致。
        """
        first = await self.fetch_alarm_page(page_no=1)
        page = first.get("page") if isinstance(first.get("page"), dict) else {}
        items: list[Any] = list(page.get("list") or [])
        try:
            total_pages = int(page.get("totalPage") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        total_pages = max(1, min(total_pages, MAX_ALARM_PAGES))

        for page_no in range(2, total_pages + 1):
            extra = await self.fetch_alarm_page(page_no=page_no)
            extra_page = (
                extra.get("page") if isinstance(extra.get("page"), dict) else {}
            )
            items.extend(extra_page.get("list") or [])

        events: list[AlertEvent] = []
        seen: set[str] = set()
        for item in items:
            try:
                event = build_alert_event(item)
            except CMAParseError:
                continue
            if event.official_id in seen:
                continue
            seen.add(event.official_id)
            events.append(event)
        return tuple(events)

    async def fetch_alarm_detail(self, alert_id: str) -> tuple[str, str]:
        """拉取一条预警的正文与防御指南。

        Args:
            alert_id: 预警编号。

        Returns:
            ``(正文, 防御指南)``。
        """
        alert_id = str(alert_id or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z_\-]{4,80}", alert_id):
            raise CMAParseError("预警编号格式不正确。")
        url = ALARM_DETAIL_URL.format(alert_id=alert_id)
        payload = await self._get(url, limit=MAX_HTML_BYTES, referer=BASE_URL + "/")
        try:
            page = payload.decode("utf-8")
        except UnicodeDecodeError:
            page = payload.decode("utf-8", "replace")
        return parse_alarm_detail(page)

    async def fetch_weather(self, station: str) -> WeatherForecast:
        """拉取一个观测站的实况与预报。

        Args:
            station: 中央气象台 stationid。

        Returns:
            归一化后的天气数据。

        Raises:
            CMAParseError: 站点编号非法或返回异常。
        """
        station = str(station or "").strip()
        if not re.fullmatch(r"[0-9A-Za-z]{3,16}", station):
            raise CMAParseError("观测站编号格式不正确。")
        payload = await self._get(
            f"{WEATHER_URL}?stationid={station}",
            limit=MAX_JSON_BYTES,
            referer=BASE_URL + "/",
        )
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CMAParseError("中央气象台天气接口不是有效 JSON。") from exc
        return build_weather_forecast(document)

    async def fetch_weather_many(
        self, stations: Iterable[str]
    ) -> tuple[dict[str, WeatherForecast], dict[str, str]]:
        """并发拉取多个站点天气。

        Args:
            stations: 观测站编号集合。

        Returns:
            ``(成功的站点数据, 失败的站点及原因)``。
        """
        selected = list(dict.fromkeys(str(item).strip() for item in stations if item))
        if not selected:
            return {}, {}
        results = await asyncio.gather(
            *(self.fetch_weather(station) for station in selected),
            return_exceptions=True,
        )
        forecasts: dict[str, WeatherForecast] = {}
        errors: dict[str, str] = {}
        for station, result in zip(selected, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                errors[station] = str(result) or result.__class__.__name__
            else:
                forecasts[station] = result
        return forecasts, errors

    async def test_connection(self) -> dict[str, Any]:
        """探测预警列表与天气接口是否可用。

        Returns:
            含 ``alarm_count`` / ``weather_city`` / ``elapsed_ms`` 的字典。
        """
        started = datetime.now(CHINA_TIMEZONE)
        data = await self.fetch_alarm_page(page_no=1, page_size=5)
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        alarm_count = int(page.get("count") or 0)
        forecast = await self.fetch_weather("Wqsps")
        elapsed = int((datetime.now(CHINA_TIMEZONE) - started).total_seconds() * 1000)
        return {
            "alarm_count": alarm_count,
            "weather_city": forecast.city or "北京",
            "elapsed_ms": elapsed,
        }


__all__ = [
    "ALARM_PAGE_SIZE",
    "BASE_URL",
    "CATEGORY_LABELS",
    "CHINA_TIMEZONE",
    "LEVEL_LABELS",
    "LEVEL_ORDER",
    "AlertEvent",
    "CMAClient",
    "CMAClientError",
    "CMAHTTPError",
    "CMAParseError",
    "ForecastPeriod",
    "WeatherForecast",
    "alert_fingerprint",
    "build_alert_event",
    "build_weather_forecast",
    "clean_text",
    "detect_action",
    "detect_category",
    "detect_level",
    "parse_admin_code",
    "parse_alarm_detail",
    "parse_alarm_page_title",
    "parse_issue_time",
]
