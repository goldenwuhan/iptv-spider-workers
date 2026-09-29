# IPTV Spider — Cloudflare Workers (Python) edition

> A pure-Python reimplementation of the portable core of **IPTV Spider**
> (the fnOS / 飞牛 app `fnnas.iptv-spider` v2.1.8), built to run on
> **Cloudflare Workers Python**.

---

## 1. 这个 fpk 到底是什么？

`fnnas.iptv-spider-2.1.8-x86.fpk` 是一个 **飞牛(fnOS) 应用安装包**，结构如下：

```
fpk (tar.gz)
 ├─ manifest                      应用元信息 (端口 50089, 作者 cqshushu)
 ├─ cmd/main                     启动脚本 (优先跑二进制, 否则跑 wsgi.py)
 ├─ app.tgz (内层 tar.gz)
 │   ├─ server/iptv-spider        87 MB 的 ELF 二进制  ← 真正的程序
 │   └─ server/qqwry.dat          26 MB 纯真 IP 库
```

把二进制解开后确认：它**不是原生 C 程序，而是用 PyInstaller 冻结的
Python 3.11 程序**，并且性能关键部分用 **Cython 编译成了 `.so`**
(`crawler.so` / `database.so` / `logic.so` / `playlist.so`)，还内置了
**79 MB 的 `ffprobe` 二进制**。

也就是说：

- 应用本体**确实是 Python**，只是被冻结/编译了；
- 但里面的**核心逻辑是 Cython 编译件 + 原生二进制 + 26MB 数据文件**，
  没有可阅读的 `.py` 源码可读。

## 2. 为什么不能“原样”搬到 Cloudflare Workers？

| 原版依赖 | Workers 能否用 | 原因 |
|---|---|---|
| Cython `.so` 扩展 | ❌ | Workers 跑在 Wasm/V8，禁止原生扩展 |
| 内置 `ffprobe` 79MB | ❌ | 不能带二进制 / 不能起子进程 |
| `qqwry.dat` 26MB | ❌ | 超过 Workers 体积上限，也没有文件系统 |
| SQLite (`_sqlite3`) | ❌ | 原生扩展；改用 **D1** |
| 常驻 Flask 服务 + 后台调度线程 | ❌ | Workers 是无状态、短时执行 |
| Flask / Werkzeug / Jinja 全套 UI | ⚠️ | Workers 只跑 handler，没有网页目录 |

**结论**：原版无法 1:1 迁移。但我们可以把**可移植的核心**用纯 Python 重写，
用 Workers 原生能力替代：

| 原版功能 | Workers 版替代 |
|---|---|
| M3U 解析 / 生成 | ✅ 纯 stdlib 实现 (`iptv_core.py`) |
| 订阅源抓取 | ✅ Workers 全局 `fetch` |
| 源存活校验 (ffprobe) | ✅ HTTP HEAD / 范围 GET 探测 |
| SQLite 存储 | ✅ **D1** 绑定 (或本地内存/KV) |
| 分组 / 排序 / 统计 | ✅ 纯 Python |
| IP 地理定位 (qqwry) | ➖ 暂略（可接第三方 IP API） |
| 分辨率/测速 (ffprobe) | ➖ 暂略（Workers 无法跑 ffmpeg） |
| 完整网页 UI / 调度器 | ➖ 暂略（可改做 API + 前端） |

## 3. 项目结构

```
iptv_spider_workers/
├── iptv_core.py     # 纯 Python 核心：M3U 解析/生成、分组、校验、订阅抓取
├── collector.py     # 自动采集器：抓取 cqshushu 的酒店/组播/咪咕源
├── webui.py         # Web 控制台页面（状态/采集/IP管理/订阅/日志/设置）
├── auth.py          # 密码登录 + HMAC 签名 Cookie 会话
├── storage.py       # 存储抽象：MemoryStore(本地) + D1Store(Workers)
│                    #   （含 sources / settings / logs 的读写）
├── router.py        # 与环境无关的请求路由（HTTP 无关，便于测试）
├── main.py          # Cloudflare Workers 入口（同时提供 on_fetch 与 Default）
├── local_dev.py     # 纯 stdlib 本地开发服务器（不需要 wrangler）
├── wrangler.toml    # Workers 配置（D1 + KV + Cron）
├── migrations/
│   ├── 0001_init.sql    # channels 表
│   └── 0002_web.sql     # sources / settings / logs 表
└── README.md
```

所有模块**只依赖 Python 标准库**，因此可以：

- 在本地用 `local_dev.py` 直接跑、用 `curl` 测；
- 原样部署到 Cloudflare Workers（D1 用绑定，本地用内存）。

