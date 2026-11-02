(() => {
  const panel = document.querySelector("[data-host-status]");
  if (!panel) return;
  const message = panel.querySelector("[data-host-status-message]");
  const list = panel.querySelector("[data-host-status-list]");
  const retry = panel.querySelector("[data-host-status-retry]");
  const names = {"douyin-web": "控制台", "douyin-scheduler": "定时调度", "login-desktop": "登录工作区", "mihomo": "备用代理"};
  let busy = false, loaded = false, controller = null;
  const refresh = async () => {
    if (busy || document.visibilityState !== "visible") return;
    busy = true;
    retry.disabled = true;
    message.textContent = "正在检查服务状态，其他操作可以继续。";
    controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch("/api/ops/host-status", {credentials: "same-origin", cache: "no-store", signal: controller.signal});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!data.ok) throw new Error();
      list.textContent = "";
      const rows = (data.containers || []).filter((row) => names[row.Names]);
      rows.forEach((row) => {
        const item = document.createElement("li");
        const dot = document.createElement("span");
        dot.className = `dot ${row.State === "running" ? "ok" : "bad"}`;
        const status = document.createElement("span");
        status.className = "muted compact";
        status.textContent = row.Status || "";
        item.append(dot, names[row.Names], status);
        list.appendChild(item);
      });
      list.hidden = !rows.length;
      message.textContent = rows.length ? "服务状态已更新。" : "面板暂未读到服务状态，可在服务器核对。";
      loaded = true;
    } catch {
      message.textContent = "暂未取到服务状态，不影响页面其他操作。稍后可以重新检查。";
    } finally {
      clearTimeout(deadline);
      retry.hidden = false;
      retry.disabled = false;
      busy = false;
    }
  };
  retry.addEventListener("click", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") controller?.abort();
    else if (!loaded) refresh();
  });
  window.addEventListener("pagehide", () => controller?.abort());
  refresh();
})();
