# B站市集好价提示系统（Bili Market Monitor）

`0.1.4` 是一个面向多用户的响应式 B 站市集价格监控工具。用户可以粘贴商品 `ClsId`（也就是 B 站接口使用的 `clusterId`），查询当前最低可购买价，收藏商品并按目标价格接收邮件提醒。查询结果和收藏卡片都会提供“前往 B 站市集”入口，方便在提醒或查询后浏览确认。

## 先找到商品 ID

可以先打开 [BiliMarketBoss](https://www.bili-market-boss.top/#/)，自行完成前期检索、收藏和复制商品 ID 的步骤，再回到本站查询。也可以直接访问应用内的 `/tutorial` 六步图文教程。BiliMarketBoss 是第三方网站，本站只提供入口；价格查询直接请求 B 站市集接口，不依赖 BiliMarketBoss 后台。

使用步骤：

1. 打开 [BiliMarketBoss](https://www.bili-market-boss.top/#/)，点击首页的“开始搜索”。
2. 在“搜索关键词”输入框填写目标商品关键词，例如“初音未来”，点击页面下方的“开始搜索”。
3. 在搜索结果中找到目标商品，确认商品名称和图片后，点击商品卡片下方的心形按钮收藏。
4. 点击 BiliMarketBoss 顶栏的“收藏”，进入已收藏商品列表。
5. 在收藏列表中找到目标商品，复制商品信息里的 `ClsId` 纯数字（例如 `10000002733`）。
6. 回到本站首页，粘贴到“商品 ID”输入框并点击“查询商品”；查询结果中的“前往 B 站市集”链接可用于打开对应页面浏览确认。

## 架构与技术栈

- `frontend/`：React、TypeScript、Vite，移动端优先的轻量 UI。
- `backend/`：Python 3.12+、FastAPI、SQLAlchemy 2、Pydantic、HTTPX、Argon2id。
- PostgreSQL 16：生产数据库；Alembic 负责迁移。
- 独立 `worker`：每秒检查到期收藏，同一商品共享缓存和 B 站请求，并使用 SMTP Outbox 发送通知。
- `docker-compose.yml`：`db`、`api`、`worker`、`web` 四个服务。

前端不会直接调用 B 站接口。所有外部字段只在 `backend/app/services/bili_market/parser.py` 和 `client.py` 中解析，业务层只使用标准化商品快照。商品封面由后端按商品 ID 从数据库读取已解析 URL，并通过受限的 B 站图片域名白名单代理，避免 CDN 防盗链导致浏览器直连失败，也不接受任意 URL 代理请求。

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
# 编辑本机 .env 中的 SMTP 和生产域名配置
docker compose up -d --build
```

生产环境应在反向代理（例如 Caddy）中启用 HTTPS，并将 `SESSION_COOKIE_SECURE=true`。数据库迁移由 API 容器启动命令执行，Worker 等待 API 健康后再启动；正式环境也建议在发布流程中单独执行迁移并备份 PostgreSQL。

### 163 SMTP 配置与验收

部署时只在服务器本机创建和编辑 `.env`：

1. 在仓库根目录执行 `cp .env.example .env`。
2. 打开 `.env`，填写 163 邮箱账号，并将 `SMTP_FROM_EMAIL` 设置为该发件地址；`SMTP_PASSWORD` 必须填写 163 邮箱后台生成的“客户端授权码”。示例（全部是占位值）：

   ```dotenv
   SMTP_HOST=smtp.163.com
   SMTP_PORT=465
   SMTP_USERNAME=your-name@163.com
   SMTP_PASSWORD=your-163-client-authorization-code
   SMTP_FROM_EMAIL=your-name@163.com
   SMTP_USE_SSL=true
   SMTP_USE_TLS=false
   ```

   `SMTP_PASSWORD` 绝对不能填写 163 网页登录密码。465 端口使用 SSL，因此保持 `SMTP_USE_SSL=true`、`SMTP_USE_TLS=false`，两者不能同时开启。不要把真实邮箱、密码或授权码粘贴到聊天、Issue、日志或 Git 提交中。
3. 确认 `.env` 未被 Git 跟踪（仓库 `.gitignore` 已忽略它），可运行 `git check-ignore -v .env` 验证忽略规则，再用 `git status --short` 检查待提交文件；`.env` 只能留在部署机，不能提交到 GitHub。若凭据曾出现在聊天、Issue、日志或 Git 提交中，应立即在 163 后台撤销并重新生成客户端授权码；若泄露的是网页登录密码，也应同时更换密码。
4. 首次部署执行 `docker compose up -d --build`。修改 SMTP 配置后，强制重建并重启 API、Worker，使新环境变量生效：

   ```bash
   docker compose up -d --build --force-recreate api worker
   ```

5. 检查服务健康和日志：

   ```bash
   curl -fsS http://localhost:8000/api/health
   docker compose ps
   docker compose logs --tail=100 api worker
   ```

   健康接口应返回版本和数据库状态；日志只会记录收件人掩码及异常类型，不会输出 SMTP 密码或验证码。若使用 HTTPS 域名，请将 `localhost:8000` 替换为实际 API 地址。
6. 使用一个可接收邮件的测试邮箱完成注册验证码或“忘记密码”流程，检查收件箱和垃圾邮件文件夹；随后再用已验证账号创建一条低价提醒。以实际收到邮件为最终验收标准；若未收到，再检查 Worker 日志和管理员 Outbox 的待发送或失败记录。不要仅凭 API 返回成功判断邮件已送达。

## 环境变量重点

完整模板见 `.env.example`。关键配置包括：

- `DATABASE_URL`：PostgreSQL 连接字符串。
- `BILI_*`：B 站接口超时、并发、缓存、全局最低请求间隔，以及历史数据保留期。`BILI_PRICE_HISTORY_RETENTION_DAYS` 默认 90 天，`BILI_REQUEST_EVENT_RETENTION_DAYS` 默认 30 天；Worker 周期清理过期记录。10 秒是可选目标间隔，不是严格实时 SLA，遇到网络、429、系统负载或 B 站限流时会退避。
- `SMTP_*`：统一发件邮箱的 SMTP 授权码/App Password，不要使用账号主密码；未配置 SMTP 时 Outbox 按 `SMTP_UNCONFIGURED_MAX_ATTEMPTS` 次数有限重试后标记失败。
- `SESSION_*`、`EMAIL_VERIFY_*`、`PASSWORD_RESET_*`：Session 与邮箱流程安全参数。

163 邮箱可以按下面的方式配置（值仅为示例，请替换为自己的邮箱；不要把真实密钥提交到 Git）：

```dotenv
SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USERNAME=your-name@163.com
SMTP_PASSWORD=your-163-client-authorization-code
SMTP_FROM_EMAIL=your-name@163.com
SMTP_USE_SSL=true
SMTP_USE_TLS=false
```

其中 `SMTP_PASSWORD` 必须填写 163 邮箱后台生成的客户端授权码，不是网页登录密码。465 端口使用 SSL，因此保持 `SMTP_USE_SSL=true`、`SMTP_USE_TLS=false`，两者不要同时开启。发送验证码后请检查收件箱和垃圾邮件；只有验证码邮件成功发送并完成验证后，用户才能开启邮件提醒，发送失败时不能用未发送的验证码完成验证。可选监控频率为 10 秒、30 秒、1 分钟、3 分钟、5 分钟、10 分钟、30 分钟、1 小时，系统会在后端校验，不能传入任意秒数。

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

管理员 Dashboard 的 B 站成功率按近 24 小时商品快照统计，Worker 周期耗时和客户端请求指标来自最近一次心跳；0.1.4 暂不持久化逐请求的一小时指标明细。

生产数据库建议在迁移前后进行 PostgreSQL `pg_dump` 备份。不要把 `.env`、SMTP 授权码、密码、验证码或 Token 提交到 Git。

## 版本

版本唯一来源是根目录 `VERSION`，当前为 `0.1.4`。发布 Tag 使用 `v0.1.4` 格式。版本策略遵循语义化版本：修复类更新递增补丁位；大更新递增中间位并将补丁位归零（例如 `0.1.4` → `0.2.0`）。
