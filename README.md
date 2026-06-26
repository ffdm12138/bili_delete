# 🧹 Bilibili 抽奖动态清理工具

自动扫描当前 B 站账号的全部动态，识别已开奖的互动抽奖动态并批量删除。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 试运行（仅扫描不删除）
python delete.py

# 3. 扫描后确认删除（候选表 → 输入 DELETE）
python delete.py --execute

# 4. 扫描后直接删除（候选表 → 3 秒倒计时）
python delete.py --execute --yes
```

程序会自动从本地浏览器读取 cookie，**不在项目目录留存任何凭证文件**。

## 支持的浏览器

| 浏览器 | 自动读取 | 说明 |
|---|---|---|
| **360 极速浏览器 X** | ✅ | AES-GCM 解密 + 32 字节前缀剥离 |
| **Chrome** | ✅ | 直接读取 SQLite，标准 AES-GCM 解密 |
| **Edge** | ✅ | 同 Chrome，统一解密方式 |
| **Firefox** | ✅ | 通过 `browser-cookie3`（需额外安装） |

## 命令行参数

```
python delete.py [选项]

选项：
  --dry-run                显式试运行（默认即为试运行，此参数可选）
  --execute                执行删除模式：扫描 → 列候选 → 输入 DELETE → 删除
  --yes                    跳过确认（与 --execute 搭配）：扫描 → 列候选 → 3秒倒计时 → 删除
  --debug                  开启调试输出（不交互询问）
  --non-interactive        非交互模式（CI/自动化），强制 dry-run，禁止删除
  --kill-browser           允许强制关闭浏览器进程（需输入 YES 二次确认）
  --export-candidates      导出脱敏候选列表为 JSON 文件
  --include-invalid-repost 同时处理原动态已失效的转发动态（默认关闭）
```

### 使用示例

```bash
# 试运行（默认）
python delete.py

# 显式试运行
python delete.py --dry-run

# 调试模式试运行
python delete.py --debug

# 扫描后确认删除（候选表 → 输入 DELETE）
python delete.py --execute

# 扫描后直接删除（候选表 → 3 秒倒计时）
python delete.py --execute --yes

# CI 环境只扫不删
python delete.py --non-interactive

# 导出候选列表
python delete.py --export-candidates

# 同时清理失效转发动态
python delete.py --include-invalid-repost
```

## 功能特性

- **🔒 Cookie 仅内存暂存** — 从浏览器 SQLite 数据库读取后暂存于内存，不会写入项目目录
- **🎯 五层检测** — 关键词递归搜索 + 字段匹配 + 正则 + 失效转发检测 + API 验证
- **⏳ 智能跳过** — 未开奖动态自动保留，已开奖带 2 小时安全缓冲才判定
- **🛡️ 安全优先** — 状态不明（UNKNOWN）一律不删；未知状态值不靠时间猜；扫描阶段绝不删除
- **🔐 安全删除** — 缺少 B 站返回的删除参数时跳过，不猜参数（宁可漏删）
- **📋 先展示后删除** — 候选表先于任何删除操作；`--execute --yes` 也在候选表后才倒计时
- **⏸️ 二次确认** — 正式删除前先列出全部候选再确认
- **🕐 随机延迟** — 内置 2-4 秒间隔，降低限流风险
- **📁 候选导出** — 可选导出脱敏 JSON（不含 Cookie/SESSDATA/bili_jct）

## Cookie 安全性说明

- 脚本会**临时复制浏览器 Cookie SQLite 数据库**到系统临时目录（`%TEMP%`），解密后立即删除
- Cookie 值**存于内存**，不会写入项目目录或任何持久化文件
- 如需手动管理，可添加 `--kill-browser` 让脚本自动关闭浏览器进程（默认不杀）

## 项目结构

```
bili_delete/
├── delete.py            # 主脚本
├── README.md            # 本文件
├── CLAUDE.md            # 技术文档
├── requirements.txt     # Python 依赖
└── .gitignore           # 排除敏感文件
```

## 工作原理

```
浏览器 Cookies SQLite → 临时复制到 tmpdir → AES-GCM 解密 → 内存暂存
                                                              ↓
                                          ┌─── 扫描阶段（绝不删除）───┐
                                          │  分页拉取全部动态          │
                                          │  本地检测抽奖 / 失效转发   │
                                          │  查询开奖状态（API + 缓存） │
                                          │  UNKNOWN → 安全跳过       │
                                          │  收集全部候选到内存        │
                                          └──────────────────────────┘
                                                              ↓
                                          ┌─── 展示阶段 ──────────────┐
                                          │  打印候选表格              │
                                          │  可选导出脱敏 JSON         │
                                          └──────────────────────────┘
                                                              ↓
                                          ┌─── 执行阶段 ──────────────┐
                                          │  dry-run → 跳过           │
                                          │  --execute → 输入 DELETE  │
                                          │  --execute --yes → 3秒    │
                                          │  批量删除（含随机延迟）    │
                                          └──────────────────────────┘
```

## 风险声明

- ⚠️ **删除不可逆**：本工具删除的动态无法恢复
- 🔍 **首次使用务必 dry-run**：先 `python delete.py` 查看候选，确认无误再 `--execute`
- 📋 **建议导出候选**：`--export-candidates` 可保存候选列表供事后核对
- 🛡️ **安全设计**：状态不明（UNKNOWN）的动态绝对不删；未知状态值不靠时间猜测
- ⏱️ **开奖缓冲**：开奖时间 +2 小时安全缓冲后才判定为已开奖

## 常见问题

| 问题 | 原因 | 解决 |
|---|---|---|
| 未登录 / Cookie 过期 | `SESSDATA` 或 `bili_jct` 失效 | 重新登录 bilibili.com 后重试 |
| 浏览器占用 Cookie DB | Chrome/Edge/360 正在运行 | 关闭浏览器，或添加 `--kill-browser` |
| B 站风控 / 限流 | 请求过快被限 | 脚本内置随机延迟，等待后重试 |
| 状态不明 (UNKNOWN) | API 返回无法识别的状态值 | 这是安全保护，不会自动删除；等待或手动检查 |
| 找不到 bilibili cookie | 浏览器未登录或 cookie 数据库路径不匹配 | 确认已在浏览器中登录 bilibili.com |

## 注意事项

- 强杀浏览器可能导致**未保存的页面内容丢失**，`--kill-browser` 默认关闭，且需输入 YES 确认
- 非互动抽奖（如转发抽奖）可能无法完全识别
- 失效转发动态默认不处理，需 `--include-invalid-repost` 显式启用
- 如果所有浏览器 cookie 均过期，请重新登录 bilibili.com 后重试

## 维护

- 仓库: https://gitee.com/adam121389/bili_delete.git
- 分支: `main`
