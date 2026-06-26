# Bilibili 抽奖动态清理工具

## 项目概述

自动扫描当前 B 站账号的全部动态，识别已开奖的互动抽奖动态并批量删除。可选检测原动态已失效的转发动态（需显式传入 `--include-invalid-repost`）。

## 技术栈

- **Python 3.13+** — ~1,600 行，纯函数 + 类设计
- **requests** — HTTP 请求，带 retry/backoff
- **pycryptodome + pywin32** — AES-GCM 解密 Chromium 系浏览器 cookie
- **browser-cookie3** — 备用读取 Firefox cookie
- **pytest** — 132 个单元/集成测试

## 项目结构

```
bili_delete/
├── delete.py            # 主脚本（~1,600 行）
├── tests/
│   ├── __init__.py
│   └── test_delete.py   # 132 个测试用例
├── README.md            # 用户文档
├── CLAUDE.md            # 本文件（技术文档）
├── requirements.txt     # Python 依赖
└── .gitignore           # 排除敏感文件 + candidates_*.json
```

## 运行方式

```bash
pip install -r requirements.txt
python delete.py                          # 默认 dry-run，只扫描
python delete.py --execute                # 扫描后列候选，输入 DELETE 确认
python delete.py --execute --yes          # 跳过确认，3 秒倒计时后删除
python delete.py --non-interactive        # CI 模式，强制 dry-run（禁止删除）
python delete.py --debug                  # 调试输出（不交互询问）
python delete.py --export-candidates      # 导出脱敏候选列表 JSON
python delete.py --kill-browser           # 允许强制关闭浏览器（需输入 YES）
```

## 架构说明

### 异常体系

```
BiliError (base)
├── ApiError          — B 站返回非 0 code
├── AuthError         — 未登录 / Cookie 过期
├── RateLimitError    — 429 / 风控
└── NetworkError      — 超时 / 连接失败
```

`retry_request()` 包装所有 HTTP 调用：3 次重试 + 指数退避 + 随机抖动，可重试错误（timeout/connection/429/502/503/504）与非可重试错误分开处理。

### 模型

| 类/枚举 | 作用 |
|---|---|
| `LotteryState` | `NOT_LOTTERY` / `ACTIVE` / `FINISHED` / `UNKNOWN`（**UNKNOWN 一律不删**） |
| `CandidateInfo` | 待删除候选的完整信息 dataclass，含脱敏导出 `sanitized_dict()` |

### 纯函数（可独立测试，无网络依赖）

| 函数 | 作用 |
|---|---|
| `parse_cookie_string()` | 解析 Cookie header 字符串 |
| `parse_lottery_state()` | 解析 API 返回的抽奖状态 → `LotteryState` |
| `deep_search_lottery()` | 递归搜索 JSON 中的抽奖关键词 |
| `is_lottery_dynamic()` | 本地检测抽奖（关键词 + 字段 + 正则） |
| `is_repost_original_invalid()` | 检测转发动态的原动态是否已失效/删除 |
| `extract_text_preview()` | 提取动态正文预览 |
| `extract_publish_time()` | 提取动态发布时间 |
| `_sanitize_for_log()` | UID 脱敏，仅显示后 4 位 |
| `_fmt_time_short()` / `_fmt_time_iso()` | 上海时区时间格式化 |

### 类：`BilibiliLotteryCleaner`

| 方法 | 作用 |
|---|---|
| `get_dynamics()` | 分页拉取用户动态，失败抛 `BiliError`（不再静默返回空列表） |
| `get_lottery_info()` | 查询抽奖状态，带缓存，网络错误缓存 None 避免重复请求 |
| `check_lottery_status()` | 返回 `(LotteryState, lottery_time, reason_detail)` |
| `delete_dynamic()` | 删除单条，返回 `(bool, error_msg)`（仅当 `item.params` 完整时执行） |
| `process_dynamics()` | 主循环，支持 dry-run / execute / require_confirm 三种路径 |
| `_print_candidates_table()` | 打印候选表格（dyn_id、抽奖 ID、时间、识别原因、预览、链接） |
| `_export_candidates_json()` | 导出脱敏候选 JSON（白名单字段，不含 Cookie） |
| `_confirm_and_delete()` | 打印摘要 → 输入 `DELETE` → 批量删除（使用 `CandidateInfo.raw_item`） |

### cookie 读取策略

`get_bilibili_cookies()` 依次尝试：

1. **360 极速浏览器 X** — AES-GCM + 32 字节前缀剥离
2. **Chrome** — 标准 AES-GCM 解密
3. **Edge** — 标准 AES-GCM 解密
4. **Firefox** — 通过 `browser-cookie3`

