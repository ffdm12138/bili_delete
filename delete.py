#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili Lottery Dynamic Cleaner
自动扫描 B 站动态，识别已开奖互动抽奖动态并批量删除。

Phase 1: Security hardening — CLI 修复、LotteryState 枚举、错误处理、
         retry/backoff、候选展示增强、失效转发检测。
"""

import argparse
import json
import os
import platform
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TZ_SHANGHAI = timezone(timedelta(hours=8))
DYNAMIC_URL_TEMPLATE = "https://t.bilibili.com/{dyn_id}"
CANDIDATE_EXPORT_PREFIX = "candidates_"

# Fields that must NEVER appear in exported JSON / logs
SANITIZE_FIELDS = {
    "cookie", "sessdata", "bili_jct", "dedeuserid",
    "access_token", "refresh_token", "csrf",
}

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class BiliError(Exception):
    """Base for all Bilibili-operation errors."""
    pass


class ApiError(BiliError):
    """B站 API returned a non-zero code."""
    def __init__(self, code: int, message: str, endpoint: str = ""):
        self.code = code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"[{endpoint}] code={code}: {message}")


class AuthError(BiliError):
    """Not logged in or cookie expired."""
    pass


class RateLimitError(BiliError):
    """Rate-limited (429 / 412 from B站)."""
    pass


class NetworkError(BiliError):
    """Network timeout / connection failure."""
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LotteryState(Enum):
    """抽奖状态枚举。

    Values:
        NOT_LOTTERY — 不是抽奖动态
        ACTIVE     — 抽奖进行中，不可删除
        FINISHED   — 已开奖，可以安全删除
        UNKNOWN    — 无法判断状态，**一律不删**
    """
    NOT_LOTTERY = auto()
    ACTIVE = auto()
    FINISHED = auto()
    UNKNOWN = auto()

    def can_delete(self) -> bool:
        """Only FINISHED (and maybe repost_invalid) states are safe to delete."""
        return self == LotteryState.FINISHED


@dataclass
class CandidateInfo:
    """待删除候选的完整信息。"""
    dyn_id: str
    orig_lottery_id: str
    publish_time: Optional[datetime] = None
    lottery_time: Optional[datetime] = None
    detect_reason: str = ""
    text_preview: str = ""
    dynamic_url: str = ""
    is_repost_invalid: bool = False
    raw_item: Optional[Dict[str, Any]] = None  # only populated for require_confirm path

    def sanitized_dict(self) -> Dict[str, Any]:
        """Return a dict safe for JSON export — no cookies or sensitive fields."""
        return {
            "dyn_id": self.dyn_id,
            "orig_lottery_id": self.orig_lottery_id,
            "publish_time": _fmt_time_iso(self.publish_time),
            "lottery_time": _fmt_time_iso(self.lottery_time),
            "detect_reason": self.detect_reason,
            "text_preview": self.text_preview[:200] if self.text_preview else "",
            "dynamic_url": self.dynamic_url,
            "is_repost_invalid": self.is_repost_invalid,
        }


@dataclass
class LotteryQueryResult:
    """Result of a lottery status API query, with error tracking."""
    info: Optional[dict] = None
    error_type: Optional[str] = None   # "api_code" | "json_decode" | "network" | "empty"
    code: Optional[int] = None
    message: str = ""

    @property
    def is_error(self) -> bool:
        return self.error_type is not None


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _fmt_time_iso(dt: Optional[datetime]) -> Optional[str]:
    """Format a datetime as ISO-8601 string in Asia/Shanghai timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return dt.astimezone(TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %z")


def _fmt_time_short(dt: Optional[datetime]) -> str:
    """Short datetime for terminal display."""
    if dt is None:
        return "未知"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return dt.astimezone(TZ_SHANGHAI).strftime("%m-%d %H:%M")


def _now_shanghai() -> datetime:
    """Current time in Asia/Shanghai."""
    return datetime.now(TZ_SHANGHAI)


def _utcfromtimestamp(ts: float) -> datetime:
    """Convert a Unix timestamp to a timezone-aware datetime in Shanghai."""
    return datetime.fromtimestamp(ts, tz=TZ_SHANGHAI)


def _sanitize_for_log(value: str) -> str:
    """Show only last 4 chars of sensitive values; used for UID display."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


def retry_request(
    fn: Callable[[], requests.Response],
    max_retries: int = 3,
    base_delay: float = 1.0,
    label: str = "",
) -> requests.Response:
    """Call *fn*() with exponential backoff + random jitter.

    Retryable: timeout, connection error, HTTP 429/502/503/504.
    Non-retryable (re-raised immediately): other HTTP errors.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            resp = fn()
            resp.raise_for_status()
            return resp
        except requests.Timeout as e:
            last_exc = e
        except requests.ConnectionError as e:
            last_exc = e
        except requests.HTTPError as e:
            status = (
                e.response.status_code
                if hasattr(e, "response") and e.response is not None
                else 0
            )
            if status in (429, 502, 503, 504):
                last_exc = e
            else:
                raise  # Don't retry 4xx (except 429) or other 5xx

        if attempt == max_retries - 1:
            break

        delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
        print(f"   ⚠️  {label} 失败，{delay:.1f}s 后重试 ({attempt + 1}/{max_retries})…")
        time.sleep(delay)

    # Exhausted retries
    if isinstance(last_exc, (requests.Timeout, requests.ConnectionError)):
        raise NetworkError(f"{label} 网络请求失败 (重试{max_retries}次)") from last_exc
    if isinstance(last_exc, requests.HTTPError):
        status = (
            last_exc.response.status_code
            if hasattr(last_exc, "response") and last_exc.response is not None
            else 0
        )
        raise RateLimitError(f"{label} HTTP {status} (重试{max_retries}次)") from last_exc
    raise NetworkError(f"{label} 重试耗尽") from (last_exc or None)


# ---------------------------------------------------------------------------
# Cookie parsing (standalone, pure — testable without browser)
# ---------------------------------------------------------------------------


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parse a raw Cookie header string into a dict.

    >>> parse_cookie_string("a=1; b=2")
    {'a': '1', 'b': '2'}
    """
    cookies: Dict[str, str] = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


# ---------------------------------------------------------------------------
# Lottery state parser (standalone, pure — testable without network)
# ---------------------------------------------------------------------------


def parse_lottery_state(info: Optional[dict]) -> Tuple[LotteryState, str]:
    """Parse lottery state from the API ``lottery_notice`` response.

    Args:
        info: The ``data`` field from the lottery_notice API, or None.

    Returns:
        (LotteryState, reason_detail)

    Priority:
    1. Explicit ``lottery_status`` / ``status`` field.
    2. Time-based fallback: if ``lottery_time`` + 2h < now → FINISHED
       (tagged ``time_fallback``).
    3. If ``lottery_time`` exists but not yet expired → ACTIVE.
    4. Otherwise → UNKNOWN (never delete).
    """
    if not isinstance(info, dict) or not info:
        return LotteryState.UNKNOWN, "empty_info"

    # --- Explicit status field (key-exists check, not `or`) ---
    # `or` would eat falsy values like 0 / False — use explicit key check.
    has_explicit_status = False
    if "lottery_status" in info:
        status_raw = info["lottery_status"]
        has_explicit_status = True
    elif "status" in info:
        status_raw = info["status"]
        has_explicit_status = True
    else:
        status_raw = None

    if has_explicit_status:
        s = str(status_raw).strip().lower()
        if s in ("1", "true", "finished", "closed", "drawn"):
            return LotteryState.FINISHED, "api_status"
        if s in ("0", "false", "active", "open", "ongoing"):
            return LotteryState.ACTIVE, "api_status"
        # Unrecognised explicit status — UNKNOWN, do NOT fall through to time.
        return LotteryState.UNKNOWN, f"unknown_status:{s}"

    # --- Time-based fallback (only when NO explicit status exists) ---
    assert not has_explicit_status  # safety: we only get here without explicit status

    # --- Time-based fallback ---
    lottery_ts = info.get("lottery_time")
    if lottery_ts is not None:
        try:
            lottery_dt = _utcfromtimestamp(float(lottery_ts))
        except (TypeError, ValueError, OSError):
            return LotteryState.UNKNOWN, "bad_timestamp"

        safe_boundary = lottery_dt + timedelta(hours=2)
        now = _now_shanghai()
        if safe_boundary < now:
            return LotteryState.FINISHED, "time_fallback"
        else:
            return LotteryState.ACTIVE, "time_not_expired"

    return LotteryState.UNKNOWN, "no_status_or_time"


# ---------------------------------------------------------------------------
# Lottery detection (standalone, pure — testable without network)
# ---------------------------------------------------------------------------


LOTTERY_KEYWORDS = {"lottery", "抽奖", "choujiang"}

LOTTERY_REGEX_PATTERNS = [
    re.compile(r"抽奖"),
    re.compile(r"关注\s*[\+➕]\s*转发"),
    re.compile(r"转发\s*[\+➕]\s*关注"),
    re.compile(r"转关"),
    re.compile(r"互动抽奖"),
    re.compile(r"开奖"),
]


def deep_search_lottery(obj: Any, path: str = "") -> Tuple[bool, str]:
    """Recursively search an object for lottery-related keys/values.

    Returns:
        (found: bool, path: str) — path is the JSON-path where matched.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_str = str(k).lower()
            if any(kw in k_str for kw in LOTTERY_KEYWORDS):
                return True, f"{path}.{k}"
            v_str = str(v).lower() if not isinstance(v, (dict, list)) else ""
            if any(kw in v_str for kw in LOTTERY_KEYWORDS):
                return True, f"{path}.{k}"
            if isinstance(v, (dict, list)):
                found, p = deep_search_lottery(v, f"{path}.{k}")
                if found:
                    return True, p
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found, p = deep_search_lottery(v, f"{path}[{i}]")
            if found:
                return True, p
    return False, ""


def is_lottery_dynamic(item: Dict) -> Tuple[bool, str, str]:
    """Local detection: is *item* an interactive lottery dynamic?

    Returns:
        (is_lottery, orig_id, detect_reason)
        — detect_reason is "+"-joined list of detection methods that matched.
    """
    if not isinstance(item, dict):
        return False, "", ""

    orig = item.get("orig", {})
    if not orig:
        return False, "", ""

    orig_id = str(orig.get("id_str", ""))
    if not orig_id:
        return False, "", ""

    reasons: List[str] = []

    # Layer 1: deep recursive keyword search
    found, path = deep_search_lottery(orig, "orig")
    if found:
        reasons.append(f"deep_search:{path}")

    # Layer 2: module field check
    modules = orig.get("modules", {})
    if isinstance(modules, dict):
        md = modules.get("module_dynamic", {})
        if isinstance(md, dict):
            additional = md.get("additional")
            if isinstance(additional, dict):
                add_type = str(additional.get("type", ""))
                if "lottery" in add_type.lower():
                    reasons.append("additional_type")

            desc_info = md.get("desc", {})
            if isinstance(desc_info, dict):
                text = str(desc_info.get("text", ""))
                if any(p.search(text) for p in LOTTERY_REGEX_PATTERNS):
                    reasons.append("desc_regex")

    if reasons:
        return True, orig_id, "+".join(reasons)
    return False, orig_id, ""


def extract_text_preview(item: Dict, max_len: int = 80) -> str:
    """Extract a short text preview from a dynamic item's description."""
    modules = item.get("modules", {})
    if isinstance(modules, dict):
        md = modules.get("module_dynamic", {})
        if isinstance(md, dict):
            desc = md.get("desc", {})
            if isinstance(desc, dict):
                text = str(desc.get("text", ""))
                return text[:max_len] + ("…" if len(text) > max_len else "")
    return ""


def extract_publish_time(item: Dict) -> Optional[datetime]:
    """Extract publish timestamp from a dynamic item."""
    ts = item.get("pub_ts") or item.get("publish_time") or item.get("timestamp")
    if ts is not None:
        try:
            return _utcfromtimestamp(float(ts))
        except (TypeError, ValueError, OSError):
            pass

    # modules.module_author.pub_ts as fallback
    modules = item.get("modules", {})
    if isinstance(modules, dict):
        author = modules.get("module_author", {})
        if isinstance(author, dict):
            ts2 = author.get("pub_ts")
            if ts2:
                try:
                    return _utcfromtimestamp(float(ts2))
                except (TypeError, ValueError, OSError):
                    pass
    return None


def is_repost_original_invalid(item: Dict) -> bool:
    """Check if a repost's original dynamic has become invalid/deleted.

    Checks:
    1. Explicit ``is_deleted`` / ``deleted`` flag on orig.
    2. ``orig.modules`` is None or empty dict (B站 strips modules on deletion).
    3. ``orig.major`` contains a "已失效" badge.
    4. Text contains known invalidity markers.

    Returns:
        True if this repost's original is likely deleted/invalid.
    """
    orig = item.get("orig", {})
    if not isinstance(orig, dict) or not orig:
        return False

    # 1. Explicit deletion markers
    if orig.get("is_deleted") or orig.get("deleted"):
        return True

    # 2. Missing/empty modules — strong signal of deletion
    modules = orig.get("modules")
    if modules is None:
        return True
    if isinstance(modules, dict) and not modules:
        return True

    # 3. "已失效" badge in major section
    major = orig.get("major")
    if isinstance(major, dict):
        archive = major.get("archive")
        if isinstance(archive, dict):
            badge = archive.get("badge", {})
            if isinstance(badge, dict):
                if "已失效" in str(badge.get("text", "")):
                    return True

    # 4. Text markers
    text = extract_text_preview(orig, 200)
    invalid_markers = [
        "动态不存在", "内容已失效", "原动态已删除", "该动态已被删除",
        "视频已失效", "稿件已失效", "啊叻？视频不见了", "动态已被删除",
    ]
    for marker in invalid_markers:
        if marker in text:
            return True

    return False


# ===================================================================
# BilibiliLotteryCleaner
# ===================================================================


class BilibiliLotteryCleaner:
    """Main class: scan dynamics, detect lotteries, check status, delete."""

    def __init__(self, cookie_str: str, debug: bool = False):
        self.cookie_str = cookie_str
        self.cookies = parse_cookie_string(cookie_str)
        self.csrf = self.cookies.get("bili_jct", "")
        self.uid = self.cookies.get("DedeUserID", "")
        self.debug = debug

        if not self.csrf or not self.uid:
            raise ValueError(
                "Cookie 中缺少必要的 bili_jct 或 DedeUserID，请确保已登录"
            )

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://t.bilibili.com/",
            "Origin": "https://t.bilibili.com",
            "Cookie": cookie_str,
            "Content-Type": "application/json;charset=UTF-8",
        }

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        # Lottery info cache (avoid duplicate API calls)
        self._lottery_cache: Dict[str, LotteryQueryResult] = {}

        self.stats = {
            "total": 0,
            "lottery": 0,
            "expired": 0,
            "invalid_repost": 0,
            "deleted": 0,
            "failed": 0,
            "skipped_unknown": 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _random_delay(self, min_sec: float = 1.0, max_sec: float = 3.0) -> None:
        time.sleep(random.uniform(min_sec, max_sec))

    def _log_debug(self, *args) -> None:
        if self.debug:
            print("   [调试]", *args)

    # ------------------------------------------------------------------
    # B 站 API (with retry & proper error handling)
    # ------------------------------------------------------------------

    def get_dynamics(
        self, offset: Optional[str] = None
    ) -> Tuple[List[Dict], Optional[str]]:
        """Fetch one page of user dynamics.  Raises BiliError on failure."""
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params: Dict[str, Any] = {
            "host_mid": self.uid,
            "timezone_offset": -480,
            "platform": "web",
            "features": (
                "itemOpusStyle,listPicScale,opusBigCover,opusHiddenCover,"
                "DynamicPageDynamicAutoSaveSwitch,DynamicUgcAttachCard"
            ),
            "web_location": "333.1330",
        }
        if offset:
            params["offset"] = offset

        label = f"动态列表 p{offset or 'first'}"

        def _do() -> requests.Response:
            return self.session.get(url, params=params, timeout=15)

        try:
            resp = retry_request(_do, label=label)
        except (NetworkError, RateLimitError):
            raise
        except requests.HTTPError as e:
            # Non-retryable HTTP error
            status = (
                e.response.status_code
                if hasattr(e, "response") and e.response is not None
                else 0
            )
            if status in (401, 403):
                raise AuthError("Cookie 已过期或未登录，请重新登录 bilibili.com") from e
            raise NetworkError(f"{label} HTTP {status}") from e

        # Parse JSON
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ApiError(-1, f"响应不是有效 JSON: {e}", url) from e

        code = data.get("code")
        if code != 0:
            msg = data.get("message", "未知错误")
            if code in (-101, -111):
                raise AuthError(f"未登录或 Cookie 已过期: {msg}")
            if code == -412:
                raise RateLimitError(f"被风控/限流: {msg}")
            raise ApiError(int(code), str(msg), url)

        response_data = data.get("data", {})
        items = response_data.get("items", [])
        has_more = response_data.get("has_more", False)
        next_offset = response_data.get("offset") if has_more else None

        self._log_debug(f"got {len(items)} items, has_more={has_more}, offset={next_offset}")
        return items, next_offset

    def get_lottery_info(self, orig_id: str) -> LotteryQueryResult:
        """Query lottery status, with cache.  Returns LotteryQueryResult."""
        if orig_id in self._lottery_cache:
            return self._lottery_cache[orig_id]

        url = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"
        params = {
            "business_id": orig_id,
            "business_type": "1",
            "csrf": self.csrf,
            "web_location": "333.1330",
        }
        label = f"抽奖状态 {orig_id[:12]}"

        def _do() -> requests.Response:
            return self.session.get(url, params=params, timeout=10)

        try:
            resp = retry_request(_do, label=label)
        except RateLimitError as e:
            result = LotteryQueryResult(
                error_type="rate_limit",
                message=str(e),
            )
            self._lottery_cache[orig_id] = result
            print(f"   ⚠️  查询抽奖状态被限流，跳过: {orig_id[:18]}")
            return result
        except NetworkError as e:
            result = LotteryQueryResult(
                error_type="network",
                message=str(e),
            )
            self._lottery_cache[orig_id] = result
            print(f"   ⚠️  查询抽奖状态失败 (网络错误)，跳过: {orig_id[:18]}")
            return result

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            result = LotteryQueryResult(
                error_type="json_decode",
                message=str(e),
            )
            self._lottery_cache[orig_id] = result
            return result

        code = data.get("code")
        if code == 0 and data.get("data"):
            result = LotteryQueryResult(info=data["data"])
        elif code is not None and code != 0:
            result = LotteryQueryResult(
                error_type="api_code",
                code=int(code),
                message=str(data.get("message", "")),
            )
        else:
            result = LotteryQueryResult(error_type="empty")

        self._lottery_cache[orig_id] = result
        return result

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def check_lottery_status(
        self, orig_id: str
    ) -> Tuple[LotteryState, Optional[datetime], str]:
        """Check if a lottery has finished.

        Returns:
            (LotteryState, lottery_time_or_None, reason_detail)
            — reason_detail includes error info when the query itself failed.
        """
        result = self.get_lottery_info(orig_id)
        info = result.info

        # If the query itself failed, surface the error in reason
        if result.is_error:
            state, base_reason = parse_lottery_state(info)
            err_detail = f"{result.error_type}"
            if result.code is not None:
                err_detail += f":{result.code}"
            if result.message:
                err_detail += f"({result.message[:40]})"
            return state, None, f"{base_reason}/{err_detail}"

        state, reason = parse_lottery_state(info)

        lottery_time: Optional[datetime] = None
        if info and info.get("lottery_time"):
            try:
                lottery_time = _utcfromtimestamp(float(info["lottery_time"]))
            except (TypeError, ValueError, OSError):
                pass

        return state, lottery_time, reason

    def delete_dynamic(self, item: Dict) -> Tuple[bool, str]:
        """Delete a single dynamic.

        Only deletes when ``item.params`` contains all required fields.
        Missing parameters → skip safely.

        Returns:
            (success: bool, error_message: str)
        """
        url = "https://api.bilibili.com/x/dynamic/feed/operate/remove"

        item_params = item.get("params", {})
        required = ["dyn_id_str", "rid_str", "dyn_type"]
        if not all(k in item_params for k in required):
            dyn_id = str(item.get("id_str", "?"))[:18]
            msg = f"缺少删除参数，安全跳过"
            print(f"   ⚠️  {msg}: {dyn_id}")
            return False, msg

        dyn_id_str = str(item_params["dyn_id_str"])
        rid_str = str(item_params["rid_str"])
        dyn_type = int(item_params["dyn_type"])

        url_params = {"platform": "web", "csrf": self.csrf}
        json_payload = {
            "dyn_id_str": dyn_id_str,
            "dyn_type": dyn_type,
            "rid_str": rid_str,
        }

        self._log_debug(f"DELETE payload: {json.dumps(json_payload, ensure_ascii=False)}")

        label = f"删除 {dyn_id_str[:18]}"

        def _do() -> requests.Response:
            return self.session.post(url, params=url_params, json=json_payload, timeout=10)

        try:
            resp = retry_request(_do, max_retries=2, label=label)
        except (NetworkError, RateLimitError) as e:
            msg = str(e)
            print(f"   ❌ 删除失败 (网络): {msg}")
            return False, msg

        try:
            result = resp.json()
        except json.JSONDecodeError as e:
            msg = f"删除响应非 JSON: {e}"
            print(f"   ❌ {msg}")
            return False, msg

        self._log_debug(f"DELETE response: {json.dumps(result, ensure_ascii=False)}")

        if result.get("code") == 0:
            print(f"   ✅ 已删除: {dyn_id_str[:18]}")
            return True, ""
        else:
            code = result.get("code", "?")
            msg = str(result.get("message", "未知错误"))
            print(f"   ❌ 删除失败: {msg} (code={code})")
            return False, f"code={code}: {msg}"

    # ------------------------------------------------------------------
    # Main process
    # ------------------------------------------------------------------

    def process_dynamics(
        self,
        dry_run: bool = True,
        require_confirm: bool = True,
        export_candidates: bool = False,
        include_invalid_repost: bool = False,
    ) -> None:
        """Main loop: scan → display → (optional) delete.

        **Delete never happens during the scan phase.**
        All candidates are collected first, then displayed, and only then
        (based on mode) either confirmed or deleted after a countdown.

        Args:
            dry_run: If True, only scan; never delete.
            require_confirm: If True (and not dry_run), require DELETE input
                             after the candidate table.
            export_candidates: If True, export sanitized candidates JSON.
            include_invalid_repost: If True, also target reposts whose
                                    original dynamic was deleted/invalid.
        """
        offset: Optional[str] = None
        page = 1
        candidates: List[CandidateInfo] = []
        failed_pages = 0

        print(f"\n{'=' * 60}")
        print(f"🚀 B站抽奖动态清理工具")
        print(f"👤 UID: {_sanitize_for_log(self.uid)}")
        if dry_run:
            print(f"🔍 模式: 试运行 (仅扫描，不删除)")
        else:
            print(f"⚡ 模式: 正式删除")
        if include_invalid_repost:
            print(f"🔗 失效转发: 一并处理")
        print(f"{'=' * 60}\n")

        # ================================================================
        # PHASE 1: Scan (NEVER delete here)
        # ================================================================
        while True:
            print(f"\n📄 第 {page} 页 …")
            try:
                items, new_offset = self.get_dynamics(offset)
            except AuthError as e:
                print(f"   ❌ 认证失败: {e}")
                break
            except RateLimitError as e:
                print(f"   ❌ 被限流: {e}")
                failed_pages += 1
                break
            except (ApiError, NetworkError) as e:
                print(f"   ❌ 获取失败: {e}")
                failed_pages += 1
                break

            if not items:
                print("✨ 已扫描全部动态")
                break

            for idx, item in enumerate(items, 1):
                try:
                    self.stats["total"] += 1
                    dyn_id = str(item.get("id_str", f"unknown_{idx}"))

                    # ── Check 1: Is it a lottery dynamic? ──
                    is_lottery, orig_id, detect_method = is_lottery_dynamic(item)

                    # ── Check 2: Is it an invalid repost? ──
                    is_invalid_repost = False
                    if not is_lottery and include_invalid_repost:
                        is_invalid_repost = is_repost_original_invalid(item)
                    elif not is_lottery:
                        # Repost detection only when opt-in
                        is_invalid_repost_detected = is_repost_original_invalid(item)
                        if is_invalid_repost_detected and self.debug:
                            print(f"\n[{idx}] {dyn_id[:18]}")
                            print(f"   🔗 检测到失效转发，但默认不处理（--include-invalid-repost 可启用）")

                    if not is_lottery and not is_invalid_repost:
                        if idx <= 3 or self.debug:
                            print(f"\n[{idx}] {dyn_id[:18]}  ⏭️  非目标动态")
                        continue

                    # ── Build shared candidate info ──
                    publish_time = extract_publish_time(item)
                    text_preview = extract_text_preview(item)
                    dynamic_url = DYNAMIC_URL_TEMPLATE.format(dyn_id=dyn_id)

                    # ── Invalid repost path ──
                    if is_invalid_repost:
                        self.stats["invalid_repost"] += 1
                        print(f"\n[{idx}] {dyn_id[:18]}")
                        print(f"   🔗 转发动态 → 原动态已失效")
                        candidates.append(CandidateInfo(
                            dyn_id=dyn_id,
                            orig_lottery_id=orig_id or str(item.get("orig", {}).get("id_str", "")),
                            publish_time=publish_time,
                            lottery_time=None,
                            detect_reason="invalid_repost",
                            text_preview=text_preview,
                            dynamic_url=dynamic_url,
                            is_repost_invalid=True,
                            raw_item=item,  # always carry item for later deletion
                        ))
                        continue  # Don't check lottery status

                    # ── Lottery status path ──
                    self.stats["lottery"] += 1
                    state, lottery_time, state_reason = self.check_lottery_status(orig_id)
                    full_reason = f"{detect_method}+{state_reason}" if detect_method else state_reason

                    print(f"\n[{idx}] {dyn_id[:18]}")
                    print(f"   🎲 检测到抽奖 ({detect_method})")

                    if state == LotteryState.ACTIVE:
                        print(f"   ⏳ 进行中 ({state_reason})")
                        continue
                    elif state == LotteryState.UNKNOWN:
                        self.stats["skipped_unknown"] += 1
                        print(f"   ❓ 状态不明 ({state_reason})，安全跳过，不删除")
                        continue
                    elif state == LotteryState.FINISHED:
                        self.stats["expired"] += 1
                        time_str = _fmt_time_short(lottery_time)
                        print(f"   🎯 已开奖 ({time_str}, {state_reason})")
                        candidates.append(CandidateInfo(
                            dyn_id=dyn_id,
                            orig_lottery_id=orig_id,
                            publish_time=publish_time,
                            lottery_time=lottery_time,
                            detect_reason=full_reason,
                            text_preview=text_preview,
                            dynamic_url=dynamic_url,
                            raw_item=item,  # always carry item for later deletion
                        ))

                except Exception as e:
                    print(f"\n   ❌ 处理动态异常: {e}")
                    if self.debug:
                        import traceback
                        traceback.print_exc()
                    continue

            # Pagination
            offset = new_offset
            if offset:
                self._random_delay(1.5, 3.0)
                page += 1
            else:
                break

        # ================================================================
        # PHASE 2: Display (always, for both dry-run and execute)
        # ================================================================
        if candidates:
            self._print_candidates_table(candidates)
            if export_candidates:
                self._export_candidates_json(candidates)

        # ================================================================
        # PHASE 3: Action (only when NOT dry_run AND candidates exist)
        # ================================================================
        if not dry_run and candidates:
            if require_confirm:
                # --execute (no --yes): input DELETE after table
                self._confirm_and_delete(candidates)
            else:
                # --execute --yes: countdown AFTER table, then delete
                print(f"\n⚠️  将在 3 秒后删除以上 {len(candidates)} 条动态 …")
                for i in range(3, 0, -1):
                    print(f"   {i}…")
                    time.sleep(1)
                print()
                self._execute_deletion(candidates)

        # ================================================================
        # Statistics
        # ================================================================
        print(f"\n{'=' * 60}")
        parts = [f"总{self.stats['total']}", f"抽奖{self.stats['lottery']}"]
        if self.stats["invalid_repost"]:
            parts.append(f"失效转发{self.stats['invalid_repost']}")
        parts += [
            f"已开奖{self.stats['expired']}",
            f"状态不明(跳过){self.stats['skipped_unknown']}",
            f"删除{self.stats['deleted']}",
            f"失败{self.stats['failed']}",
        ]
        print(f"📊 统计: {' | '.join(parts)}")
        print(f"{'=' * 60}")

        if failed_pages > 0:
            print(f"\n⚠️  {failed_pages} 页获取失败，建议稍后重试。")

    # ------------------------------------------------------------------
    # Candidate display & export
    # ------------------------------------------------------------------

    def _print_candidates_table(self, candidates: List[CandidateInfo]) -> None:
        """Print a formatted table of deletion candidates."""
        n = len(candidates)
        print(f"\n{'─' * 70}")
        print(f"📋 待处理候选: {n} 条")
        print(f"{'─' * 70}")

        for i, c in enumerate(candidates, 1):
            dyn_short = c.dyn_id[:18]
            orig_short = c.orig_lottery_id[:18]
            pub = _fmt_time_short(c.publish_time)
            lot = _fmt_time_short(c.lottery_time)

            tag = ""
            if c.is_repost_invalid:
                tag = " [失效转发]"
            elif "time_fallback" in c.detect_reason:
                tag = " [时间推断]"

            reason_short = c.detect_reason[:30]

            print(f"\n  #{i:<3} dyn={dyn_short}{tag}")
            print(f"       抽奖ID: {orig_short}")
            print(f"       发布时间: {pub}  |  开奖时间: {lot}")
            print(f"       识别原因: {reason_short}")
            if c.text_preview:
                preview = c.text_preview[:60]
                print(f"       预览: {preview}")
            if c.dynamic_url:
                print(f"       链接: {c.dynamic_url}")

        print(f"\n{'─' * 70}")

    def _export_candidates_json(self, candidates: List[CandidateInfo]) -> None:
        """Export sanitized candidate list to a JSON file."""
        timestamp = _now_shanghai().strftime("%Y%m%d_%H%M")
        filename = f"{CANDIDATE_EXPORT_PREFIX}{timestamp}.json"
        filepath = Path(filename)

        data = {
            "export_time": _fmt_time_iso(_now_shanghai()),
            "total": len(candidates),
            "candidates": [c.sanitized_dict() for c in candidates],
        }

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"\n📁 候选列表已导出: {filepath.resolve()}")
            print(f"   (已脱敏，不含 Cookie / SESSDATA / bili_jct)")
        except OSError as e:
            print(f"\n⚠️  导出候选列表失败: {e}")

    # ------------------------------------------------------------------
    # Confirmation & batch delete
    # ------------------------------------------------------------------

    def _confirm_and_delete(self, candidates: List[CandidateInfo]) -> None:
        """Print candidate summary, wait for DELETE input, then batch-delete."""
        # Re-print a compact summary
        print(f"\n{'=' * 60}")
        print(f"⚠️  确认删除以上 {len(candidates)} 条动态？")
        print(f"{'=' * 60}")
        for i, c in enumerate(candidates, 1):
            tag = ""
            if c.is_repost_invalid:
                tag = " [失效转发]"
            elif "time_fallback" in c.detect_reason:
                tag = " [时间推断]"
            print(f"  {i:2d}. {c.dyn_id[:18]}  开奖: {_fmt_time_short(c.lottery_time)}{tag}")

        try:
            confirm = input(
                f"\n输入 DELETE 并回车执行删除 ({len(candidates)} 条)，直接回车取消: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n⏹  已取消删除")
            return

        if confirm != "DELETE":
            print("⏹  已取消删除")
            return

        print(f"\n开始删除 {len(candidates)} 条动态 …")
        for c in candidates:
            if c.raw_item is None:
                print(f"   ⚠️  缺少原始动态数据，跳过: {c.dyn_id[:18]}")
                self.stats["failed"] += 1
                continue
            try:
                ok, err = self.delete_dynamic(c.raw_item)
                if ok:
                    self.stats["deleted"] += 1
                else:
                    self.stats["failed"] += 1
            except Exception as e:
                print(f"   ❌ 删除异常: {e}")
                self.stats["failed"] += 1
            self._random_delay(2.0, 4.0)

    def _execute_deletion(self, candidates: List[CandidateInfo]) -> None:
        """Batch-delete candidates without confirmation (used by --execute --yes)."""
        for c in candidates:
            if c.raw_item is None:
                print(f"   ⚠️  缺少原始动态数据，跳过: {c.dyn_id[:18]}")
                self.stats["failed"] += 1
                continue
            try:
                ok, err = self.delete_dynamic(c.raw_item)
                if ok:
                    self.stats["deleted"] += 1
                else:
                    self.stats["failed"] += 1
            except Exception as e:
                print(f"   ❌ 删除异常: {e}")
                self.stats["failed"] += 1
            self._random_delay(2.0, 4.0)


