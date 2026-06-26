#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili Lottery Dynamic Cleaner
自动扫描 B 站动态，识别已开奖互动抽奖动态并批量删除。
"""

import argparse
import json
import os
import platform
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


class BilibiliLotteryCleaner:
    def __init__(self, cookie_str: str, debug: bool = False):
        self.cookie_str = cookie_str
        self.cookies = self._parse_cookie(cookie_str)
        self.csrf = self.cookies.get("bili_jct", "")
        self.uid = self.cookies.get("DedeUserID", "")
        self.debug = debug

        if not self.csrf or not self.uid:
            raise ValueError("Cookie 中缺少必要的 bili_jct 或 DedeUserID，请确保已登录")

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://t.bilibili.com/",
            "Origin": "https://t.bilibili.com",
            "Cookie": cookie_str,
            "Content-Type": "application/json;charset=UTF-8",
        }

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        # 抽奖信息缓存（避免重复请求）
        self._lottery_cache: Dict[str, Optional[dict]] = {}

        self.stats = {
            "total": 0,
            "lottery": 0,
            "expired": 0,
            "deleted": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for item in cookie_str.split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookies[key] = value
        return cookies

    def _random_delay(self, min_sec: float = 1.0, max_sec: float = 3.0):
        time.sleep(random.uniform(min_sec, max_sec))

    # ------------------------------------------------------------------
    # B 站 API
    # ------------------------------------------------------------------

    def get_dynamics(self, offset: Optional[str] = None) -> Tuple[List[Dict], Optional[str]]:
        """分页拉取用户动态列表"""
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {
            "host_mid": self.uid,
            "timezone_offset": -480,
            "platform": "web",
            "features": "itemOpusStyle,listPicScale,opusBigCover,opusHiddenCover,DynamicPageDynamicAutoSaveSwitch,DynamicUgcAttachCard",
            "web_location": "333.1330",
        }
        if offset:
            params["offset"] = offset

        try:
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()

            if data.get("code") != 0:
                print(f"❌ 获取动态列表失败: {data.get('message', '未知错误')}")
                return [], None

            response_data = data.get("data", {})
            items = response_data.get("items", [])
            has_more = response_data.get("has_more", False)
            next_offset = response_data.get("offset") if has_more else None
            return items, next_offset

        except Exception as e:
            print(f"❌ 请求异常: {e}")
            return [], None

    def get_lottery_info(self, orig_id: str) -> Optional[dict]:
        """
        查询抽奖状态，带缓存。
        返回 lottery_notice 的 data 字段，或 None。
        """
        if orig_id in self._lottery_cache:
            return self._lottery_cache[orig_id]

        url = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"
        params = {
            "business_id": orig_id,
            "business_type": "1",
            "csrf": self.csrf,
            "web_location": "333.1330",
        }
        try:
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            if data.get("code") == 0 and data.get("data"):
                info = data["data"]
            else:
                info = None
        except Exception:
            info = None

        self._lottery_cache[orig_id] = info
        return info

    def is_lottery_dynamic(self, item: Dict) -> Tuple[bool, str, str]:
        """本地快速检测是否为抽奖动态（关键词 + 字段匹配），不做 API 请求"""
        if not isinstance(item, dict):
            return False, "", ""

        orig_info = item.get("orig", {})
        if not orig_info:
            return False, "", ""

        orig_id = str(orig_info.get("id_str", ""))
        if not orig_id:
            return False, "", ""

        reasons = []

        # 深度递归搜索
        found, path = self.deep_search_lottery(orig_info, "orig")
        if found:
            reasons.append(f"deep_search:{path}")

        modules = orig_info.get("modules", {})
        if isinstance(modules, dict):
            module_dynamic = modules.get("module_dynamic", {})
            if isinstance(module_dynamic, dict):
                additional = module_dynamic.get("additional")
                if isinstance(additional, dict):
                    add_type = additional.get("type", "")
                    if isinstance(add_type, str) and "lottery" in add_type.lower():
                        reasons.append("additional_type")

                desc_info = module_dynamic.get("desc", {})
                if isinstance(desc_info, dict):
                    text = desc_info.get("text", "")
                    lottery_patterns = [
                        r"抽奖",
                        r"关注\s*[\+➕]\s*转发",
                        r"转发\s*[\+➕]\s*关注",
                        r"转关",
                    ]
                    if any(re.search(p, text) for p in lottery_patterns):
                        reasons.append("desc_regex")

        if reasons:
            return True, orig_id, "+".join(reasons)
        return False, orig_id, ""

    def deep_search_lottery(self, obj: Any, path: str = "") -> Tuple[bool, str]:
        """深度递归搜索对象中是否包含抽奖特征"""
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_str = str(k).lower()
                v_str = str(v).lower() if not isinstance(v, (dict, list)) else ""
                if any(x in k_str for x in ["lottery", "抽奖", "choujiang"]):
                    return True, f"{path}.{k}"
                if any(x in v_str for x in ["lottery", "抽奖", "互动抽奖"]):
                    return True, f"{path}.{k}"
                if isinstance(v, (dict, list)):
                    found, p = self.deep_search_lottery(v, f"{path}.{k}")
                    if found:
                        return True, p
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                found, p = self.deep_search_lottery(v, f"{path}[{i}]")
                if found:
                    return True, p
        return False, ""

    def check_lottery_status(self, orig_id: str) -> Tuple[bool, Optional[datetime]]:
        """
        检查抽奖是否已开奖。
        优先使用 API 返回的明确状态字段；无状态字段时用开奖时间判断（加 2 小时安全缓冲）。
        """
        info = self.get_lottery_info(orig_id)
        if not info:
            return False, None

        # 如果有明确的状态字段，优先使用
        status = info.get("lottery_status") or info.get("status")
        if status is not None:
            try:
                # 假设 1/true = 已开奖
                if str(status) in ("1", "true", "True"):
                    lottery_time = info.get("lottery_time")
                    if lottery_time:
                        return True, datetime.fromtimestamp(lottery_time)
                    return True, None
                elif str(status) in ("0", "false", "False"):
                    return False, None
            except (ValueError, TypeError):
                pass

        # 无状态字段，通过时间判断（加 2 小时安全缓冲，防止延迟开奖）
        lottery_time = info.get("lottery_time")
        if not lottery_time:
            return False, None

        lottery_dt = datetime.fromtimestamp(lottery_time)
        safe_boundary = lottery_dt + timedelta(hours=2)
        is_expired = safe_boundary < datetime.now()
        return is_expired, lottery_dt

    def delete_dynamic(self, item: Dict) -> bool:
        """
        删除动态。只有 item['params'] 包含必要参数时才删除，
        缺少参数时跳过（宁可漏删，不猜错参数）。
        """
        url = "https://api.bilibili.com/x/dynamic/feed/operate/remove"

        item_params = item.get("params", {})
        required = ["dyn_id_str", "rid_str", "dyn_type"]
        if not all(k in item_params for k in required):
            dyn_id = item.get("id_str", "?")
            dyn_id_short = str(dyn_id)[:18]
            print(f"   ⚠️  缺少 B 站返回的完整删除参数，安全跳过: {dyn_id_short}")
            return False

        dyn_id_str = str(item_params["dyn_id_str"])
        rid_str = str(item_params["rid_str"])
        dyn_type = int(item_params["dyn_type"])

        url_params = {"platform": "web", "csrf": self.csrf}
        json_payload = {
            "dyn_id_str": dyn_id_str,
            "dyn_type": dyn_type,
            "rid_str": rid_str,
        }

        if self.debug:
            print(f"   [调试] DELETE: {json_payload}")

        try:
            resp = self.session.post(url, params=url_params, json=json_payload, timeout=10)
            result = resp.json()
            if self.debug:
                print(f"   [调试] 删除响应: {result}")
            if result.get("code") == 0:
                print(f"   ✅ 已删除: {dyn_id_str}")
                return True
            else:
                msg = result.get("message", "未知错误")
                code = result.get("code")
                print(f"   ❌ 删除失败: {msg} (code: {code})")
                return False
        except Exception as e:
            print(f"   ❌ 删除异常: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process_dynamics(self, dry_run: bool = False, require_confirm: bool = True):
        """
        主循环：分页拉取 → 本地检测抽奖 → 查询开奖状态 → 删除。

        dry_run: True 只扫描不删除
        require_confirm: 非 dry_run 时，扫描完候选列表后要求用户输入 DELETE 确认
        """
        offset = None
        page = 1
        # 收集待删除候选（仅用于确认阶段）
        candidates: List[Dict] = []

        print(f"\n{'='*60}")
        print(f"🚀 B站抽奖动态清理工具")
        print(f"👤 UID: {self.uid}")
        print(f"{'='*60}\n")

        while True:
            print(f"\n📄 第 {page} 页...")
            items, offset = self.get_dynamics(offset)

            if not items:
                print("✨ 完成")
                break

            for idx, item in enumerate(items, 1):
                try:
                    self.stats["total"] += 1
                    dyn_id = str(item.get("id_str", f"unknown_{idx}"))
                    print(f"\n[{idx}] {dyn_id}")

                    is_lottery, orig_id, method = self.is_lottery_dynamic(item)
                    if not is_lottery:
                        print(f"   ⏭️  非抽奖动态")
                        continue

                    self.stats["lottery"] += 1
                    print(f"   🎲 检测到抽奖 ({method})")

                    is_expired, lottery_time = self.check_lottery_status(orig_id)
                    if not is_expired:
                        status = lottery_time.strftime("%m-%d %H:%M") if lottery_time else "未开奖"
                        print(f"   ⏳ {status}")
                        continue

                    self.stats["expired"] += 1
                    expire_str = lottery_time.strftime("%m-%d %H:%M") if lottery_time else "未知"
                    print(f"   🎯 已开奖 ({expire_str})")

                    if dry_run:
                        print(f"   ⏸️  试运行，跳过")
                        continue

                    # 收集候选，等扫描完再一起删
                    candidates.append({"item": item, "dyn_id": dyn_id, "expire": expire_str})

                    if not require_confirm:
                        # 直接删除（--execute 直接模式）
                        if self.delete_dynamic(item):
                            self.stats["deleted"] += 1
                        else:
                            self.stats["failed"] += 1
                        self._random_delay(2.0, 4.0)

                except Exception as e:
                    print(f"   ❌ 错误: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            if offset:
                self._random_delay(1.5, 3.0)
                page += 1
            else:
                break

        # 【二次确认】非 dry_run + require_confirm + 有候选
        if not dry_run and require_confirm and candidates:
            self._confirm_and_delete(candidates)

        # 统计
        print(f"\n{'='*60}")
        print(f"📊 统计: 总{self.stats['total']} | 抽奖{self.stats['lottery']} | "
              f"开奖{self.stats['expired']} | 删除{self.stats['deleted']} | "
              f"失败{self.stats['failed']}")
        print(f"{'='*60}")

    def _confirm_and_delete(self, candidates: List[Dict]):
        """打印候选列表并等待用户确认后再删除"""
        print(f"\n{'='*60}")
        print(f"⚠️  以下 {len(candidates)} 条已开奖动态准备删除:")
        print(f"{'='*60}")
        for i, c in enumerate(candidates, 1):
            print(f"  {i:2d}. {c['dyn_id']}  (开奖: {c['expire']})")

        print(f"\n确认删除以上 {len(candidates)} 条动态？")
        confirm = input("输入 DELETE 并回车执行删除，直接回车取消: ").strip()
        if confirm != "DELETE":
            print("⏹  已取消删除")
            return

        print(f"\n开始删除 {len(candidates)} 条动态...")
        for c in candidates:
            if self.delete_dynamic(c["item"]):
                self.stats["deleted"] += 1
            else:
                self.stats["failed"] += 1
            self._random_delay(2.0, 4.0)


# ------------------------------------------------------------------
# Cookie 读取（不落盘，仅内存暂存）
# ------------------------------------------------------------------

def _try_chromium_browser(
    cookie_paths: List[str],
    local_state_paths: List[str],
    browser_name: str,
    prefix_32: bool = False,
    allow_kill_browser: bool = False,
) -> Optional[str]:
    """
    通用的 Chromium 系浏览器 cookie 读取。
    读取失败时默认提示用户手动关闭浏览器，只有 allow_kill_browser=True 才强制杀进程。
    """
    import shutil
    import sqlite3
    import subprocess
    import tempfile
    import json as pyjson
    import base64
    from win32crypt import CryptUnprotectData
    from Cryptodome.Cipher import AES

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
        """安全复制 Cookie DB（不强杀浏览器）"""
        # 1. 直接复制
        try:
            shutil.copy2(src, dst)
            return True
        except PermissionError:
            pass
        # 2. PowerShell 绕锁
        try:
            r = subprocess.run(
                ["powershell.exe", "-Command",
                 f"Copy-Item -Force '{src}' '{dst}'; exit 0"],
                capture_output=True, timeout=10,
            )
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return True
        except Exception:
            pass
        # 3. 除非用户显式允许，否则不杀进程
        if not allow_kill_browser:
            return False
        # 即使 --kill-browser，仍需交互确认
        confirm = input("⚠️  浏览器 Cookie 数据库被占用。输入 YES 允许强制关闭浏览器: ").strip()
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

    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        if not _copy_db(cookie_file, tmp.name):
            print(f"⚠️  {browser_name} 的 Cookie 数据库被占用，无法读取")
            if not allow_kill_browser:
                print("   请手动关闭浏览器后重试，或添加 --kill-browser 参数自动关闭")
            return None

        with open(ls_file, "r", encoding="utf-8") as f:
            enc_key = base64.b64decode(pyjson.load(f)["os_crypt"]["encrypted_key"])[5:]
        master_key = CryptUnprotectData(enc_key, None, None, None, 0)[1]

        def decrypt_one(enc_val: bytes, key: bytes) -> str:
            nonce = enc_val[3:15]
            raw = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                enc_val[15:-16], enc_val[-16:]
            )
            data = raw[32:] if prefix_32 else raw
            return data.decode("utf-8")

        cookies_dict = {}
        conn = sqlite3.connect(tmp.name)
        conn.text_factory = bytes
        cur = conn.cursor()
        cur.execute(
            "SELECT host_key, name, encrypted_value FROM cookies "
            "WHERE host_key = '.bilibili.com'"
        )
        for host, name, enc_val in cur.fetchall():
            n = name.decode("utf-8")
            if enc_val and enc_val[:3] == b"v10":
                try:
                    cookies_dict[n] = decrypt_one(enc_val, master_key)
                except Exception:
                    pass
        conn.close()
        os.unlink(tmp.name)

        if "DedeUserID" in cookies_dict and "bili_jct" in cookies_dict:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            print(f"✅ 已从 {browser_name} 读取到 {len(cookies_dict)} 个 bilibili cookie")
            print(f"   DedeUserID: {cookies_dict.get('DedeUserID', '???')}")
            return cookie_str
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    return None


def get_bilibili_cookies(allow_kill_browser: bool = False) -> str:
    """从本地浏览器自动读取 bilibili cookie，仅内存暂存。"""
    CHROMIUM_BROWSERS = [
        {
            "name": "360极速浏览器X",
            "cookie_paths": [
                os.path.expanduser(r"~\AppData\Local\360ChromeX\Chrome\User Data\Default\Network\Cookies"),
                os.path.expanduser(r"~\AppData\Local\360ChromeX\Chrome\User Data\Profile 1\Network\Cookies"),
                os.path.expanduser(r"~\AppData\Local\360ChromeX\Chrome\User Data\Profile 2\Network\Cookies"),
            ],
            "ls_paths": [
                os.path.expanduser(r"~\AppData\Local\360ChromeX\Chrome\User Data\Local State"),
            ],
            "prefix_32": True,
        },
        {
            "name": "Chrome",
            "cookie_paths": [
                os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Default\Network\Cookies"),
                os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Profile 1\Network\Cookies"),
                os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Profile 2\Network\Cookies"),
            ],
            "ls_paths": [
                os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data\Local State"),
            ],
            "prefix_32": False,
        },
        {
            "name": "Edge",
            "cookie_paths": [
                os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data\Default\Network\Cookies"),
                os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data\Profile 1\Network\Cookies"),
                os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data\Profile 2\Network\Cookies"),
            ],
            "ls_paths": [
                os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data\Local State"),
            ],
            "prefix_32": False,
        },
    ]

    for cfg in CHROMIUM_BROWSERS:
        result = _try_chromium_browser(
            cfg["cookie_paths"], cfg["ls_paths"],
            cfg["name"], prefix_32=cfg["prefix_32"],
            allow_kill_browser=allow_kill_browser,
        )
        if result:
            return result

    # Firefox 备选
    try:
        import browser_cookie3
        cj = browser_cookie3.firefox(domain_name="bilibili.com")
        if cj:
            cookies_dict = {}
            for cookie in cj:
                if "bilibili" in cookie.domain:
                    cookies_dict[cookie.name] = cookie.value
            if "DedeUserID" in cookies_dict and "bili_jct" in cookies_dict:
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
                print(f"✅ 已从 Firefox 读取到 {len(cookies_dict)} 个 bilibili cookie")
                print(f"   DedeUserID: {cookies_dict.get('DedeUserID', '???')}")
                return cookie_str
    except ImportError:
        pass
    except Exception:
        pass

    print("❌ 本地浏览器中未找到有效的 bilibili cookie")
    print("   请先在 Chrome / Edge / Firefox / 360极速浏览器X 中登录 bilibili.com")
    sys.exit(1)


# ------------------------------------------------------------------
# CLI 入口
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bilibili 抽奖动态清理工具 — 自动识别并删除已开奖互动抽奖动态",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="试运行模式（默认）：只扫描不删除",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="执行删除（覆盖 --dry-run）",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="开启调试输出",
    )
    parser.add_argument(
        "--kill-browser", action="store_true",
        help="允许强制关闭浏览器进程（Cookie 文件被占用时使用）",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="跳过二次确认（与 --execute 搭配使用）",
    )
    args = parser.parse_args()

    print("Bilibili Lottery Dynamic Cleaner")
    print("=" * 60)

    # 确定模式
    dry_run = not args.execute  # --execute 覆盖 --dry-run
    if dry_run:
        print("🔍 试运行模式（仅扫描，不删除）")
    else:
        print("⚡ 正式删除模式")

    # 读取 cookie（仅内存）
    cookie = get_bilibili_cookies(allow_kill_browser=args.kill_browser)

    # 交互式调试询问（仅当没传 --debug 时询问）
    if not args.debug:
        debug = input("调试模式? (y/n, 默认n): ").strip().lower() == "y"
    else:
        debug = True

    try:
        cleaner = BilibiliLotteryCleaner(cookie, debug=debug)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # 交互式模式选择（仅当既没 --dry-run 也没 --execute 时）
    if not args.dry_run and not args.execute:
        print("\n1. 试运行 (仅检测)")
        print("2. 正式运行 (删除)")
        choice = input("选择 (1/2, 默认1): ").strip() or "1"
        dry_run = choice == "1"

    try:
        cleaner.process_dynamics(
            dry_run=dry_run,
            require_confirm=(not args.yes and not dry_run),
        )
    except KeyboardInterrupt:
        print("\n\n⚠️ 中断")
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        if debug:
            import traceback
            traceback.print_exc()

    if not dry_run:
        input("\n按回车退出...")


if __name__ == "__main__":
    main()
