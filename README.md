# BiliPulse · B站多账号实时遥测监控台

一个轻量、零前端外部依赖、可自托管的 **B站（Bilibili）多账号实时监控系统**。

系统按设定周期（默认每 **5 分钟**）自动采集指定 UP 主的 **粉丝量变化** 与 **全部主创投稿视频的实时在线观看人数总和 / 累计播放量**，持久化存储于本地 SQLite（WAL 模式），并通过原生 SVG 示波器级折线图、双排行榜与多账号横向对比看板实时呈现。

---

## 功能特性

- **多账号频道管理与横向对比**
  - 支持同时监控多个 B站账号，左侧控制栏支持一键添加、启停、移除及排序。
  - 提供 **「单账号遥测视图」** 与 **「全账号对比看板」**（全账号粉丝量 / 实时在线水平进度条排行对比）。
- **核心遥测曲线与自然日涨掉粉统计**
  - 实时追踪账号粉丝总量，自动按北京时间（`Asia/Shanghai`）统计 **昨日粉丝变化** 与 **今日粉丝变化**。
  - 支持 `1小时` / `6小时` / `24小时` / `7天` / `30天` / `全部` 多时间窗口平滑切换。
- **全视频实时在看汇总与单视频双通道波形**
  - 自动同步账号下所有主创投稿视频及分 P（缓存 `CID`，自动识别并过滤非主创联合投稿）。
  - 并发采集各分 P 实时在线人数（自动扣除 B站接口单分 P 默认计入的 `1` 名基准值），汇总全站实时在看总人数。
  - 内置 **实时在线榜** 与 **总播放榜**，点击任意视频可查看该视频的 **实时在线人数** 与 **累计播放量** 双通道历史曲线、在看峰值与区间播放增量。
- **零外部 CDN 依赖 & 内置示例模式**
  - 前端采用纯原生 HTML + CSS + ES Modules 与自研交互式 SVG 图表引擎，无任何第三方前端框架或外部 CDN 依赖，内网或国内云服务器环境下均可秒开。
  - 支持 **暗夜 / 明亮** 主题切换；未绑定账号时可一键开启本地 **示例模式** 预览完整动态面板（不写入数据库）。
- **可选管理员权限保护与防爆破限流**
  - 看板只读接口默认可直接访问；开启管理员密码后，「添加/删除监控账号」「修改设置」「手动触发采集」等写操作需通过密码验证。
  - 采用 `PBKDF2-HMAC-SHA256` 密码哈希、HMAC 签名 `HttpOnly` 会话 Cookie 以及单 IP 连续失败限流保护。
- **开箱即用的反爬适配与数据生命周期管理**
  - 自动握手补齐匿名会话所需的 `buvid3` / `buvid4` / `bili_ticket` 并支持 `WBI` 接口签名，可选配置全局或单账号 Cookie（前端脱敏展示）。
  - 内置定时清理任务，按保留天数（默认 `90` 天，可设为 `0` 永久保留）自动清理过期快照。

---

## 技术栈

- **后端**：Python 3.10+ · FastAPI · Uvicorn · HTTPX (Async) · APScheduler · Pydantic v2
- **存储**：SQLite 3（WAL 并发读写模式，零外部数据库依赖）
- **前端**：Vanilla ES Modules · Native `<dialog>` · Procedural & Interactive SVG Charts · CSS Custom Properties
- **部署**：Docker / Docker Compose（非 root 容器用户 + 内置健康检查）

---

## 目录结构

