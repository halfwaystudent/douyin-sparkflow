(() => {
  const root = document.documentElement;
  const storageKey = "sparkflow-theme";

  const storedTheme = () => {
    try {
      return localStorage.getItem(storageKey);
    } catch {
      return null;
    }
  };

  const applyTheme = (theme) => {
    const value = theme === "light" ? "light" : "dark";
    root.dataset.theme = value;
    root.style.colorScheme = value;
    try {
      localStorage.setItem(storageKey, value);
    } catch {
      // The active page can still switch themes when storage is unavailable.
    }
  };

  applyTheme(storedTheme() || "dark");
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      applyTheme(root.dataset.theme === "light" ? "dark" : "light");
    });
  });
})();

(() => {
  const body = document.body;
  document.querySelectorAll("[data-nav-toggle]").forEach((button) => {
    button.addEventListener("click", () => body.classList.add("nav-open"));
  });
  document.querySelectorAll("[data-nav-close]").forEach((button) => {
    button.addEventListener("click", () => body.classList.remove("nav-open"));
  });
  document.querySelectorAll(".nav-item").forEach((link) => {
    link.addEventListener("click", () => body.classList.remove("nav-open"));
  });
})();

(() => {
  const dialog = document.getElementById("confirm-dialog");
  if (!dialog) return;
  const title = document.getElementById("confirm-title");
  const message = document.getElementById("confirm-message");
  const accept = dialog.querySelector("[data-confirm-accept]");
  const cancel = dialog.querySelector("[data-confirm-cancel]");
  let pendingForm = null;
  let pendingLink = "";
  let pendingButton = null;

  const openDialog = (node) => {
    const source = node.closest("[data-confirm]") || node;
    title.textContent = source.dataset.confirmTitle || "确认操作";
    message.textContent =
      source.dataset.confirm ||
      "该操作会立即影响续火花任务，请确认是否继续。";
    accept.textContent = source.dataset.confirmAccept || "确认执行";
    accept.className =
      source.dataset.confirmTone === "primary"
        ? "button button-primary"
        : "button button-danger";
    dialog.showModal();
  };

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      pendingForm = form;
      pendingLink = "";
      pendingButton = null;
      openDialog(form);
    });
  });

  document.querySelectorAll("a[data-confirm]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      pendingForm = null;
      pendingLink = link.href;
      pendingButton = null;
      openDialog(link);
    });
  });

  document.querySelectorAll("button[data-confirm]").forEach((button) => {
    button.addEventListener(
      "click",
      (event) => {
        if (button.dataset.confirmApproved === "1") {
          delete button.dataset.confirmApproved;
          return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        pendingForm = null;
        pendingLink = "";
        pendingButton = button;
        openDialog(button);
      },
      true,
    );
  });

  cancel.addEventListener("click", () => {
    pendingForm = null;
    pendingLink = "";
    pendingButton = null;
    dialog.close();
  });

  accept.addEventListener("click", () => {
    const form = pendingForm;
    const href = pendingLink;
    const button = pendingButton;
    pendingForm = null;
    pendingLink = "";
    pendingButton = null;
    dialog.close();
    if (form) {
      HTMLFormElement.prototype.submit.call(form);
    } else if (href) {
      window.location.assign(href);
    } else if (button) {
      button.dataset.confirmApproved = "1";
      button.click();
    }
  });

  dialog.addEventListener("cancel", () => {
    pendingForm = null;
    pendingLink = "";
    pendingButton = null;
  });
})();

(() => {
  document.querySelectorAll("[data-segment-group]").forEach((group) => {
    const buttons = [...group.querySelectorAll("[data-segment-target]")];
    const owner = group.closest("[data-segment-owner]") || document;
    const panels = [...owner.querySelectorAll("[data-segment-panel]")];
    const activate = (name) => {
      buttons.forEach((button) => {
        const active = button.dataset.segmentTarget === name;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.segmentPanel !== name;
      });
    };
    buttons.forEach((button) => {
      button.addEventListener("click", () =>
        activate(button.dataset.segmentTarget),
      );
    });
    const initial =
      buttons.find((button) => button.classList.contains("active")) ||
      buttons[0];
    if (initial) activate(initial.dataset.segmentTarget);
  });
})();