# ===================================================================
# Cookie reading (unchanged core logic — kept as-is for stability)
# ===================================================================


def _try_chromium_browser(
    cookie_paths: List[str],
    local_state_paths: List[str],
    browser_name: str,
    prefix_32: bool = False,
    allow_kill_browser: bool = False,
) -> Optional[str]:
    """Generic Chromium-based browser cookie reader."""
    import shutil
    import sqlite3
    import subprocess
    import tempfile
    import base64
    import json as _json

    try:
        from win32crypt import CryptUnprotectData
        from Cryptodome.Cipher import AES
    except ImportError as e:
        print(f"⚠️  缺少依赖: {e}")
        print("   pip install pycryptodomex pywin32")
        return None

    cookie_file = ls_file = None
    for p in cookie_paths:
        if os.path.isfile(p):
            cookie_file = p
            break
    for p in local_state_paths:
        if os.path.isfile(p):
            ls_file = p
            break
    if not cookie_file or not ls_file:
        return None

    def _copy_db(src: str, dst: str) -> bool:
        """Safe copy of browser cookie DB (no forced kill unless allowed)."""
        # 1. Direct copy
        try:
            shutil.copy2(src, dst)
            return True
        except PermissionError:
            pass
        # 2. PowerShell copy (bypasses some locks)
        try:
            r = subprocess.run(
                [
                    "powershell.exe", "-Command",
                    f"Copy-Item -Force '{src}' '{dst}'; exit 0",
                ],
                capture_output=True, timeout=10,
            )
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return True
        except Exception:
            pass
        # 3. Manual browser kill (only if user explicitly allowed)
        if not allow_kill_browser:
            return False
        confirm = input(
            "⚠️  浏览器 Cookie 数据库被占用。输入 YES 允许强制关闭浏览器: "
        ).strip()
        if confirm != "YES":
            print("⏹  取消操作")
            return False
        proc_map = {
            "360Chrome": "360ChromeX.exe",
            "Chrome": "chrome.exe",
            "Edge": "msedge.exe",
        }
        proc_name = None
        for key, name in proc_map.items():
            if key in src:
                proc_name = name
                break
        if proc_name:
            subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True, timeout=5,
            )
            time.sleep(1)
            try:
                shutil.copy2(src, dst)
                return True
            except PermissionError:
                pass
        return False

    tmp_name = None
    conn = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        tmp_name = tmp.name

        if not _copy_db(cookie_file, tmp_name):
            print(f"⚠️  {browser_name} 的 Cookie 数据库被占用，无法读取")
            if not allow_kill_browser:
                print("   请手动关闭浏览器后重试，或添加 --kill-browser 参数自动关闭")
            return None

        with open(ls_file, "r", encoding="utf-8") as f:
            enc_key = base64.b64decode(
                _json.load(f)["os_crypt"]["encrypted_key"]
            )[5:]
        master_key = CryptUnprotectData(enc_key, None, None, None, 0)[1]

        def decrypt_one(enc_val: bytes, key: bytes) -> str:
            nonce = enc_val[3:15]
            raw = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                enc_val[15:-16], enc_val[-16:]
            )
            data = raw[32:] if prefix_32 else raw
            return data.decode("utf-8")

        cookies_dict: Dict[str, str] = {}
        conn = sqlite3.connect(tmp_name)
        conn.text_factory = bytes
        cur = conn.cursor()
        cur.execute(
            "SELECT host_key, name, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%bilibili.com'"
        )
        for host, name, enc_val in cur.fetchall():
            n = name.decode("utf-8")
            if enc_val and enc_val[:3] == b"v10":
                try:
                    cookies_dict[n] = decrypt_one(enc_val, master_key)
                except Exception:
                    pass

        if "DedeUserID" in cookies_dict and "bili_jct" in cookies_dict:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            print(f"✅ 已从 {browser_name} 读取到 {len(cookies_dict)} 个 bilibili cookie")
            print(f"   DedeUserID: {_sanitize_for_log(cookies_dict.get('DedeUserID', '???'))}")
            return cookie_str
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    return None


