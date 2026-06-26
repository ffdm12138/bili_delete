# 🧹 Bilibili 抽奖动态清理工具

自动扫描当前 B 站账号的全部动态，识别已开奖的互动抽奖动态并批量删除。

## 快速开始

```bash
# 1. 安装依赖
pip install requests pycryptodome pywin32

# 2. 试运行（仅扫描不删除）
python delete.py

# 3. 确认无误后正式删除
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
  --dry-run       试运行（默认）：只扫描不删除
  --execute       执行删除模式
  --debug         开启调试输出
  --kill-browser  允许强制关闭浏览器进程（Cookie 被占用时）
  --yes           跳过删除前的二次确认
```

### 使用示例

```bash
# 试运行
python delete.py

# 调试模式试运行
python delete.py --debug

# 直接删除（两次确认：先扫描再确认）
python delete.py --execute

# 跳过确认一键删除（确认已理解风险时使用）
python delete.py --execute --yes
```

## 功能特性

- **🔒 Cookie 仅内存暂存** — 从浏览器 SQLite 数据库读取后暂存于内存，不会写入项目目录
- **🎯 四层检测** — 关键词递归搜索 + 字段匹配 + 正则 + API 验证
- **⏳ 智能跳过** — 未开奖动态自动保留，已开奖带 2 小时安全缓冲才判定
- **🔐 安全删除** — 缺少 B 站返回的删除参数时跳过，不猜参数（宁可漏删）
- **⏸️ 二次确认** — 正式删除前先列出全部候选，输入 `DELETE` 才执行
- **🕐 随机延迟** — 内置 2-4 秒间隔，降低限流风险

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
                                                     拉取动态列表
                                                              ↓
                                             本地检测是否为抽奖动态
                                              ├─ 关键词递归搜索
                                              ├─ 模块字段匹配
                                              ├─ 文案正则匹配
                                              └─ (API 验证延迟到开奖判断)
                                                              ↓
                                             检查开奖状态（+2h 安全缓冲）
                                                              ↓
                                             未开奖 → 跳过 / 已开奖 → 二次确认 → 删除
```

## 注意事项

- 强杀浏览器可能导致**未保存的页面内容丢失**，`--kill-browser` 默认关闭
- 非互动抽奖（如转发抽奖）可能无法完全识别
- 如果所有浏览器 cookie 均过期，请重新登录 bilibili.com 后重试

## 维护

- 仓库: https://gitee.com/adam121389/bili_delete.git
- 分支: `main`
