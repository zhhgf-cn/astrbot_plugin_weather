import asyncio
import json
import time
from typing import Any

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# 天气代码到中文描述的映射 (补充 wttr.in lang_zh-cn 缺失的情况)
WEATHER_CODE_MAP: dict[str, str] = {
    "113": "晴",
    "116": "多云",
    "119": "阴",
    "122": "阴",
    "143": "薄雾",
    "176": "小雨",
    "179": "雨夹雪",
    "182": "雨夹雪",
    "185": "雨夹雪",
    "200": "雷阵雨",
    "227": "小雪",
    "230": "大雪",
    "248": "雾",
    "260": "雾",
    "263": "小雨",
    "266": "小雨",
    "281": "冻雨",
    "284": "冻雨",
    "293": "小雨",
    "296": "小雨",
    "299": "中雨",
    "302": "中雨",
    "305": "大雨",
    "308": "大雨",
    "311": "暴雨",
    "314": "暴雨",
    "317": "雨夹雪",
    "320": "小雪",
    "323": "小雪",
    "326": "中雪",
    "329": "大雪",
    "332": "大雪",
    "335": "暴雪",
    "338": "暴雪",
    "350": "冻雨",
    "353": "小雨",
    "356": "中雨",
    "359": "大雨",
    "362": "雨夹雪",
    "365": "雨夹雪",
    "368": "小雪",
    "371": "大雪",
    "374": "冻雨",
    "377": "冻雨",
    "386": "雷阵雨",
    "389": "雷暴",
    "392": "雷阵雪",
    "395": "大雪",
}

# 天气代码到 emoji 的映射
WEATHER_EMOJI_MAP: dict[str, str] = {
    "113": "☀️",
    "116": "⛅",
    "119": "☁️",
    "122": "☁️",
    "143": "🌫️",
    "176": "🌦️",
    "179": "🌨️",
    "182": "🌨️",
    "185": "🌨️",
    "200": "⛈️",
    "227": "🌨️",
    "230": "❄️",
    "248": "🌫️",
    "260": "🌫️",
    "263": "🌦️",
    "266": "🌦️",
    "281": "🌧️",
    "284": "🌧️",
    "293": "🌦️",
    "296": "🌦️",
    "299": "🌧️",
    "302": "🌧️",
    "305": "🌧️",
    "308": "🌧️",
    "311": "🌧️",
    "314": "🌧️",
    "317": "🌨️",
    "320": "🌨️",
    "323": "🌨️",
    "326": "❄️",
    "329": "❄️",
    "332": "❄️",
    "335": "❄️",
    "338": "❄️",
    "350": "🌧️",
    "353": "🌦️",
    "356": "🌧️",
    "359": "🌧️",
    "362": "🌨️",
    "365": "🌨️",
    "368": "🌨️",
    "371": "❄️",
    "374": "🌧️",
    "377": "🌧️",
    "386": "⛈️",
    "389": "⛈️",
    "392": "🌨️",
    "395": "❄️",
}


def _get_weather_desc(weather_code: str, lang_zh: str | None) -> str:
    """获取天气描述，优先使用 API 返回的中文，其次用本地映射。"""
    if lang_zh:
        desc = lang_zh.strip()
        if desc and desc != "N/A":
            return desc
    return WEATHER_CODE_MAP.get(weather_code, "未知")


def _get_weather_emoji(weather_code: str) -> str:
    """根据天气代码获取 emoji。"""
    return WEATHER_EMOJI_MAP.get(weather_code, "🌡️")


def _wind_dir_translate(dir_en: str) -> str:
    """将英文风向翻译为中文。"""
    mapping = {
        "N": "北", "NNE": "北东北", "NE": "东北", "ENE": "东东北",
        "E": "东", "ESE": "东东南", "SE": "东南", "SSE": "南东南",
        "S": "南", "SSW": "南西南", "SW": "西南", "WSW": "西西南",
        "W": "西", "WNW": "西西北", "NW": "西北", "NNW": "北西北",
    }
    return mapping.get(dir_en, dir_en)


async def _fetch_weather(session: aiohttp.ClientSession, location: str, lang: str = "zh-cn") -> dict[str, Any] | None:
    """
    从 wttr.in 获取天气 JSON 数据。

    Args:
        session: aiohttp 会话
        location: 城市名（支持中英文、机场代码、坐标等）
        lang: 语言代码

    Returns:
        天气 JSON 字典，失败返回 None
    """
    # 对 location 进行 URL 编码（支持中文城市名）
    from urllib.parse import quote

    encoded_loc = quote(location.strip(), safe="")
    url = f"https://wttr.in/{encoded_loc}?format=j1&lang={lang}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.warning(f"wttr.in 请求失败: HTTP {resp.status}, {text[:200]}")
                return None
            data = await resp.json(content_type=None)
            return data
    except asyncio.TimeoutError:
        logger.warning(f"wttr.in 请求超时: {location}")
        return None
    except Exception as e:
        logger.warning(f"wttr.in 请求异常: {location}, {e}")
        return None


def _parse_current(data: dict[str, Any]) -> dict[str, Any]:
    """解析当前天气数据。"""
    cur = data["current_condition"][0]
    area = data.get("nearest_area", [{}])[0]

    weather_code = cur.get("weatherCode", "113")
    lang_zh = None
    for key in ("lang_zh-cn", "lang_zh", "lang_xx"):
        if key in cur:
            lang_zh = cur[key][0]["value"]
            break

    area_name = ""
    if area:
        area_name = area.get("areaName", [{}])[0].get("value", "")
        country = area.get("country", [{}])[0].get("value", "")
        if country:
            area_name = f"{area_name}, {country}"

    return {
        "location": area_name or "未知位置",
        "temp_C": cur.get("temp_C", "?"),
        "feels_like_C": cur.get("FeelsLikeC", "?"),
        "humidity": cur.get("humidity", "?"),
        "weather_code": weather_code,
        "weather_desc": _get_weather_desc(weather_code, lang_zh),
        "weather_emoji": _get_weather_emoji(weather_code),
        "wind_dir": _wind_dir_translate(cur.get("winddir16Point", "")),
        "wind_speed": cur.get("windspeedKmph", "?"),
        "pressure": cur.get("pressure", "?"),
        "visibility": cur.get("visibility", "?"),
        "uv_index": cur.get("uvIndex", "?"),
        "precip_mm": cur.get("precipMM", "0"),
        "cloud_cover": cur.get("cloudcover", "?"),
        "observation_time": cur.get("observation_time", ""),
    }