def get_bilibili_cookies(allow_kill_browser: bool = False) -> str:
    """Auto-read bilibili cookies from local browsers.  In-memory only."""
    CHROMIUM_BROWSERS = [
        {
            "name": "360极速浏览器X",
            "cookie_paths": [
                os.path.expanduser(
                    r"~\AppData\Local\360ChromeX\Chrome\User Data\Default\Network\Cookies"
                ),
                os.path.expanduser(
                    r"~\AppData\Local\360ChromeX\Chrome\User Data\Profile 1\Network\Cookies"
                ),
                os.path.expanduser(
                    r"~\AppData\Local\360ChromeX\Chrome\User Data\Profile 2\Network\Cookies"
                ),
            ],
            "ls_paths": [
                os.path.expanduser(
                    r"~\AppData\Local\360ChromeX\Chrome\User Data\Local State"
                ),
            ],
            "prefix_32": True,
        },
        {
            "name": "Chrome",
            "cookie_paths": [
                os.path.expanduser(
                    r"~\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies"
                ),
                os.path.expanduser(
                    r"~\AppData\Local\Google\Chrome\User Data\Profile 1\Network\Cookies"
                ),
                os.path.expanduser(
                    r"~\AppData\Local\Google\Chrome\User Data\Profile 2\Network\Cookies"
                ),
            ],
            "ls_paths": [
                os.path.expanduser(
                    r"~\AppData\Local\Google\Chrome\User Data\Local State"
                ),
            ],
            "prefix_32": False,
        },
        {
            "name": "Edge",
            "cookie_paths": [
                os.path.expanduser(
                    r"~\AppData\Local\Microsoft\Edge\User Data\Default\Network\Cookies"
                ),
                os.path.expanduser(
                    r"~\AppData\Local\Microsoft\Edge\User Data\Profile 1\Network\Cookies"
                ),
                os.path.expanduser(
                    r"~\AppData\Local\Microsoft\Edge\User Data\Profile 2\Network\Cookies"
                ),
            ],
            "ls_paths": [
                os.path.expanduser(
                    r"~\AppData\Local\Microsoft\Edge\User Data\Local State"
                ),
            ],
            "prefix_32": False,
        },
    ]

    for cfg in CHROMIUM_BROWSERS:
        result = _try_chromium_browser(
            cfg["cookie_paths"],
            cfg["ls_paths"],
            cfg["name"],
            prefix_32=cfg["prefix_32"],
            allow_kill_browser=allow_kill_browser,
        )
        if result:
            return result

    # Firefox fallback
    try:
        import browser_cookie3

        cj = browser_cookie3.firefox(domain_name="bilibili.com")
        if cj:
            cookies_dict: Dict[str, str] = {}
            for cookie in cj:
                if "bilibili" in cookie.domain:
                    cookies_dict[cookie.name] = cookie.value
            if "DedeUserID" in cookies_dict and "bili_jct" in cookies_dict:
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
                print(
                    f"✅ 已从 Firefox 读取到 {len(cookies_dict)} 个 bilibili cookie"
                )
                print(
                    f"   DedeUserID: {_sanitize_for_log(cookies_dict.get('DedeUserID', '???'))}"
                )
                return cookie_str
    except ImportError:
        pass
    except Exception:
        pass

    print("❌ 本地浏览器中未找到有效的 bilibili cookie")
    print("   请先在 Chrome / Edge / Firefox / 360极速浏览器X 中登录 bilibili.com")
    sys.exit(1)