(() => {
  const overviewRoots = document.querySelectorAll("[data-overview-root]");
  if (!overviewRoots.length) return;
  let previousRunning = null;
  let timer = null;

  const formatTime = (raw) => {
    if (!raw) return "-";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(parsed);
  };

  const setText = (selector, value) => {
    document.querySelectorAll(selector).forEach((node) => {
      node.textContent = String(value ?? "");
    });
  };

  const updateTaskBanner = (task) => {
    document.querySelectorAll("[data-task-banner]").forEach((banner) => {
      banner.className = "status-banner";
      if (task.running) {
        banner.classList.add("warning");
        banner.querySelector("[data-task-text]").textContent =
          `发送任务运行中，已运行约 ${task.ageSeconds || 0} 秒`;
      } else if (task.stale) {
        banner.classList.add("info");
        banner.querySelector("[data-task-text]").textContent =
          "检测到过期任务锁，下次启动任务时会自动清理";
      } else {
        banner.classList.add("success");
        banner.querySelector("[data-task-text]").textContent =
          "当前没有发送任务运行";
      }
    });
  };

  const updateAccounts = (accounts) => {
    accounts.forEach((account) => {
      const selector = `[data-account-overview="${CSS.escape(account.uniqueId)}"]`;
      document.querySelectorAll(selector).forEach((row) => {
        row.dataset.accountState = account.state;
        row.querySelectorAll("[data-account-confirmed]").forEach((node) => {
          node.textContent = account.confirmed;
        });
        row.querySelectorAll("[data-account-attention]").forEach((node) => {
          node.textContent = account.attention;
        });
        row.querySelectorAll("[data-account-pending]").forEach((node) => {
          node.textContent = account.pending;
        });
        row.querySelectorAll("[data-account-progress]").forEach((node) => {
          const pct = account.total
            ? Math.round((account.confirmed / account.total) * 100)
            : 0;
          node.style.width = `${pct}%`;
        });
        row.querySelectorAll("[data-account-progress-text]").forEach((node) => {
          node.textContent = `${account.confirmed}/${account.total}`;
        });
      });
    });
  };

  const updateActions = (summary, running) => {
    const counts = {
      attention: summary.attention,
      pending: summary.pending + summary.unprocessed,
      total: summary.total,
    };
    document.querySelectorAll("[data-action-count-source]").forEach((button) => {
      const count = counts[button.dataset.actionCountSource] || 0;
      button.disabled = running || count <= 0;
      const countNode = button.querySelector("[data-action-count]");
      if (countNode) countNode.textContent = count;
    });
    document.querySelectorAll("[data-disable-while-running]").forEach((button) => {
      if (!button.dataset.actionCountSource) {
        button.disabled = running;
      }
    });
  };

  const render = (data) => {
    const summary = data.summary || {};
    const task = data.task || {};
    updateTaskBanner(task);
    setText("[data-overview-value='total']", summary.total || 0);
    setText("[data-overview-value='confirmed']", summary.confirmed || 0);
    setText(
      "[data-overview-value='sentTotal']",
      (summary.confirmed || 0) + (summary.pageEcho || 0),
    );
    setText("[data-overview-value='pageEcho']", summary.pageEcho || 0);
    setText("[data-overview-value='attention']", summary.attention || 0);
    setText(
      "[data-overview-value='pending']",
      (summary.pending || 0) + (summary.unprocessed || 0),
    );
    setText("[data-overview-value='remaining']", summary.remaining || 0);
    setText(
      "[data-overview-value='progress']",
      `${summary.confirmed || 0}/${summary.total || 0}`,
    );
    setText(
      "[data-overview-value='progressPercent']",
      summary.total
        ? `${Math.round((summary.confirmed / summary.total) * 100)}%`
        : "0%",
    );
    setText(
      "[data-overview-value='lastConfirmedAt']",
      formatTime(summary.lastConfirmedAt),
    );
    setText(
      "[data-overview-value='nextTriggerAt']",
      formatTime(data.schedule?.nextTriggerAt),
    );
    setText(
      "[data-overview-value='scheduleLabel']",
      data.schedule?.label || "-",
    );
    const remainingForNext = data.schedule?.remainingTargets || 0;
    setText(
      "[data-overview-value='nextTriggerHint']",
      data.schedule?.hasWorkNextTrigger
        ? `仍有 ${remainingForNext} 个待发送目标`
        : "当前没有待发送目标，下一次触发不会实际发送",
    );
    updateAccounts(data.accounts || []);
    updateActions(summary, Boolean(task.running));

    if (previousRunning === true && !task.running) {
      document
        .querySelectorAll("[data-refresh-notice]")
        .forEach((node) => node.classList.add("visible"));
    }
    previousRunning = Boolean(task.running);
    document.querySelectorAll("[data-overview-live-state]").forEach((node) => {
      node.textContent = "实时";
      node.classList.remove("poll-stale");
    });
  };

  const refresh = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const response = await fetch("/api/ops/overview", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch {
      document.querySelectorAll("[data-overview-live-state]").forEach((node) => {
        node.textContent = "更新延迟";
        node.classList.add("poll-stale");
      });
    }
  };

  document.querySelectorAll("[data-refresh-page]").forEach((button) => {
    button.addEventListener("click", () => window.location.reload());
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
  timer = window.setInterval(refresh, 10000);
  window.addEventListener("pagehide", () => window.clearInterval(timer));
})();

(() => {
  const root = document.getElementById("login-desktop-controls");
  if (!root) return;
  const section = document.getElementById("interactive-login-section");
  const csrfToken = root.dataset.csrfToken || "";
  const displayMode = root.dataset.displayMode || "novnc";
  const configuredPublicUrl = root.dataset.publicUrl || "";
  const publicUrl = (() => {
    if (!configuredPublicUrl) return "";
    try {
      return new URL(configuredPublicUrl, window.location.href).href;
    } catch {
      return configuredPublicUrl;
    }
  })();
  const runtimeState = document.getElementById("login-desktop-runtime-state");
  const statusText = document.getElementById("login-desktop-status-text");
  const frame = document.querySelector("[data-login-frame]");
  const frameWrap = document.querySelector(".desktop-frame-wrap");
  const nativePanel = document.querySelector("[data-native-login]");
  const copyLoginUrlButton = document.querySelector("[data-copy-login-url]");
  const qrImage = document.querySelector("[data-login-qr]");
  const qrStatus = document.querySelector("[data-login-qr-status]");
  let timer = null;
  let heartbeatTimer = null;
  let countdownTimer = null;
  let qrRefreshTimer = null;
  let workspace = { state: "closed", active: false, position: 0, ticket: "" };
  if (displayMode === "native" && copyLoginUrlButton) copyLoginUrlButton.hidden = true;
  if (displayMode === "native" && frameWrap) frameWrap.hidden = true;

  const setStatus = (text, tone = "") => {
    if (statusText) statusText.textContent = text;
    if (runtimeState) {
      runtimeState.className = `pill${tone ? ` ${tone}` : ""}`;
      runtimeState.textContent = tone === "success" ? "使用中" : tone === "danger" ? "异常" : tone === "warning" ? "排队中" : "已关闭";
    }
  };

  const postForm = async (url, payload = {}) => {
    const formData = new FormData();
    formData.set("csrf_token", csrfToken);
    Object.entries(payload).forEach(([key, value]) => formData.set(key, String(value ?? "")));
    const response = await fetch(url, { method: "POST", body: formData, credentials: "same-origin" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const error = new Error(data.error || `请求失败：${response.status}`);
      // Keep the server's grading (category / categoryLabel / retryable) so
      // callers can render an actionable message instead of a bare string.
      error.payload = data;
      throw error;
    }
    return data;
  };

  const loadFrame = (force = false) => {
    if (displayMode === "native") return;
    if (frame && (force || frame.dataset.loaded !== "1") && frame.dataset.src) {
      frame.src = frame.dataset.src;
      frame.dataset.loaded = "1";
    }
  };

  const closeFrame = () => {
    if (nativePanel) nativePanel.hidden = true;
    if (frameWrap) {
      frameWrap.classList.remove("native-login-mode");
      if (displayMode === "native") frameWrap.hidden = true;
    }
    if (qrImage) {
      qrImage.hidden = true;
      const previous = qrImage.dataset.objectUrl || "";
      if (previous) URL.revokeObjectURL(previous);
      delete qrImage.dataset.objectUrl;
      qrImage.removeAttribute("src");
    }
    if (!frame) return;
    frame.removeAttribute("src");
    frame.dataset.loaded = "0";
  };

  const renderWorkspace = (next) => {
    const previousState = workspace.state;
    const previousTicket = workspace.ticket;
    workspace = next || { state: "closed", active: false, position: 0, ticket: "" };
    if (workspace.state === "queued") {
      setStatus(`登录工作区排队中，前面还有 ${Math.max(0, Number(workspace.position || 1) - 1)} 人。`, "warning");
      if (qrStatus) qrStatus.textContent = "排队成功，轮到你后会自动打开登录二维码。";
      return;
    }
    if (workspace.state === "resetting") {
      setStatus("正在清理上一位用户的登录环境，请稍候。", "warning");
      return;
    }
    if (workspace.state === "active" && workspace.active) {
      const remaining = Math.max(0, Number(workspace.remaining_seconds || 0));
      const tone = remaining > 0 && remaining <= 60 ? "warning" : "success";
      const promoted =
        (previousState !== "active" || previousTicket !== workspace.ticket) &&
        lastPromotedTicket !== workspace.ticket;
      if (promoted) {
        // Only take over once per ticket: re-loading the heavy noVNC frame and
        // re-fetching the QR code on every poll burns the single login-desktop
        // page lock and makes the whole login flow feel sluggish.
        lastPromotedTicket = workspace.ticket;
        if (section && !section.open) section.open = true;
        loadFrame(true);
        qrPollStartedAt = Date.now();
        setQrButtons("读取中…", { busy: true });
        refreshLoginQr(500);
      }
      if (displayMode === "native") {
        if (nativePanel) nativePanel.hidden = false;
        if (frameWrap) {
          frameWrap.hidden = false;
          frameWrap.classList.add("native-login-mode");
        }
        setStatus(
          promoted
            ? `已轮到你：Windows 本地登录浏览器已打开，剩余 ${remaining} 秒。完成扫码后请保存登录态。`
            : `Windows 本地登录浏览器已打开，剩余 ${remaining} 秒。完成扫码后请保存登录态。`,
          tone,
        );
      } else {
        setStatus(
          promoted
            ? `已轮到你：登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`
            : `登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`,
          tone,
        );
      }
      return;
    }
    setStatus("登录工作区当前关闭。请从账号卡片点击“重新登录”。");
  };

  let qrPollStartedAt = 0;
  let lastPromotedTicket = "";
  const setQrButtons = (label, { busy = false, stopped = false } = {}) => {
    document.querySelectorAll("[data-refresh-login-qr]").forEach((button) => {
      const text = button.querySelector("span");
      if (text) text.textContent = label;
      button.disabled = busy;
      button.dataset.qrStopped = stopped ? "1" : "";
    });
  };
  const qrWaitLabel = () => {
    if (!qrPollStartedAt) return "";
    return `（已等待 ${Math.round((Date.now() - qrPollStartedAt) / 1000)} 秒）`;
  };

  const refreshLoginQr = async (delay = 0, retries = 400) => {
    if (!qrImage || workspace.state !== "active" || !workspace.active) return;
    window.clearTimeout(qrRefreshTimer);
    qrRefreshTimer = window.setTimeout(async () => {
      if (qrStatus) qrStatus.textContent = `正在读取登录二维码…${qrWaitLabel()}`;
      const retryLater = (message, delayMs = 3000) => {
        if (retries > 1 && workspace.state === "active") {
          if (qrStatus) qrStatus.textContent = `${message} 继续等待…${qrWaitLabel()}`;
          refreshLoginQr(delayMs, retries - 1);
        } else if (qrStatus) {
          qrStatus.textContent = `${message} 已停止自动重试，请点击“刷新二维码”。`;
          setQrButtons("已停止，点此重试", { stopped: true });
        }
      };
      try {
        const response = await fetch(`/login-desktop/qr?t=${Date.now()}`, { credentials: "same-origin", cache: "no-store" });
        if (response.status === 409) {
          // Only an explicit refresh regenerates an expired QR code, so stop
          // polling and put the button into a visible retry state.
          const data = await response.json().catch(() => ({}));
          if (qrStatus) qrStatus.textContent = data.message || "二维码已过期。点击“刷新二维码”重新生成。";
          setQrButtons("已停止，点此重试", { stopped: true });
          return;
        }
        if (response.status === 202) {
          const data = await response.json().catch(() => ({}));
          if (data.logged_in || data.state === "qr_logged_in") {
            if (qrStatus) qrStatus.textContent = data.message || "检测到浏览器里还保留着登录状态，已重置，正在生成新的二维码...";
          }
          retryLater(data.message || "浏览器正在生成二维码");
          return;
        }
        if (response.status === 503) {
          const data = await response.json().catch(() => ({}));
          const retryAfter = Number(response.headers.get("Retry-After") || data.retry_after || 0);
          retryLater(
            data.message || "登录页正在处理上一个请求",
            retryAfter > 0 ? retryAfter * 1000 : 1500,
          );
          return;
        }
        if (response.status === 502) {
          const data = await response.json().catch(() => ({}));
          // Distinguish "cannot reach Douyin" from "the login service is down";
          // the old copy blamed the login desktop for every upstream failure.
          const upstream = /douyin|network|proxy|timeout|超时|网络/i.test(
            String(data.message || data.error || ""),
          );
          if (qrStatus) {
            qrStatus.textContent =
              data.message ||
              data.error ||
              (upstream
                ? "无法访问抖音（网络或代理异常），请检查代理后重试。"
                : "登录桌面服务暂时不可用，请稍后点击刷新二维码。");
          }
          return;
        }
        if (!response.ok) throw new Error(String(response.status));
        const blob = await response.blob();
        if (blob.type && blob.type.includes("json")) {
          const data = await blob.text().then((text) => JSON.parse(text)).catch(() => ({}));
          if (data.message || data.error) {
            retryLater(data.message || data.error);
            return;
          }
        }
        const previous = qrImage.dataset.objectUrl || "";
        const objectUrl = URL.createObjectURL(blob);
        qrImage.src = objectUrl;
        qrImage.dataset.objectUrl = objectUrl;
        qrImage.hidden = false;
        if (previous) URL.revokeObjectURL(previous);
        if (qrStatus) qrStatus.textContent = `二维码已加载。如果过期，点击刷新。${qrWaitLabel()}`;
        setQrButtons("刷新二维码");
      } catch {
        retryLater("登录页正在加载");
      }
    }, delay);
  };

  const pollStatus = async () => {
    if (document.visibilityState !== "visible") return;
    try {
      const statusUrl = workspace.state === "active" ? "/login-desktop/status" : "/login-desktop/workspace-status";
      const response = await fetch(statusUrl, { credentials: "same-origin", cache: "no-store" });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        setStatus(data.error || "登录工作区不可用，请检查 login-desktop 服务。", "danger");
        return;
      }
      renderWorkspace(data.workspace);
      if (workspace.state === "active" && workspace.active) {
        loadFrame();
        if (data.logged_in) {
          setStatus(`当前浏览器已登录：${data.username}，请保存登录态。`, "success");
        } else if (data.login_state === "unknown") {
          // The page was mid-operation, so "not logged in" would be a guess.
          setStatus("正在检查登录状态…（页面正忙，请稍候再保存）", "warning");
        }
      } else {
        closeFrame();
      }
    } catch (error) {
      setStatus(`状态检查失败：${error.message}`, "danger");
    }
  };

  const heartbeat = async () => {
    if (workspace.state !== "active" || !workspace.active || !workspace.ticket) return;
    try {
      const data = await postForm("/login-desktop/heartbeat", { ticket: workspace.ticket });
      renderWorkspace(data.workspace);
    } catch (error) {
      workspace = { state: "closed", active: false, position: 0, ticket: "" };
      closeFrame();
      setStatus(`登录工作区已释放：${error.message}`, "danger");
    }
  };

  document.querySelectorAll(".login-desktop-open").forEach((button) => {
    button.addEventListener("click", async () => {
      if (section) section.open = true;
      try {
        const reloginUniqueId = button.dataset.reloginUniqueId || "";
        const mode = button.dataset.loginMode || (reloginUniqueId ? "relogin" : "add");
        const data = await postForm("/login-desktop/open", {
          mode,
          ...(reloginUniqueId ? { relogin_unique_id: reloginUniqueId } : {}),
        });
        renderWorkspace(data.workspace);
        if (data.state === "queued") return;
        loadFrame(true);
        refreshLoginQr(500);
        if (frame) frame.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        setStatus(`申请登录工作区失败：${error.message}`, "danger");
      }
    });
  });

  document.querySelectorAll("[data-focus-native-browser]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await postForm("/login-desktop/focus", { ticket: workspace.ticket });
        setStatus("已请求显示本机登录浏览器，请在桌面窗口中继续操作。", "success");
      } catch (error) {
        setStatus(`显示登录浏览器失败：${error.message}`, "danger");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-refresh-login-qr]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      qrPollStartedAt = Date.now();
      try {
        const refreshForm = new FormData();
        refreshForm.set("csrf_token", csrfToken);
        refreshForm.set("ticket", workspace.ticket);
        const response = await fetch("/login-desktop/qr/refresh", {
          method: "POST",
          body: refreshForm,
          credentials: "same-origin",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
          // A 202 with ok:false means the page did not produce a QR code yet.
          if (qrStatus) {
            qrStatus.textContent = `刷新二维码未完成：${data.message || data.error || response.status}`;
          }
          if (data.state === "logged_in" || data.logged_in) {
            if (qrStatus) {
              qrStatus.textContent = data.message || "检测到浏览器里还保留着登录状态，已重置，正在生成新的二维码...";
            }
          }
          refreshLoginQr(1200);
          return;
        }
        refreshLoginQr(500);
      } catch (error) {
        if (qrStatus) qrStatus.textContent = `刷新二维码失败：${error.message}`;
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-cookie-login]").forEach((panel) => {
    const input = panel.querySelector("[data-cookie-input]");
    const displayName = panel.querySelector("[data-cookie-display-name]");
    const reloginToggle = panel.querySelector("[data-cookie-relogin-mode]");
    const reloginRow = panel.querySelector("[data-cookie-relogin-row]");
    const reloginSelect = panel.querySelector("[data-cookie-relogin-select]");
    const submit = panel.querySelector("[data-cookie-login-submit]");
    const status = panel.querySelector("[data-cookie-login-status]");

    reloginToggle?.addEventListener("change", () => {
      if (reloginRow) reloginRow.hidden = !reloginToggle.checked;
    });

    submit?.addEventListener("click", async () => {
      const cookieInput = String(input?.value || "").trim();
      if (!cookieInput) {
        if (status) status.textContent = "请先粘贴 Cookie 内容。";
        return;
      }
      submit.disabled = true;
      const previousLabel = status ? status.textContent : "";
      if (status) status.textContent = "正在验证 Cookie 并登录，请稍候...";
      try {
        const data = await postForm("/accounts/cookies", {
          cookie_input: cookieInput,
          display_name: String(displayName?.value || "").trim(),
          relogin_unique_id: reloginToggle?.checked ? String(reloginSelect?.value || "") : "",
        });
        if (status) status.textContent = data.message || "Cookie 登录成功，页面即将刷新。";
        if (input) input.value = "";
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        // The server already grades the failure (category / retryable), so the
        // operator can tell "pasted the wrong thing" from "the session is gone"
        // from "the service hiccuped" instead of guessing.
        if (status) {
          const payload = error.payload || {};
          const label = payload.categoryLabel ? `（${payload.categoryLabel}）` : "";
          const retryHint =
            payload.retryable === false
              ? "这类失败重试通常无效，请重新获取 Cookie。"
              : "可稍后重试。";
          status.textContent = `Cookie 登录失败${label}：${error.message}${retryHint}`;
        }
      } finally {
        submit.disabled = false;
      }
    });
  });

  document.querySelectorAll(".login-desktop-save").forEach((button) => {
    button.addEventListener("click", async () => {
      const saveLogin = (extra = {}) =>
        postForm("/login-desktop/save", {
          relogin_unique_id: button.dataset.reloginUniqueId || "",
          ...extra,
        });
      try {
        let data;
        try {
          data = await saveLogin();
        } catch (error) {
          const candidate = error.payload && error.payload.duplicate_candidate;
          if (!candidate) throw error;
          const candidates =
            (error.payload && error.payload.duplicate_candidates) || [candidate];
          if (candidates.length > 1) {
            // Guessing here could merge the login into the wrong account, so the
            // operator is asked to resolve the duplicates first.
            setStatus(
              `有 ${candidates.length} 个同名账号（${candidates
                .map((item) => item.unique_id || item.account_ref)
                .join("、")}）：请先在账号管理里确认要更新的是哪一个（删除多余的那条或先改昵称）再保存。`,
              "danger",
            );
            return;
          }
          // "Cancel" must mean "do nothing": binding it to "create a duplicate
          // anyway" is the opposite of what the button suggests and is how a
          // second identical account gets created by accident.
          const update = window.confirm(
            `已有一个同名账号（${candidate.username || "未命名"}）。\n\n` +
              "点“确定”＝更新这个已有账号，保留它的目标与发送记录；\n" +
              "点“取消”＝放弃本次保存，不做任何改动。",
          );
          if (!update) {
            setStatus("已取消保存：没有改动任何账号。若确实要新建一个同名账号，请先确认它和已有账号不是同一个人。");
            return;
          }
          data = await saveLogin({ merge_with: candidate.account_ref });
        }
        renderWorkspace(data.workspace);
        if (data.verified === false) {
          // The cookies were stored, but the login state is not usable: show the
          // reason instead of a success toast.
          setStatus(
            `已保存登录态，但验证未通过：${data.verification_error || "登录态不可用"}`,
            "danger",
          );
          closeFrame();
          window.setTimeout(() => window.location.reload(), 1500);
          return;
        }
        setStatus(`已保存登录账号：${data.account?.username || ""}`, "success");
        closeFrame();
        window.setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        setStatus(`保存登录账号失败：${error.message}`, "danger");
      }
    });
  });

  document.querySelectorAll(".login-desktop-close").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const data = await postForm("/login-desktop/close");
        renderWorkspace(data.workspace);
        closeFrame();
        if (qrImage) qrImage.hidden = true;
      } catch (error) {
        setStatus(`关闭登录界面失败：${error.message}`, "danger");
      }
    });
  });

  document.querySelectorAll(".login-desktop-reset").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const data = await postForm("/login-desktop/reset");
        renderWorkspace(data.workspace);
        closeFrame();
        if (qrImage) qrImage.hidden = true;
      } catch (error) {
        setStatus(`结束登录流程失败：${error.message}`, "danger");
      }
    });
  });

  document.querySelectorAll("[data-copy-login-url]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(publicUrl);
        setStatus("登录工作区地址已复制。", "success");
      } catch (error) {
        setStatus(`复制失败：${error.message}`, "danger");
      }
    });
  });

  if (section) section.addEventListener("toggle", () => { if (section.open) pollStatus(); });
  pollStatus();
  timer = window.setInterval(pollStatus, 10000);
  heartbeatTimer = window.setInterval(heartbeat, 10000);
  countdownTimer = window.setInterval(() => {
    if (workspace.state !== "active" || !workspace.active) return;
    workspace.remaining_seconds = Math.max(0, Number(workspace.remaining_seconds || 0) - 1);
    const remaining = workspace.remaining_seconds;
    setStatus(`登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`, remaining <= 60 ? "warning" : "success");
  }, 1000);
  // Releasing the workspace when the page goes away lets the next operator in
  // immediately. Switching tabs only counts after a grace period, because a
  // quick glance elsewhere should not drop the lease.
  let hiddenReleaseTimer = null;
  const releaseWorkspace = () => {
    if (!workspace.ticket || workspace.state !== "active") return;
    const body = new FormData();
    body.set("csrf_token", csrfToken);
    body.set("ticket", workspace.ticket);
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/login-desktop/release", body);
    } else {
      fetch("/login-desktop/release", {
        method: "POST",
        body,
        credentials: "same-origin",
        keepalive: true,
      }).catch(() => {});
    }
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      hiddenReleaseTimer = window.setTimeout(releaseWorkspace, 15000);
    } else if (hiddenReleaseTimer) {
      window.clearTimeout(hiddenReleaseTimer);
      hiddenReleaseTimer = null;
    }
  });
  window.addEventListener("pagehide", () => {
    window.clearInterval(timer);
    window.clearInterval(heartbeatTimer);
    window.clearInterval(countdownTimer);
    if (hiddenReleaseTimer) {
      window.clearTimeout(hiddenReleaseTimer);
      hiddenReleaseTimer = null;
    }
    releaseWorkspace();
  });
})();

