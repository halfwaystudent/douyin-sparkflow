# 只读服务状态快照

未向 Web 容器开放 Docker 的部署，可设置 `SPARKFLOW_HOST_STATUS_FILE`，让管理员的 `/api/ops/host-status` 从只读 JSON 文件取状态。未设置此变量时，保留原有 Docker 查询路径。普通用户和未登录访问仍被拒绝。

## 宿主机采集

将 `scripts/export_host_status.py` 安装到 **Web 容器无法写入、也未挂载给 Web 的宿主机目录**，由宿主机定时任务运行。不要让高权限采集器执行 Web 的可写源码挂载中的脚本。

独立预览的采集参数：

```sh
python3 /opt/douyin-sparkflow-preview/host-status/export_host_status.py \
  --output /opt/douyin-sparkflow-preview/state/service-status/status.json \
  --service douyin-web=spark-preview-web \
  --service login-desktop=spark-preview-login
```

- 建议每 15 秒采集，单次查询每个映射容器最多等待 3 秒。
- 只执行指定容器的 `docker container inspect --format '{{.State.Status}}'`，不会启动、停止或重启容器。
- 映射参数省略的逻辑服务显示“未部署”；定时调度会明确标注“未启用定时发送”。
- 不带任何映射参数时，默认查询标准部署的四个同名容器。
- 导出文件仅含固定逻辑服务名、状态、中文状态说明和时间，不含真实容器名、环境变量、标签、账号或凭据。
- 文件在同目录原子替换；应只读挂载整个目录，而不是单个文件，以便更新立即可见。

## Web 接入

将宿主机快照目录只读挂载到 `/run/service-status`，设置：

```text
SPARKFLOW_HOST_STATUS_FILE=/run/service-status/status.json
```

不需要向 Web 安装 Docker CLI、挂载 Docker socket 或开放 Docker 网络 API。

Web 每次读取会检查格式、32 KiB 大小上限、固定四个角色、状态枚举及时间有效性。超过 60 秒、无时区或明显未来的时间戳视为无法确认状态；不会回退到 Docker，也不会继续显示之前的绿色状态。只读状态采集不是发送调度器，不会启用自动发送或打开抖音浏览器。