SQL 查询: `host_key LIKE '%bilibili.com'`（覆盖所有子域名，不限于 `.bilibili.com`）
`_copy_db()` 三步绕锁：直接复制 → PowerShell → 用户手动关浏览器（`--kill-browser` 才杀进程）
临时文件在 `finally` 块中清理。

### 检测策略

五层检测：
1. **递归搜 JSON** — 匹配 `lottery`/`抽奖`/`choujiang` 等关键词
2. **字段匹配** — `additional.type` 含 `lottery`
3. **正则** — 文案匹配 `抽奖`、`关注+转发`、`转关`、`互动抽奖`、`开奖`
4. **失效转发** — 检测 `is_deleted`、空 modules、已失效 badge、内容失效标记
5. **API 验证** — `check_lottery_status()` 中统一查询（带缓存）

### 开奖状态判断优先级

`parse_lottery_state()`:
1. 显式字段（key 存在判断，非 `or`，避免 0/False 被吃掉）：
   - `"lottery_status"` 优先于 `"status"`
   - `1` / `true` / `finished` / `closed` / `drawn` → `FINISHED`
   - `0` / `false` / `active` / `open` / `ongoing` → `ACTIVE`
   - **无法识别的显式值 → `UNKNOWN`，不走时间 fallback**
     （新/未知状态可能代表"已取消""异常"等，时间推断不安全）
2. 仅当**完全没有显式 status 字段**时，才允许时间 fallback：
   - `lottery_time` + 2h 安全缓冲 < 当前时间 → `FINISHED`（标记 `time_fallback`）
   - `lottery_time` 存在但未过期 → `ACTIVE`
3. 没有任何信息 → `UNKNOWN`（**绝对不删**）

所有 datetime 为 timezone-aware（`Asia/Shanghai`, UTC+8）。

### 安全设计

- Cookie 仅内存暂存，不落项目目录
- 删除必需 `item.params` 含 `dyn_id_str`/`rid_str`/`dyn_type`，缺少时跳过
- `LotteryState.UNKNOWN` 一律不删除（宁可漏删，不错删）
- 开奖时间 +2h 安全缓冲，时间推断标记 `time_fallback`
- 二次确认：列出全部候选，键入 `DELETE` 才执行
- `--execute --yes` 仍有 3 秒倒计时
- `--non-interactive` 模式下禁止 `--execute`
- `--kill-browser` 需输入 `YES` 二次确认
- UID 仅显示后 4 位（`_sanitize_for_log`）
- 导出候选 JSON 使用白名单字段，不含 Cookie / SESSDATA / bili_jct / csrf
- 临时文件在 `finally` 中清理
- candidates JSON 文件已加入 `.gitignore`

## CLI 模式一览

| 命令 | 扫描 | 列候选 | 确认 | 删除 |
|---|---|---|---|---|
| `python delete.py` | ✅ | ✅ | — | — |
| `python delete.py --execute` | ✅ | ✅ | 输入 DELETE | ✅ |
| `python delete.py --execute --yes` | ✅ | ✅ | 3 秒倒计时 | ✅ |
| `python delete.py --non-interactive` | ✅ | ✅ | — | ❌ 禁止 |

## B 站 API 要点

| 接口 | 用途 |
|---|---|
| `x/polymer/web-dynamic/v1/feed/space` | 获取用户动态列表 |
| `vc.bilibili.com/lottery_svr/.../lottery_notice` | 查询抽奖状态 |
| `x/dynamic/feed/operate/remove` | 删除动态（POST JSON） |

## 测试

```bash
pytest tests/ -v        # 131 个测试，~0.3s 完成（已消除真实 sleep）
```

测试覆盖：
- Cookie 解析、抽奖检测（深层搜索/字段/正则）、失效转发检测
- `LotteryState` 全分支 + `or` 吃 0/False 回归
- `delete_dynamic` total function（非 dict、None params、HTTPError、异常）
- `_decrypt_chromium_cookies` 成功路径回归
- `CandidateInfo` 脱敏导出
- CLI argparse 全模式 + `main()` 冲突退出
- Mock API 错误处理 + API code 字符串安全转换
- `process_dynamics` 集成：dry_run 不删、UNKNOWN 不删、Active 不删、扫描失败禁删
- 异常继承体系、UID 脱敏、时间格式化

## 维护

- **每次代码改动完成后，必须运行 `python pack.py` 打包 zip**（用于同步到其他环境 / 绕过 git 推送延迟）。
- 仓库: https://gitee.com/adam121389/bili_delete.git
- 仓库: https://github.com/ffdm12138/bili_delete
- 分支: `main`
