# B站市集好价提示系统（Bili Market Monitor）

`0.1.0` 是一个面向多用户的响应式 B 站市集价格监控工具。用户可以粘贴商品 `ClsId`（也就是 B 站接口使用的 `clusterId`），查询当前最低可购买价，收藏商品并按目标价格接收邮件提醒。查询结果和收藏卡片都会提供“前往 B 站市集”入口，方便在提醒或查询后浏览确认。

## 先找到商品 ID

可以先打开 [BiliMarketBoss](https://www.bili-market-boss.top/#/)，自行搜索或收藏商品，在商品信息中复制 `ClsId` 后面的纯数字，然后回到本站粘贴并查询。BiliMarketBoss 是第三方网站，本站只提供入口；价格查询直接请求 B 站市集接口，不依赖 BiliMarketBoss 后台。

使用步骤：

1. 点击首页的“打开 BiliMarketBoss 寻找商品”。
2. 找到商品的 `ClsId`，例如 `10000002733`，复制数字。
3. 粘贴到本站“商品 ID”输入框并点击“查询商品”。
4. 在结果卡片中查看当前状态和价格，也可以点击“前往 B 站市集”打开对应详情页确认。

## 架构与技术栈

- `frontend/`：React、TypeScript、Vite，移动端优先的轻量 UI。
- `backend/`：Python 3.12+、FastAPI、SQLAlchemy 2、Pydantic、HTTPX、Argon2id。
- PostgreSQL 16：生产数据库；Alembic 负责迁移。
- 独立 `worker`：每秒检查到期收藏，同一商品共享缓存和 B 站请求，并使用 SMTP Outbox 发送通知。
- `docker-compose.yml`：`db`、`api`、`worker`、`web` 四个服务。

前端不会直接调用 B 站接口。所有外部字段只在 `backend/app/services/bili_market/parser.py` 和 `client.py` 中解析，业务层只使用标准化商品快照。

## 本地开发

后端：

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp ../.env.example .env
# 开发时可把 DATABASE_URL 改为 sqlite:///./app.db
alembic upgrade head
uvicorn app.main:app --reload
```

Worker 另开终端运行：

```bash
cd backend
source .venv/bin/activate
python -m app.worker
```

前端：

```bash
cd frontend
pnpm install
pnpm dev
```

仓库已锁定 `pnpm-lock.yaml`；也可以使用等价的 `pnpm build` 完成生产构建。

默认 API 地址为同源 `/api`；本地 Vite 开发服务器将 `/api` 代理到 `http://localhost:8000`。

## Docker 部署

```bash
cp .env.example .env
# 修改 .env 中的 SMTP 和生产域名配置
docker compose up --build
```

生产环境应在反向代理（例如 Caddy）中启用 HTTPS，并将 `SESSION_COOKIE_SECURE=true`。数据库迁移由 API 容器启动命令执行，Worker 等待 API 健康后再启动；正式环境也建议在发布流程中单独执行迁移并备份 PostgreSQL。

## 环境变量重点

完整模板见 `.env.example`。关键配置包括：

- `DATABASE_URL`：PostgreSQL 连接字符串。
- `BILI_*`：B 站接口超时、并发、缓存、全局最低请求间隔，以及历史数据保留期。`BILI_PRICE_HISTORY_RETENTION_DAYS` 默认 90 天，`BILI_REQUEST_EVENT_RETENTION_DAYS` 默认 30 天；Worker 周期清理过期记录。10 秒是可选目标间隔，不是严格实时 SLA，遇到网络、429、系统负载或 B 站限流时会退避。
- `SMTP_*`：统一发件邮箱的 SMTP 授权码/App Password，不要使用账号主密码；未配置 SMTP 时 Outbox 按 `SMTP_UNCONFIGURED_MAX_ATTEMPTS` 次数有限重试后标记失败。
- `SESSION_*`、`EMAIL_VERIFY_*`、`PASSWORD_RESET_*`：Session 与邮箱流程安全参数。

只有邮箱完成验证码验证后，用户才能开启邮件提醒。可选监控频率为 10 秒、30 秒、1 分钟、3 分钟、5 分钟、10 分钟、30 分钟、1 小时，系统会在后端校验，不能传入任意秒数。

## 管理员

不要开放网页管理员注册。首次部署后在后端容器或虚拟环境中执行：

```bash
python -m app.cli.create_admin
```

管理员可查看用户、监控、Outbox 和系统健康状态，并启用/禁用账号；账号状态修改会写入审计日志。管理员接口不会返回密码、验证码、Session 或 SMTP 密钥。

## 测试、迁移与排障

```bash
cd backend
pip install -e '.[dev]'
pytest
ruff check app tests
alembic upgrade head
```

`GET /api/health` 用于容器健康检查，会返回版本和数据库状态。B 站接口异常时会保留最近一次成功价格，不会把网络失败误判为售罄；管理员系统状态页会展示最近错误、429 和 Outbox 积压。

管理员 Dashboard 的 B 站成功率按近 24 小时商品快照统计，Worker 周期耗时和客户端请求指标来自最近一次心跳；0.1.0 暂不持久化逐请求的一小时指标明细。

生产数据库建议在迁移前后进行 PostgreSQL `pg_dump` 备份。不要把 `.env`、SMTP 授权码、密码、验证码或 Token 提交到 Git。

## 版本

版本唯一来源是根目录 `VERSION`，当前为 `0.1.0`。发布 Tag 使用 `v0.1.0` 格式。
