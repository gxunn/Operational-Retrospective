# 自媒体账号每日复盘系统

这是一个适合小团队使用的网页后台：每天上传抖音、小红书、视频号、公众号、B站等平台导出的 CSV/Excel，系统会清洗数据、计算前日和近 7 日对比，并生成中文复盘报告。

## 项目类型与部署结论

本项目不是纯静态、Vite、React 或 Next.js 项目，而是一个完整的 **Python FastAPI 服务端网页应用**：

- 后端：FastAPI + SQLAlchemy + SQLite
- 页面：Jinja2 + 原生 JavaScript/CSS
- 后台能力：登录、文件上传、定时任务、邮件、OpenAI 报告、PDF 导出
- 前端没有 `package.json`，不需要运行 `npm install` 或 `npm run build`
- 页面由 Python 服务动态生成，因此根目录不需要 `index.html`

推荐使用 Docker 部署到 Ubuntu 服务器。Cloudflare 可以用于域名解析、CDN 和 HTTPS，但不能只用 Cloudflare Pages 承载本项目，原因见“Cloudflare 部署说明”。

## 一、最快的本地运行方法

已经安装过的 Mac 用户，可以直接双击项目里的 `启动后台.command`。它会安全关闭本项目的旧服务并加载最新代码；终端窗口需要保持打开。

### 1. 准备 Python

安装 Python 3.12。安装时如果看到“Add Python to PATH”，请勾选。

### 2. 打开项目目录

Mac/Linux：

```bash
cd /Users/gxunn/Documents/test1/self-media-review
python3.12 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell：

```powershell
cd C:\你的路径\self-media-review
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. 安装依赖

```bash
python -m pip install -r requirements.txt
```

### 4. 配置 `.env`

项目已经有本地 `.env`。请参考 `.env.example` 补充这些内容：

- `ADMIN_USERNAME`：后台登录名
- `ADMIN_PASSWORD`：后台初始密码，至少 12 位
- `SESSION_SECRET`：一段至少 32 位的随机字符
- `OPENAI_API_KEY`：OpenAI 密钥
- `OPENAI_MODEL`：默认 `gpt-5.4-mini`，需要时可以更改
- `HOTSPOT_HOUR` / `HOTSPOT_MINUTE`：每日热点自动刷新时间，默认北京时间 09:00

如果要发邮件，还要填写 `SMTP_HOST`、`SMTP_USERNAME`、`SMTP_PASSWORD` 和 `MAIL_FROM`。QQ/163 邮箱通常需要使用邮箱后台生成的“授权码”，不是网页登录密码。

### 5. 启动

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。未在 `.env` 配置管理员信息时，本地默认账号为 `admin`，默认密码为 `admin123456`；登录后请立即在“系统设置”中修改。

## 二、第一次使用

1. 登录后台。
2. 打开“账号管理”，添加平台和账号名称。
3. 打开“上传数据”，选择账号并上传 CSV/XLSX/XLS。
4. 在预览页确认每一列对应的指标，然后点击“确认并导入”。
5. 打开“复盘报告”，点击“生成日报”。
6. 在“系统设置”添加收件邮箱；配置好 SMTP 后即可发送。

含“内容标题”的表格会被识别成内容明细，并自动汇总成账号日报。没有标题、只有指标的表格会直接作为账号日报导入。

## 三、Docker 本地运行

先安装 Docker Desktop，然后在项目目录执行：

```bash
docker compose up -d --build
```

打开 `http://localhost:8000`。查看运行状态：

```bash
docker compose ps
docker compose logs -f app
```

停止服务：

```bash
docker compose down
```

`down` 不会删除数据库和上传文件。不要添加 `-v`，否则可能删除 Docker 数据卷。

## 四、Cloudflare Pages 静态部署

如果你要长期免费公开访问，请使用仓库里的静态导出版。它会把当前页面导出到 `dist/`，适合 Cloudflare Pages。

构建命令：

```bash
python scripts/build_pages.py
```

输出目录：

```text
dist
```

说明：

- `main` 分支更新后，只要 Cloudflare Pages 连接了这个仓库，就会自动重新构建并发布。
- `dist/_redirects` 已处理页面刷新，直接访问 `/metrics`、`/ai-review`、`/accounts` 等路径不会 404。
- 这个静态版本保留了现有页面样式和展示结构，但不包含 FastAPI 的服务端写入能力。
- 如果你还要保留本地完整版，继续使用 `uvicorn app.main:app --reload` 即可。

## 五、部署到 Ubuntu 服务器（推荐）

推荐 Ubuntu 24.04、2 核 CPU、2GB 内存、20GB 磁盘，并准备一个已经解析到服务器 IP 的域名。

