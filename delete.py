#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili Lottery Dynamic Cleaner - JSON格式修复版
解决 4101001 参数错误
"""

import requests
import time
import random
import os
import sys
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any


class BilibiliLotteryCleaner:
    def __init__(self, cookie_str: str, debug: bool = False):
        self.cookie_str = cookie_str
        self.cookies = self._parse_cookie(cookie_str)
        self.csrf = self.cookies.get('bili_jct', '')
        self.uid = self.cookies.get('DedeUserID', '')
        self.debug = debug
        
        if not self.csrf or not self.uid:
            raise ValueError("Cookie中缺少必要的bili_jct或DedeUserID，请确保已登录")
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://t.bilibili.com/',
            'Origin': 'https://t.bilibili.com',
            'Cookie': cookie_str,
            'Content-Type': 'application/json;charset=UTF-8'  # 关键：改为JSON格式
        }
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        self.stats = {
            'total': 0,
            'lottery': 0,
            'expired': 0,
            'deleted': 0,
            'failed': 0
        }

    def _parse_cookie(self, cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for item in cookie_str.split(';'):
            if '=' in item:
                key, value = item.strip().split('=', 1)
                cookies[key] = value
        return cookies

    def _random_delay(self, min_sec: float = 1.0, max_sec: float = 3.0):
        delay = random.uniform(min_sec, max_sec)
        time.sleep(delay)

    def get_dynamics(self, offset: Optional[str] = None) -> Tuple[List[Dict], Optional[str]]:
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        params = {
            'host_mid': self.uid,
            'timezone_offset': -480,
            'platform': 'web',
            'features': 'itemOpusStyle,listPicScale,opusBigCover,opusHiddenCover,DynamicPageDynamicAutoSaveSwitch,DynamicUgcAttachCard',
            'web_location': '333.1330'
        }
        if offset:
            params['offset'] = offset

        try:
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get('code') != 0:
                print(f"❌ 获取动态列表失败: {data.get('message', '未知错误')}")
                return [], None
            
            response_data = data.get('data', {})
            items = response_data.get('items', [])
            has_more = response_data.get('has_more', False)
            next_offset = response_data.get('offset') if has_more else None
            
            return items, next_offset
            
        except Exception as e:
            print(f"❌ 请求异常: {e}")
            return [], None

    def check_lottery_status(self, orig_id_str: str) -> Tuple[bool, Optional[datetime]]:
        """检查原动态是否是已开奖的抽奖"""
        url = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"
        params = {
            'business_id': orig_id_str,
            'business_type': '1',
            'csrf': self.csrf,
            'web_location': '333.1330'
        }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()
            
            if data.get('code') != 0:
                return False, None
            
            lottery_data = data.get('data', {})
            if not lottery_data:
                return False, None
            
            lottery_time = lottery_data.get('lottery_time')
            
            if not lottery_time:
                return False, None
            
            lottery_dt = datetime.fromtimestamp(lottery_time)
            now = datetime.now()
            
            is_expired = lottery_dt < now
            return is_expired, lottery_dt
            
        except Exception as e:
            return False, None

    def deep_search_lottery(self, obj: Any, path: str = "") -> Tuple[bool, str]:
        """深度递归搜索对象中是否包含抽奖特征"""
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_str = str(k).lower()
                v_str = str(v).lower() if not isinstance(v, dict) and not isinstance(v, list) else ""
                
                if any(x in k_str for x in ['lottery', '抽奖', 'choujiang']):
                    return True, f"{path}.{k}"
                
                if any(x in v_str for x in ['lottery', '抽奖', '互动抽奖']):
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

    def is_lottery_dynamic(self, item: Dict) -> Tuple[bool, str, str]:
        """全面检测是否为抽奖动态"""
        if not isinstance(item, dict):
            return False, "", ""
        
        dyn_id = str(item.get('id_str', 'unknown'))
        
        orig_info = item.get('orig', {})
        if not orig_info:
            return False, "", ""
        
        orig_id = str(orig_info.get('id_str', ''))
        if not orig_id:
            return False, "", ""
        
        reasons = []
        
        found, path = self.deep_search_lottery(orig_info, "orig")
        if found:
            reasons.append(f"deep_search:{path}")
        
        modules = orig_info.get('modules', {})
        if isinstance(modules, dict):
            module_dynamic = modules.get('module_dynamic', {})
            if isinstance(module_dynamic, dict):
                additional = module_dynamic.get('additional')
                if isinstance(additional, dict):
                    add_type = additional.get('type', '')
                    if isinstance(add_type, str) and 'lottery' in add_type.lower():
                        reasons.append("additional_type")
                
                desc_info = module_dynamic.get('desc', {})
                if isinstance(desc_info, dict):
                    text = desc_info.get('text', '')
                    lottery_patterns = [r'抽奖', r'关注\s*[\+➕]\s*转发', r'转发\s*[\+➕]\s*关注', r'转关']
                    if any(re.search(p, text) for p in lottery_patterns):
                        reasons.append("desc_regex")
        
        # API验证
        try:
            url = "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice"
            params = {
                'business_id': orig_id,
                'business_type': '1',
                'csrf': self.csrf
            }
            resp = self.session.get(url, params=params, timeout=5)
            data = resp.json()
            if data.get('code') == 0 and data.get('data'):
                reasons.append("api_verified")
        except:
            pass
        
        if reasons:
            return True, orig_id, "+".join(reasons)
        
        return False, orig_id, ""

    def delete_dynamic(self, item: Dict) -> bool:
        """
        删除动态 - 使用JSON格式发送
        关键修复：使用 json= 而不是 data= 发送请求
        """
        url = "https://api.bilibili.com/x/dynamic/feed/operate/remove"
        
        # 从item的params字段获取参数（如果存在）
        # B站动态数据结构中的params字段才包含真正的删除参数
        item_params = item.get('params', {})
        
        if item_params and 'dyn_id_str' in item_params:
            # 如果API返回的params字段中有删除所需参数，直接使用
            dyn_id_str = str(item_params.get('dyn_id_str'))
            rid_str = str(item_params.get('rid_str', dyn_id_str))
            dyn_type = int(item_params.get('dyn_type', 1))
        else:
            # 否则手动构造
            dyn_id_str = str(item.get('id_str', ''))
            # 对于转发动态，rid_str 应该是原动态的ID
            orig_info = item.get('orig', {})
            orig_id = str(orig_info.get('id_str', '')) if orig_info else ''
            rid_str = orig_id if orig_id else dyn_id_str
            dyn_type = 1
        
        if not dyn_id_str:
            print(f"   ❌ 动态ID为空")
            return False
        
        # URL参数
        url_params = {
            'platform': 'web',
            'csrf': self.csrf
        }
        
        # JSON请求体（关键：使用JSON格式）
        json_payload = {
            'dyn_id_str': dyn_id_str,
            'dyn_type': dyn_type,
            'rid_str': rid_str
        }
        
        if self.debug:
            print(f"   [调试] DELETE URL: {url}")
            print(f"   [调试] URL Params: {url_params}")
            print(f"   [调试] JSON Payload: {json_payload}")

        try:
            # 关键：使用 json= 发送JSON格式数据，而不是 data=
            resp = self.session.post(url, params=url_params, json=json_payload, timeout=10)
            result = resp.json()
            
            if self.debug:
                print(f"   [调试] 删除响应: {result}")
            
            if result.get('code') == 0:
                print(f"   ✅ 已删除动态: {dyn_id_str}")
                return True
            else:
                msg = result.get('message', '未知错误')
                code = result.get('code')
                print(f"   ❌ 删除失败: {msg} (code: {code})")
                return False
                
        except Exception as e:
            print(f"   ❌ 删除异常: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False

    def process_dynamics(self, dry_run: bool = False):
        offset = None
        page = 1
        
        print(f"\n{'='*60}")
        print(f"🚀 B站抽奖动态清理工具 (JSON格式修复版)")
        print(f"👤 UID: {self.uid}")
        print(f"📝 模式: {'试运行' if dry_run else '正式删除'}")
        print(f"{'='*60}\n")
        
        while True:
            print(f"\n📄 第 {page} 页...")
            items, offset = self.get_dynamics(offset)
            
            if not items:
                print("✨ 完成")
                break
            
            for idx, item in enumerate(items, 1):
                try:
                    self.stats['total'] += 1
                    dyn_id = str(item.get('id_str', f'unknown_{idx}'))
                    
                    print(f"\n[{idx}] {dyn_id}")
                    
                    is_lottery, orig_id, method = self.is_lottery_dynamic(item)
                    
                    if not is_lottery:
                        print(f"   ⏭️  非抽奖动态")
                        continue
                    
                    self.stats['lottery'] += 1
                    print(f"   🎲 检测到抽奖 ({method})")
                    
                    is_expired, lottery_time = self.check_lottery_status(orig_id)
                    
                    if not is_expired:
                        status = lottery_time.strftime('%m-%d %H:%M') if lottery_time else '未开奖'
                        print(f"   ⏳ {status}")
                        continue
                    
                    self.stats['expired'] += 1
                    expire_str = lottery_time.strftime('%m-%d %H:%M') if lottery_time else '未知'
                    print(f"   🎯 已开奖 ({expire_str})")
                    
                    if dry_run:
                        print(f"   ⏸️  试运行，跳过")
                        continue
                    
                    if self.delete_dynamic(item):
                        self.stats['deleted'] += 1
                    else:
                        self.stats['failed'] += 1
                    
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
        
        print(f"\n{'='*60}")
        print(f"📊 统计: 总{self.stats['total']} | 抽奖{self.stats['lottery']} | 开奖{self.stats['expired']} | 删除{self.stats['deleted']} | 失败{self.stats['failed']}")
        print(f"{'='*60}")


def main():
    print("Bilibili Lottery Dynamic Cleaner - JSON Format")
    print("="*60)
    
    cookie_file = Path("./cookie.txt")
    if not cookie_file.exists():
        print("❌ 未找到 cookie.txt")
        sys.exit(1)
    
    cookie = cookie_file.read_text(encoding='utf-8').strip()
    if not cookie:
        print("❌ cookie为空")
        sys.exit(1)
    
    debug = input("调试模式? (y/n, 默认n): ").strip().lower() == 'y'
    
    try:
        cleaner = BilibiliLotteryCleaner(cookie, debug=debug)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)
    
    print("\n1. 试运行 (仅检测)")
    print("2. 正式运行 (删除)")
    choice = input("选择 (1/2, 默认1): ").strip() or "1"
    
    try:
        cleaner.process_dynamics(dry_run=(choice == "1"))
    except KeyboardInterrupt:
        print("\n\n⚠️ 中断")
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        import traceback
        traceback.print_exc()
    
    input("\n按回车退出...")


if __name__ == "__main__":
    main()