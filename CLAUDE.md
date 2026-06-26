# Bilibili 抽奖动态清理工具

## 项目概述

自动扫描当前 B 站账号的全部动态，识别已开奖的互动抽奖动态并批量删除。

## 技术栈

- **Python 3** — 单文件脚本（≈550 行）
- **requests** — HTTP 请求
- **browser-cookie3** — 从 Chrome/Edge/Firefox 读取 cookie（不落盘）
- **pycryptodomex + pywin32** — 解密 360 极速浏览器 X 的加密 cookie

## 项目结构

```
bili_delete/
├── delete.py       # 主脚本
├── README.md       # 用户文档
├── CLAUDE.md       # 本文件（技术文档）
└── .gitignore      # 排除所有敏感文件
```

## 运行方式

```bash
pip install requests browser-cookie3 pycryptodomex pywin32
python delete.py
```

程序自动从浏览器读取 cookie，交互式询问调试模式和运行模式（试运行/正式删除）。

## 架构说明

### 类：`BilibiliLotteryCleaner`

| 方法 | 作用 |
|---|---|
| `get_dynamics()` | 分页拉取用户全部动态（调用 B 站 API） |
| `is_lottery_dynamic()` | 检测动态是否为抽奖（4 层策略） |
| `check_lottery_status()` | 检查抽奖是否已开奖 |
| `delete_dynamic()` | 删除单条动态（JSON 格式 POST） |
| `process_dynamics()` | 主循环：遍历 → 检测 → 开奖检查 → 删除 |

### 关键函数

| 函数 | 作用 |
|---|---|
| `get_bilibili_cookies()` | 入口：优先 360 → Chrome/Edge/Firefox 自动读取 cookie |
| `_try_360chrome_x()` | 读取 360 极速浏览器 X 的加密 cookie（32 字节前缀特殊处理） |
| `main()` | 交互式运行入口 |

### cookie 读取策略

1. **360 极速浏览器 X**（首选）— 直接读取 SQLite 数据库，AES-GCM 解密（360 额外加了 32 字节前缀需剥离），通过 PowerShell 绕过文件锁
2. **Chrome** — `browser-cookie3.chrome()`
3. **Edge** — `browser-cookie3.edge()`
4. **Firefox** — `browser-cookie3.firefox()`

### 检测策略（`is_lottery_dynamic`）

1. **深度递归搜索** — 递归扫描 API 返回的 JSON 中是否有 `lottery`/`抽奖` 等关键词
2. **模块字段检测** — `additional.type` 字段匹配 `lottery` 特征
3. **正则匹配** — 动态文案匹配 `抽奖`、`关注+转发`、`转关` 等模式
4. **API 验证** — 调用 B 站抽奖状态接口确认

### 删除逻辑（`delete_dynamic`）

POST 方式发送 JSON 请求，需要三个参数：
- `dyn_id_str` — 动态 ID
- `rid_str` — 原动态 ID（转发用原动态）
- `dyn_type` — 动态类型

使用 `json=` 而非 `data=` 发送请求体。

### 数据安全

- cookie **不落盘**：浏览器 → 内存 → 用完即弃
- `.gitignore` 排除了 `cookie.txt`、`debug_*.json`
- 项目仓库不含任何用户凭证

## B 站 API 要点

| 接口 | 用途 |
|---|---|
| `x/polymer/web-dynamic/v1/feed/space` | 获取用户动态列表 |
| `vc.bilibili.com/lottery_svr/.../lottery_notice` | 查询抽奖状态 |
| `x/dynamic/feed/operate/remove` | 删除动态（POST JSON） |

## 已知问题

- 360 极速浏览器 X 有**常驻后台进程**，拷贝 cookie DB 时可能需 PowerShell 绕锁
- B 站 API 可能对高频率请求限流，脚本内置 2-4 秒随机延迟
- 非互动抽奖（如转发抽奖）可能无法完全识别
- 如所有浏览器 cookie 均过期，需重新登录

## 维护者

- Git 远端: https://gitee.com/adam121389/bili_delete.git
- 分支策略: 单分支 `main`，直接推送
