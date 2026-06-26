# Bilibili 抽奖动态清理工具

## 项目概述

自动扫描当前 B 站账号的所有动态，识别已开奖的互动抽奖动态并批量删除。

## 技术栈

- **Python 3** — 单文件脚本
- **requests** — HTTP 请求
- **browser-cookie3** — 从本地浏览器读取 bilibili cookie（不落盘）

## 项目结构

```
bili_delete/
├── delete.py          # 主脚本 ≈450 行
├── .gitignore         # 排除所有敏感文件
└── CLAUDE.md          # 本文件
```

## 运行方式

```bash
# 首次需要安装依赖
pip install requests browser-cookie3

# 运行（要求已在本地 Chrome/Edge/Firefox 登录 bilibili.com）
python delete.py
```

程序启动后会交互式询问：
1. 是否开启调试模式
2. 试运行（只检测不删除）还是正式运行

## 架构说明

### 类：`BilibiliLotteryCleaner`

| 方法 | 作用 |
|---|---|
| `get_dynamics()` | 分页拉取用户全部动态（调用 B 站 API） |
| `is_lottery_dynamic()` | 检测动态是否为抽奖（关键词 + API 双重验证） |
| `check_lottery_status()` | 检查抽奖是否已开奖 |
| `delete_dynamic()` | 删除单条动态（JSON 格式 POST） |
| `process_dynamics()` | 主循环：遍历 → 检测 → 开奖检查 → 删除 |

### 关键函数

- `get_bilibili_cookies()` — 从本地浏览器加密存储中读取 bilibili cookie，仅存内存，不写任何文件
- `main()` — 交互入口

### 检测策略（`is_lottery_dynamic`）

1. **深度递归搜索** — 递归扫描 API 返回的 JSON 中是否有 `lottery`/`抽奖` 等关键词
2. **模块字段检测** — `additional.type` 字段匹配 `lottery` 特征
3. **正则匹配** — 动态文案匹配 `抽奖`、`关注+转发`、`转关` 等模式
4. **API 验证** — 调用 B 站抽奖状态接口确认

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

删除接口需 `dyn_id_str` + `rid_str` + `dyn_type` 三参数，以 `json=` 而非 `data=` 发送。

## 已知问题

- 依赖 `browser-cookie3` 解密浏览器 cookie，需确保浏览器配置文件未被加密/权限受限
- B 站 API 可能对高频率请求限流，脚本内置 2-4 秒随机延迟

## 维护者

- Git 远端: https://gitee.com/adam121389/bili_delete.git
- 分支策略: 单分支 `main`，直接推送