(() => {
  const parseJson = (id) => {
    const node = document.getElementById(id);
    if (!node) return [];
    try {
      return JSON.parse(node.textContent || "[]");
    } catch {
      return [];
    }
  };

  document.querySelectorAll(".friend-picker").forEach((picker) => {
    const accountId = picker.dataset.accountId;
    const refreshUrl = picker.dataset.refreshUrl;
    const csrfToken = picker.dataset.csrfToken;
    const form = picker.closest("form");
    const textarea = form?.querySelector(".targets-textarea");
    const search = picker.querySelector(".friend-search-input");
    const refreshButton = picker.querySelector(".friend-refresh-button");
    const list = picker.querySelector(".friend-picker-list");
    const summary = picker.querySelector(".friend-picker-summary");
    const status = picker.querySelector(".friend-picker-status");
    let friends = parseJson(`friends-cache-${accountId}`);
    let selected = new Set(parseJson(`selected-targets-${accountId}`));

    const parseTargets = (value) =>
      [...new Set(
        String(value || "")
          .replaceAll(",", "\n")
          .split(/\r?\n/)
          .map((item) => item.trim())
          .filter(Boolean),
      )];

    const combined = () => [...new Set([...selected, ...friends])];

    const syncTextarea = () => {
      if (textarea) textarea.value = [...selected].join("\n");
    };

    const render = () => {
      const query = String(search?.value || "").trim().toLowerCase();
      const names = combined().filter((name) =>
        name.toLowerCase().includes(query),
      );
      if (summary) summary.textContent = `已选 ${selected.size} 人`;
      list.innerHTML = "";
      if (!names.length) {
        const empty = document.createElement("div");
        empty.className = "friend-picker-empty";
        empty.textContent = combined().length
          ? "没有匹配的好友。"
          : "点击“刷新好友列表”后再选择目标。";
        list.appendChild(empty);
        return;
      }
      names.forEach((name) => {
        const label = document.createElement("label");
        label.className = `friend-option${selected.has(name) ? " selected" : ""}`;
        const text = document.createElement("span");
        text.textContent = name;
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selected.has(name);
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selected.add(name);
          else selected.delete(name);
          syncTextarea();
          render();
        });
        label.append(text, checkbox);
        list.appendChild(label);
      });
    };

    textarea?.addEventListener("input", () => {
      selected = new Set(parseTargets(textarea.value));
      render();
    });
    search?.addEventListener("input", render);
    // This element is the only place the last successful refresh time is shown,
    // so remember its initial text and keep it visible while reporting failures.
    const initialStatusText = String(status?.textContent || "").trim();
    let lastSuccessAt = "";
    const describeLastSuccess = () => {
      const previous = String(lastSuccessAt || "").trim();
      if (previous) return `上次成功刷新：${previous}`;
      return initialStatusText || "尚未读取好友列表";
    };
    const showRefreshOutcome = (message) => {
      if (!status) return;
      status.textContent = message;
      const detail = document.createElement("span");
      detail.className = "friend-picker-last-refresh";
      detail.textContent = describeLastSuccess();
      status.append(" ", detail);
    };

    const stageLabel = (job) => {
      if (job.stage === "starting") return "正在启动浏览器…";
      if (job.stage === "collecting") return `正在读取好友列表…已采集 ${job.collected || 0} 个`;
      return "正在处理…";
    };

    const pollJob = async () => {
      for (let attempt = 0; attempt < 200; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        const response = await fetch(`${refreshUrl}/status`, {
          credentials: "same-origin",
          cache: "no-store",
        });
        const job = await response.json().catch(() => ({}));
        if (job.state === "running") {
          if (status) status.textContent = stageLabel(job);
          continue;
        }
        return job;
      }
      return { state: "failed", error: "刷新仍未完成，请稍后在运行日志中查看结果。" };
    };

    const doRefresh = async () => {
      refreshButton.disabled = true;
      if (status) status.textContent = "正在启动刷新…";
      try {
        const formData = new FormData();
        formData.set("csrf_token", csrfToken);
        const response = await fetch(`${refreshUrl}/async`, {
          method: "POST",
          body: formData,
          credentials: "same-origin",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
          // Keep an in-session timestamp when this failure body omits it (busy,
          // forbidden, or unexpected errors).
          lastSuccessAt = data.previousUpdatedAt || lastSuccessAt;
          const label = data.categoryLabel ? `（${data.categoryLabel}）` : "";
          throw new Error(`${data.error || "刷新失败"}${label}`);
        }
        const job = await pollJob();
        if (job.state === "done") {
          lastSuccessAt = job.updatedAt || lastSuccessAt;
          friends = job.friends || [];
          if (status) status.textContent = job.message || "好友列表已刷新";
          render();
          return { ok: true, message: job.message || "已刷新" };
        }
        lastSuccessAt = job.previousUpdatedAt || lastSuccessAt;
        const label = job.categoryLabel ? `（${job.categoryLabel}）` : "";
        throw new Error(`${job.error || "刷新失败"}${label}`);
      } catch (error) {
        // Keep the previous successful refresh time visible after a failure.
        showRefreshOutcome(`刷新失败：${error.message}`);
        return { ok: false, message: error.message };
      } finally {
        refreshButton.disabled = false;
      }
    };

    // Exposed so the batch button can drive every account sequentially.
    picker.refreshFriends = doRefresh;
    refreshButton?.addEventListener("click", doRefresh);
    render();
  });

  // Batch refresh: run every account sequentially (the server also refuses to
  // overlap with a send run) and summarise partial failures instead of hiding
  // them behind per-account status text.
  const batchButton = document.querySelector("[data-refresh-all-friends]");
  const batchSummary = document.querySelector("[data-refresh-all-friends-summary]");
  const renderBatchSummary = (results) => {
    if (!batchSummary) return;
    const ok = results.filter((item) => item.ok).length;
    const failed = results.filter((item) => !item.ok);
    batchSummary.textContent = failed.length
      ? `成功 ${ok} 个，失败 ${failed.length} 个：${failed.map((item) => item.message).join("；")}`
      : `全部成功（${ok} 个账号）`;
  };
  batchButton?.addEventListener("click", async () => {
    const pickers = [...document.querySelectorAll(".friend-picker")];
    const results = [];
    batchButton.disabled = true;
    if (batchSummary) batchSummary.textContent = `正在刷新 ${pickers.length} 个账号…`;
    try {
      for (const picker of pickers) {
        if (typeof picker.refreshFriends !== "function") continue;
        results.push(await picker.refreshFriends());
      }
    } finally {
      batchButton.disabled = false;
    }
    renderBatchSummary(results);
  });
})();