## 4. 本地运行 / 测试

```bash
cd iptv_spider_workers
python local_dev.py                 # 默认 http://127.0.0.1:8000
python local_dev.py --port 9000 --data ./iptv_data.json
```

快速试一下：

```bash
# 健康检查
curl -s http://127.0.0.1:8000/ | head

# 手动加一个频道
curl -s -X POST http://127.0.0.1:8000/api/channels \
  -H 'Content-Type: application/json' \
  -d '{"name":"测试台","url":"http://example.com/t","group":"测试"}'

# 导入一段 M3U
curl -s -X POST http://127.0.0.1:8000/api/import \
  -H 'Content-Type: application/json' \
  -d '{"m3u":"#EXTM3U\n#EXTINF:-1 group-title=央视,CCTV-1\nhttp://example.com/cctv1\n"}'

# 拉取远程订阅（换成你自己的 M3U 地址）
curl -s -X POST http://127.0.0.1:8000/api/subscribe \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/playlist.m3u"}'

# 校验全部源（HTTP 探测）
curl -s -X POST http://127.0.0.1:8000/api/validate

# 生成 M3U / TXT 播放列表
curl -s http://127.0.0.1:8000/m3u
curl -s http://127.0.0.1:8000/txt
```

## 5. 部署到 Cloudflare Workers

```bash
# 0) 登录（Python Workers 需要开启兼容标志，见 wrangler.toml 已配置）
npx wrangler login

# 1) 创建 D1 数据库，把输出的 database_id 填进 wrangler.toml
npx wrangler d1 create iptv-spider

# 2) 创建 KV 命名空间用于缓存播放列表，把输出的 id 填进 wrangler.toml
npx wrangler kv namespace create iptv-spider-cache

# 3) 初始化表（本地 + 远程各跑一次）
npx wrangler d1 execute iptv-spider --local --file=./migrations/0001_init.sql
npx wrangler d1 execute iptv-spider --remote --file=./migrations/0001_init.sql

# 4) 部署
npx wrangler deploy
```

- `wrangler.toml` 里已开启 `compatibility_flags = ["python_workers"]`、
  `[[d1_databases]]` 绑定、`[[kv_namespaces]]` 绑定（CACHE），以及
  `[triggers] crons = ["0 9 * * *"]`（每天 09:00 UTC 自动校验源）。
- **不配置 D1 也能跑**——会自动退化为内存存储（但冷启动会清空数据，仅供演示）。
- **不配置 KV 也能跑**——`main.py` 做了兼容：没有 CACHE 绑定时直接生成播放列表，
  不读写缓存。
- 想本地连 D1 / 看真实行为，用 `npx wrangler dev` 起开发服务器。

## 5.1 定时校验（Cron Trigger）

`wrangler.toml` 已配置 `crons = ["0 9 * * *"]`，部署后 Cloudflare 会每天 09:00 UTC
自动调用 `main.scheduled()`，对数据库里所有源跑一遍 HTTP 存活校验并写回状态。
手动触发（或改频率）后重新 `wrangler deploy` 即可。

也可手动跑一次校验：

```bash
curl -s -X POST https://<你的子域>.workers.dev/api/validate
```

## 5.2 播放列表缓存（KV）

`GET /m3u` 和 `GET /txt` 的结果会按查询串缓存到 KV（TTL 600 秒）。新增 / 导入 /
删除 / 校验等写操作后，`main.py` 会自动清掉相关缓存键，避免拿到过期列表。
缓存缺失或绑定不存在时都安全降级为实时生成。

## 5.3 排错笔记（真实踩坑记录，按发生顺序）

以下都是本项目在线上实际遇到并解决的问题。

### ① 入口约定随 `compatibility_date` 变化 —— 最关键的一个

Python Workers 有**两套**入口约定，运行时按 `compatibility_date` 选择：

| compatibility_date | 入口约定 | `workers` 模块来源 |
|---|---|---|
| 较新（约 2025-08 起） | `class Default(WorkerEntrypoint)` + `fetch(self, request)`，绑定走 `self.env` | 必须由 pywrangler 打包进 `python_modules/` |
| 较旧（本项目固定 `2025-01-01`） | 模块级 `on_fetch(request, env)`，或入口对象上的 `on_fetch` 方法 | 运行时内置 |

- 新日期但没打包 SDK → **部署失败**
  `ModuleNotFoundError: No module named 'workers'` **[10021]**
  （提示需 workers-py >= 1.9 或 `disable_python_external_sdk`）
- 旧日期但只写了 class → **请求期失败**
  `TypeError: Method on_fetch does not exist` → **1101**