def _parse_forecast(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 3 天预报数据。"""
    forecasts: list[dict[str, Any]] = []
    for day in data.get("weather", []):
        # 取中午 12 点的 hourly 作为白天天气代表
        hourly_list = day.get("hourly", [])
        noon = hourly_list[4] if len(hourly_list) > 4 else (hourly_list[0] if hourly_list else {})

        weather_code = noon.get("weatherCode", "113")
        lang_zh = None
        for key in ("lang_zh-cn", "lang_zh", "lang_xx"):
            if key in noon:
                lang_zh = noon[key][0]["value"]
                break

        # 取日出日落
        astro = day.get("astronomy", [{}])[0] if day.get("astronomy") else {}

        forecasts.append({
            "date": day.get("date", ""),
            "max_temp": day.get("maxtempC", "?"),
            "min_temp": day.get("mintempC", "?"),
            "avg_temp": day.get("avgtempC", "?"),
            "weather_desc": _get_weather_desc(weather_code, lang_zh),
            "weather_emoji": _get_weather_emoji(weather_code),
            "sunrise": astro.get("sunrise", ""),
            "sunset": astro.get("sunset", ""),
            "max_uv": day.get("uvIndex", "?"),
            "rain_chance": noon.get("chanceofrain", "0"),
        })
    return forecasts


def _format_current_text(c: dict[str, Any]) -> str:
    """格式化当前天气为可读文本。"""
    lines = [
        f"{c['weather_emoji']} {c['location']} 当前天气",
        f"━━━━━━━━━━━━━━━━━━",
        f"🌡️ 温度: {c['temp_C']}°C（体感 {c['feels_like_C']}°C）",
        f"🌤️ 天气: {c['weather_desc']}",
        f"💧 湿度: {c['humidity']}%",
        f"🌬️ 风向风速: {c['wind_dir']}风 {c['wind_speed']}km/h",
        f"📊 气压: {c['pressure']}hPa",
        f"👁️ 能见度: {c['visibility']}km",
        f"☀️ 紫外线指数: {c['uv_index']}",
        f"🌧️ 降水量: {c['precip_mm']}mm",
        f"☁️ 云量: {c['cloud_cover']}%",
    ]
    if c["observation_time"]:
        lines.append(f"🕐 观测时间: {c['observation_time']}")
    return "\n".join(lines)


def _format_forecast_text(forecasts: list[dict[str, Any]]) -> str:
    """格式化预报数据为可读文本。"""
    if not forecasts:
        return "暂无预报数据"

    lines = ["\n📅 未来 3 天预报", "━━━━━━━━━━━━━━━━━━"]
    for f in forecasts:
        lines.append(
            f"{f['weather_emoji']} {f['date']} | {f['weather_desc']} | "
            f"{f['min_temp']}~{f['max_temp']}°C | "
            f"降雨概率 {f['rain_chance']}% | UV {f['max_uv']}"
        )
    return "\n".join(lines)


def _parse_hourly(data: dict[str, Any], day_index: int = 0) -> list[dict[str, Any]]:
    """解析逐小时预报数据。

    Args:
        data: wttr.in JSON 数据
        day_index: 天数索引（0=今天, 1=明天, 2=后天）
    """
    weather_days = data.get("weather", [])
    if day_index >= len(weather_days):
        return []

    day = weather_days[day_index]
    hourly_list: list[dict[str, Any]] = []

    for hour in day.get("hourly", []):
        weather_code = hour.get("weatherCode", "113")
        lang_zh = None
        for key in ("lang_zh-cn", "lang_zh", "lang_xx"):
            if key in hour:
                lang_zh = hour[key][0]["value"]
                break

        # time 字段是 "0", "300", "600", ... 表示 0:00, 3:00, 6:00 ...
        time_str = hour.get("time", "0")
        try:
            hour_val = int(time_str) // 100
            time_formatted = f"{hour_val:02d}:00"
        except (ValueError, TypeError):
            time_formatted = time_str

        hourly_list.append({
            "time": time_formatted,
            "temp_C": hour.get("tempC", "?"),
            "feels_like_C": hour.get("FeelsLikeC", "?"),
            "weather_desc": _get_weather_desc(weather_code, lang_zh),
            "weather_emoji": _get_weather_emoji(weather_code),
            "humidity": hour.get("humidity", "?"),
            "chance_of_rain": hour.get("chanceofrain", "0"),
            "wind_speed": hour.get("windspeedKmph", "?"),
            "wind_dir": _wind_dir_translate(hour.get("winddir16Point", "")),
            "precip_mm": hour.get("precipMM", "0"),
        })

    return hourly_list


def _parse_astronomy(data: dict[str, Any]) -> list[dict[str, Any]]:
    """解析天文数据（日出日落、月相）。"""
    astronomy_list: list[dict[str, Any]] = []
    for day in data.get("weather", []):
        astro = day.get("astronomy", [{}])[0] if day.get("astronomy") else {}
        astronomy_list.append({
            "date": day.get("date", ""),
            "sunrise": astro.get("sunrise", ""),
            "sunset": astro.get("sunset", ""),
            "moonrise": astro.get("moonrise", ""),
            "moonset": astro.get("moonset", ""),
            "moon_phase": astro.get("moon_phase", ""),
            "moon_illumination": astro.get("moon_illumination", ""),
        })
    return astronomy_list


def _format_hourly_text(location: str, hourly: list[dict[str, Any]], day_label: str = "今天") -> str:
    """格式化逐小时预报为可读文本。"""
    if not hourly:
        return "暂无逐小时预报数据"

    lines = [f"\n⏰ {location} {day_label}逐小时预报", "━━━━━━━━━━━━━━━━━━"]
    for h in hourly:
        lines.append(
            f"{h['weather_emoji']} {h['time']} | {h['weather_desc']} | "
            f"{h['temp_C']}°C(体感{h['feels_like_C']}°C) | "
            f"💧{h['humidity']}% | 🌧️{h['chance_of_rain']}% | "
            f"🌬️{h['wind_dir']}{h['wind_speed']}km/h"
        )
    return "\n".join(lines)


def _format_astronomy_text(location: str, astronomy: list[dict[str, Any]]) -> str:
    """格式化日出日落数据为可读文本。"""
    if not astronomy:
        return "暂无天文数据"

    moon_emoji_map = {
        "New Moon": "🌑", "Waxing Crescent": "🌒", "First Quarter": "🌓",
        "Waxing Gibbous": "🌔", "Full Moon": "🌕", "Waning Gibbous": "🌖",
        "Last Quarter": "🌗", "Waning Crescent": "🌘",
    }

    lines = [f"\n🌅 {location} 日出日落信息", "━━━━━━━━━━━━━━━━━━"]
    for a in astronomy:
        moon_emoji = moon_emoji_map.get(a["moon_phase"], "🌙")
        lines.append(
            f"📅 {a['date']}\n"
            f"  🌅 日出: {a['sunrise']} | 🌇 日落: {a['sunset']}\n"
            f"  {moon_emoji} 月相: {a['moon_phase']}（亮度 {a['moon_illumination']}%）\n"
            f"  🌙 月升: {a['moonrise']} | 🌚 月落: {a['moonset']}"
        )
    return "\n".join(lines)


def _format_compare_text(results: list[tuple[str, dict[str, Any] | None]]) -> str:
    """格式化多城市天气对比为可读文本。

    Args:
        results: [(城市名, current_dict or None), ...]
    """
    lines = ["📊 多城市天气对比", "━━━━━━━━━━━━━━━━━━"]
    for city, current in results:
        if current is None:
            lines.append(f"❌ {city}: 查询失败")
        else:
            lines.append(
                f"{current['weather_emoji']} {city}: {current['weather_desc']} | "
                f"{current['temp_C']}°C(体感{current['feels_like_C']}°C) | "
                f"💧{current['humidity']}% | "
                f"🌬️{current['wind_dir']}{current['wind_speed']}km/h"
            )
    return "\n".join(lines)


async def _query_weather(location: str, include_forecast: bool = True) -> str:
    """
    查询天气并返回格式化文本。

    Args:
        location: 城市名
        include_forecast: 是否包含 3 天预报

    Returns:
        格式化的天气信息文本
    """
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        data = await _fetch_weather(session, location)
        if data is None:
            return f"❌ 无法获取「{location}」的天气信息，请检查城市名是否正确或稍后重试。"

        if "current_condition" not in data or not data["current_condition"]:
            return f"❌ 未找到「{location}」的天气数据，请尝试使用英文城市名。"

        current = _parse_current(data)
        result = _format_current_text(current)

        if include_forecast:
            forecasts = _parse_forecast(data)
            result += "\n" + _format_forecast_text(forecasts)

        return result


async def _query_hourly(location: str, day_index: int = 0) -> str:
    """查询逐小时预报并返回格式化文本。"""
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        data = await _fetch_weather(session, location)
        if data is None:
            return f"❌ 无法获取「{location}」的天气信息，请检查城市名是否正确或稍后重试。"

        if "weather" not in data or not data["weather"]:
            return f"❌ 未找到「{location}」的预报数据。"

        area = data.get("nearest_area", [{}])[0]
        area_name = ""
        if area:
            area_name = area.get("areaName", [{}])[0].get("value", location)

        hourly = _parse_hourly(data, day_index=day_index)
        day_labels = ["今天", "明天", "后天"]
        day_label = day_labels[day_index] if day_index < len(day_labels) else f"第{day_index+1}天"

        return _format_hourly_text(area_name or location, hourly, day_label)


async def _query_astronomy(location: str) -> str:
    """查询日出日落信息并返回格式化文本。"""
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        data = await _fetch_weather(session, location)
        if data is None:
            return f"❌ 无法获取「{location}」的信息，请检查城市名是否正确或稍后重试。"

        if "weather" not in data or not data["weather"]:
            return f"❌ 未找到「{location}」的天文数据。"

        area = data.get("nearest_area", [{}])[0]
        area_name = ""
        if area:
            area_name = area.get("areaName", [{}])[0].get("value", location)

        astronomy = _parse_astronomy(data)
        return _format_astronomy_text(area_name or location, astronomy)


async def _query_compare(locations: list[str]) -> str:
    """查询多个城市的天气并对比。"""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}

    results: list[tuple[str, dict[str, Any] | None]] = []

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        # 并发查询所有城市
        tasks = [_fetch_weather(session, loc) for loc in locations]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for loc, resp in zip(locations, responses):
            if isinstance(resp, Exception) or resp is None:
                results.append((loc, None))
                continue
            if "current_condition" not in resp or not resp["current_condition"]:
                results.append((loc, None))
                continue
            current = _parse_current(resp)
            results.append((loc, current))

    return _format_compare_text(results)


# ═══════════════════════════════════════════════════════════════
#  灾害天气检测系统
# ═══════════════════════════════════════════════════════════════

# 灾害等级
ALERT_LEVEL_INFO = "info"        # 提醒
ALERT_LEVEL_WARN = "warn"        # 警告
ALERT_LEVEL_DANGER = "danger"    # 危险

ALERT_LEVEL_EMOJI = {
    ALERT_LEVEL_INFO: "🟡",
    ALERT_LEVEL_WARN: "🟠",
    ALERT_LEVEL_DANGER: "🔴",
}

ALERT_LEVEL_LABEL = {
    ALERT_LEVEL_INFO: "关注",
    ALERT_LEVEL_WARN: "警告",
    ALERT_LEVEL_DANGER: "紧急",
}

# 极端天气代码集合
_HEAVY_RAIN_CODES = {"305", "308", "311", "314", "356", "359"}      # 大雨/暴雨
_THUNDERSTORM_CODES = {"200", "386", "389", "392"}                   # 雷暴
_HEAVY_SNOW_CODES = {"230", "329", "332", "335", "338", "371", "395"}  # 大雪/暴雪
_FREEZING_CODES = {"281", "284", "350", "374", "377"}                # 冻雨


def _detect_alerts(current: dict[str, Any], forecasts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """检测灾害天气，返回预警列表。

    Args:
        current: _parse_current() 返回的当前天气字典
        forecasts: _parse_forecast() 返回的预报列表（可选）

    Returns:
        预警字典列表，每项含 level, type, title, detail
    """
    alerts: list[dict[str, Any]] = []

    weather_code = current.get("weather_code", "113")
    temp_c = _safe_int(current.get("temp_C"))
    feels_c = _safe_int(current.get("feels_like_C"))
    wind_speed = _safe_int(current.get("wind_speed"))
    precip = _safe_float(current.get("precip_mm"))
    visibility = _safe_int(current.get("visibility"))
    uv = _safe_int(current.get("uv_index"))

    # 1. 暴雨
    if weather_code in _HEAVY_RAIN_CODES:
        level = ALERT_LEVEL_DANGER if weather_code in {"311", "314", "359"} else ALERT_LEVEL_WARN
        alerts.append({
            "level": level,
            "type": "heavy_rain",
            "title": f"{'暴雨' if level == ALERT_LEVEL_DANGER else '大雨'}预警",
            "detail": f"当前天气: {current['weather_desc']}，降水量 {precip}mm，"
                      f"能见度 {current.get('visibility', '?')}km，注意防范洪涝。",
        })

    # 2. 雷暴
    if weather_code in _THUNDERSTORM_CODES:
        level = ALERT_LEVEL_DANGER if weather_code == "389" else ALERT_LEVEL_WARN
        alerts.append({
            "level": level,
            "type": "thunderstorm",
            "title": f"{'强雷暴' if level == ALERT_LEVEL_DANGER else '雷阵雨'}预警",
            "detail": f"当前有{'强雷暴' if level == ALERT_LEVEL_DANGER else '雷阵雨'}，"
                      f"请避免户外活动，远离高大物体和水域。",
        })

    # 3. 暴雪
    if weather_code in _HEAVY_SNOW_CODES:
        level = ALERT_LEVEL_DANGER if weather_code in {"335", "338", "395"} else ALERT_LEVEL_WARN
        alerts.append({
            "level": level,
            "type": "heavy_snow",
            "title": f"{'暴雪' if level == ALERT_LEVEL_DANGER else '大雪'}预警",
            "detail": f"当前天气: {current['weather_desc']}，注意保暖和交通安全，"
                      f"避免不必要的外出。",
        })

    # 4. 冻雨
    if weather_code in _FREEZING_CODES:
        alerts.append({
            "level": ALERT_LEVEL_DANGER,
            "type": "freezing_rain",
            "title": "冻雨预警",
            "detail": f"当前有冻雨，路面易结冰，气温 {temp_c}°C，"
                      f"出行极其危险，请尽量避免外出。",
        })

    # 5. 极端高温
    if temp_c is not None and temp_c >= 38:
        level = ALERT_LEVEL_DANGER if temp_c >= 40 else ALERT_LEVEL_WARN
        alerts.append({
            "level": level,
            "type": "extreme_heat",
            "title": f"{'极端高温' if level == ALERT_LEVEL_DANGER else '高温'}预警",
            "detail": f"当前气温 {temp_c}°C（体感 {feels_c}°C），"
                      f"请注意防暑降温，避免长时间户外活动，多补充水分。",
        })

    # 6. 极端低温
    if temp_c is not None and temp_c <= -10:
        level = ALERT_LEVEL_DANGER if temp_c <= -20 else ALERT_LEVEL_WARN
        alerts.append({
            "level": level,
            "type": "extreme_cold",
            "title": f"{'极端低温' if level == ALERT_LEVEL_DANGER else '严寒'}预警",
            "detail": f"当前气温 {temp_c}°C（体感 {feels_c}°C），"
                      f"请注意防寒保暖，防止冻伤，留意水管冻裂。",
        })

    # 7. 大风
    if wind_speed is not None and wind_speed >= 62:  # ≈ 8级风 62km/h
        level = ALERT_LEVEL_DANGER if wind_speed >= 88 else ALERT_LEVEL_WARN  # 10级 88km/h
        alerts.append({
            "level": level,
            "type": "strong_wind",
            "title": f"{'狂风' if level == ALERT_LEVEL_DANGER else '大风'}预警",
            "detail": f"当前风速 {wind_speed}km/h（{current['wind_dir']}风），"
                      f"请关好门窗，远离广告牌和临时建筑，注意高空坠物。",
        })

    # 8. 低能见度（雾）
    if visibility is not None and visibility <= 1:
        level = ALERT_LEVEL_DANGER if visibility <= 0.5 else ALERT_LEVEL_WARN
        alerts.append({
            "level": level,
            "type": "low_visibility",
            "title": f"{'强浓雾' if level == ALERT_LEVEL_DANGER else '大雾'}预警",
            "detail": f"当前能见度仅 {visibility}km，"
                      f"驾驶请减速慢行，注意交通安全。",
        })

    # 9. 极端紫外线
    if uv is not None and uv >= 10:
        alerts.append({
            "level": ALERT_LEVEL_WARN,
            "type": "extreme_uv",
            "title": "强紫外线预警",
            "detail": f"紫外线指数 {uv}（极强），"
                      f"请做好防晒措施，避免正午时段外出。",
        })

    # 10. 检查预报中的高降雨概率
    if forecasts:
        for f in forecasts[:1]:  # 只看今天
            rain_chance = _safe_int(f.get("rain_chance"))
            if rain_chance is not None and rain_chance >= 80:
                max_t = _safe_int(f.get("max_temp"))
                # 如果温度低于 0 则是雪，否则雨
                precip_type = "降雪" if max_t is not None and max_t <= 0 else "降雨"
                alerts.append({
                    "level": ALERT_LEVEL_INFO,
                    "type": "high_rain_chance",
                    "title": f"高{precip_type}概率提醒",
                    "detail": f"今日{precip_type}概率高达 {rain_chance}%，"
                              f"出门请携带雨具。",
                })
                break

    return alerts


def _safe_int(val: Any) -> int | None:
    """安全转换为 int。"""
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> float | None:
    """安全转换为 float。"""
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _format_alert_text(location: str, current: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    """格式化灾害预警为推送文本。"""
    if not alerts:
        return ""

    # 按等级排序：danger > warn > info
    level_order = {ALERT_LEVEL_DANGER: 0, ALERT_LEVEL_WARN: 1, ALERT_LEVEL_INFO: 2}
    sorted_alerts = sorted(alerts, key=lambda a: level_order.get(a["level"], 99))

    # 最高等级决定整体标题
    top_level = sorted_alerts[0]["level"]

    lines = [
        f"{ALERT_LEVEL_EMOJI[top_level]} 【{ALERT_LEVEL_LABEL[top_level]}】{location} 天气预警",
        f"━━━━━━━━━━━━━━━━━━",
    ]

    for alert in sorted_alerts:
        emoji = ALERT_LEVEL_EMOJI[alert["level"]]
        lines.append(f"{emoji} {alert['title']}")
        lines.append(f"   {alert['detail']}")
        lines.append("")

    lines.append(f"🌡️ 当前: {current['weather_desc']} | {current['temp_C']}°C | 💧{current['humidity']}% | 🌬️{current['wind_dir']}{current['wind_speed']}km/h")
    lines.append(f"⏰ {time.strftime('%Y-%m-%d %H:%M', time.localtime())}")

    return "\n".join(lines)


async def _check_weather_alerts(location: str) -> tuple[str | None, dict[str, Any] | None]:
    """
    检查指定城市的灾害天气预警。

    Returns:
        (alert_text, current_dict): 如果有预警返回 (格式化文本, 当前天气字典)，
        如果无预警或查询失败返回 (None, None)
    """
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        data = await _fetch_weather(session, location)
        if data is None or "current_condition" not in data or not data["current_condition"]:
            return None, None

        current = _parse_current(data)
        forecasts = _parse_forecast(data)
        alerts = _detect_alerts(current, forecasts)

        if not alerts:
            return None, current

        # 获取位置名
        area_name = current.get("location", location)
        alert_text = _format_alert_text(area_name, current, alerts)
        return alert_text, current


@register(
    "astrbot_plugin_weather",
    "zhhgf-cn",
    "天气查询插件 - 实时天气/逐小时预报/多城市对比/日出日落/灾害预警推送",
    "2.1.1",
    "https://github.com/zhhgf-cn/astrbot_plugin_weather",
)
class WeatherPlugin(Star):
    """天气查询插件。

    功能：
    - /天气 <城市> 直接查询天气
    - /天气 <城市> -f 查询含 3 天预报的详细天气
    - /天气订阅 <城市> 订阅灾害天气预警推送
    - /天气退订 <城市> 取消订阅
    - /预警列表 查看当前订阅和预警状态
    - /检查预警 立即检查所有订阅城市的预警
    - 8 个 LLM 工具被 AI 自主调用
    - 定时检查订阅城市，发现灾害天气自动全群推送
    """

    # cron 任务名称
    _CRON_JOB_NAME = "weather_alert_check"

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        """插件初始化。"""
        # 读取配置
        cron_expr = "0 */2 * * *"  # 默认每 2 小时检查一次
        if self.config:
            cron_expr = self.config.get("alert_check_interval", cron_expr)

        logger.info(f"天气查询插件已加载 ✅ 预警检查周期: {cron_expr}")

        # 注册定时检查任务
        try:
            cron_mgr = self.context.cron_manager
            # 先删除旧任务（如果存在）
            try:
                jobs = cron_mgr.list_jobs()
                for job in (jobs or []):
                    if getattr(job, "name", "") == self._CRON_JOB_NAME:
                        cron_mgr.delete_job(getattr(job, "id", ""))
                        break
            except Exception:
                pass

            await cron_mgr.add_basic_job(
                name=self._CRON_JOB_NAME,
                cron_expression=cron_expr,
                handler=self._cron_check_alerts,
                persistent=True,
                description="定时检查订阅城市的灾害天气预警",
                enabled=True,
            )
            logger.info("天气预警定时检查任务已注册 ✅")
        except Exception as e:
            logger.warning(f"注册天气预警定时任务失败: {e}")

    @filter.command("天气", alias=["查天气", "天气查询"])
    async def weather_command(self, event: AstrMessageEvent, location: str = ""):
        """查询指定城市的天气。用法: /天气 <城市名> 或 /天气 <城市名> -f 查看含预报的详细信息"""
        location = location.strip()
        if not location:
            yield event.plain_result(
                "🌤️ 天气查询\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "用法:\n"
                "  /天气 <城市名>  — 查询当前天气\n"
                "  /天气 <城市名> -f  — 查询当前天气+3天预报\n"
                "  /查天气 <城市名>  — 同上（别名）\n\n"
                "示例:\n"
                "  /天气 北京\n"
                "  /天气 上海 -f\n"
                "  /查天气 广州\n\n"
                "💡 AI 工具（自然语言触发）:\n"
                "  • 查天气 — 当前天气/预报\n"
                "  • 多城市对比 — 多城市天气对比\n"
                "  • 逐小时预报 — 每时段天气\n"
                "  • 日出日落 — 日出日落月相\n"
                "  • 订阅预警 — 订阅灾害天气推送\n"
                "  • 退订预警 — 取消灾害天气订阅\n"
                "  • 预警列表 — 查看订阅城市\n"
                "  • 检查预警 — 立即检查灾害预警\n\n"
                "🚨 灾害预警推送:\n"
                "  • /天气订阅 <城市> — 订阅预警\n"
                "  • /天气退订 <城市> — 取消订阅\n"
                "  • /预警列表 — 查看订阅列表\n"
                "  • /检查预警 — 立即检查预警\n\n"
                "直接问 AI 即可，例如:\n"
                "  \"北京和上海哪个热？\"\n"
                "  \"今天下午几点下雨？\"\n"
                "  \"东京今天日出几点？\""
            )
            return

        # 检查是否包含 -f 参数
        parts = location.rsplit(" -f", 1)
        include_forecast = location.endswith(" -f")
        if include_forecast:
            location = location[:-3].strip()
            if not location:
                yield event.plain_result("请提供城市名，例如: /天气 北京 -f")
                return

        # 对中文城市名做简单处理：空格用 + 连接
        location = location.replace(" ", "+")

        yield event.plain_result(f"🔍 正在查询「{location.replace('+', ' ')}」的天气...")

        try:
            result = await _query_weather(location, include_forecast=include_forecast)
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"天气查询异常: {e}")
            yield event.plain_result(f"❌ 查询天气时出错: {e}，请稍后重试。")

    @filter.llm_tool(name="get_weather")
    async def get_weather_tool(
        self,
        event: AstrMessageEvent,
        location: str,
        include_forecast: bool = False,
    ):
        """获取指定城市的天气信息。当用户询问天气相关问题时调用此工具。

        Args:
            location(string): 城市名称，支持中文、英文、机场代码。例如: "北京"、"Shanghai"、"Paris"
            include_forecast(boolean): 是否包含未来3天天气预报。默认为 False（仅当前天气）。用户明确要求"预报"或"未来几天"时设为 True。
        """
        location = location.strip().replace(" ", "+")
        result = await _query_weather(location, include_forecast=include_forecast)
        return result

    @filter.llm_tool(name="compare_weather")
    async def compare_weather_tool(
        self,
        event: AstrMessageEvent,
        locations: list[str],
    ):
        """对比多个城市的当前天气。当用户想比较不同城市天气时调用此工具，例如"北京和上海哪个热"、"对比东京伦敦纽约的天气"。

        Args:
            locations(array[string]): 要对比的城市名称列表（2-5个城市）。例如: ["北京", "上海", "广州"]
        """
        if not locations or len(locations) < 2:
            return "请提供至少 2 个城市进行对比。"
        if len(locations) > 5:
            locations = locations[:5]

        # 清理城市名
        cleaned = [loc.strip().replace(" ", "+") for loc in locations if loc.strip()]
        if len(cleaned) < 2:
            return "请提供至少 2 个有效城市名。"

        result = await _query_compare(cleaned)
        return result

    @filter.llm_tool(name="get_hourly_forecast")
    async def get_hourly_forecast_tool(
        self,
        event: AstrMessageEvent,
        location: str,
        day: str = "today",
    ):
        """获取指定城市的逐小时天气预报。当用户需要知道一天中某个时段的天气时调用，例如"今天下午会下雨吗"、"明天几点出太阳"。

        Args:
            location(string): 城市名称，支持中文、英文、机场代码。例如: "北京"、"Shanghai"
            day(string): 要查询哪一天。可选值: "today"（今天）、"tomorrow"（明天）、"day_after_tomorrow"（后天）。默认 "today"。
        """
        day_map = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
        day_index = day_map.get(day, 0)

        location = location.strip().replace(" ", "+")
        result = await _query_hourly(location, day_index=day_index)
        return result

    @filter.llm_tool(name="get_sunrise_sunset")
    async def get_sunrise_sunset_tool(
        self,
        event: AstrMessageEvent,
        location: str,
    ):
        """获取指定城市的日出日落和月相信息。当用户询问日出日落时间、月相、天文信息时调用。

        Args:
            location(string): 城市名称，支持中文、英文、机场代码。例如: "北京"、"Tokyo"
        """
        location = location.strip().replace(" ", "+")
        result = await _query_astronomy(location)
        return result

    # ═══════════════════════════════════════════════════════════════
    #  灾害预警 LLM 工具（与下方指令一一对应）
    # ═══════════════════════════════════════════════════════════════

    @filter.llm_tool(name="subscribe_weather_alert")
    async def subscribe_weather_alert_tool(
        self,
        event: AstrMessageEvent,
        location: str,
    ):
        """订阅指定城市的灾害天气预警。当用户要求订阅天气预警、开启天气提醒时调用。订阅后该城市出现灾害天气会自动推送到当前会话。

        Args:
            location(string): 要订阅的城市名称。例如: "北京"、"上海"、"Tokyo"
        """
        location = location.strip()
        if not location:
            return "请提供要订阅的城市名称。"

        umo = event.unified_msg_origin
        city_key = location.replace(" ", "+").lower()

        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})
        if umo not in subs:
            subs[umo] = []

        if city_key in subs[umo]:
            return f"当前会话已订阅「{location}」的天气预警，无需重复订阅。"

        subs[umo].append(city_key)
        await self.put_kv_data("weather_alert_subs", subs)

        pushed: dict[str, list] = await self.get_kv_data("weather_pushed_alerts", {})
        if umo not in pushed:
            pushed[umo] = {}

        return (
            f"✅ 已订阅「{location}」的灾害天气预警。\n"
            f"每 2 小时自动检查，发现暴雨/雷暴/暴雪/极端高温/大风/大雾等灾害天气时自动推送。"
        )

    @filter.llm_tool(name="unsubscribe_weather_alert")
    async def unsubscribe_weather_alert_tool(
        self,
        event: AstrMessageEvent,
        location: str,
    ):
        """取消订阅灾害天气预警。当用户要求取消天气预警订阅、关闭天气提醒时调用。

        Args:
            location(string): 要取消订阅的城市名称。传 "全部" 或 "all" 可取消所有订阅。
        """
        location = location.strip()
        umo = event.unified_msg_origin

        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs or not subs[umo]:
            return "当前会话没有订阅任何城市的天气预警。"

        if location in ("全部", "所有", "all", ""):
            count = len(subs[umo])
            subs[umo] = []
            await self.put_kv_data("weather_alert_subs", subs)
            return f"✅ 已取消当前会话的全部 {count} 个天气预警订阅。"

        city_key = location.replace(" ", "+").lower()
        if city_key not in subs[umo]:
            return f"当前会话未订阅「{location}」的天气预警。"

        subs[umo].remove(city_key)
        await self.put_kv_data("weather_alert_subs", subs)

        pushed: dict[str, list] = await self.get_kv_data("weather_pushed_alerts", {})
        if umo in pushed and city_key in pushed[umo]:
            del pushed[umo][city_key]
            await self.put_kv_data("weather_pushed_alerts", pushed)

        return f"✅ 已取消订阅「{location}」的天气预警。"

    @filter.llm_tool(name="list_weather_alerts")
    async def list_weather_alerts_tool(
        self,
        event: AstrMessageEvent,
    ):
        """查看当前会话已订阅的天气预警城市列表。当用户询问订阅了哪些城市天气预警时调用。"""
        umo = event.unified_msg_origin
        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs or not subs[umo]:
            return "当前会话未订阅任何城市的天气预警。可使用订阅功能添加城市。"

        lines = [f"当前会话共订阅 {len(subs[umo])} 个城市的天气预警:"]
        for i, city in enumerate(subs[umo], 1):
            lines.append(f"  {i}. {city.replace('+', ' ')}")
        return "\n".join(lines)

    @filter.llm_tool(name="check_weather_alerts")
    async def check_weather_alerts_tool(
        self,
        event: AstrMessageEvent,
    ):
        """立即检查当前会话所有订阅城市的灾害天气预警。当用户要求检查天气预警、看看有没有灾害天气时调用。无需参数，自动检查当前会话订阅的所有城市。"""
        umo = event.unified_msg_origin
        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs or not subs[umo]:
            return "当前会话未订阅任何城市预警。请先订阅城市后再检查。"

        cities = subs[umo]
        results: list[str] = []
        has_alert = False

        for city in cities:
            alert_text, current = await _check_weather_alerts(city)
            if alert_text:
                has_alert = True
                results.append(alert_text)
            else:
                if current:
                    results.append(
                        f"✅ {current.get('location', city)}: {current['weather_desc']} | "
                        f"{current['temp_C']}°C — 暂无灾害预警"
                    )
                else:
                    results.append(f"❌ {city}: 查询失败")

        if has_alert:
            return "🚨 发现天气预警！\n\n" + "\n\n".join(results)
        else:
            return "✅ 所有订阅城市暂无灾害预警\n\n" + "\n\n".join(results)

    # ═══════════════════════════════════════════════════════════════
    #  灾害预警订阅 & 推送（指令）
    # ═══════════════════════════════════════════════════════════════

    @filter.command("天气订阅", alias=["订阅天气", "天气预警订阅"])
    async def weather_sub_command(self, event: AstrMessageEvent, location: str = ""):
        """订阅指定城市的灾害天气预警。用法: /天气订阅 <城市名>"""
        location = location.strip()
        if not location:
            yield event.plain_result(
                "🔔 天气预警订阅\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "用法: /天气订阅 <城市名>\n"
                "示例: /天气订阅 北京\n\n"
                "订阅后，当检测到灾害天气（暴雨、雷暴、暴雪、极端高温/低温、大风、大雾等）"
                "时会自动推送到当前群/会话。\n\n"
                "相关指令:\n"
                "  /天气退订 <城市> — 取消订阅\n"
                "  /预警列表 — 查看订阅列表\n"
                "  /检查预警 — 立即检查预警"
            )
            return

        umo = event.unified_msg_origin
        city_key = location.replace(" ", "+").lower()

        # 获取当前订阅列表
        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs:
            subs[umo] = []

        if city_key in subs[umo]:
            yield event.plain_result(f"⚠️ 当前会话已订阅「{location}」的天气预警，无需重复订阅。")
            return

        subs[umo].append(city_key)
        await self.put_kv_data("weather_alert_subs", subs)

        # 初始化已推送记录（避免订阅后立刻重复推送）
        pushed: dict[str, list] = await self.get_kv_data("weather_pushed_alerts", {})
        if umo not in pushed:
            pushed[umo] = {}

        yield event.plain_result(
            f"✅ 已订阅「{location}」的灾害天气预警\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📍 订阅城市: {location}\n"
            f"🏠 推送目标: 当前会话\n"
            f"⏰ 检查频率: 每 2 小时自动检查\n"
            f"🚨 预警类型: 暴雨/雷暴/暴雪/冻雨/极端高温/严寒/大风/大雾/强紫外线\n\n"
            f"💡 输入 /检查预警 可立即检查一次\n"
            f"💡 输入 /天气退订 {location} 可取消订阅"
        )

    @filter.command("天气退订", alias=["取消天气", "天气预警退订"])
    async def weather_unsub_command(self, event: AstrMessageEvent, location: str = ""):
        """取消订阅灾害天气预警。用法: /天气退订 <城市名> 或 /天气退订 全部 取消全部"""
        location = location.strip()
        umo = event.unified_msg_origin

        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs or not subs[umo]:
            yield event.plain_result("⚠️ 当前会话没有订阅任何城市的天气预警。")
            return

        if location in ("全部", "所有", "all"):
            count = len(subs[umo])
            subs[umo] = []
            await self.put_kv_data("weather_alert_subs", subs)
            yield event.plain_result(f"✅ 已取消当前会话的 {count} 个天气预警订阅。")
            return

        city_key = location.replace(" ", "+").lower()
        if city_key not in subs[umo]:
            yield event.plain_result(f"⚠️ 当前会话未订阅「{location}」的天气预警。")
            return

        subs[umo].remove(city_key)
        await self.put_kv_data("weather_alert_subs", subs)

        # 清理已推送记录
        pushed: dict[str, list] = await self.get_kv_data("weather_pushed_alerts", {})
        if umo in pushed and city_key in pushed[umo]:
            del pushed[umo][city_key]
            await self.put_kv_data("weather_pushed_alerts", pushed)

        yield event.plain_result(f"✅ 已取消订阅「{location}」的天气预警。")

    @filter.command("预警列表", alias=["天气预警", "订阅列表"])
    async def weather_alerts_command(self, event: AstrMessageEvent):
        """查看当前会话的天气预警订阅列表。"""
        umo = event.unified_msg_origin
        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs or not subs[umo]:
            yield event.plain_result(
                "📋 天气预警订阅列表\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "当前会话未订阅任何城市的天气预警。\n"
                "使用 /天气订阅 <城市名> 订阅。"
            )
            return

        lines = [
            "📋 天气预警订阅列表",
            "━━━━━━━━━━━━━━━━━━",
            f"当前会话共订阅 {len(subs[umo])} 个城市:",
        ]
        for i, city in enumerate(subs[umo], 1):
            lines.append(f"  {i}. {city.replace('+', ' ')}")
        lines.append("")
        lines.append("💡 /检查预警 — 立即检查预警")
        lines.append("💡 /天气退订 <城市> — 取消订阅")
        lines.append("💡 /天气退订 全部 — 取消全部订阅")

        yield event.plain_result("\n".join(lines))

    @filter.command("检查预警", alias=["天气检查", "预警检查"])
    async def weather_check_command(self, event: AstrMessageEvent):
        """立即检查当前会话订阅的所有城市的灾害天气预警。"""
        umo = event.unified_msg_origin
        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})

        if umo not in subs or not subs[umo]:
            yield event.plain_result("⚠️ 当前会话未订阅任何城市预警。使用 /天气订阅 <城市名> 订阅。")
            return

        cities = subs[umo]
        yield event.plain_result(f"🔍 正在检查 {len(cities)} 个城市的天气预警...")

        has_alert = False
        results: list[str] = []

        for city in cities:
            alert_text, current = await _check_weather_alerts(city)
            if alert_text:
                has_alert = True
                results.append(alert_text)
            else:
                if current:
                    results.append(f"✅ {current.get('location', city)}: {current['weather_desc']} | {current['temp_C']}°C — 暂无灾害预警")
                else:
                    results.append(f"❌ {city}: 查询失败")

        if has_alert:
            yield event.plain_result("🚨 发现天气预警！\n\n" + "\n\n".join(results))
        else:
            yield event.plain_result("✅ 所有订阅城市暂无灾害预警\n\n" + "\n\n".join(results))

    async def _cron_check_alerts(self, **kwargs):
        """定时检查所有订阅城市的灾害天气预警并推送。"""
        subs: dict[str, list] = await self.get_kv_data("weather_alert_subs", {})
        pushed: dict[str, list] = await self.get_kv_data("weather_pushed_alerts", {})

        if not subs:
            return

        # 收集所有需要检查的城市（去重）
        all_cities: set[str] = set()
        for city_list in subs.values():
            all_cities.update(city_list)

        if not all_cities:
            return

        logger.info(f"天气预警定时检查: 检查 {len(all_cities)} 个城市...")

        # 并发查询所有城市
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"Accept-Language": "zh-CN,zh;q=0.9"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            tasks = {city: _fetch_weather(session, city) for city in all_cities}
            results = {}
            for city, task in tasks.items():
                try:
                    results[city] = await task
                except Exception as e:
                    logger.warning(f"定时检查 {city} 失败: {e}")
                    results[city] = None

        # 当前小时，用于去重（同一城市同一小时只推送一次）
        current_hour_key = time.strftime("%Y%m%d%H", time.localtime())

        push_count = 0
        for umo, city_list in subs.items():
            if not city_list:
                continue

            for city in city_list:
                data = results.get(city)
                if data is None or "current_condition" not in data or not data["current_condition"]:
                    continue

                current = _parse_current(data)
                forecasts = _parse_forecast(data)
                alerts = _detect_alerts(current, forecasts)

                if not alerts:
                    continue

                # 去重：同一城市同一小时不重复推送
                if umo not in pushed:
                    pushed[umo] = {}
                city_pushed = pushed[umo].get(city, {})

                if city_pushed.get("hour_key") == current_hour_key:
                    continue  # 本小时已推送过

                # 生成预警文本
                area_name = current.get("location", city)
                alert_text = _format_alert_text(area_name, current, alerts)

                # 推送消息
                try:
                    chain = MessageChain().message(alert_text)
                    await self.context.send_message(umo, chain)
                    push_count += 1

                    # 记录已推送
                    pushed[umo][city] = {
                        "hour_key": current_hour_key,
                        "timestamp": time.time(),
                        "level": alerts[0]["level"],
                        "type": alerts[0]["type"],
                    }

                    logger.info(f"天气预警推送: {umo} -> {city} ({alerts[0]['title']})")

                    # 间隔 2 秒，避免推送过快
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.warning(f"推送天气预警失败 {umo} -> {city}: {e}")

        # 清理过期的推送记录（保留最近 7 天）
        week_ago = time.time() - 7 * 24 * 3600
        for umo in list(pushed.keys()):
            for city in list(pushed[umo].keys()):
                ts = pushed[umo][city].get("timestamp", 0)
                if ts < week_ago:
                    del pushed[umo][city]
            if not pushed[umo]:
                del pushed[umo]

        await self.put_kv_data("weather_pushed_alerts", pushed)

        if push_count > 0:
            logger.info(f"天气预警定时检查完成: 推送了 {push_count} 条预警")
        else:
            logger.info("天气预警定时检查完成: 无灾害预警")

    async def terminate(self):
        """插件卸载时清理资源。"""
        # 清理定时任务
        try:
            cron_mgr = self.context.cron_manager
            jobs = cron_mgr.list_jobs()
            for job in (jobs or []):
                if getattr(job, "name", "") == self._CRON_JOB_NAME:
                    cron_mgr.delete_job(getattr(job, "id", ""))
                    break
        except Exception:
            pass
        logger.info("天气查询插件已卸载 👋")
