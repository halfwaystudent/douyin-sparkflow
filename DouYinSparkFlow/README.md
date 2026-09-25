# DouYinSparkFlow - 核心应用

> 抖音多账号火花自动维护系统 - 核心源码模块

这是 DouYin SparkFlow 的核心应用源码目录，包含所有业务逻辑、Web 界面和自动化任务实现。

> ⚠️ 本项目为非官方的第三方公开源码项目，与抖音及其关联方不存在隶属、授权、赞助或合作关系。本目录中的自有代码采用 [PolyForm Noncommercial License 1.0.0](../LICENSE)，仅授权非商业用途。使用者只能操作本人拥有或已获得明确授权的账号，并须自行遵守适用的平台规则和法律法规。

---

## 📁 目录结构

### `core/` - 核心功能模块

核心业务逻辑实现，包括浏览器自动化、消息发送、好友管理等。

| 文件 | 说明 | 大小 |
|------|------|------|
| `browser.py` | 浏览器控制和页面操作 | 6.9 KB |
| `cookies.py` | Cookie 解析、校验与登录态判定 | 6.5 KB |
| `friends.py` | 好友列表管理和刷新逻辑 | 29.0 KB |
| `login.py` | 登录流程控制 | 7.3 KB |
| `msg_builder.py` | 消息内容构建（一言、祝福等） | 4.5 KB |
| `protocol_dispatch.py` | 协议分发和路由 | 25.5 KB |
| `protocol_sender.mjs` | 消息发送协议（Node.js 脚本） | 29.2 KB |
| `send_state.py` | 统一判定强确认、待核验和当日发送状态 | 1.9 KB |
| `streak_state.py` | 目标状态机、在途租约与失败分类 | 59.1 KB |
| `tasks.py` | **任务调度核心**（定时任务、状态管理） | 144.3 KB |
| `unicode_casefold.mjs` | 生成的大小写折叠表（供 `protocol_sender.mjs` 匹配昵称） | 约 0.96 MB |

> 表中大小会随开发变化，仅作规模参考。

### `webui/` - Web 管理界面

基于 FastAPI 的 Web 管理控制台，提供可视化操作界面。

#### 后端模块

| 文件 | 说明 |
|------|------|
| `app.py` | FastAPI 主应用，路由定义 |
| `auth.py` | 用户认证和会话管理 |
| `ops.py` | 操作接口（启动/停止任务、刷新好友等） |

#### 前端资源

```
webui/
├── static/              # 静态资源
│   ├── app.css         # 主题样式（亮色/暗色）
│   ├── app.js          # 主题切换脚本
│   ├── lucide.min.js      # 本地化图标库
│   ├── lucide-LICENSE.txt # 图标库许可证
│   ├── styles.css      # 基础样式
│   └── multiPagePlugins/  # 浏览器扩展插件
└── templates/          # HTML 模板
    ├── base.html       # 基础布局模板
    ├── dashboard.html  # 仪表盘（主界面）
    ├── login.html      # 登录页
    ├── send_console.html  # 发送控制台
    └── logs.html       # 日志查看
```

### `utils/` - 工具模块

通用辅助功能和配置管理。

| 文件 | 说明 |
|------|------|
| `config.py` | 配置文件加载和管理 |
| `logger.py` | 日志系统配置 |
| `hitokoto.py` | 一言 API 接口封装 |
| `github_action_config.py` | GitHub Actions 配置生成 |

### `scripts/` - 辅助脚本

| 文件 | 说明 |
|------|------|
| `cron_runner.py` | Cron 任务运行器 |
| `start_login_desktop.sh` | 登录桌面启动脚本 |

### `docs/` - 截图资源

| 目录 | 说明 |
|----------|------|
| `images/` | 界面截图和示意图 |

---

## 🚀 运行方式

### 开发环境运行

#### 1. 安装依赖

```bash
# 核心依赖
pip install -r requirements.txt

# Web UI 依赖
pip install -r requirements-web.txt

# 安装 Playwright 浏览器
playwright install chromium
```

