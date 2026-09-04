# 🔥 DouYin SparkFlow

<div align="center">

**抖音多账号火花自动维护系统**

一个智能化的抖音好友互动管理工具，自动维护好友火花标记，支持多账号管理、定时发送、Web 控制台

[![GitHub stars](https://img.shields.io/github/stars/halfwaystudent/douyin-sparkflow?style=social)](https://github.com/halfwaystudent/douyin-sparkflow)
[![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-Discussion-blue)](https://linux.do)

[功能特性](#-功能特性) • [快速开始](#-快速开始) • [使用文档](#-使用文档) • [部署指南](#-部署指南) • [社区讨论](https://linux.do)

</div>

---

> ⚠️ **重要提示**
>
> 本项目是非官方的第三方公开源码项目，与抖音及其关联方不存在隶属、授权、赞助、代理或合作关系。
> 本项目自有代码采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，仅授权非商业用途；未经版权持有人事先书面授权，不得将本项目用于收费服务、商业运营、商业账号管理、营销推广、客户代运营、商业产品集成或其他商业用途。
> 使用者只能操作本人拥有或已获得明确授权的账号，并须自行遵守抖音用户协议、相关法律法规及账号管理要求。自动化操作可能导致验证、限流、功能限制、账号封禁、登录态失效、数据丢失或其他后果。

## 📸 主界面预览

### 🌙 暗色模式

<div align="center">
  <img src="DouYinSparkFlow/docs/images/screenshot-dark.png" alt="主界面预览 - 暗色模式" width="800"/>
  <p><i>Web 管理控制台 - 仪表盘视图（暗色主题）</i></p>
</div>

### ☀️ 亮色模式

<div align="center">
  <img src="DouYinSparkFlow/docs/images/screenshot-light.png" alt="主界面预览 - 亮色模式" width="800"/>
  <p><i>Web 管理控制台 - 仪表盘视图（亮色主题）</i></p>
</div>

---

## ✨ 功能特性

### 🎯 核心功能

- **🔄 自动维护火花标记** - 智能识别需要维护的好友关系，自动发送消息保持火花
- **👥 多账号管理** - 支持同时管理多个抖音账号，集中控制、独立配置
- **⏰ 定时任务调度** - 灵活的定时发送策略，支持自定义发送时间窗口
- **🎨 消息模板系统** - 内置多种消息模板（一言、节日祝福等），支持自定义
- **📊 可视化仪表盘** - 实时监控账号状态、任务进度、发送历史

### 🛠️ 技术特性

- **🌐 Web 管理界面** - 现代化的 Web UI，支持移动端访问
- **🎭 主题切换** - 内置亮色/暗色主题，自适应系统偏好
- **🔐 扫码登录** - 安全的二维码登录方式，无需密码
- **🔌 浏览器自动化** - 基于 Playwright 的稳定浏览器控制
- **🐳 容器化部署** - 开箱即用的 Docker 支持，一键部署
- **🔄 登录态持久化** - 自动保存登录状态，减少重复登录
- **📝 完整日志系统** - 详细的操作日志，方便问题追踪

### 🎨 界面特点

- **现代化设计** - 简洁美观的深色模式主界面，炫彩火花渐变效果
- **响应式布局** - 适配桌面和移动设备
- **侧边栏导航** - 清晰的功能分区：概览、登录、账号、控制台、日志、设置
- **实时状态更新** - 账号在线状态、任务执行进度实时显示
- **交互式控制** - 一键启动/停止任务，批量操作支持

---

## 🚀 快速开始

### 前置要求

- Python 3.9 或更高版本
- Docker 和 Docker Compose（用于容器部署）
- 稳定的网络连接

### 📦 安装部署

<details open>
<summary><b>🐳 方式一：Docker 一键部署（推荐）</b></summary>

**适用场景**：服务器部署、生产环境

```bash
# 1. 克隆仓库
git clone https://github.com/halfwaystudent/douyin-sparkflow.git
cd douyin-sparkflow

# 2. 创建本地环境变量
cp .env.example .env
nano .env  # 根据需要修改配置
# 可选：在本地 .env 中填写 PROXY_SUB_URL，不要提交真实订阅地址

# 3. 初始化运行时文件并启动服务
# 会创建 proxy/config.yaml；没有订阅时使用 DIRECT-only 配置
bash ./deploy/install-local.sh

# Windows PowerShell 使用：
# powershell -ExecutionPolicy Bypass -File .\deploy\install-local.ps1

# 4. 访问 Web 界面
# 浏览器打开 http://localhost:8787
```

**服务端口说明**：
- `8787`：Web 管理控制台，默认监听全部网卡
- `8788`：noVNC 登录桌面，默认只绑定 `127.0.0.1`
- `7890` / `9090`：代理和控制端口，默认只绑定 `127.0.0.1`

服务器远程访问 noVNC 时，请先建立 SSH 隧道：

```bash
ssh -L 8788:127.0.0.1:8788 <user>@<server-ip>
```

然后打开 `http://127.0.0.1:8788/vnc.html?autoconnect=1&resize=scale&view_only=0`。

</details>

<details>
<summary><b>💻 方式二：本地开发运行</b></summary>

**适用场景**：本地开发、功能测试

```bash
# 1. 克隆仓库
git clone https://github.com/halfwaystudent/douyin-sparkflow.git
cd douyin-sparkflow/DouYinSparkFlow

# 2. 安装依赖
pip install -r requirements.txt
pip install -r requirements-web.txt

# 3. 安装 Playwright 浏览器
playwright install chromium

# 4. 启动 Windows 本地登录浏览器（另开一个 PowerShell）
.\scripts\start_login_desktop.ps1

# 5. 启动 Web 服务（再开一个终端）
python main.py --web

# 6. 访问 http://localhost:8787
```

</details>

### 🎬 使用流程

1. **登录账号** → 进入"登录工作区"，扫码登录抖音账号
2. **添加好友** → 在"账号管理"中刷新好友列表，选择需要维护火花的好友
3. **配置任务** → 设置发送时间窗口、消息模板
4. **启动任务** → 在"概览"页面启动定时任务
5. **监控运行** → 在"发送控制台"查看实时日志和发送记录

📖 详细使用教程请查看 [使用文档](docs/usage.md)

---

## 📂 项目结构

```
douyin-sparkflow/
├── DouYinSparkFlow/          # 核心应用源码
│   ├── core/                 # 核心功能模块
│   │   ├── browser.py        # 浏览器控制
│   │   ├── friends.py        # 好友管理
│   │   ├── login.py          # 登录处理
│   │   ├── msg_builder.py    # 消息构建
│   │   ├── protocol_sender.mjs  # 协议发送
│   │   └── tasks.py          # 任务调度（核心）
│   ├── webui/                # Web 界面
│   │   ├── app.py            # FastAPI 主应用
│   │   ├── auth.py           # 认证模块
│   │   ├── ops.py            # 操作接口
│   │   ├── static/           # 静态资源（CSS/JS）
│   │   └── templates/        # HTML 模板
│   ├── utils/                # 工具模块
│   │   ├── config.py         # 配置管理
│   │   ├── logger.py         # 日志记录
│   │   └── hitokoto.py       # 一言 API
│   ├── scripts/              # 辅助脚本
│   ├── docs/                 # 文档和截图
│   ├── main.py               # 主入口
│   └── login_desktop_server.py  # 登录桌面服务
├── .github/workflows/       # GitHub Actions 定时任务
├── proxy/                    # 代理配置
│   ├── config.example.yaml   # Git 跟踪的安全模板
│   └── config.yaml           # 本地生成，Git 忽略
├── docker-compose.yml        # 容器编排配置
├── .env.example              # 环境变量模板
├── refresh_proxy.sh          # 代理刷新脚本
└── README.md                 # 本文件
```

---

## 📖 使用文档

### 🔑 账号管理

- **扫码登录**：使用抖音 APP 扫描二维码完成登录
- **多账号支持**：可同时管理多个账号，独立配置每个账号的发送策略
- **登录态保持**：自动保存登录状态，无需频繁重新登录
- **状态监控**：实时显示账号在线状态和登录有效期

### 📝 消息管理

**内置模板类型**：
- **一言（Hitokoto）**：随机诗词、名言警句
- **节日祝福**：自动识别节假日，发送对应祝福语
- **自定义消息**：支持纯文本自定义消息

**发送策略**：
- 时间窗口设置（如：09:00-22:00）
- 可配置发送间隔与限速，用于降低操作频率和对消息接收方的打扰；不保证规避平台风控，也不保证账号不会受到平台处理
- 发送失败自动重试
- 发送确认机制

### ⚙️ 配置说明

<details>
<summary>点击查看配置文件说明</summary>

#### `.env` - 环境变量配置

```bash
WEB_BIND_ADDRESS=0.0.0.0
WEB_PORT=8787

# noVNC 默认仅允许本机或 SSH 隧道访问
LOGIN_DESKTOP_BIND_ADDRESS=127.0.0.1
LOGIN_DESKTOP_WEB_PORT=8788
LOGIN_DESKTOP_PUBLIC_URL=/login-desktop/proxy/vnc.html?autoconnect=1&resize=scale&view_only=0&path=login-desktop/proxy/websockify

# 登录浏览器默认先直连抖音，直连失败时再尝试 Mihomo
LOGIN_DESKTOP_PROXY_MODE=auto
LOGIN_DESKTOP_PROXY=http://proxy:7890


# Mihomo 代理和控制端口默认仅绑定本机
PROXY_BIND_ADDRESS=127.0.0.1
PROXY_HTTP_PORT=7890
PROXY_CONTROLLER_PORT=9090
# 可选：Mihomo/Clash 订阅地址。通常包含敏感 token，只写入本地 .env。
PROXY_SUB_URL=
```


登录、好友刷新和发送任务默认都采用“直连优先，Mihomo 回退”的网络策略。`LOGIN_DESKTOP_PROXY_MODE=auto` 时，登录浏览器先直连 `creator.douyin.com`，直连预检失败后才使用 `LOGIN_DESKTOP_PROXY`。好友刷新和续火花任务也会在任务开始前选择可用出口；发送动作开始后不会因响应不明确而盲目切换代理重发。没有配置有效订阅时，Mihomo 仅提供 DIRECT-only 配置，回退不会凭空产生代理节点。


#### `config.example.json` 与 `config.json` - 应用配置

仓库跟踪 `DouYinSparkFlow/config.example.json`；首次运行会生成被 Git 忽略的 `DouYinSparkFlow/config.json`。常用配置示例：

```json
{
  "messageTemplate": "✨今日火花+1\n",
  "useProtocolSender": false,
  "browserSenderAccounts": [],
  "dailySendWindow": {
    "enabled": true,
    "startHour": 10,
    "endHour": 18,
    "scheduleIntervalMinutes": 20
  },
  "friendListScan": {
    "maxScanSeconds": 300,
    "idleScanSeconds": 120,
    "scrollStepPx": 400,
    "scrollDelaySeconds": 0.8
  },
  "persistentBrowserProfiles": {
    "enabled": true,
    "root": "/opt/douyin-sparkflow/state/browser-profiles",
    "seedCookiesWhenEmpty": true,
    "syncStoredCookiesBeforeRun": true,
    "refreshStoredCookiesAfterLogin": true
  }
}
```

`usersData.json`、`webui_settings.json`、浏览器 Profile、Cookie 和日志均为运行时敏感数据，不应提交到 Git。

</details>

---

## 🐳 部署指南

### Docker Compose 部署（推荐）

本项目提供完整的 Docker Compose 配置，包含以下服务：

- **web**（容器名 `douyin-web`）：Web 管理控制台服务
- **login-desktop**: 登录桌面服务（包含浏览器环境）
- **proxy**: Mihomo 代理服务（可选）
- **scheduler**: 发送窗口定时调度服务
- **task**: 一次性发送任务服务

定时任务由 `scheduler` 容器直接运行 `/app/main.py --doTask`，不再通过
`docker ps` 和 `docker exec` 控制 `web` 容器，因此 scheduler 不需要挂载宿主机
Docker socket。旧部署中已经写入的共享定时文件，会在 scheduler 启动时自动迁移；
服务器更新脚本还会备份并清理宿主机 root crontab 中旧的 Docker 发送任务，避免两套
定时器同时运行。

#### 快速部署

```bash
# 1. 准备环境变量
cp .env.example .env

# 2. 初始化 proxy/config.yaml 并启动所有服务
bash ./deploy/install-local.sh

# 3. 查看日志
docker compose logs -f

# 4. 停止服务
docker compose down
```

#### 仅部署 Web 服务

```bash
docker compose up -d web
```

### 服务器部署最佳实践

<details>
<summary>查看部署建议</summary>

#### 系统要求

- **CPU**: 2 核心或以上
- **内存**: 2GB 或以上
- **存储**: 10GB 可用空间
- **系统**: Ubuntu 20.04+ / CentOS 7+ / Debian 10+

#### 持久化数据

以下目录建议挂载为数据卷：

```yaml
volumes:
  - ./state:/app/state          # 运行状态数据
  - ./logs:/app/logs            # 日志文件
  - ./DouYinSparkFlow:/app      # 应用代码（开发环境）
```

#### 网络配置

如需外网访问，建议配置反向代理（Nginx/Caddy）：

```nginx
# Nginx 配置示例
server {
    listen 80;
    server_name sparkflow.example.com;
    
    location / {
        proxy_pass http://localhost:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

#### 安全建议

- ✅ 修改默认管理员密码
- ✅ 启用 HTTPS（使用 Let's Encrypt）
- ✅ 配置防火墙规则
- ✅ 定期备份 `state/` 和 `usersData.json`
- ✅ 使用环境变量管理敏感配置

</details>

---

## 🔧 高级配置

### 代理配置

项目支持通过代理访问抖音服务。仓库提供 `proxy/config.example.yaml` 作为安全模板，部署脚本会在启动前生成本地的 `proxy/config.yaml`：

```yaml
mixed-port: 7890
allow-lan: true
mode: rule
# ... 更多配置见 proxy/config.example.yaml
```

如果 `PROXY_SUB_URL` 不为空，`refresh_proxy.sh` 会下载订阅并更新本地配置；如果为空，则生成 DIRECT-only 配置。不要在 Git 中提交包含订阅 token 的 `proxy/config.yaml`。首次部署不要跳过初始化步骤直接执行 `docker compose up -d`，否则 Docker 可能把缺失的配置文件创建成目录。

如果宿主机 Docker Engine 较旧，Web 容器中的 Docker 运维功能可能需要显式指定
`DOCKER_API_VERSION`。默认留空即可；只有确认宿主机 API 版本后，才在 `.env` 中设置，
例如 `DOCKER_API_VERSION=1.43`。


### 默认网络安全

noVNC、Mihomo 代理端口和控制端口默认仅绑定 `127.0.0.1`。远程服务器请优先通过 SSH 隧道、VPN 或带认证的 HTTPS 反向代理访问，不建议直接把 8788、7890、9090 暴露到公网。

Web 通过 HTTPS 反向代理部署时，可设置 `SPARKFLOW_SESSION_COOKIE_SECURE=1`。

### GitHub Actions 定时任务

工作流位于 `.github/workflows/schedule.yml`。在仓库的 `user-data` Environment 中配置 `USER_DATA` Secret 后，可以手动触发或按北京时间 10:00 定时执行一次手动模式发送。工作流会先执行单元测试和网络可达性检查，再处理当天尚未强确认的目标。

---

## 📊 技术栈

- **后端框架**: FastAPI - 现代化的 Python Web 框架
- **前端**: HTML5 + CSS3 + Vanilla JavaScript
- **浏览器自动化**: Playwright - 跨浏览器自动化
- **容器化**: Docker + Docker Compose
- **代理**: Mihomo (Clash Meta)
- **任务调度**: `scheduler` 容器 + `scripts/cron_runner.py`
- **模板引擎**: Jinja2

---

## ⚠️ 免责声明

### 非官方项目声明

本项目为第三方个人项目，与抖音及其关联方不存在隶属、授权、代理、赞助、技术支持或合作关系。项目名称中出现的“抖音”仅用于说明兼容目标或使用场景，不代表任何官方认可。

### 使用授权与账号责任

- 使用者只能操作本人拥有或已获得明确授权的账号。
- 不得使用他人未经授权的账号、Cookies、登录态、身份凭据或个人信息。
- 使用者应自行判断使用本项目是否符合适用的法律法规、抖音用户协议及其他平台规则。

### 平台风险与责任限制

- 抖音可能随时调整服务、接口、页面、风控策略和用户协议。
- 自动化操作可能导致验证码、限流、功能限制、账号异常、账号封禁、登录态失效或其他平台处理。
- 本项目不保证平台兼容性、持续可用性、账号安全或任何特定功能结果，也不提供规避平台风控或绕过平台限制的保证。
- 在法律允许的最大范围内，作者不对因使用、修改、部署或运行本项目产生的账号处置、数据丢失、服务中断、隐私泄露、业务损失或其他损害承担责任；法律不得排除或限制的责任除外。

### 消息内容与数据责任

- 使用者对自动发送的消息内容、发送对象、发送频率以及由此产生的骚扰、侵权、违法或其他纠纷承担相应责任。
- 登录态、Cookies、账号信息、好友信息和发送记录可能属于敏感数据。使用者应自行负责其存储、访问控制、备份、传输和删除，不得将相关数据提交到公开仓库、公开日志或不可信服务器。

### 商业使用限制

本项目自有代码采用 PolyForm Noncommercial License 1.0.0。未经版权持有人事先书面授权，禁止将本项目用于以下场景：

- 收费软件、收费脚本、收费镜像或收费部署服务；
- SaaS、托管服务、代运营或客户账号管理；
- 企业、品牌、店铺、主播或其他商业账号运营；
- 广告投放、商业推广、营销获客或客户维护；
- 将本项目集成到商业产品、商业平台或付费课程；
- 以部署、维护、代发、运营等方式向第三方收取费用；
- 其他直接或间接服务于营利性活动的场景。

如需将本项目用于商业场景，请事先联系版权持有人并取得单独的书面商业授权。未经书面授权，不得进行商业使用。

### 知识产权与侵权联系

本项目中的自有代码、文档、截图、图标和其他素材分别按照其适用的许可或权利声明使用。抖音名称、商标、页面内容、用户内容及相关数据的权利归相应权利人所有，本项目不主张取得任何相关权利。

如认为仓库中的特定文件侵犯了您的合法权益，请通过 [GitHub Issues](https://github.com/halfwaystudent/douyin-sparkflow/issues) 提交通知，并提供涉嫌侵权的具体文件路径或内容、权利归属或授权证明、侵权理由及可用于回复的联系方式。请勿在公开 Issue 中提交身份证件、账号凭据或其他敏感材料。维护者将在收到完整通知后进行核查，并根据核查结果采取替换、删除、断开访问或其他适当处理措施。

---

## 📝 更新日志

查看 [CHANGELOG.md](DouYinSparkFlow/CHANGELOG.md) 了解版本更新历史。

**最新更新** (2026-08-27):
- ✨ 重新设计 Web UI，全新视觉风格
- 🎨 新增亮色/暗色主题切换
- 📱 优化移动端响应式布局
- 🔄 改进好友列表刷新可靠性
- 🚀 优化一键部署流程
- 📖 新增截图使用指南
- 📄 自有代码许可证调整为 PolyForm Noncommercial License 1.0.0，并补充非官方、非商业使用及知识产权说明

---

## 🤝 贡献

本项目目前作为个人项目维护，暂不接受外部 Pull Request。

如果你有：
- 🐛 **发现问题** - 欢迎提交 [Issue](https://github.com/halfwaystudent/douyin-sparkflow/issues) 报告 Bug
- 💡 **功能建议** - 欢迎在 Issue 中提出改进想法
- 🤔 **使用疑问** - 可以在 [Linux Do 社区](https://linux.do) 或 Issue 中讨论

感谢你的理解和支持！

---

## 📄 许可证

本项目自有代码采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，仅授权非商业用途。本项目为公开源码项目，不授予商业使用权；如需商业使用，请事先取得版权持有人的书面授权。

第三方依赖、图标、字体、图片、截图、商标、平台内容及其他外部素材不自动继承本项目许可证，请以各自目录中的许可证、授权文件或上游项目声明为准；例如 Lucide 图标的许可证见 [`DouYinSparkFlow/webui/static/lucide-LICENSE.txt`](DouYinSparkFlow/webui/static/lucide-LICENSE.txt)。

---

## 🌟 Star History

[![Star History Chart](https://api.star-history.com/svg?repos=halfwaystudent/douyin-sparkflow&type=Date)](https://star-history.com/#halfwaystudent/douyin-sparkflow&Date)

---

## 🔗 相关链接

- **项目主页**: [GitHub Repository](https://github.com/halfwaystudent/douyin-sparkflow)
- **社区讨论**: [Linux Do 社区](https://linux.do)
- **使用文档**: [docs/usage.md](docs/usage.md)
- **问题反馈**: [Issues](https://github.com/halfwaystudent/douyin-sparkflow/issues)

---

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

Made with ❤️ by [halfwaystudent](https://github.com/halfwaystudent)

</div>