```text
bilipulse/
├── app/
│   ├── bilibili/
│   │   ├── client.py          # B站异步 HTTP 客户端（自动 buvid3/bili_ticket 握手与 WBI 签名）
│   │   └── collectors.py      # 模块化采集器（粉丝统计、全视频同步与分P在线人数汇总）
│   ├── routers/
│   │   ├── accounts.py        # 多账号增删改查、排序与切换 API
│   │   ├── admin.py           # 管理员登录验证、状态查询与登出 API
│   │   ├── dashboard.py       # 监控面板聚合数据、单视频双通道历史与采集日志 API
│   │   └── settings.py        # 全局监控配置、连接测试与立即采集 API
│   ├── auth.py                # PBKDF2 密码哈希、签名会话 Cookie 与登录限流器
│   ├── config.py              # 环境变量解析与默认配置项
│   ├── database.py            # SQLite 表结构初始化、平滑迁移与时序查询封装
│   ├── main.py                # FastAPI 应用工厂与生命周期管理
│   ├── models.py              # Pydantic 请求/响应数据模型
│   └── scheduler.py           # APScheduler 定时采集与快照过期清理调度器
├── static/
│   ├── index.html             # 单页监控台主视图与模态框结构
│   ├── app.js                 # 状态管理、多账号交互与实时轮询逻辑
│   ├── charts.js              # 原生交互式 SVG 示波器折线图引擎
│   ├── motion.js              # 视图过渡与数值动效辅助模块
│   ├── demo-data.js           # 纯离线程序化生成的示例遥测流与几何封面
│   ├── tokens-layout.css      # 设计令牌与响应式布局系统
│   └── styles.css             # 组件与暗夜/明亮主题样式
├── tests/
│   └── test_app.py            # 单元测试套件（客户端签名、数据库、鉴权与 API）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── run.py                     # 本地启动入口
```

---

## 快速开始

### 方式一：Docker Compose 一键部署（推荐）

```bash
git clone https://github.com/canxiaocai/bilipulse.git
cd bilipulse
docker compose up -d --build
```

启动后浏览器访问：`http://127.0.0.1:8000`（或服务器对应 IP / 域名端口）。

- SQLite 数据文件持久化挂载在宿主机 `./data/bili_monitor.db`。
- 容器默认以非 root 用户 `appuser` 运行；Linux 宿主机挂载 `./data` 时请确保该目录对容器用户具备写权限。

### 方式二：本地 Python 运行

#### 1. 创建虚拟环境并安装依赖

```bash
git clone https://github.com/canxiaocai/bilipulse.git
cd bilipulse

python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

#### 2. 启动服务

```bash
python run.py
```

访问 <http://127.0.0.1:8000>：

1. 点击左侧 **「添加监控账号」** 或 **「设置」**，输入目标 UP 主的纯数字 **B站 UID**（即个人空间 `https://space.bilibili.com/<UID>` 中的数字）。
2. 点击 **「测试连接」** 验证能否正常拉取昵称、粉丝数与投稿视频数。
3. 保存后系统将自动执行首次全量采集，并按设定间隔（默认每 5 分钟）在后台持续采样。

---

## 环境变量说明

所有配置均支持通过环境变量覆盖（可在 `docker-compose.yml` 或系统环境中设置）：

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `BILI_HOST` | `0.0.0.0` | HTTP 服务监听地址 |
| `BILI_PORT` | `8000` | HTTP 服务监听端口 |
| `BILI_DATA_DIR` | `./data` | 数据存储目录 |
| `BILI_DB_PATH` | `<BILI_DATA_DIR>/bili_monitor.db` | SQLite 数据库文件路径 |
| `BILI_ADMIN_PASSWORD` | 空 | 固定管理员密码（设置后优先级高于网页端密码，无法在网页端覆盖或清除） |
| `BILI_DEFAULT_ADMIN_PASSWORD` | 空 | 新建数据库时写入的初始管理员密码（后续可在网页端修改或清除） |
| `BILI_ADMIN_SESSION_HOURS` | `12` | 管理员登录会话 Cookie 有效时长（小时） |
| `BILI_TRUST_PROXY` | `false` | 部署于受信任反向代理（Nginx / Caddy）后时设为 `true`，以识别 `X-Forwarded-For` 与 `X-Forwarded-Proto` |
| `BILI_DEFAULT_UID` | 空 | 首次初始化数据库时的默认监控 UID |
| `BILI_DEFAULT_COOKIE` | 空 | 首次初始化数据库时的默认全局 B站 Cookie |
| `BILI_DEFAULT_INTERVAL` | `5` | 默认定时采集间隔（分钟，范围 `1` ~ `1440`） |
| `BILI_VIDEO_SYNC_INTERVAL` | `30` | 投稿视频列表全量同步周期（分钟，范围 `5` ~ `1440`） |
| `BILI_REQUEST_DELAY_MS` | `200` | 单个视频/分 P 请求间隔（毫秒，范围 `50` ~ `5000`，降低风控概率） |
| `BILI_SNAPSHOT_RETENTION_DAYS` | `90` | 历史快照保留天数（设为 `0` 则永久保留所有历史采样） |