#### 2. 启动方式

**Web 管理模式**（推荐）：
```bash
python main.py --web
# 访问 http://localhost:8787
```

**命令行模式**：
```bash
python main.py
# 打开交互式菜单（可执行任务、导出 USER_DATA 等）
```

**登录桌面服务**：
```bash
python login_desktop_server.py
# 启动登录桌面 API（用于扫码登录）
```

### Docker 容器运行

参考根目录的 `docker-compose.yml` 配置：

```bash
# 构建镜像
docker build -f Dockerfile.server -t douyin-sparkflow:local .

# 运行容器
docker run -d \
  -p 8787:8787 \
  -v $(pwd):/app \
  douyin-sparkflow:local
```

---

## ⚙️ 配置文件

### `config.example.json` 与 `config.json` - 应用配置

`config.example.json` 是公开模板；首次运行会生成不受 Git 跟踪的 `config.json`，用于保存 Web 中修改的发送窗口、好友扫描、浏览器 Profile 和消息策略：

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

| 配置项 | 说明 |
|--------|------|
| `dailySendWindow` | 每日发送窗口和调度间隔 |
| `sendStrategy` | 账号启动延迟、消息间隔及消息变体 |
| `friendListScan` | 好友列表扫描时限、空闲等待和滚动参数 |
| `persistentBrowserProfiles` | 持久化 Playwright Profile 及 Cookie 同步策略 |
| `browserSenderAccounts` | 强制使用浏览器发送的账号列表；公开模板默认为空 |

### `usersData.json` - 用户数据

存储账号信息、好友列表、发送记录等运行时数据。

⚠️ **此文件包含敏感数据，不应提交到 Git 仓库**

示例结构：
```json
[
  {
    "unique_id": "123456789",
    "username": "账号显示名",
    "cookies": [],
    "targets": ["目标好友"],
    "enabled": true,
    "message_history": {},
    "failure_queue": {}
  }
]
```

### `webui_settings.json` - Web UI 设置

Web 管理界面的配置（管理员密码、端口等）。

⚠️ **此文件包含敏感数据，不应提交到 Git 仓库**

---

## 🔌 API 接口

Web UI 提供以下 RESTful API 接口：

### 账号管理

- `GET /api/accounts` - 获取账号列表
- `POST /api/accounts/refresh` - 刷新好友列表
- `DELETE /api/accounts/{id}` - 删除账号

### 任务控制

- `POST /api/tasks/start` - 启动定时任务
- `POST /api/tasks/stop` - 停止定时任务
- `GET /api/tasks/status` - 获取任务状态

### 登录管理

- `GET /api/login/qrcode` - 获取登录二维码
- `GET /api/login/status` - 检查登录状态
- `POST /api/login/logout` - 登出账号

### 消息发送

- `POST /api/send/manual` - 手动发送消息
- `GET /api/send/history` - 获取发送历史

详细 API 文档请查看 `webui/app.py` 中的路由定义。

---

## 🔧 核心工作流程

### 1. 登录流程

```
用户扫码 → browser.py 打开登录页
       → login.py 生成二维码
       → 用户扫码确认
       → 保存登录态到 state/
       → 返回登录成功
```

### 2. 好友列表刷新

```
触发刷新 → friends.py 启动浏览器
        → 访问好友列表页面
        → 解析好友数据（昵称、火花状态等）
        → 保存到 usersData.json
        → 关闭浏览器
```

### 3. 消息发送流程

```
定时触发 → tasks.py 检查发送条件
        → 筛选需要发送的好友
        → msg_builder.py 构建消息内容
        → 按配置选择 Playwright 浏览器发送或 protocol_sender.mjs 协议发送
        → 等待强证据发送确认
        → 记录发送历史
        → 更新下次发送时间
```

### 4. 任务调度逻辑

`tasks.py` 是核心调度器，负责：

