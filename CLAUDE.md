# Bilibili 抽奖动态清理工具

## 项目概述

自动扫描当前 B 站账号的全部动态，识别已开奖的互动抽奖动态并批量删除。

## 技术栈

- **Python 3** — 单文件脚本（≈600 行）
- **requests** — HTTP 请求
- **pycryptodome + pywin32** — AES-GCM 解密 Chromium 系浏览器 cookie
- **browser-cookie3** — 备用读取 Firefox cookie

## 项目结构

```
bili_delete/
├── delete.py            # 主脚本
├── README.md            # 用户文档
├── CLAUDE.md            # 本文件（技术文档）
├── requirements.txt     # Python 依赖
└── .gitignore           # 排除所有敏感文件
```

## 运行方式

```bash
pip install -r requirements.txt
python delete.py            # 试运行
python delete.py --execute  # 正式删除
```

## 架构说明

### 类：`BilibiliLotteryCleaner`

| 方法 | 作用 |
|---|---|
| `get_dynamics()` | 分页拉取用户全部动态 |
| `is_lottery_dynamic()` | 本地检测抽奖（关键词 + 字段 + 正则，不做 API） |
| `get_lottery_info()` | 查询抽奖状态，带缓存避免重复请求 |
| `check_lottery_status()` | 判断是否已开奖（优先状态字段，无则时间 + 2h 安全缓冲） |
| `delete_dynamic()` | 删除单条（仅当 `item.params` 含 `dyn_id_str` 时才删） |
| `process_dynamics()` | 主循环，支持二次确认 |
| `_confirm_and_delete()` | 打印候选列表，输入 `DELETE` 才执行 |

### cookie 读取策略

`get_bilibili_cookies()` 依次尝试：

1. **360 极速浏览器 X** — AES-GCM + 32 字节前缀剥离
2. **Chrome** — 标准 AES-GCM 解密
3. **Edge** — 标准 AES-GCM 解密
4. **Firefox** — 通过 `browser-cookie3`

`_copy_db()` 三步绕锁：直接复制 → PowerShell → 用户手动关浏览器（`--kill-browser` 才杀进程）

### 检测策略

四层检测（本地，不调 API）：
1. **递归搜 JSON** — 匹配 `lottery`/`抽奖` 等关键词
2. **字段匹配** — `additional.type` 含 `lottery`
3. **正则** — 文案匹配 `抽奖`、`关注+转发`、`转关`
4. **API 验证** — 延迟到 `check_lottery_status` 中统一查询（带缓存）

### 安全设计

- Cookie 仅内存暂存，不自持永久文件
- 删除必需 `item.params.dyn_id_str`，缺少时跳过（宁可漏删）
- 开奖时间 +2h 安全缓冲，防止延迟开奖误删
- 二次确认：列出全部候选，键入 `DELETE` 才执行
- `--kill-browser` 默认关闭

## B 站 API 要点

| 接口 | 用途 |
|---|---|
| `x/polymer/web-dynamic/v1/feed/space` | 获取用户动态列表 |
| `vc.bilibili.com/lottery_svr/.../lottery_notice` | 查询抽奖状态 |
| `x/dynamic/feed/operate/remove` | 删除动态（POST JSON） |

## 维护

- 仓库: https://gitee.com/adam121389/bili_delete.git
- 分支: `main`
