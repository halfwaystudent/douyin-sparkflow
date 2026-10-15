// Query-based positioning avoids fragment-encoding bugs in mobile redirects.
(() => {
  const position = () => {
    const anchor = new URLSearchParams(window.location.search).get("_anchor");
    if (!anchor) return;
    const target = document.getElementById(anchor);
    if (target) target.scrollIntoView({ block: "start" });
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", position, { once: true });
  } else {
    position();
  }
})();

(() => {
  const forms = document.querySelectorAll("form[data-ajax-submit]");
  if (!forms.length) return;

  const setFeedback = (node, level, message) => {
    if (!node) return;
    node.hidden = false;
    node.className = `flash flash-${level}`;
    node.textContent = message;
    node.setAttribute("tabindex", "-1");
    node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    node.focus({ preventScroll: true });
  };

  const feedbackMessageFor = (response) => {
    if (response.status === 401 || response.redirected) {
      return "登录状态已失效，请重新登录后重试。";
    }
    if (response.status === 403) {
      return "会话已失效或请求校验失败，请刷新页面后重试。";
    }
    return `请求失败（HTTP ${response.status}），请检查后重试。`;
  };

  forms.forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const feedback = form.querySelector("[data-form-feedback]")
        || document.getElementById("notify-feedback");
      const submit = form.querySelector('button[type="submit"]');
      const buttons = document.querySelectorAll("form[data-ajax-submit] button[type=submit]");
      buttons.forEach((button) => { button.disabled = true; });
      setFeedback(feedback, "info", "正在处理…");

      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!response.ok) {
          setFeedback(feedback, "error", feedbackMessageFor(response));
          return;
        }
        if (!response.headers.get("content-type")?.includes("application/json")) {
          setFeedback(feedback, "error", "登录状态已失效，请刷新页面后重试。");
          return;
        }
        const result = await response.json();
        setFeedback(feedback, result.level || "info", result.message || "操作完成。");
      } catch {
        setFeedback(feedback, "error", "网络请求失败，请检查连接后重试。");
      } finally {
        buttons.forEach((button) => { button.disabled = false; });
      }
    });
  });
})();