- ⏰ 定时检查发送窗口
- 🔄 循环遍历所有账号
- 📊 统计发送成功/失败
- 🛡️ 失败保护（冷却机制）
- 📝 日志记录

---

## 🐛 调试和开发

### 日志系统

日志文件位于 `logs/` 目录：

```
logs/
├── app.log              # 应用主日志
├── webui.log           # Web UI 日志
├── tasks.log           # 任务调度日志
└── browser.log         # 浏览器操作日志
```

查看实时日志：
```bash
tail -f logs/app.log
```

### 开发建议

1. **修改模板文件**：编辑 `webui/templates/*.html`，刷新浏览器即可看到效果（自动重载已启用）
2. **修改 Python 代码**：需要重启服务才能生效
3. **调试浏览器操作**：设置 `browser_headless: false` 可以看到浏览器窗口
4. **测试消息发送**：使用 Web UI 的"手动发送"功能，避免等待定时任务

### 常见问题

**Q: 登录二维码不显示？**  
A: 检查 `login_desktop_server.py` 是否正常运行，端口 18090 是否被占用。

**Q: 浏览器启动失败？**  
A: Windows 本地模式请先运行 `.\scripts\start_login_desktop.ps1`；同时确保已安装 Playwright：`playwright install chromium`

**Q: 消息发送失败？**  
A: 检查网络连接，查看 `logs/app.log` 或 Web 运行日志中的错误信息。

**Q: Web UI 无法访问？**  
A: 检查端口 8787 是否被占用，防火墙是否允许该端口。

---

## 📦 依赖说明

### `requirements.txt` - 核心依赖

```
playwright>=1.40.0      # 浏览器自动化
自定义 cron_runner.py      # 任务调度（无需额外 Python 依赖）
```

### `requirements-web.txt` - Web 依赖

```
fastapi>=0.104.0        # Web 框架
uvicorn>=0.24.0         # ASGI 服务器
jinja2>=3.1.0           # 模板引擎
```

完整依赖列表请查看对应的 `requirements*.txt` 文件。

---

## 🔐 安全注意事项

### 敏感文件保护

以下文件**绝对不要**提交到 Git 仓库：

- ❌ `config.json` - 运行时发送配置
- ❌ `usersData.json` - 包含账号数据和好友信息
- ❌ `webui_settings.json` - 包含管理员密码
- ❌ `.env` - 包含环境变量和密钥
- ❌ `state/` - 包含浏览器登录态
- ❌ `logs/` - 可能包含敏感日志
- ❌ `.im_sdk_cache/` - IM SDK 缓存

已在 `.gitignore` 中配置忽略这些文件。

### 密码安全

- 修改 `webui_settings.json` 中的默认管理员密码
- 不要在配置文件中明文存储密码
- 使用环境变量管理敏感配置

---

## 📚 相关文档

- [项目主 README](../README.md) - 整体介绍和快速开始
- [使用文档](../docs/usage.md) - 详细使用教程
- [更新日志](../CHANGELOG.md) - 版本更新历史
- [Docker 部署](../docker-compose.yml) - 容器编排配置

---

## 🤝 贡献指南

欢迎提交代码改进和功能建议！

开发规范：
- Python 代码遵循 PEP 8 规范
- 提交前运行测试确保功能正常
- 添加必要的代码注释
- 更新相关文档

---

## 📄 许可证

本项目自有代码采用 [PolyForm Noncommercial License 1.0.0](../LICENSE)，仅授权非商业用途。未经版权持有人事先书面授权，不得将本项目用于收费服务、商业运营、商业账号管理、营销推广、客户代运营、商业产品集成或其他商业用途。

第三方依赖、图标、字体、图片、截图、商标、平台内容及其他外部素材以各自许可证、授权文件或权利声明为准；Lucide 图标许可证见 [`webui/static/lucide-LICENSE.txt`](webui/static/lucide-LICENSE.txt)。

---

<div align="center">

**返回 [项目主页](../README.md)**

Made with ❤️ by [halfwaystudent](https://github.com/halfwaystudent)

</div>