**本项目的解法**：固定 `compatibility_date = "2025-01-01"`（用内置 SDK，
`npx wrangler deploy` 直接可用，不需要 uv / pywrangler），并在 `main.py`
同时提供三种入口——模块级 `on_fetch()`、`Default.on_fetch()`、`Default.fetch()`，
共享同一份 `_handle()` 实现。

> 不要为了"追新"改 compatibility_date，会切到另一套约定并连带要求打包 SDK。

### ② D1 返回的是 Pyodide JS 代理，不是 Python dict

```python
rows = res.get("results", [])   # ❌ JsProxy 没有 .get()，也不可迭代
```
会报 `TypeError: 'pyodide.ffi.JsProxy' object is not iterable`。
`storage.py` 里的 `_to_py()` / `_rows()` 先用 `to_py()` 归一化。

### ③ Python `None` 过 FFI 变成 JS `undefined`，D1 拒绝

```
D1_TYPE_ERROR: Type 'undefined' not supported for value 'undefined'
```
所有可空列在 `bind()` 前转成具体值（`0` / `0.0` / `""`）。

### ④ 旧模式下 `fetch` 不是 Python 全局

`NameError: name 'fetch' is not defined`。
`main.py` 的 `_get_fetch()` 依次尝试 Python 全局和 `from js import fetch`。

### ⑤ D1 表未建

`no such table: channels`。执行：
```bash
npx wrangler d1 execute iptv-spider --remote --file=./migrations/0001_init.sql
```

### ⑥ 诊断手段

- 所有请求被 try/except 包住，异常返回 **500 + JSON(type/message/traceback)**，
  不会退化成看不到原因的 1101。
- 实时堆栈：`npx wrangler tail`（本项目就是靠它定位到 `Method on_fetch does not exist`）。

## 6. API 一览

**公开**（无需登录，供播放器直接使用）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/m3u` , `/txt` | 生成 M3U / TXT 播放列表（`?group=` 过滤） |
| GET | `/api/health` | 健康检查 |

**需登录**（未登录时页面 302 → `/login`，写接口返回 401）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/stats` | 状态页数据（IP/频道数、资源库统计、参数） |
| GET | `/api/sources` | 源服务器列表（`?kind=hotel/multicast/migu`） |
| DELETE | `/api/sources/{id}` | 删除源服务器 |
| POST | `/api/collect` | 触发采集 `{kind, pages, max_sources}` |
| GET | `/api/subscriptions` | 订阅列表 |
| POST | `/api/subscribe` | 添加并抓取订阅 URL |
| DELETE | `/api/subscriptions/{id}` | 删除订阅及其频道 |
| GET / POST | `/api/settings` | 读取 / 保存设置（含改密码） |
| GET / DELETE | `/api/logs` | 日志列表 / 清空 |
| GET | `/api/channels` | 列出频道（`?group=` / `?status=`） |
| POST | `/api/channels` | 新增单个频道 |
| DELETE | `/api/channels` | 清空全部频道 |
| POST | `/api/import` | 导入一段 M3U 文本 |
| POST | `/api/validate` | 校验源（`{"url":可选, "timeout":5}`） |
| GET | `/api/groups` | 分组与数量统计 |
| DELETE | `/api/channels/{id}` | 删除频道 |

## 7. Web 管理界面（已实现）

访问域名会先跳转 `/login`。**默认密码 `iptv-spider`**（登录页会提示），
登录后请立刻在「设置」里改掉。也可用 Worker 环境变量 `ADMIN_PASSWORD` 覆盖
（优先级高于页面保存的密码）。

| 页面 | 路径 | 内容 |
|---|---|---|
| 状态 | `/status` | IP 数量、节目总量、新上线 IP、三类资源库统计、运行参数 |
| 采集 | `/collection` | 手动触发采集（选类型 / 页数 / 每类数量）+ 已采集源列表 |
| IP 管理 | `/ip_manager` | 源服务器表格，按类型 / 状态筛选、搜索、删除 |
| 订阅 | `/subscriptions` | 按 类型 / 省份 / 运营商 筛选源，生成带 Token 的分享链接 |
| 日志 | `/logs` | 采集、登录、设置变更等活动记录 |
| 设置 | `/settings` | 改密码、采集参数、清空频道 |

界面样式**内嵌在 Worker 里，不依赖任何 CDN**，国内 / 海外都能正常渲染。

### 订阅机制（与原版一致）

⚠️ 这里的「订阅」**不是**"导入别人的 M3U 地址"，而是
**对已采集的源做条件筛选，生成一个带 Token 的分享链接**：

