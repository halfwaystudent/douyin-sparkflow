# 数据存储与恢复

## 加密范围

配置存储密钥后，以下运行文件使用 Fernet 认证加密，文件损坏或密钥错误会阻止读取和覆盖：

- `config.json`：运行设置与通知渠道配置。
- `usersData.json`：账号、Cookie、好友和发送状态。
- `webui_settings.json`：管理员密码哈希、会话签名密钥及面板设置。
- `webui_users.json`：普通用户密码哈希、权限、注册策略与邀请摘要。
- `webui_passkeys.json`：Passkey 公钥及临时验证挑战。
- `webui_sessions.json`：尚未过期的会话撤销记录。
- `.data-backups/` 下的账号 JSON 备份。

密码仍使用不可逆哈希，不保存原始密码。Passkey 私钥和生物识别数据由设备或密码管理器保管，不上传服务器。邀请密钥只在生成页面显示，存储中只有摘要。

这层加密没有覆盖 Chromium Profile、日志、截图和登录桌面内部的所有文件。它们仍需依靠目录权限、受限访问、备份权限和主机或云盘加密保护。加密无法阻止已经控制服务器、能读取密钥或进程内存的人。

日志查看与下载仅允许应用日志目录中的 `.log` 文件，不能通过面板日志路径读取存储密钥或其他运行文件。日志预览最多读取末尾 512 KiB，日志轮转或短暂不可读时不会拖垮页面。

## 首次生成密钥

仓库根目录执行：

```bash
python3 deploy/init-storage-key.py
```

生成位置为 `state/keys/storage.key`，POSIX 权限为 600。不要把它提交到 Git，也不要复制到应用源代码目录。共享配置只给 Web、调度和一次性任务只读挂载密钥；登录桌面不挂载平台数据库、全部日志或存储密钥。密钥丢失后，修改密码不能恢复加密数据。

## 迁移已有数据

升级前先停止所有应用服务，备份现有数据，并将备份放在受限位置。不要在定时发送、登录或 Web 服务仍运行时迁移。

```bash
docker compose -f docker-compose.yml -f docker-compose.shared.yml down
docker compose -f docker-compose.yml -f docker-compose.shared.yml build web
docker compose -f docker-compose.yml -f docker-compose.shared.yml run --rm --no-deps web python scripts/migrate_secure_storage.py
docker compose -f docker-compose.yml -f docker-compose.shared.yml run --rm --no-deps web python scripts/migrate_secure_storage.py --check
docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d web scheduler
```

迁移程序先检查所有现有文件是否可读，再逐个加密并原子替换。失败后可以修复原因再运行；不要删除原数据或另建一个密钥来强行启动。程序也会迁移 `.data-backups/` 中的旧账号备份。未纳入迁移的日志和外部旧备份仍需自行保护或安全清理。

非容器运行时，先安装依赖，将 `SPARKFLOW_STORAGE_KEY_FILE` 设置为主机上的密钥路径，再运行相同脚本。设置 `SPARKFLOW_STORAGE_REQUIRE_ENCRYPTED=1` 后将拒绝明文运行文件。

## 密钥轮换

密钥文件支持每行一个密钥。停止服务，把新密钥放在第一行、旧密钥留在后面，保持文件权限 600，再运行迁移和 `--check`。新的写入使用第一行密钥，读取时也可使用后面的旧密钥。

确认全部数据、账号备份和需要保留的外部备份均已迁移后，才移除旧密钥。先用独立副本验证恢复，不要直接拿生产数据做试验。

## 恢复

同时恢复互相对应的数据文件和密钥，在停止服务的状态下运行检查，再启动单个 Web 实例。近期备份可能包含旧用户权限、旧邀请或旧会话撤销状态；恢复后需要管理员重新核对用户归属并撤销旧会话。不要只恢复某一个用户文件而忽略账号分配和会话版本。

本版保留单 Web 进程和文件存储的限制。公开注册需要管理员自行评估运行资源和滥用风险；默认的审核、总人数限制和注册限流不等于完整的反滥用系统。