---

## 公网部署与安全建议

1. **开启管理员密码**：公网部署时，建议在 `docker-compose.yml` 中配置 `BILI_ADMIN_PASSWORD=你的强密码`，或在首次启动后进入网页「设置」配置管理员密码，防止未授权人员修改监控列表或读取配置状态。
2. **启用 HTTPS 反向代理**：建议通过 Nginx 或 Caddy 配置 HTTPS 反代，并将环境变量 `BILI_TRUST_PROXY=true` 打开，以便正确下发 `Secure` 属性 Cookie 并按真实客户端 IP 执行密码防爆破限流。
3. **关于 B站 Cookie（可选）**：绝大多数公开账号无需配置 Cookie 即可正常采集粉丝数与视频在线人数；若监控投稿量极大或遇到接口频控（如 `-352` / `412`），可在后台「设置」中填入普通账号的 Cookie（如包含 `SESSDATA`）并适当调大「单视频请求间隔」。

---

## 主要 API 一览

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/health` | 公开 | 健康检查接口 |
| `GET` | `/api/dashboard` | 公开 | 获取指定账号（或当前激活账号）聚合看板、曲线快照与排行榜 |
| `GET` | `/api/videos` | 公开 | 获取指定账号缓存的视频列表及最新在线人数 |
| `GET` | `/api/videos/{bvid}/history` | 公开 | 获取单个视频历史在线人数与播放量趋势序列 |
| `GET` | `/api/accounts` | 公开 | 获取所有监控账号摘要列表 |
| `POST` | `/api/accounts` | 管理员 | 添加新的监控账号并触发初始化采集 |
| `PUT` | `/api/accounts/reorder` | 管理员 | 调整监控账号排序 |
| `PUT` | `/api/accounts/{uid}` | 管理员 | 更新指定监控账号启停状态或专属 Cookie |
| `DELETE` | `/api/accounts/{uid}` | 管理员 | 移除监控账号（可选连同清理历史快照数据） |
| `GET / PUT` | `/api/settings` | 管理员 | 读取或更新全局监控参数与管理员密码 |
| `POST` | `/api/test-connection` | 管理员 | 测试指定 UID 与 Cookie 的连通性 |
| `POST` | `/api/crawl/now` | 管理员 | 立即触发一次采集（支持强制重新同步视频列表） |
| `GET / POST` | `/api/admin/status` · `/verify` · `/logout` | 公开 | 查询鉴权状态、校验管理员密码、退出管理会话 |

---

## 扩展自定义采集指标

项目采用模块化采集器设计，如需新增自定义监控维度（如直播间人气、专栏阅读量、涨粉 Webhook 通知等）：

1. 在 `app/bilibili/collectors.py` 中继承 `BaseCollector` 并实现 `async def collect(self, ctx: CrawlContext) -> None`。
2. 将采集结果写入 `ctx.extra_metrics["your_metric_key"] = ...`。
3. 在 `CrawlService` 初始化列表或通过 `crawl_service.register_collector(...)` 注册该采集器，指标会自动随每次快照持久化到 SQLite，并在 `/api/dashboard` 的 `snapshots[*].extra_metrics` 中返回。

---

## 运行测试

```bash
pytest -v
```