- 筛选维度：**类型**（hotel / migu / multicast）、**省份**、**运营商**，均支持多选，
  **不选即全选**（与原版弹窗的「不选则全选」一致）。
- 列表列：`Token | 类型 | 省份 | 运营商 | 最小速度/分辨率 | 生成文件 | 创建时间 | 操作`。
- 操作：**TXT / M3U / 编辑 / 删除**；Token 可随机生成（8 位）也可自定义。
- 选项（省份 / 运营商）由真实采集到的源自动汇总，所以**先采集再建订阅**。
- 分享链接**公开**（播放器要能直接取）：
  - `GET /sub/<token>` → M3U
  - `GET /sub/<token>/txt` → TXT

订阅输出会把**同一频道的多个源合并在一起**，并按数字排序
（`CCTV1 → CCTV2 → … → CCTV10`，不是字典序）：

```
央视,#genre#
CCTV1,http://…/hls/54/index.m3u8                                  ← CCTV1 的全部源连续排列
CCTV1,http://…:85/tsfile/live/0001_1.m3u8?key=txiptv&playlive=1&authid=0
CCTV1,http://…:808/hls/1/index.m3u8
CCTV2,http://…
```

为此做了两件事：

1. **频道名归一化** —— 不同源写法不同：`CCTV-1` / `CCTV1` / `CCTV1综合` / `CCTV 1`
   统一成 `CCTV1`（`CCTV5+` 这类保留 `+`）。实现见 `iptv_core.normalize_channel_name()`。
2. **URL 反转义** —— 上游 M3U 里的 URL 带 HTML 转义（`&amp;playlive=1&amp;authid=0`），
   直接输出会让播放器请求失败。生成时会还原成真实的 `&`
   （`_clean_url()` 在入库与输出两处都生效，所以历史数据无需重新采集）。

省份 / 运营商从源名称自动解析，例如「广西贵港酒店 广西联通」→ 广西 / 联通。

> 「最低速度 / 分辨率」字段按原版保留在 UI 上，但 Workers 版没有 ffprobe 测速，
> 该筛选**暂不生效**（页面上已注明）。

### 附加功能：从 M3U 链接导入

原版没有这个功能，作为补充保留：`POST /api/import-url {"url": "..."}`
抓取远程 M3U 并入库（采集页底部也有入口）。

## 8. 自动采集（已实现）

采集链路（已对线上接口逐一验证）：

```
列表页   https://api.cqshushu.com/hotel.php      酒店源
         https://api.cqshushu.com/multicast.php  组播源
         https://api.cqshushu.com/migu.php       咪咕源
   │     HTML 表格：IP:端口 | 节目数 | 地区名称 | 上线/更新时间 | 状态
   │     每行带详情 token → ?p=<token>
   ├─ 详情页 ?p=<token> → 频道按钮 ?s=<token>
   └─ 频道页 ?s=<token>&download=m3u → 直接返回标准 M3U
         （group-title 即源/地区名，如「广西贵港酒店 广西联通」）
```

即：解析表格拿到源服务器，再逐个下载它的 M3U 入库。**全程不需要 ffmpeg**。

触发方式：

```bash
# 手动（需先登录拿到 Cookie）
curl -b cookies.txt -X POST https://<你的域名>/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"kind":"hotel","pages":1,"max_sources":6}'

# 自动：Cron Trigger（UTC），对应原版的 09:20 / 18:00 / 22:00（UTC+8）
crons = ["20 1 * * *", "0 10 * * *", "0 14 * * *"]
```

实测：三类源各 10 个节点 / 页，单源 95~262 个频道；批量写入已改用 D1
`batch()`，单源采集约 **20 秒**（优化前 200+ 秒）。

## 9. 已知限制 —— 与原版的差异

- **ffprobe 测速 / 分辨率**：Workers 跑不了 ffmpeg，改为 HTTP 可用性 + 延迟判定。
  原版的「下载速度 MB/s」「分辨率」无法等价复刻。
- **强依赖第三方服务**：采集数据来自 `api.cqshushu.com`（原作者的服务）。它
  一旦下线、改版或加限制，采集即失效 —— 这不是本项目的 bug。
- **源的可达性**：酒店 / 组播 / 咪咕源多为国内运营商内网地址，Workers 的海外
  出口**直连探测基本不通**，因此「存活状态」采用上游报告的状态，频道列表由
  上游代抓后以 M3U 形式下发。
- **未移植**：qqwry IP 地理定位、播放器页面、模板管理。
- 原版为 **Python 3.11 + Cython**，本目录是其**纯 Python 重写**，并非逐行反编译
  结果。需要还原的源码请看同包的 `iptv_spider_extracted/`。