window.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) {
    window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }
});

(() => {
  const form = document.querySelector("[data-schedule-preview-url]");
  if (!form) return;
  const button = form.querySelector("[data-schedule-preview]");
  const input = form.querySelector("input[name='daily_schedule']");
  const result = form.querySelector("[data-schedule-preview-result]");
  const csrf = form.querySelector("input[name='csrf_token']");
  if (!button || !input || !result) return;

  const appendLine = (text, className = "") => {
    const line = document.createElement("div");
    if (className) line.className = className;
    line.textContent = text;
    result.appendChild(line);
  };

  const render = (data) => {
    result.hidden = false;
    result.textContent = "";
    if (!data || data.ok !== true) {
      appendLine(`无法解析：${(data && data.error) || "格式不正确"}`, "schedule-preview-error");
      return;
    }
    appendLine(`将生效为：${data.label}`);
    appendLine(`下一次触发：${data.nextTriggerDisplay || "-"}`);
    appendLine(`一轮预计耗时（估算）：${data.estimatedRunDisplay || "-"}`);
    appendLine(`当前目标数：${data.targetCount || 0}`);
    (data.warnings || []).forEach((text) => {
      appendLine(`⚠ ${text}`, "schedule-preview-warning");
    });
  };

  button.addEventListener("click", async () => {
    button.disabled = true;
    result.hidden = false;
    result.textContent = "正在校验…";
    try {
      const body = new FormData();
      body.set("csrf_token", csrf ? csrf.value : "");
      body.set("daily_schedule", input.value);
      const response = await fetch(form.dataset.schedulePreviewUrl, {
        method: "POST",
        body,
        credentials: "same-origin",
      });
      render(await response.json().catch(() => ({})));
    } catch (error) {
      result.textContent = `预览失败：${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
})();