# ===================================================================
# CLI
# ===================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (testable in isolation)."""
    p = argparse.ArgumentParser(
        description="Bilibili 抽奖动态清理工具 — 自动识别并删除已开奖互动抽奖动态",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="显式试运行模式（默认即为试运行）。与 --execute 互斥，同时传入报错。",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="执行删除模式。扫描后列出候选，需输入 DELETE 确认。",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="跳过二次确认（需与 --execute 搭配）。候选表打印后等待 3 秒倒计时再删除。",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="开启调试输出，打印 API 请求/响应详情。（不交互询问，显式传参才开启）",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="非交互模式（CI/自动化）。仅允许 dry-run 扫描，禁止删除。防止 CI 中意外删除。",
    )
    p.add_argument(
        "--kill-browser",
        action="store_true",
        help="允许强制关闭浏览器进程（Cookie 文件被占用时使用）。需输入 YES 二次确认。",
    )
    p.add_argument(
        "--export-candidates",
        action="store_true",
        help="导出候选列表为 JSON 文件（已脱敏，不含 Cookie/SESSDATA/bili_jct）。",
    )
    p.add_argument(
        "--include-invalid-repost",
        action="store_true",
        help="同时处理原动态已失效的转发动态（默认关闭，仅处理抽奖动态）。",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ── Mode resolution ──
    # Default (no flags): dry-run (scan only)
    # --dry-run: explicit dry-run (same as default)
    # --execute: scan + display candidates + input DELETE → delete
    # --execute --yes: scan + display candidates + 3s countdown → delete
    # --non-interactive: force dry-run, error if combined with --execute

    # Conflict: --dry-run and --execute together
    if args.dry_run and args.execute:
        print("❌ --dry-run 与 --execute 互斥，不能同时使用")
        sys.exit(1)

    dry_run = not args.execute
    # --dry-run without --execute is fine (same as default)
    if args.dry_run:
        dry_run = True

    if args.non_interactive and args.execute:
        print("❌ --non-interactive 模式下不允许使用 --execute（禁止 CI 中删除）")
        sys.exit(1)

    if args.yes and not args.execute:
        print("⚠️  --yes 需要与 --execute 搭配使用，已忽略")

    require_confirm = not args.yes and not dry_run

    # ── Print banner ──
    print("Bilibili Lottery Dynamic Cleaner")
    print("=" * 60)

    if args.non_interactive:
        print("🤖 非交互模式 (non-interactive): 仅允许试运行扫描")
        dry_run = True
        require_confirm = False

    if dry_run:
        print("🔍 试运行模式 (仅扫描，不删除)")
    else:
        if args.yes:
            print("⚡ 正式删除模式 (候选表后 3 秒倒计时)")
        else:
            print("⚡ 正式删除模式 (候选表后需输入 DELETE 确认)")

    if args.include_invalid_repost:
        print("🔗 失效转发: 一并处理")

    # ── Read cookies (in-memory only) ──
    cookie = get_bilibili_cookies(allow_kill_browser=args.kill_browser)

    # ── Debug: no interactive prompt, use flag directly ──
    debug = args.debug

    try:
        cleaner = BilibiliLotteryCleaner(cookie, debug=debug)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # ── Run (countdown is now inside process_dynamics, after candidate table) ──
    try:
        cleaner.process_dynamics(
            dry_run=dry_run,
            require_confirm=require_confirm,
            export_candidates=args.export_candidates,
            include_invalid_repost=args.include_invalid_repost,
        )
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断")
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        if debug:
            import traceback
            traceback.print_exc()

    # Interactive wait only when running in interactive terminal with deletion
    if not dry_run and not args.non_interactive:
        try:
            input("\n按回车退出…")
        except (EOFError, KeyboardInterrupt):
            print()


if __name__ == "__main__":
    main()