### 1. 安装 Docker

按照 Docker 官方 Ubuntu 安装说明安装 Docker Engine 和 Compose 插件。

### 2. 获取项目

通过 GitHub 克隆项目到服务器，例如 `/opt/self-media-review`。不要把本地真实 `.env` 发到公开代码仓库；应在服务器单独创建 `.env.production`：

```bash
git clone 你的GitHub仓库地址 /opt/self-media-review
cd /opt/self-media-review
cp .env.production.example .env.production
```

### 3. 修改生产配置

编辑 `.env.production`，至少设置：

```dotenv
ADMIN_USERNAME=你的管理员账号
ADMIN_PASSWORD=至少12位强密码
SESSION_SECRET=至少32位随机字符
COOKIE_SECURE=true
DOMAIN=report.example.com
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=gpt-5.4-mini
```

### 4. 启动 HTTPS

```bash
cd /opt/self-media-review
DOMAIN=report.example.com docker compose -f compose.production.yml up -d --build
```

Caddy 会自动申请 HTTPS 证书。浏览器访问 `https://你的域名`。

### 5. 更新

上传新版文件后执行：

```bash
git pull
DOMAIN=report.example.com docker compose -f compose.production.yml up -d --build
```

SQLite 和上传文件保存在服务器目录中，重新构建容器不会丢失。

## 六、Cloudflare 部署说明

### Cloudflare Pages 配置

本项目不能直接部署到 Cloudflare Pages。Pages 的构建表单应理解为：

| 配置项 | 本项目填写值 |
| --- | --- |
| Framework preset | 不适用 |
| Build command | 不适用（没有 npm 构建） |
| Output directory | 不适用（没有 `dist` 静态目录） |

如果强行按静态网站发布，登录、上传、SQLite、定时日报、邮件和 AI 报告都会无法运行。

### 正确使用 Cloudflare 的方式

1. 按上一节把应用部署到 Ubuntu 服务器。
2. 在 Cloudflare 添加域名，把 A 记录指向服务器公网 IP。
3. 初次签发证书时可先设为“仅 DNS”，确认 `https://你的域名` 可访问。
4. 需要启用 Cloudflare 代理时，再打开小云朵，并把 SSL/TLS 模式设为“完全（严格）”。

也就是说：**FastAPI 应用运行在服务器，Cloudflare 负责域名和网络入口，不使用 Cloudflare Pages。**

## 七、备份与恢复

建议每天备份这三个目录：

- `data/`：数据库
- `uploads/`：原始上传文件
- `reports/`：生成的 PDF

备份前可以先执行 `docker compose stop app`，复制完成后执行 `docker compose start app`。恢复时停止应用，再把备份文件复制回原位置。

## 八、每天 10 点自动日报

- 默认时区：`Asia/Shanghai`
- 默认时间：10:00
- 可在 `.env` 修改 `REPORT_HOUR` 和 `REPORT_MINUTE`
- 可在 `.env` 修改 `HOTSPOT_HOUR` 和 `HOTSPOT_MINUTE`，控制每日热点刷新时间
- 如果昨天没有数据，会使用数据库里的最新数据日期
- 同一份日报只会自动发送一次
- 生产环境必须保持一个应用进程；Dockerfile 已经固定为一个进程

## 九、常见问题

**上传后字段识别不对**：在预览页用下拉框重新选择。系统会记住该平台的映射。

**AI 报告失败**：系统仍会生成纯数据报告。检查 OpenAI 额度、网络和 `OPENAI_MODEL`。

**PDF 中文乱码**：Docker 镜像已经安装中文字体。本地系统通常使用苹方或微软雅黑。

**邮件发送失败**：确认 SMTP 地址、端口、SSL 设置和邮箱授权码。

**数据重复**：完全相同的文件不会再次导入；补传的新文件会按“账号 + 日期”更新。

## 十、上线前检查

在提交或部署前执行：

```bash
python -m pytest -q
```

如果使用 Docker，还可以检查生产配置：

```bash
DOMAIN=report.example.com docker compose -f compose.production.yml config
```

健康检查地址为 `/health`。服务启动后访问 `https://你的域名/health`，看到 `{"status":"ok"}` 表示后端正常。

## 十、安全提醒

- `.env` 已被 `.gitignore` 排除，绝不要截图或粘贴其中的密钥。
- `.dockerignore` 也会阻止本地密钥、数据库、上传文件和报告进入 Docker 镜像。
- 服务器必须使用 HTTPS，并设置 `COOKIE_SECURE=true`。
- 上线后立即修改默认密码。
- 定期备份数据库和报告。