(() => {
  const root = document.documentElement;
  const storageKey = "sparkflow-theme";
  const systemDark = window.matchMedia
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

  const storedTheme = () => {
    try {
      const value = localStorage.getItem(storageKey);
      return value === "light" || value === "dark" ? value : null;
    } catch {
      return null;
    }
  };

  const systemTheme = () => (systemDark && systemDark.matches ? "dark" : "light");

  const applyTheme = (theme) => {
    root.dataset.theme = theme === "dark" ? "dark" : "light";
  };

  // The <head> script already applied this before first paint; repeat it here
  // so pages without that script still end up consistent.
  applyTheme(storedTheme() || systemTheme());
  // Until the user picks a theme explicitly, keep following the system.
  systemDark?.addEventListener?.("change", () => {
    if (!storedTheme()) applyTheme(systemTheme());
  });
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(storageKey, next);
      } catch {
        // The active page can still switch themes when storage is unavailable.
      }
    });
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
          const chip = node.closest(".pill");
          if (chip && row.matches("a")) chip.hidden = !account.attention;
        });
        row.querySelectorAll("[data-account-pending]").forEach((node) => {
          node.textContent = account.pending;
          const chip = node.closest(".pill");
          if (chip && row.matches("a")) chip.hidden = !account.pending;
        });
        const strongCount = account.confirmed || 0;
        const weakCount = (account.pageEcho || 0) + (account.receiptOnly || 0);
        const accountTotal = account.total || 0;
        // Multiply before dividing so this matches the template's half-up maths,
        // and derive the weak segment as the remainder so the two segments can
        // never add up to more than 100%.
        const strongPct = accountTotal
          ? Math.round((strongCount * 100) / accountTotal)
          : 0;
        const sentPct = accountTotal
          ? Math.round(((strongCount + weakCount) * 100) / accountTotal)
          : 0;
        const weakPct = sentPct - strongPct;
        row.querySelectorAll("[data-account-progress]").forEach((node) => {
          node.style.width = `${strongPct}%`;
        });
        row.querySelectorAll("[data-account-progress-echo]").forEach((node) => {
          node.style.width = `${weakPct}%`;
        });
        row.querySelectorAll("[data-account-progress-text]").forEach((node) => {
          node.textContent = `已发送 ${strongCount + weakCount}/${accountTotal} · 强确认 ${strongCount}`;
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
    setText("[data-overview-value='receiptOnly']", summary.receiptOnly || 0);
    setText("[data-overview-value='attention']", summary.attention || 0);
    setText(
      "[data-overview-value='pending']",
      (summary.pending || 0) + (summary.unprocessed || 0),
    );
    setText("[data-overview-value='remaining']", summary.remaining || 0);
    const strongCount = summary.confirmed || 0;
    const weakCount = (summary.pageEcho || 0) + (summary.receiptOnly || 0);
    const totalCount = summary.total || 0;
    // Same half-up maths and remainder rule as the server-rendered template.
    const strongPct = totalCount
      ? Math.round((strongCount * 100) / totalCount)
      : 0;
    const sentPct = totalCount
      ? Math.round(((strongCount + weakCount) * 100) / totalCount)
      : 0;
    const weakPct = sentPct - strongPct;
    setText(
      "[data-overview-value='progress']",
      `${strongCount + weakCount}/${totalCount}`,
    );
    setText(
      "[data-overview-value='progressPercent']",
      `${sentPct}%`,
    );
    setText("[data-overview-value='weakSent']", weakCount);
    const remaining = Math.max(0, totalCount - strongCount - weakCount);
    document.querySelectorAll("[data-overview-headline]").forEach((node) => {
      node.textContent = !totalCount
        ? "还没有要续的火花"
        : remaining
          ? `今天还差 ${remaining} 个火花`
          : "今天的火花都续上了";
    });
    const attentionCount = summary.attention || 0;
    document.querySelectorAll("[data-attention-callout]").forEach((node) => {
      node.hidden = attentionCount <= 0;
    });
    document.querySelectorAll("[data-nav-attention]").forEach((node) => {
      node.textContent = attentionCount;
      node.hidden = attentionCount <= 0;
    });
    document.querySelectorAll("[data-overview-badge]").forEach((node) => {
      node.style.setProperty("--strong-pct", `${strongPct}%`);
      node.style.setProperty("--weak-pct", `${weakPct}%`);
      node.style.setProperty(
        "--ring-gap",
        strongPct && weakPct ? "0.6%" : "0%",
      );
    });
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
  // Until this time the routine lines (lease countdown, status poll) leave the
  // status text alone, so "saving..." or a save error stays readable.
  let statusHeldUntil = 0;
  const statusHeld = () => Date.now() < statusHeldUntil;
  let qrRefreshTimer = null;
  let recoveryTimer = null;
  // How many automatic re-acquisitions one user action may cause. The budget is
  // set by a user action and is never renewed by the automatic path itself, so a
  // flapping lease cannot drive an endless cycle of POST /login-desktop/open
  // (each one performs a page operation on the shared Douyin login page).
  const RECOVER_BUDGET = 2;
  let workspace = { state: "closed", active: false, position: 0, ticket: "" };
  if (displayMode === "native" && copyLoginUrlButton) copyLoginUrlButton.hidden = true;
  if (displayMode === "native" && frameWrap) frameWrap.hidden = true;
  // "显示本机登录浏览器" only makes sense for the Windows native mode; in the
  // noVNC mode the browser is already shown in the frame, so leaving the button
  // visible only invited clicks that come back as a 423 lease error.
  if (displayMode !== "native") {
    document.querySelectorAll("[data-focus-native-browser]").forEach((button) => {
      button.hidden = true;
    });
  }

  const setStatus = (text, tone = "") => {
    if (statusText) statusText.textContent = text;
    if (runtimeState) {
      runtimeState.className = `pill${tone ? ` ${tone}` : ""}`;
      // The label follows the workspace state; the tone only colours it. A
      // held workspace near its lease renewal is warning-toned but still in use.
      const state = workspace ? workspace.state : "";
      runtimeState.textContent =
        tone === "danger"
          ? "异常"
          : state === "active"
            ? "使用中"
            : state === "queued"
              ? "排队中"
              : state === "resetting"
                ? "清理中"
                : "空闲";
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
      // 423 is the lease gate: this session no longer holds the login
      // workspace, which callers must recover from instead of reporting the
      // login-desktop service as broken.
      error.payload.leaseLost = response.status === 423;
      throw error;
    }
    return data;
  };

  // A legacy account record has no bound platform UID and its old login could
  // not be checked: the server asks the operator once (issue #41). The token is
  // opaque, single-use and expires; it carries no cookie or session material.
  const confirmIdentityMigration = (info) => {
    const minutes = Math.max(1, Math.round(Number(info.expires_in || 600) / 60));
    return window.confirm(
      "这条旧账号记录无法自动核实身份（旧登录已失效）。\n\n" +
        `账号：${info.username || "未命名"}\n` +
        `历史 ID：${info.history_id || "无"}（${info.history_id_type || "类型未知"}）\n` +
        `本次登录的平台 UID：${info.platform_uid || ""}\n` +
        (info.display_id ? `本次页面显示的 ID：${info.display_id}\n` : "") +
        "\n点“确定”＝确认本次登录的就是这个原账号：绑定平台 UID，保留它的目标、发送记录和归属" +
        `（${minutes} 分钟内有效，只能使用一次）；\n` +
        "点“取消”＝不做任何改动。",
    );
  };
  const identityFailureText = (error) => {
    const payload = (error && error.payload) || {};
    const label = payload.identityLabel || payload.categoryLabel;
    const hint = payload.hint ? ` ${payload.hint}` : "";
    const retry = payload.retryable ? " 可稍后重试。" : "";
    return `${label ? `【${label}】` : ""}${error.message}${hint}${retry}`;
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
        // A new workspace starts a fresh QR wait, whichever path granted it
        // (user click or automatic recovery).
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
        if (statusHeld()) return;
        setStatus(
          promoted
            ? `已轮到你：Windows 本地登录浏览器已打开，剩余 ${remaining} 秒。完成扫码后请保存登录态。`
            : `Windows 本地登录浏览器已打开，剩余 ${remaining} 秒。完成扫码后请保存登录态。`,
          tone,
        );
      } else if (!statusHeld()) {
        setStatus(
          promoted
            ? `已轮到你：登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`
            : `登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`,
          tone,
        );
      }
      return;
    }
    setStatus("登录工作区当前未分配给本会话（空闲或由其他会话占用）。点“添加新账号”，或在账号页选择“重新登录”。");
  };

  let qrPollStartedAt = 0;
  let lastPromotedTicket = "";
  // How long to keep waiting for a QR code after the workspace opened. Each
  // GET /qr already waits upstream for the code, so the page only re-asks
  // about a second after each answer.
  const QR_WAIT_LIMIT_MS = 60000;
  const QR_RETRY_MS = 1000;
  let recoveringLease = false;
  let recoverAttempts = 0;
  let acquiringWorkspace = false;
  const setQrButtons = (label, { busy = false, stopped = false } = {}) => {
    document.querySelectorAll("[data-refresh-login-qr]").forEach((button) => {
      const text = button.querySelector("span");
      if (text) text.textContent = label;
      button.disabled = busy;
      button.dataset.qrStopped = stopped ? "1" : "";
    });
    // Any final QR outcome (shown, expired, stopped) ends the opening feedback.
    if (!busy) stopOpening();
  };

  // From the click until a QR code shows, starting the login browser and
  // rendering the Douyin page takes tens of seconds on this host; say so and
  // count, instead of leaving both panes blank and the button clickable.
  const qrPlaceholder = document.querySelector(".qr-placeholder span");
  const qrPlaceholderText = qrPlaceholder ? qrPlaceholder.textContent : "";
  let openingTimer = null;
  let openingStartedAt = 0;
  const renderOpening = () => {
    const seconds = Math.round((Date.now() - openingStartedAt) / 1000);
    const text = `正在启动登录浏览器…已等待 ${seconds} 秒（通常 20 秒左右）`;
    if (qrPlaceholder) qrPlaceholder.textContent = text;
    if (frameWrap) frameWrap.dataset.progress = text;
  };
  const startOpening = () => {
    window.clearInterval(openingTimer);
    openingStartedAt = Date.now();
    document.querySelectorAll(".login-desktop-open").forEach((button) => {
      button.disabled = true;
    });
    renderOpening();
    openingTimer = window.setInterval(renderOpening, 1000);
  };
  function stopOpening() {
    if (!openingStartedAt) return;
    window.clearInterval(openingTimer);
    openingTimer = null;
    openingStartedAt = 0;
    document.querySelectorAll(".login-desktop-open").forEach((button) => {
      button.disabled = false;
    });
    if (qrPlaceholder) qrPlaceholder.textContent = qrPlaceholderText;
    if (frameWrap) delete frameWrap.dataset.progress;
  }
  const qrWaitLabel = () => {
    if (!qrPollStartedAt) return "";
    return `（已等待 ${Math.round((Date.now() - qrPollStartedAt) / 1000)} 秒）`;
  };

  // 423 means this session does not hold the login workspace lease. That is a
  // workspace-state problem, not a slow page: retrying blindly made the QR code
  // spin forever and the noVNC frame never load. Drop the stale local view,
  // re-read the real workspace state and try to take the workspace back.
  const setRecoverVisible = (visible) => {
    document.querySelectorAll("[data-recover-login-workspace]").forEach((button) => {
      button.hidden = !visible;
    });
  };

  const refreshLoginQr = async (delay = 0, retries = 40) => {
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
        if (response.status === 423) {
          recoverLease();
          return;
        }
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
          // GET /qr only reads the page, and a healthy login page produces its
          // code within a few seconds on its own. If none appears after a few
          // polls, stop guessing and hand the decision back: each
          // POST /qr/refresh reloads the shared Douyin login page while the
          // operator may be scanning in the noVNC frame, because the page lock
          // is global.
          if (Date.now() - qrPollStartedAt >= QR_WAIT_LIMIT_MS) {
            if (qrStatus) {
              qrStatus.textContent = (data.message || "浏览器仍未生成二维码") + " 请点击“刷新二维码”重试，或稍后再试。";
            }
            setQrButtons("已停止，点此重试", { stopped: true });
            return;
          }
          // The request itself already waited upstream, so ask again soon.
          retryLater(data.message || "浏览器正在生成二维码", QR_RETRY_MS);
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
          const upstream =
            data.category === "douyin_unreachable" ||
            /douyin|network|proxy|timeout|超时|网络/i.test(String(data.message || data.error || ""));
          if (qrStatus) {
            qrStatus.textContent =
              data.message ||
              data.error ||
              (upstream
                ? "无法访问抖音（网络或代理异常），请检查代理后重试。"
                : "登录桌面服务暂时不可用，请稍后点击刷新二维码。");
          }
          // Polling stops here, so the refresh button must not stay disabled
          // from the "读取中…" state the grant put it in.
          setQrButtons("已停止，点此重试", { stopped: true });
          return;
        }
        if (response.status === 401) {
          if (qrStatus) qrStatus.textContent = "登录状态已失效，请重新登录控制台。";
          setQrButtons("已停止，点此重试", { stopped: true });
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
      if (response.status === 423) {
        // The lease is not held by this session any more. Showing "check the
        // login-desktop service" here pointed operators at a container that was
        // perfectly healthy. The recovery path hides the stale frame, re-reads
        // the workspace state and takes the workspace back with a bounded
        // backoff; recoverLease() itself refuses to start a second loop, so the
        // 10s status poll cannot stack recoveries.
        if (qrStatus) {
          qrStatus.textContent = "本会话当前未持有登录工作区：正在重新申请…";
        }
        recoverLease();
        return;
      }
      if (response.status === 401) {
        setRecoverVisible(false);
        setStatus("登录状态已失效，请重新登录控制台。", "danger");
        return;
      }
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        setStatus(data.error || "登录工作区暂时不可用，请稍后重试。", "danger");
        return;
      }
      if (data.workspace && data.workspace.state === "active" && data.workspace.active) {
        setRecoverVisible(false);
      }
      renderWorkspace(data.workspace);
      if (workspace.state === "active" && workspace.active) {
        loadFrame();
        if (statusHeld()) {
          // An in-progress save or its error owns the status line.
        } else if (data.logged_in) {
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
      if (error.payload && error.payload.leaseLost) {
        recoverLease();
        return;
      }
      workspace = { state: "closed", active: false, position: 0, ticket: "" };
      closeFrame();
      setStatus(`登录工作区已释放：${error.message}`, "danger");
    }
  };

  document.querySelectorAll(".login-desktop-open").forEach((button) => {
    button.addEventListener("click", async () => {
      if (section) section.open = true;
      startOpening();
      try {
        resetRecoveryBudget();
        const reloginUniqueId = button.dataset.reloginUniqueId || "";
        const mode = button.dataset.loginMode || (reloginUniqueId ? "relogin" : "add");
        const data = await postForm("/login-desktop/open", {
          mode,
          ...(reloginUniqueId ? { relogin_unique_id: reloginUniqueId } : {}),
        });
        const promotedBefore = lastPromotedTicket;
        renderWorkspace(data.workspace);
        if (data.state === "queued") {
          stopOpening();
          return;
        }
        // A fresh grant already loaded the frame and started the QR wait in
        // renderWorkspace; loading it again here reloaded noVNC mid-connect.
        // Re-opening a workspace this session already holds keeps the ticket,
        // so only then start them here.
        if (!workspace.active) {
          // Granted and immediately lost (a racing release): nothing will
          // finish the feedback, so end it here.
          stopOpening();
        } else if (lastPromotedTicket === promotedBefore) {
          loadFrame(true);
          qrPollStartedAt = Date.now();
          refreshLoginQr(500);
        }
        if (frame) frame.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        stopOpening();
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
        if (error.payload && error.payload.leaseLost) {
          recoverLease();
          return;
        }
        setStatus(`显示登录浏览器失败：${error.message}`, "danger");
      } finally {
        button.disabled = false;
      }
    });
  });

  // Only the manual "刷新二维码" button calls this: the POST reloads the shared
  // login page, so it must never be triggered automatically.
  const requestQrRefresh = async () => {
    qrPollStartedAt = Date.now();
    const refreshForm = new FormData();
    refreshForm.set("csrf_token", csrfToken);
    refreshForm.set("ticket", workspace.ticket);
    const response = await fetch("/login-desktop/qr/refresh", {
      method: "POST",
      body: refreshForm,
      credentials: "same-origin",
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      if (qrStatus) qrStatus.textContent = "登录状态已失效，请重新登录控制台。";
      setQrButtons("已停止，点此重试", { stopped: true });
      return;
    }
    if (response.status === 423) {
      recoverLease();
      return;
    }
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
      qrPollStartedAt = Date.now();
      refreshLoginQr(1200);
      return;
    }
    qrPollStartedAt = Date.now();
    refreshLoginQr(500);
  };

  document.querySelectorAll("[data-refresh-login-qr]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        resetRecoveryBudget();
        await requestQrRefresh();
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
        const cookiePayload = {
          cookie_input: cookieInput,
          display_name: String(displayName?.value || "").trim(),
          relogin_unique_id: reloginToggle?.checked ? String(reloginSelect?.value || "") : "",
        };
        let data;
        try {
          data = await postForm("/accounts/cookies", cookiePayload);
        } catch (error) {
          const info = error.payload && error.payload.identity_confirmation;
          if (!info) throw error;
          if (!confirmIdentityMigration(info)) {
            if (status) status.textContent = "已取消：没有改动任何账号。";
            return;
          }
          data = await postForm("/accounts/cookies", { ...cookiePayload, confirm_identity: info.token });
        }
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
          status.textContent = payload.identityState
            ? `Cookie 登录未保存：${identityFailureText(error)}`
            : `Cookie 登录失败${label}：${error.message}${retryHint}`;
        }
      } finally {
        submit.disabled = false;
      }
    });
  });

  // Saving exports the login, verifies it in a second browser and releases the
  // workspace: tens of seconds on this host. Without feedback the operator
  // clicked again, and the second save raced the first one's release.
  let savingTimer = null;
  const setSaving = (saving, { keepDisabled = false } = {}) => {
    window.clearInterval(savingTimer);
    savingTimer = null;
    document.querySelectorAll(".login-desktop-save").forEach((item) => {
      item.disabled = saving || keepDisabled;
    });
    // Whatever is shown next (result or error) stays readable for a while.
    statusHeldUntil = saving ? Infinity : Date.now() + 8000;
    if (!saving) return;
    const startedAt = Date.now();
    const render = () => {
      const seconds = Math.round((Date.now() - startedAt) / 1000);
      setStatus(`正在保存登录账号…已等待 ${seconds} 秒（通常几秒，请不要重复点击）`, "warning");
    };
    render();
    savingTimer = window.setInterval(render, 1000);
  };
  document.querySelectorAll(".login-desktop-save").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const postSave = (extra = {}) =>
        postForm("/login-desktop/save", {
          relogin_unique_id: button.dataset.reloginUniqueId || "",
          ...extra,
        });
      const saveLogin = (extra = {}) =>
        postSave(extra).catch((error) => {
          const info = error.payload && error.payload.identity_confirmation;
          if (!info) throw error;
          setSaving(false);
          if (!confirmIdentityMigration(info)) {
            const cancelled = new Error("已取消保存：没有改动任何账号。");
            cancelled.cancelled = true;
            throw cancelled;
          }
          setSaving(true);
          return postSave({ ...extra, confirm_identity: info.token });
        });
      setSaving(true);
      try {
        resetRecoveryBudget();
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
            setSaving(false);
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
          setSaving(false);
          const update = window.confirm(
            `已有一个同名账号（${candidate.username || "未命名"}）。\n\n` +
              "点“确定”＝更新这个已有账号，保留它的目标与发送记录；\n" +
              "点“取消”＝放弃本次保存，不做任何改动。",
          );
          if (!update) {
            // The dialog may have stayed open longer than the hold; restart it.
            statusHeldUntil = Date.now() + 8000;
            setStatus("已取消保存：没有改动任何账号。若确实要新建一个同名账号，请先确认它和已有账号不是同一个人。");
            return;
          }
          setSaving(true);
          data = await saveLogin({ merge_with: candidate.account_ref });
        }
        setSaving(false, { keepDisabled: true });
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
        // A login save starts a friend refresh; when it could not start, say why
        // instead of leaving the operator waiting for a list that never updates.
        // The login state is re-checked after the save, in the background.
        const refreshNote =
          data.verification === "pending"
            ? "，正在后台验证登录态并刷新好友列表（账号页会显示“登录态验证中”）"
            : data.friend_refresh === "started"
            ? ""
            : data.friend_refresh === "busy"
              ? "（发送任务正在运行，好友列表稍后会自动刷新）"
              : data.friend_refresh === "running"
                ? "（好友列表正在刷新中）"
                : "（好友列表未自动刷新，可手动点刷新）";
        const identityNote = data.identityLabel ? `（${data.identityLabel}）` : "";
        setStatus(
          `已保存登录账号：${data.account?.username || ""}${identityNote}${refreshNote}`,
          data.verification === "pending" || data.friend_refresh === "started" ? "success" : "warning",
        );
        closeFrame();
        window.setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        setSaving(false);
        if (error.payload && error.payload.leaseLost) {
          // The workspace moved on while the operator was finishing the scan;
          // report the real condition instead of a generic save failure.
          setStatus("保存时本会话已不再持有登录工作区：请重新申请工作区后再保存。", "warning");
          recoverLease();
          return;
        }
        if (error.cancelled) {
          statusHeldUntil = Date.now() + 8000;
          setStatus(error.message);
          return;
        }
        if (error.payload && error.payload.identityState) {
          setStatus(`登录未保存：${identityFailureText(error)}`, error.payload.retryable ? "warning" : "danger");
          return;
        }
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

  // Defined after the pollers so the recovery path can safely call back into
  // them (const bindings are not hoisted).
  const trackWorkspace = (payload) => {
    const next = payload || { state: "closed", active: false, position: 0, ticket: "" };
    if (next.state === "active") {
      acquiringWorkspace = false;
    }
    return next;
  };
  const applyWorkspace = (data, { openFrame = false, pollQr = false } = {}) => {
    workspace = trackWorkspace(data.workspace);
    // Granting a workspace starts a fresh QR wait even though this path
    // assigns `workspace` first (so renderWorkspace's promote branch is skipped).
    if (workspace.state === "active" && workspace.active) qrPollStartedAt = Date.now();
    renderWorkspace(workspace);
    if (openFrame && data.state !== "queued") loadFrame(true);
    if (pollQr && data.state !== "queued" && workspace.active) refreshLoginQr(500);
  };
  const acquireWorkspace = async () => {
    const wasAcquiring = acquiringWorkspace;
    acquiringWorkspace = true;
    try {
      const data = await postForm("/login-desktop/open", { mode: "add" });
      // A fresh grant cancels any pending re-acquisition: without this the timer
      // armed during recovery still fires and reloads the shared login page.
      window.clearTimeout(recoveryTimer);
      recoveryTimer = null;
      applyWorkspace(data, { openFrame: true, pollQr: true });
    } catch (error) {
      if (error.payload && error.payload.leaseLost) {
        recoverLease({ acquire: false });
        return;
      }
      if (!wasAcquiring) {
        setStatus(`申请登录工作区失败：${error.message}`, "danger");
      }
      // Always report the failure: an automatic attempt that fails must not
      // leave the stale "正在重新检查状态并尝试重新申请…" line on screen.
      if (qrStatus) qrStatus.textContent = `重新申请登录工作区失败：${error.message}`;
    }
  };
  // Any deliberate user action starts a fresh recovery budget; only this resets
  // it, so an automatic loop can never renew its own budget.
  const resetRecoveryBudget = () => {
    window.clearTimeout(recoveryTimer);
    recoveryTimer = null;
    recoverAttempts = 0;
    recoveringLease = false;
  };
  const recoverLease = ({ acquire = true } = {}) => {
    // The status poll calls back into this function when the workspace is not
    // ours, so a server that keeps contradicting itself must not recurse here.
    if (recoveringLease) return;
    recoveringLease = true;
    window.clearTimeout(qrRefreshTimer);
    window.clearTimeout(recoveryTimer);
    recoveryTimer = null;
    workspace = { state: "closed", active: false, position: 0, ticket: "" };
    qrPollStartedAt = 0;
    lastPromotedTicket = "";
    closeFrame();
    setRecoverVisible(true);
    if (qrStatus) {
      qrStatus.textContent = "本会话当前未持有登录工作区：正在重新检查状态并尝试重新申请…";
    }
    setQrButtons("刷新二维码");
    Promise.resolve(pollStatus())
      .catch(() => {})
      .then(() => {
        if (!acquire) {
          recoveringLease = false;
          return;
        }
        // Re-acquiring is safe even when someone else holds the workspace: the
        // server puts us in the queue. The budget is set by a user action and is
        // never renewed here, so a flapping lease cannot drive an endless cycle
        // of POST /login-desktop/open (each one reloads the shared login page).
        recoverAttempts += 1;
        if (recoverAttempts <= RECOVER_BUDGET) {
          const backoff = [5000, 15000][Math.min(recoverAttempts - 1, 1)];
          recoveryTimer = window.setTimeout(() => {
            recoveryTimer = null;
            // A workspace granted in the meantime makes re-acquisition pointless.
            if (workspace.state === "active" && workspace.active) {
              recoveringLease = false;
              return;
            }
            recoveringLease = false;
            acquireWorkspace();
          }, backoff);
          return;
        }
        if (qrStatus) {
          qrStatus.textContent = "登录工作区仍未分配给本会话：请从账号页点“重新登录”，或点“重新检查工作区”再试一次。";
        }
        setRecoverVisible(true);
        setQrButtons("已停止，点此重试", { stopped: true });
        recoveringLease = false;
      });
  };
  document.querySelectorAll("[data-recover-login-workspace]").forEach((button) => {
    button.addEventListener("click", () => {
      resetRecoveryBudget();
      recoverLease();
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
    if (statusHeld()) return;
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
    // A successful login save starts a friend refresh in the background, so pick
    // its progress up on load instead of leaving the operator with a stale list.
    fetch(`${refreshUrl}/status`, { credentials: "same-origin", cache: "no-store" })
      .then((response) => response.json())
      .then((job) => {
        if (!job || job.state !== "running") return;
        if (status) status.textContent = stageLabel(job);
        return pollJob().then((finished) => {
          if (finished.state === "done") {
            lastSuccessAt = finished.updatedAt || lastSuccessAt;
            friends = finished.friends || friends;
            if (status) status.textContent = finished.message || "好友列表已刷新";
            render();
          } else if (finished.state === "failed") {
            showRefreshOutcome(`刷新失败：${finished.error || "未知原因"}`);
          }
        });
      })
      .catch(() => {});
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
    batchSummary.hidden = false;
    batchSummary.textContent = failed.length
      ? `成功 ${ok} 个，失败 ${failed.length} 个：${failed.map((item) => item.message).join("；")}`
      : `全部成功（${ok} 个账号）`;
  };
  batchButton?.addEventListener("click", async () => {
    const pickers = [...document.querySelectorAll(".friend-picker")];
    const results = [];
    batchButton.disabled = true;
    if (batchSummary) {
      batchSummary.hidden = false;
      batchSummary.textContent = `正在刷新 ${pickers.length} 个账号…`;
    }
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

// Chromium does not scroll a half-hidden item of a sideways-scrolling strip into view on Tab,
// and for a textarea it only reveals the caret line. Only for Tab: a click, a tap that opens the
// soft keyboard, or focus coming back to the window must not move the page.
(() => {
  let focusFromTab = false;
  document.addEventListener("keydown", (event) => { focusFromTab = event.key === "Tab"; }, true);
  document.addEventListener("pointerdown", () => { focusFromTab = false; }, true);
  window.addEventListener("blur", () => { focusFromTab = false; });
  document.addEventListener("focusin", (event) => {
    const item = event.target;
    if (!focusFromTab || !(item instanceof Element)) return;
    if (!item.matches("textarea") && !item.closest(".settings-nav, .segmented-control")) return;
    item.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
})();

(() => {
  const form = document.querySelector("[data-schedule-preview-url]");
  if (!form) return;
  const button = form.querySelector("[data-schedule-preview]");
  const input = form.querySelector("input[name='daily_schedule']");
  const result = form.querySelector("[data-schedule-preview-result]");
  const csrf = form.querySelector("input[name='csrf_token']");
  if (!button || !input || !result) return;

  const appendLine = (text, className = "", icon = "") => {
    const line = document.createElement("div");
    if (className) line.className = className;
    if (icon) {
      const glyph = document.createElement("i");
      glyph.dataset.lucide = icon;
      const copy = document.createElement("span");
      copy.textContent = text;
      line.append(glyph, copy);
    } else {
      line.textContent = text;
    }
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
    const warnings = data.warnings || [];
    warnings.forEach((text) => {
      appendLine(text, "notice-inline warning", "triangle-alert");
    });
    if (warnings.length && window.lucide) {
      window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
    }
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

// "Immediate patrol" on the accounts page: start it, then poll until it ends
// and reload so the server-rendered result is shown.
(() => {
  const buttons = document.querySelectorAll("[data-patrol-url]");
  buttons.forEach((button) => {
    const panel = button.closest("[data-patrol-panel]");
    const message = panel ? panel.querySelector("[data-patrol-message]") : null;
    const show = (text, level) => {
      if (!message) return;
      message.hidden = false;
      message.className = `notice-inline${level ? ` ${level}` : ""}`;
      message.textContent = text;
    };
    const poll = async () => {
      for (let round = 0; round < 120; round += 1) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        try {
          const response = await fetch(button.dataset.patrolStatusUrl, { credentials: "same-origin" });
          const data = await response.json().catch(() => ({}));
          if (data.state && data.state !== "running") {
            show(data.state === "done" ? `巡查完成：${data.conclusionLabel || ""}` : `巡查失败：${data.reason || ""}`, data.state === "done" ? "" : "warning");
            setTimeout(() => window.location.reload(), 800);
            return;
          }
        } catch (error) {
          // keep polling; a transient error is not the patrol's result
        }
      }
      show("巡查仍在进行，请稍后刷新页面查看结果。", "warning");
      button.disabled = false;
    };
    button.addEventListener("click", async () => {
      button.disabled = true;
      show("正在巡查：检查登录态并读取好友列表，不会打开聊天或发送消息…", "");
      try {
        const body = new FormData();
        body.set("csrf_token", button.dataset.csrfToken || "");
        const response = await fetch(button.dataset.patrolUrl, {
          method: "POST",
          body,
          credentials: "same-origin",
        });
        const data = await response.json().catch(() => ({}));
        if (response.status !== 202) {
          show(data.error || `无法开始巡查（${response.status}）`, "warning");
          button.disabled = false;
          return;
        }
        poll();
      } catch (error) {
        show(`无法开始巡查：${error.message}`, "warning");
        button.disabled = false;
      }
    });
  });
})();
