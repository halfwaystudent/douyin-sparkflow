(() => {
  const layout = document.querySelector("[data-settings-tabs]");
  if (!layout) return;
  const links = [...layout.querySelectorAll(".settings-nav a")];
  const panels = [...layout.querySelectorAll("section.settings-section")];
  const select = () => {
    const requested = window.location.hash.slice(1)
      || new URLSearchParams(window.location.search).get("_anchor");
    const selected = panels.find((panel) => panel.id === requested) || panels[0];
    panels.forEach((panel) => { panel.hidden = panel !== selected; });
    links.forEach((link) => {
      const active = link.hash === `#${selected.id}`;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  };
  links.forEach((link) => {
    link.addEventListener("click", (event) => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (!panels.some((panel) => `#${panel.id}` === link.hash)) return;
      event.preventDefault();
      const url = new URL(window.location.href);
      url.searchParams.delete("_anchor");
      url.hash = link.hash;
      window.history.pushState(null, "", url);
      select();
      layout.scrollIntoView({ block: "start" });
    });
  });
  window.addEventListener("hashchange", select);
  window.addEventListener("popstate", select);
  select();
})();

// Query-based positioning avoids fragment-encoding bugs in mobile redirects.
(() => {
  const position = () => {
    const anchor = new URLSearchParams(window.location.search).get("_anchor") || window.location.hash.slice(1);
    if (!anchor) return;
    const target = document.getElementById(anchor);
    if (target) {
      if (target.matches("details")) target.open = true;
      for (let parent = target.parentElement; parent; parent = parent.parentElement) {
        if (parent.matches("details")) parent.open = true;
      }
      target.scrollIntoView({ block: "start" });
    }
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
    const editWarning = window.sparkflowEditorWarning?.();
    if (editWarning) message.textContent += ` ${editWarning}`;
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
      window.dispatchEvent(new Event("sparkflow:leave-approved"));
      HTMLFormElement.prototype.submit.call(form);
    } else if (href) {
      window.dispatchEvent(new Event("sparkflow:leave-approved"));
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

  let refreshing = false;
  let refreshController = null;
  const refresh = async () => {
    if (document.visibilityState !== "visible" || refreshing) return;
    refreshing = true;
    refreshController = new AbortController();
    const deadline = window.setTimeout(() => refreshController.abort(), 10000);
    try {
      const response = await fetch("/api/ops/overview", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: refreshController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch {
      document.querySelectorAll("[data-overview-live-state]").forEach((node) => {
        node.textContent = "更新延迟";
        node.classList.add("poll-stale");
      });
    } finally { window.clearTimeout(deadline); refreshing = false; }
  };

  document.querySelectorAll("[data-refresh-page]").forEach((button) => {
    button.addEventListener("click", () => window.location.reload());
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
  timer = window.setInterval(refresh, 10000);
  window.addEventListener("pagehide", () => {
    window.clearInterval(timer);
    if (refreshController) refreshController.abort();
  });
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
  const qrOpenLink = document.querySelector("[data-open-login-qr]");
  let timer = null;
  let heartbeatTimer = null;
  let pageActive = true, lifecycleVersion = 0, resumeChecking = false;
  let statusController = null, statusAgain = false, statusFailures = 0;
  let qrController = null, qrRequestPending = false, qrManualPending = false, qrVersion = 0;
  let lastHeartbeatAt = 0, browserLoginLabel = "";
  const transientRequests = new Set();
  const canUseWorkspace = () => pageActive && !resumeChecking && !bindingBusy && !bindingNeedsReview && document.visibilityState === "visible";
  const publishWorkspace = () => {
    root.dataset.workspaceTicket = workspace.ticket || "";
    root.dataset.workspaceActive = workspace.state === "active" && workspace.active && canUseWorkspace() ? "1" : "0";
  };
  const cancelQr = () => {
    qrVersion += 1;
    window.clearTimeout(qrRefreshTimer);
    qrController?.abort();
    // A new tab cannot attach the workspace header. Only expose the local
    // snapshot already obtained through the guarded QR read, never its URL.
    if (qrOpenLink) {
      qrOpenLink.hidden = true;
      qrOpenLink.removeAttribute("href");
    }
  };
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
  let workspaceVersion = 0;
  let bindingBusy = false;
  let bindingNeedsReview = false;
  const requestedRelogin = document.querySelector(".login-desktop-open[data-relogin-unique-id]")?.dataset.reloginUniqueId || "";
  const requestedRef = document.querySelector(".login-desktop-open[data-relogin-account-ref]")?.dataset.reloginAccountRef || "";
  const requestedGeneration = document.querySelector(".login-desktop-open[data-login-generation]")?.dataset.loginGeneration || "";
  const workspaceRequest = () => requestedRef ? { mode: "relogin", relogin_unique_id: requestedRelogin, relogin_account_ref: requestedRef, login_generation: requestedGeneration } : { mode: "add" };
  const bindingResult = document.querySelector("[data-binding-result]");
  let bindingResultVersion = 0;
  let saveOperation = null, saveReadVersion = 0;
  const saveCheckButton = bindingResult?.querySelector("[data-recheck-login-save]");
  const saveCheckStatus = bindingResult?.querySelector("[data-save-check-status]");
  const setBindingBusy = (busy, { uncertain = false } = {}) => {
    bindingBusy = busy;
    bindingNeedsReview = bindingNeedsReview || uncertain;
    publishWorkspace();
    if (busy || uncertain) cancelQr();
    if (!busy && !bindingNeedsReview) scheduleHiddenRelease();
    document.querySelectorAll(".login-desktop-open, .login-desktop-save, [data-cookie-login-submit], [data-refresh-login-qr], .login-desktop-close, .login-desktop-reset").forEach((button) => {
      button.disabled = bindingBusy || bindingNeedsReview;
    });
  };
  const showBindingResult = (account, message, { pending = false, uncertain = false } = {}) => {
    if (!bindingResult) return;
    bindingResult.hidden = false;
    bindingResult.querySelector("[data-binding-result-title]").textContent = uncertain ? "保存结果待核对" : pending ? "账号已保存，登录待确认" : "账号已保存";
    bindingResult.querySelector("[data-binding-result-message]").textContent = message;
    const next = bindingResult.querySelector("[data-binding-next]");
    next.hidden = false;
    if (saveCheckButton) { saveCheckButton.hidden = !(uncertain && saveOperation); saveCheckButton.disabled = false; }
    if (saveCheckStatus) saveCheckStatus.hidden = true;
    const preferences = bindingResult.querySelector("[data-binding-preferences]");
    if (preferences) preferences.hidden = uncertain;
    const ref = account?.account_ref || "";
    const id = account?.unique_id || "";
    next.href = !uncertain && ref && root.dataset.setupInProgress === "1"
      ? `/setup?account=${encodeURIComponent(ref)}`
      : (ref || id) ? `/accounts?_anchor=account-${encodeURIComponent(ref || id)}` : "/accounts";
    next.textContent = uncertain ? "查看账号列表" : root.dataset.setupInProgress === "1" ? "继续设置" : "选择好友和群";
    bindingResult.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const followBindingVerification = async (account) => {
    const version = ++bindingResultVersion;
    if (!account?.account_ref) return;
    for (let attempt = 0; attempt < 36; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 5000));
      if (version !== bindingResultVersion) return;
      if (document.visibilityState !== "visible") continue;
      const controller = new AbortController();
      const deadline = window.setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(`/accounts/binding-status?account_ref=${encodeURIComponent(account.account_ref)}`, {
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) return;
        const data = await response.json();
        if (version !== bindingResultVersion) return;
        const current = (data.accounts || []).find((row) => row.account_ref === account.account_ref);
        if (!current) {
          showBindingResult(null, "账号已移除或权限已变化，请重新打开账号页核对。", { uncertain: true });
          return;
        }
        if (account.login_generation && account.login_generation !== current.login_generation) {
          showBindingResult(current, "这个账号的登录已在另一操作中更新，请到账号页查看最新核验结果。", { pending: true });
          return;
        }
        const state = current.verification?.state || "verified";
        if (state === "verifying") continue;
        const reason = current.verification?.reason || "";
        const message = state === "verified" && current.identity_state === "verified"
          ? `账号 ${current.username} 的登录验证已通过，可以继续选择目标。`
          : state === "login_required" || state === "identity_conflict" || current.identity_state === "conflict"
            ? `账号需要重新登录。${reason}`
            : `账号身份已保存，暂时未能确认登录状态。${reason} 可以先配置目标和消息，并在账号页核对后续结果。`;
        showBindingResult(current, message, { pending: state !== "verified" || current.identity_state !== "verified" });
        return;
      } catch { /* Keep the saved result and its account link visible. */ }
      finally { window.clearTimeout(deadline); }
    }
  };
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
    if (!pageActive) return;
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
    const controller = new AbortController();
    const version = lifecycleVersion;
    const binding = url === "/login-desktop/save" || url === "/accounts/cookies";
    if (!binding) transientRequests.add(controller);
    const timeout = url === "/accounts/cookies" ? 210000 : url === "/login-desktop/save" ? (payload.async_save ? 12000 : 120000) : url.endsWith("/open") ? 100000 : 20000;
    const deadline = window.setTimeout(() => controller.abort(), timeout);
    let response, data;
    try {
      response = await fetch(url, { method: "POST", body: formData, credentials: "same-origin", cache: "no-store", signal: controller.signal });
      data = await response.json().catch((error) => {
        if (error.name === "AbortError") throw error;
        if (binding) throw error;
        return {};
      });
    } catch (error) {
      if (binding) {
        const uncertain = new Error("请求没有返回完整结果，服务器可能仍在保存。请先核对账号，不要重复提交。");
        uncertain.bindingUncertain = true;
        throw uncertain;
      }
      throw error;
    } finally { window.clearTimeout(deadline); transientRequests.delete(controller); }
    if (!binding && (!pageActive || version !== lifecycleVersion)) throw new DOMException("页面已离开", "AbortError");
    if (!response.ok || data.ok === false) {
      const error = new Error(data.error || `请求失败：${response.status}`);
      // Keep the server's grading (category / categoryLabel / retryable) so
      // callers can render an actionable message instead of a bare string.
      error.payload = data;
      if (binding && response.status >= 500 && data.binding_pending !== false) {
        error.bindingUncertain = true;
        error.message = "服务器没有返回确定的保存结果，请先到账号页核对，暂时不要重复提交。";
      }
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
    cancelQr();
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
    if (!pageActive) return;
    const previousState = workspace.state;
    const previousTicket = workspace.ticket;
    workspace = next || { state: "closed", active: false, position: 0, ticket: "" };
    if (workspace.state !== previousState || workspace.ticket !== previousTicket) workspaceVersion += 1;
    publishWorkspace();
    if (workspace.state !== previousState || workspace.ticket !== previousTicket) {
      cancelQr();
      browserLoginLabel = "";
      scheduleStatus(workspace.state === "queued" || workspace.state === "resetting" ? 4000 : workspace.state === "active" ? 20000 : 30000);
    }
    const progress = root.querySelector("[data-login-progress]");
    if (progress) progress.textContent = workspace.state === "queued" ? "第 1 步：排队等待，轮到你后自动显示二维码。"
      : workspace.state === "active" && workspace.active ? "第 2 步：扫码，按提示完成验证码或密码验证。"
      : "第 1 步：申请登录工作区。";
    document.querySelectorAll("[data-login-step]").forEach((item) => {
      if (Number(item.dataset.loginStep) === (workspace.state === "active" && workspace.active ? 2 : 1)) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
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
        if (canUseWorkspace()) loadFrame(true);
        qrPollStartedAt = Date.now();
        setQrButtons("读取中…", { busy: true });
        if (canUseWorkspace()) refreshLoginQr(100);
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
      button.disabled = bindingBusy || bindingNeedsReview;
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
    if (!qrImage || workspace.state !== "active" || !workspace.active || !canUseWorkspace() || bindingNeedsReview) return;
    window.clearTimeout(qrRefreshTimer);
    const heldTicket = workspace.ticket, heldVersion = qrVersion;
    const current = () => canUseWorkspace() && heldTicket === workspace.ticket && heldVersion === qrVersion;
    qrRefreshTimer = window.setTimeout(async () => {
      if (!current() || qrRequestPending || qrManualPending) return;
      qrRequestPending = true;
      const controller = new AbortController();
      qrController = controller;
      const deadline = window.setTimeout(() => controller.abort(), 25000);
      if (qrStatus) qrStatus.textContent = `正在读取登录二维码…${qrWaitLabel()}`;
      const retryLater = (message, delayMs = 3000) => {
        if (!current()) return;
        if (retries > 1 && workspace.state === "active") {
          if (qrStatus) qrStatus.textContent = `${message} 继续等待…${qrWaitLabel()}`;
          refreshLoginQr(delayMs, retries - 1);
        } else if (qrStatus) {
          qrStatus.textContent = `${message} 已停止自动重试，请点击“刷新二维码”。`;
          setQrButtons("已停止，点此重试", { stopped: true });
        }
      };
      try {
        const response = await fetch(`/login-desktop/qr?t=${Date.now()}`, { credentials: "same-origin", cache: "no-store", headers: { "X-SparkFlow-Workspace": heldTicket }, signal: controller.signal });
        if (!current()) return;
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
        if (!current()) return;
        const previous = qrImage.dataset.objectUrl || "";
        const objectUrl = URL.createObjectURL(blob);
        qrImage.src = objectUrl;
        qrImage.dataset.objectUrl = objectUrl;
        qrImage.hidden = false;
        if (qrOpenLink) {
          qrOpenLink.href = objectUrl;
          qrOpenLink.hidden = false;
        }
        if (previous) URL.revokeObjectURL(previous);
        if (qrStatus) qrStatus.textContent = `二维码已加载。如果过期，点击刷新。${qrWaitLabel()}`;
        setQrButtons("刷新二维码");
      } catch (error) {
        if (current()) retryLater(error.name === "AbortError" ? "二维码读取超时" : "登录页正在加载");
      } finally {
        window.clearTimeout(deadline);
        qrRequestPending = false;
        if (qrController === controller) qrController = null;
        if (heldVersion !== qrVersion && canUseWorkspace() && workspace.active && qrImage.hidden && !qrManualPending && !bindingNeedsReview) refreshLoginQr(100);
      }
    }, delay);
  };

  const scheduleStatus = (delay) => {
    window.clearTimeout(timer);
    timer = pageActive && document.visibilityState === "visible" ? window.setTimeout(pollStatus, delay) : null;
  };
  let statusPolling = false;
  const pollStatus = async () => {
    if (!pageActive || document.visibilityState !== "visible" || bindingBusy) { scheduleStatus(5000); return; }
    if (statusPolling) { statusAgain = statusAgain || resumeChecking; return; }
    window.clearTimeout(timer);
    statusPolling = true;
    const controller = new AbortController();
    statusController = controller;
    const version = lifecycleVersion;
    const ownershipOnly = resumeChecking;
    const deadline = window.setTimeout(() => controller.abort(), 10000);
    const heldTicket = workspace.ticket;
    try {
      const statusUrl = workspace.state === "active" && !ownershipOnly ? "/login-desktop/status" : "/login-desktop/workspace-status";
      const response = await fetch(statusUrl, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
      if (heldTicket !== workspace.ticket || version !== lifecycleVersion || !pageActive || document.visibilityState !== "visible") return;
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
        renderWorkspace({ state: "closed", active: false, position: 0, ticket: "" });
        closeFrame();
        setRecoverVisible(false);
        setStatus("登录状态已失效，请重新登录控制台。", "danger");
        return;
      }
      const data = await response.json();
      if (heldTicket !== workspace.ticket || version !== lifecycleVersion || !pageActive || document.visibilityState !== "visible") return;
      if (!response.ok || data.ok === false) {
        setStatus(data.error || "登录工作区暂时不可用，请稍后重试。", "danger");
        return;
      }
      if (data.workspace && data.workspace.state === "active" && data.workspace.active) {
        setRecoverVisible(false);
      }
      statusFailures = 0;
      resumeChecking = false;
      renderWorkspace(data.workspace);
      if (workspace.state === "active" && workspace.active) {
        if (Object.prototype.hasOwnProperty.call(data, "logged_in") && !data.logged_in) browserLoginLabel = "";
        loadFrame();
        if (qrImage?.hidden && !qrRequestPending && !bindingNeedsReview) refreshLoginQr(100);
        if (statusHeld()) {
          // An in-progress save or its error owns the status line.
        } else if (data.logged_in) {
          browserLoginLabel = data.username || "当前账号";
          const progress = root.querySelector("[data-login-progress]");
          if (progress) progress.textContent = "第 3 步：浏览器已登录，保存后还会核对账号身份。";
          document.querySelectorAll("[data-login-step]").forEach((item) => {
            if (item.dataset.loginStep === "3") item.setAttribute("aria-current", "step");
            else item.removeAttribute("aria-current");
          });
          setStatus(`当前浏览器已登录：${browserLoginLabel}，请保存登录态。`, "success");
        } else if (data.login_state === "unknown") {
          // The page was mid-operation, so "not logged in" would be a guess.
          setStatus("正在检查登录状态…（页面正忙，请稍候再保存）", "warning");
        }
      } else {
        closeFrame();
      }
    } catch (error) {
      if (version === lifecycleVersion && pageActive && document.visibilityState === "visible") {
        statusFailures += 1;
        setStatus(error.name === "AbortError" ? "状态检查响应较慢，将自动重试。" : `状态检查失败：${error.message}`, "warning");
        if (resumeChecking) setRecoverVisible(true);
      }
    } finally {
      window.clearTimeout(deadline);
      if (statusController === controller) statusController = null;
      statusPolling = false;
      const delay = statusAgain || (ownershipOnly && !resumeChecking) ? 100
        : statusFailures ? Math.min(60000, 5000 * 2 ** Math.min(statusFailures - 1, 4))
        : workspace.state === "queued" || workspace.state === "resetting" ? 4000
        : workspace.state === "active" ? 20000 : 30000;
      statusAgain = false;
      scheduleStatus(delay);
    }
  };

  let heartbeatPending = false;
  const heartbeat = async () => {
    if (!pageActive || resumeChecking || heartbeatPending || workspace.state !== "active" || !workspace.active || !workspace.ticket) return;
    if (Date.now() - lastHeartbeatAt < 25000) return;
    const heldTicket = workspace.ticket;
    const version = workspaceVersion;
    heartbeatPending = true;
    lastHeartbeatAt = Date.now();
    try {
      const data = await postForm("/login-desktop/heartbeat", { ticket: heldTicket });
      if (heldTicket !== workspace.ticket || version !== workspaceVersion) return;
      renderWorkspace(data.workspace);
    } catch (error) {
      if (heldTicket !== workspace.ticket || version !== workspaceVersion) return;
      if (error.payload && error.payload.leaseLost) {
        recoverLease();
        return;
      }
      setStatus("工作区续期暂未收到回应，正在核对状态；请先保留当前页面。", "warning");
      pollStatus();
    } finally { heartbeatPending = false; }
  };

  document.querySelectorAll(".login-desktop-open").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!pageActive || acquiringWorkspace || bindingBusy || bindingNeedsReview) return;
      acquiringWorkspace = true;
      bindingResultVersion += 1;
      if (bindingResult) bindingResult.hidden = true;
      if (section) section.open = true;
      startOpening();
      try {
        resetRecoveryBudget();
        const data = await postForm("/login-desktop/open", workspaceRequest());
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
      } finally { acquiringWorkspace = false; }
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
    if (qrManualPending) return;
    if (!canUseWorkspace() || !workspace.active || !workspace.ticket) {
      if (qrStatus) qrStatus.textContent = "请先申请自己的登录工作区，再刷新二维码。";
      return;
    }
    qrManualPending = true;
    cancelQr();
    const heldTicket = workspace.ticket, heldVersion = qrVersion;
    const controller = new AbortController();
    qrController = controller;
    const deadline = window.setTimeout(() => controller.abort(), 25000);
    qrPollStartedAt = Date.now();
    const refreshForm = new FormData();
    refreshForm.set("csrf_token", csrfToken);
    refreshForm.set("ticket", workspace.ticket);
    let response, data;
    try {
      response = await fetch("/login-desktop/qr/refresh", {
        method: "POST", body: refreshForm, credentials: "same-origin", cache: "no-store", signal: controller.signal,
      });
      data = await response.json().catch((error) => { if (error.name === "AbortError") throw error; return {}; });
    } finally {
      window.clearTimeout(deadline);
      if (qrController === controller) qrController = null;
      qrManualPending = false;
    }
    if (!pageActive || !canUseWorkspace() || heldTicket !== workspace.ticket || heldVersion !== qrVersion) return;
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
      if (bindingBusy || bindingNeedsReview) return;
      button.disabled = true;
      const heldTicket = workspace.ticket, version = lifecycleVersion;
      try {
        resetRecoveryBudget();
        await requestQrRefresh();
      } catch (error) {
        if (pageActive && version === lifecycleVersion && heldTicket === workspace.ticket && qrStatus) qrStatus.textContent = error.name === "AbortError"
          ? "刷新请求超时，页面可能仍在生成二维码。请先更新画面核对，再决定是否重新刷新。"
          : `刷新二维码失败：${error.message}`;
      } finally {
        button.disabled = bindingBusy || bindingNeedsReview;
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
      if (bindingBusy || bindingNeedsReview) return;
      const cookieInput = String(input?.value || "").trim();
      if (!cookieInput) {
        if (status) status.textContent = "请先粘贴 Cookie 内容。";
        return;
      }
      setBindingBusy(true);
      const previousLabel = status ? status.textContent : "";
      if (status) status.textContent = "正在验证 Cookie 并登录，请稍候...";
      try {
        const selectedRecord = reloginToggle?.checked ? reloginSelect?.selectedOptions?.[0] : null;
        const cookiePayload = {
          cookie_input: cookieInput,
          display_name: String(displayName?.value || "").trim(),
          relogin_unique_id: selectedRecord ? String(selectedRecord.dataset.uniqueId || "") : "",
          relogin_account_ref: selectedRecord ? String(selectedRecord.value || "") : "",
          login_generation: selectedRecord ? String(selectedRecord.dataset.loginGeneration || "") : "",
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
        if (status) status.textContent = data.message || "Cookie 登录成功，请继续设置发送目标。";
        if (input) input.value = "";
        showBindingResult(data.account, data.message || "账号身份和登录状态已核验，可以继续配置发送目标。");
      } catch (error) {
        if (error.bindingUncertain) {
          setBindingBusy(false, { uncertain: true });
          if (input) input.value = "";
          if (status) status.textContent = error.message;
          showBindingResult(null, error.message, { uncertain: true });
          return;
        }
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
        setBindingBusy(false);
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
    setBindingBusy(saving);
    document.querySelectorAll(".login-desktop-save").forEach((item) => {
      item.disabled = saving || keepDisabled;
    });
    // Whatever is shown next (result or error) stays readable for a while.
    statusHeldUntil = saving ? Infinity : Date.now() + 8000;
    if (!saving) return;
    const startedAt = Date.now();
    const render = () => {
      const seconds = Math.round((Date.now() - startedAt) / 1000);
      setStatus(`正在核对保存结果…已等待 ${seconds} 秒，请不要重复保存。`, "warning");
    };
    render();
    savingTimer = window.setInterval(render, 1000);
  };
  const saveUncertain = (message) => {
    const error = new Error(message);
    error.bindingUncertain = true;
    return error;
  };
  const saveError = (payload, status) => {
    const error = new Error(payload.error || `保存未完成（HTTP ${status}）`);
    error.payload = { ...payload, leaseLost: Number(status) === 423 };
    return error;
  };
  const showSaveUncertain = (operation, message) => {
    if (operation !== saveOperation || !pageActive) return;
    setSaving(false, { keepDisabled: true });
    setBindingBusy(false, { uncertain: true });
    const text = message + " 账号列表里暂时没有记录，也不代表保存失败。可以点“重新检查保存结果”，只查询这次操作，不会重复保存。";
    setStatus(message, "warning");
    showBindingResult(null, text, { uncertain: true });
  };
  const showSaveProgress = (operation, detail) => {
    if (operation !== saveOperation || !pageActive || !bindingResult) return;
    bindingResult.hidden = false;
    bindingResult.querySelector("[data-binding-result-title]").textContent = "正在核对保存结果";
    bindingResult.querySelector("[data-binding-result-message]").textContent = "保存任务已发出，正在核对登录信息和账号身份。请保留本页，不必重复保存。";
    bindingResult.querySelector("[data-binding-next]").hidden = true;
    const preferences = bindingResult.querySelector("[data-binding-preferences]");
    if (preferences) preferences.hidden = true;
    if (saveCheckButton) { saveCheckButton.hidden = false; saveCheckButton.disabled = true; }
    if (saveCheckStatus) { saveCheckStatus.hidden = false; if (saveCheckStatus.textContent !== detail) saveCheckStatus.textContent = detail; }
  };
  const observeSave = async (operation) => {
    if (operation.checking) return operation.checking;
    const version = saveReadVersion;
    const until = Date.now() + 180000;
    const valid = () => operation === saveOperation && pageActive && version === saveReadVersion;
    const checkCurrent = () => {
      if (!valid()) {
        const error = new Error("页面已离开，返回后可以继续检查这次保存。");
        error.saveSuspended = true;
        throw error;
      }
      if (workspace.ticket && workspace.ticket !== operation.ticket) throw saveUncertain("工作区已切换，这次保存的结果需要单独核对。");
    };
    const check = async () => {
      let failures = 0;
      while (Date.now() < until) {
        checkCurrent();
        const controller = new AbortController();
        operation.controller = controller;
        const deadline = window.setTimeout(() => controller.abort(), Math.min(12000, until - Date.now()));
        try {
          const response = await fetch(`/login-desktop/save/status?operation_id=${encodeURIComponent(operation.id)}`, {
            credentials: "same-origin", cache: "no-store", signal: controller.signal,
          });
          const data = await response.json();
          checkCurrent();
          if (response.status === 401 || response.status === 403) throw saveUncertain("平台会话或权限已变化，请重新登录平台后核对保存结果。");
          if (response.status === 404) throw saveUncertain("暂未查到这次保存的任务记录，可能尚未接收或记录已过期。请稍后重新检查。");
          if (response.status === 409 && data.binding_pending) throw saveUncertain(data.error || "账号登录状态已更新，请核对最新结果。");
          if (!response.ok || data.ok === false) throw new Error(data.error || `检查响应 ${response.status}`);
          if (data.ticket && data.ticket !== operation.ticket) throw saveUncertain("这条保存记录属于之前的工作区，请在账号页单独核对。");
          if (data.operation_id && data.operation_id !== operation.id) throw saveUncertain("保存任务编号不一致，请重新检查这次保存。");
          if (data.state === "done" || data.state === "failed") {
            if (!data.result || typeof data.result !== "object" || Array.isArray(data.result)) throw saveUncertain("服务器没有返回完整的保存结果。");
            if (data.state === "failed" || data.result.ok === false) throw saveError(data.result, data.http_status || 400);
            return data.result;
          }
          if (data.state === "uncertain") throw saveUncertain(data.result?.error || data.message || "服务器暂时无法确认最终保存结果。");
          if (data.state !== "running") throw saveUncertain("保存任务返回了未知状态，请重新检查。");
          failures = 0;
          showSaveProgress(operation, "服务器仍在处理，请保留当前页面。");
        } catch (error) {
          if (!valid() || error.saveSuspended || error.bindingUncertain || error.payload) throw error;
          failures += 1;
          showSaveProgress(operation, error.name === "AbortError"
            ? "本次结果检查超时，正在继续核对；不会再次提交保存。"
            : "暂时没有收到检查回应，正在重试查询；不会再次提交保存。");
        } finally {
          window.clearTimeout(deadline);
          if (operation.controller === controller) operation.controller = null;
        }
        checkCurrent();
        await new Promise((resolve) => {
          operation.cancelWait = () => { window.clearTimeout(operation.waitTimer); resolve(); };
          operation.waitTimer = window.setTimeout(resolve, Math.min(until - Date.now(), failures ? Math.min(10000, 2500 * failures) : document.visibilityState === "visible" ? 2000 : 5000));
        });
        operation.cancelWait = null;
      }
      throw saveUncertain("已等待 3 分钟，尚未收到最终保存结果。后台可能仍在处理。");
    };
    operation.checking = check().finally(() => {
      operation.checking = null;
      if (operation === saveOperation && saveCheckButton) saveCheckButton.disabled = false;
    });
    return operation.checking;
  };
  const finishSavedLogin = (operation, data) => {
    if (operation !== saveOperation || !pageActive) return;
    if (!data.account || (!data.account.account_ref && !data.account.unique_id)) throw saveUncertain("保存响应缺少账号信息，请重新检查最终结果。");
    saveOperation = null;
    bindingNeedsReview = false;
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
      showBindingResult(data.account, data.verification_error || "账号已保存，但登录验证没有通过，请在账号页重新登录。", { pending: true });
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
    showBindingResult(data.account, `已保存账号 ${data.account?.username || ""}${refreshNote}。接下来选择好友或群，再设置自己的发送内容。`, { pending: data.verification === "pending" });
    if (data.verification === "pending") followBindingVerification(data.account);
  };
  const settleSave = async (operation, read) => {
    const version = saveReadVersion;
    try {
      const data = await read();
      if (version !== saveReadVersion) { const error = new Error("页面已离开"); error.saveSuspended = true; throw error; }
      if (workspace.ticket && workspace.ticket !== operation.ticket) throw saveUncertain("工作区已切换，原保存结果仍需单独核对。");
      if (operation === saveOperation && pageActive) finishSavedLogin(operation, data);
    } catch (error) {
      if (operation !== saveOperation || !pageActive) return;
      if (error.saveSuspended || version !== saveReadVersion) { showSaveUncertain(operation, "页面已返回，可以继续检查上一次保存。"); return; }
      if (error.bindingUncertain) { showSaveUncertain(operation, error.message); return; }
      bindingNeedsReview = false;
      setSaving(false);
      const info = error.payload?.identity_confirmation;
      const candidates = error.payload?.duplicate_candidates || (error.payload?.duplicate_candidate ? [error.payload.duplicate_candidate] : []);
      if ((info || candidates.length) && operation.confirmations < 3) {
        let extra = null;
        if (info && confirmIdentityMigration(info)) extra = { confirm_identity: info.token };
        else if (!info && candidates.length === 1 && window.confirm(
          `已有一个同名账号（${candidates[0].username || "未命名"}）。\n\n点“确定”＝更新这个已有账号，保留目标和发送记录；\n点“取消”＝放弃保存，不做任何改动。`,
        )) extra = { merge_with: candidates[0].account_ref };
        if (extra) { startSave({ ...operation.payload, ...extra }, operation.confirmations + 1); return; }
        if (candidates.length > 1) error.message = "有多个同名账号，请先到账号页确认要更新哪一个，再重新保存。";
        else { error.cancelled = true; error.message = "已取消保存，没有改动任何账号。"; }
      }
      saveOperation = null;
      if (saveCheckButton) saveCheckButton.hidden = true;
      if (saveCheckStatus) saveCheckStatus.hidden = true;
      if (bindingResult) {
        bindingResult.hidden = false;
        bindingResult.querySelector("[data-binding-result-title]").textContent = error.cancelled ? "已取消保存" : "保存未完成";
        bindingResult.querySelector("[data-binding-result-message]").textContent = error.payload?.identityState ? identityFailureText(error) : error.message;
        const next = bindingResult.querySelector("[data-binding-next]");
        next.hidden = false; next.href = "/accounts"; next.textContent = "查看账号列表";
        const preferences = bindingResult.querySelector("[data-binding-preferences]");
        if (preferences) preferences.hidden = true;
      }
      statusHeldUntil = Date.now() + 10000;
      setStatus(error.cancelled ? error.message : `登录账号未保存：${error.message}`, error.cancelled ? "" : "warning");
      if (error.payload?.leaseLost) recoverLease();
      else pollStatus();
    }
  };
  const startSave = (payload, confirmations = 0) => {
    let id;
    try { id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join(""); }
    catch { setStatus("浏览器无法生成安全的保存编号，请使用 HTTPS 重新打开本页。", "warning"); return; }
    const operation = { id, ticket: payload.ticket, payload, confirmations, startedAt: Date.now(), checking: null };
    saveReadVersion += 1;
    saveOperation = operation;
    bindingNeedsReview = false;
    setSaving(true);
    showSaveProgress(operation, "正在提交保存任务…");
    resetRecoveryBudget();
    settleSave(operation, async () => {
      let data;
      try { data = await postForm("/login-desktop/save", { ...payload, async_save: "1", request_id: id }); }
      catch (error) {
        if (error.payload?.operation_id && /^[A-Za-z0-9_-]{16,128}$/.test(error.payload.operation_id)) {
          operation.id = error.payload.operation_id;
          showSaveProgress(operation, "服务器已有正在处理的保存，正在检查它的结果；不会重新保存。");
          return observeSave(operation);
        }
        if (!error.bindingUncertain) throw error;
        showSaveProgress(operation, "保存提交没有返回完整回应，正在按任务编号核对；不会重复提交。");
        return observeSave(operation);
      }
      if (!pageActive) { const error = new Error("页面已离开"); error.saveSuspended = true; throw error; }
      if (data.ticket && data.ticket !== operation.ticket) throw saveUncertain("保存记录与当前工作区不一致，请重新核对。");
      if (data.operation_id && data.operation_id !== id) throw saveUncertain("服务器返回的保存编号不一致，请重新检查。");
      if (data.state === "running" || data.operation_id) return observeSave(operation);
      return data;
    });
  };
  saveCheckButton?.addEventListener("click", () => {
    const operation = saveOperation;
    if (!operation || operation.checking || !pageActive) return;
    setSaving(true);
    showSaveProgress(operation, "正在重新检查同一次保存，仅查询结果…");
    settleSave(operation, () => observeSave(operation));
  });
  document.querySelectorAll(".login-desktop-save").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.disabled || bindingBusy || bindingNeedsReview) return;
      if (!workspace.ticket) { setStatus("请先申请自己的登录工作区，完成登录后再保存。", "warning"); return; }
      startSave({ ticket: workspace.ticket, relogin_unique_id: button.dataset.reloginUniqueId || "", relogin_account_ref: button.dataset.reloginAccountRef || "", login_generation: button.dataset.loginGeneration || "" });
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
    const previousTicket = workspace.ticket;
    renderWorkspace(trackWorkspace(data.workspace));
    if (openFrame && data.state !== "queued" && canUseWorkspace()) loadFrame();
    if (pollQr && data.state !== "queued" && workspace.active && previousTicket === workspace.ticket && qrImage?.hidden) refreshLoginQr(100);
  };
  const acquireWorkspace = async () => {
    if (!pageActive || acquiringWorkspace || bindingBusy || bindingNeedsReview) return;
    const wasAcquiring = acquiringWorkspace;
    acquiringWorkspace = true;
    try {
      const data = await postForm("/login-desktop/open", workspaceRequest());
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
    } finally { acquiringWorkspace = false; }
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
    if (!pageActive || document.visibilityState !== "visible" || bindingBusy || bindingNeedsReview) return;
    // The status poll calls back into this function when the workspace is not
    // ours, so a server that keeps contradicting itself must not recurse here.
    if (recoveringLease) return;
    recoveringLease = true;
    cancelQr();
    window.clearTimeout(recoveryTimer);
    recoveryTimer = null;
    workspace = { state: "closed", active: false, position: 0, ticket: "" };
    root.dataset.workspaceTicket = "";
    root.dataset.workspaceActive = "0";
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
  root.addEventListener("login-interaction-lease-lost", () => recoverLease());

  if (section) section.addEventListener("toggle", () => { if (section.open) pollStatus(); });
  const countdown = () => {
    if (!pageActive || document.visibilityState !== "visible" || workspace.state !== "active" || !workspace.active) return;
    workspace.remaining_seconds = Math.max(0, Number(workspace.remaining_seconds || 0) - 1);
    const remaining = workspace.remaining_seconds;
    if (statusHeld() || resumeChecking) return;
    setStatus(browserLoginLabel
      ? `当前浏览器已登录：${browserLoginLabel}，请保存登录态。`
      : `登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`, remaining <= 60 ? "warning" : "success");
  };
  const startTimers = () => {
    window.clearInterval(heartbeatTimer);
    window.clearInterval(countdownTimer);
    // The server lease lasts 180s. Renew every 30s, including the 120s grace
    // while hidden; frames and upstream status checks pause immediately.
    heartbeatTimer = window.setInterval(heartbeat, 30000);
    countdownTimer = window.setInterval(countdown, 1000);
  };
  let hiddenReleaseTimer = null, hiddenSince = 0;
  const scheduleHiddenRelease = () => {
    window.clearTimeout(hiddenReleaseTimer);
    hiddenReleaseTimer = null;
    if (!pageActive || !hiddenSince || document.visibilityState !== "hidden" || bindingNeedsReview) return;
    hiddenReleaseTimer = window.setTimeout(releaseWorkspace, Math.max(0, 120000 - (Date.now() - hiddenSince)));
  };
  const releaseWorkspace = () => {
    if (bindingBusy || bindingNeedsReview || !workspace.ticket || !["active", "queued"].includes(workspace.state)) return;
    const body = new FormData();
    body.set("csrf_token", csrfToken);
    body.set("ticket", workspace.ticket);
    if (!navigator.sendBeacon || !navigator.sendBeacon("/login-desktop/release", body)) {
      fetch("/login-desktop/release", { method: "POST", body, credentials: "same-origin", keepalive: true }).catch(() => {});
    }
    workspaceVersion += 1;
    workspace = { state: "closed", active: false, position: 0, ticket: "" };
    publishWorkspace();
    closeFrame();
    setRecoverVisible(true);
    if (pageActive) {
      setStatus("离开页面较久，已释放登录工作区。回来后点“添加新账号”重新申请。", "warning");
      if (qrStatus) qrStatus.textContent = "未保存的登录窗口已关闭，其他用户可以继续使用工作区。";
    }
  };
  const pauseReads = () => {
    lifecycleVersion += 1;
    window.clearTimeout(timer);
    cancelQr();
    statusController?.abort();
    publishWorkspace();
  };
  const checkOwnership = () => {
    resumeChecking = true;
    publishWorkspace();
    setStatus("正在重新检查工作区归属，请稍候…");
    pollStatus();
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      if (!hiddenSince) hiddenSince = Date.now();
      pauseReads();
      scheduleHiddenRelease();
    } else {
      window.clearTimeout(hiddenReleaseTimer);
      hiddenReleaseTimer = null;
      hiddenSince = 0;
      checkOwnership();
    }
  });
  window.addEventListener("pagehide", () => {
    bindingResultVersion += 1;
    saveReadVersion += 1;
    saveOperation?.controller?.abort();
    saveOperation?.cancelWait?.();
    pageActive = false;
    pauseReads();
    window.clearInterval(heartbeatTimer);
    window.clearInterval(countdownTimer);
    stopOpening();
    window.clearInterval(savingTimer);
    window.clearTimeout(recoveryTimer);
    recoveryTimer = null;
    transientRequests.forEach((controller) => controller.abort());
    document.querySelectorAll("[data-cookie-input]").forEach((input) => { input.value = ""; });
    window.clearTimeout(hiddenReleaseTimer);
    hiddenReleaseTimer = null;
    hiddenSince = 0;
    releaseWorkspace();
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    pageActive = true;
    lifecycleVersion += 1;
    recoveringLease = false;
    acquiringWorkspace = false;
    startTimers();
    if (saveOperation) showSaveUncertain(saveOperation, "页面已返回，请继续检查上一次保存的结果。");
    checkOwnership();
  });
  startTimers();
  pollStatus();
})();

(() => {
  const parseJson = (id, fallback = []) => {
    try { return JSON.parse(document.getElementById(id)?.textContent || JSON.stringify(fallback)); }
    catch { return fallback; }
  };
  const delay = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const refreshController = ({ button, status, url, csrf, apply, progress, autoCheck = true }) => {
    const jobUrl = (suffix) => {
      const boundary = url.search(/[?#]/);
      return boundary < 0 ? `${url}/${suffix}`
        : `${url.slice(0, boundary)}/${suffix}${url.slice(boundary)}`;
    };
    let pending = null, checkAgain = false, stopped = false, version = 0;
    const requests = new Set();
    const initialStatus = String(status?.textContent || "").trim();
    let lastSuccess = "";
    const rememberPrevious = (value) => {
      if (!value) return;
      const incoming = Date.parse(value), current = Date.parse(lastSuccess);
      if (!lastSuccess || (Number.isFinite(incoming) && Number.isFinite(current) && incoming > current)) lastSuccess = value;
    };
    const show = (message, preservePrevious = false) => {
      if (!status) return;
      status.textContent = message;
      if (preservePrevious) {
        const detail = document.createElement("span");
        detail.className = "friend-picker-last-refresh";
        detail.textContent = lastSuccess ? `上次成功刷新：${lastSuccess}` : initialStatus || "尚未成功读取列表";
        status.append(" ", detail);
      }
    };
    const request = async (path, body) => {
      const controller = new AbortController();
      requests.add(controller);
      const timeout = window.setTimeout(() => controller.abort(), 12000);
      try {
        const response = await fetch(path, {
          method: body ? "POST" : "GET", ...(body ? { body } : {}),
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (response.status === 401 || response.redirected) {
          const error = new Error("登录状态已失效，请重新登录平台后再检查。");
          error.known = true;
          throw error;
        }
        if (response.status === 403 || response.status === 404) {
          const error = new Error("账号已移除或权限已变化，请重新打开账号页核对。");
          error.known = true;
          throw error;
        }
        const data = await response.json();
        if (!response.ok || data.ok === false) {
          const label = data.categoryLabel ? `（${data.categoryLabel}）` : "";
          const error = new Error(`${data.error || `请求失败（${response.status}）`}${label}`);
          error.known = response.status < 500;
          error.previousUpdatedAt = data.previousUpdatedAt;
          throw error;
        }
        return data;
      } finally {
        window.clearTimeout(timeout);
        requests.delete(controller);
      }
    };
    const execute = async (start) => {
      const heldVersion = version;
      if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
      let started = false;
      try {
        let job = await request(jobUrl("status"));
        if (stopped || heldVersion !== version) return { ok: false, pending: true, started, message: "页面已关闭，未继续检查" };
        if (start && job.state !== "running" && !checkAgain) {
          show("正在启动刷新…");
          const body = new FormData();
          body.set("csrf_token", csrf);
          const data = await request(jobUrl("async"), body);
          job = data.job || data;
          started = true;
        }
        const deadline = Date.now() + 360000;
        while (job.state === "running") {
          started = true;
          if (stopped || heldVersion !== version) return { ok: false, pending: true, started, message: "页面已关闭，未继续检查" };
          show(progress(job));
          if (Date.now() >= deadline) {
            checkAgain = true;
            const message = "暂时未收到最终结果，后台可能仍在处理。请稍后点刷新重新检查；当前选择已保留。";
            show(message, true);
            return { ok: false, pending: true, started, message };
          }
          await delay(2000);
          if (stopped || heldVersion !== version) return { ok: false, pending: true, started, message: "页面已关闭，未继续检查" };
          job = await request(jobUrl("status"));
        }
        if (stopped || heldVersion !== version) return { ok: false, pending: true, started, message: "页面已关闭，未继续检查" };
        if (job.state === "done") {
          checkAgain = false;
          lastSuccess = job.updatedAt || lastSuccess;
          apply(job);
          const message = job.message || "列表已刷新，当前选择已保留。";
          show(message);
          return { ok: true, started, message };
        }
        if (job.state === "failed") {
          rememberPrevious(job.previousUpdatedAt);
          checkAgain = false;
          const error = new Error(job.error || "未能完成刷新，请稍后重试。");
          error.known = true;
          throw error;
        }
        checkAgain = false;
        if (start) {
          const message = "当前没有运行中的刷新任务，可以再点一次刷新。";
          show(message, true);
          return { ok: false, started, message };
        }
        return { ok: true, started, message: "已检查当前状态" };
      } catch (error) {
        if (stopped || heldVersion !== version) return { ok: false, pending: true, started, message: "页面已关闭，未继续检查" };
        rememberPrevious(error.previousUpdatedAt);
        checkAgain = !error.known;
        const message = error.known
          ? `刷新未完成：${error.message}`
          : "暂时未收到刷新结果，后台可能仍在处理。请稍后点刷新重新检查；当前选择已保留。";
        show(message, true);
        return { ok: false, pending: !error.known, started, message };
      } finally {
        if (button) { button.disabled = false; button.removeAttribute("aria-busy"); }
      }
    };
    const run = (start = true) => {
      if (stopped) return Promise.resolve({ ok: false, pending: true, message: "页面已关闭" });
      if (pending) return start ? pending.then((result) => result.started ? result : run(true)) : pending;
      pending = execute(start).finally(() => { pending = null; });
      return pending;
    };
    button?.addEventListener("click", () => run(true));
    window.addEventListener("pagehide", () => {
      stopped = true;
      version += 1;
      requests.forEach((controller) => controller.abort());
    });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) {
        stopped = false;
        const heldVersion = version;
        Promise.resolve(pending).then(() => {
          if (!stopped && heldVersion === version) run(false);
        });
      }
    });
    if (autoCheck) run(false);
    return run;
  };

  // Two pickers in one account share a single read, never a browser refresh.
  const cacheReads = new Map();
  const cacheControllers = new Set();
  let cacheStopped = false;
  window.addEventListener("pagehide", () => {
    cacheStopped = true;
    cacheControllers.forEach((controller) => controller.abort());
    cacheReads.clear();
  });
  window.addEventListener("pageshow", () => { cacheStopped = false; });
  const readCache = (url) => {
    if (cacheReads.has(url)) return cacheReads.get(url);
    const controller = new AbortController();
    cacheControllers.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    const pending = (async () => {
      const response = await fetch(url, {credentials: "same-origin", cache: "no-store", signal: controller.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "列表暂时无法加载，请重试。");
      return result;
    })().catch((error) => {
      if (cacheReads.get(url) === pending) cacheReads.delete(url);
      throw error;
    }).finally(() => {
      window.clearTimeout(timeout);
      cacheControllers.delete(controller);
    });
    cacheReads.set(url, pending);
    return pending;
  };
  const mountCache = (picker, apply) => {
    const url = picker.dataset.cacheUrl;
    if (!url) return () => {};
    const message = document.createElement("p"), retry = document.createElement("button");
    message.className = "notice-inline";
    message.dataset.cacheState = "";
    message.setAttribute("role", "status");
    retry.type = "button";
    retry.className = "button button-quiet button-sm";
    retry.textContent = "重新加载列表";
    let superseded = false, loading = false;
    const load = async () => {
      if (superseded || loading || cacheStopped) return;
      loading = true;
      message.textContent = "正在加载已缓存的列表…";
      picker.append(message);
      try {
        const data = await readCache(url);
        if (!superseded && !cacheStopped) apply(data);
        message.remove();
      } catch (error) {
        if (!superseded && !cacheStopped) {
          message.textContent = error.name === "AbortError" ? "列表加载超时，当前选择已保留。" : error.message;
          message.append(" ", retry);
        }
      } finally { loading = false; }
    };
    retry.addEventListener("click", load);
    window.addEventListener("pageshow", (event) => { if (event.persisted && !superseded) load(); });
    load();
    return () => { superseded = true; message.remove(); };
  };
  const pageControls = (list) => {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "button button-quiet button-sm";
    more.dataset.pickerMore = "";
    more.hidden = true;
    list.after(more);
    return more;
  };

  const initializeWhenVisible = (picker, initialize) => {
    const folds = [];
    for (let node = picker.parentElement; node; node = node.parentElement) {
      if (node.tagName === "DETAILS") folds.push(node);
    }
    const check = () => { if (folds.every((fold) => fold.open)) initialize(); };
    folds.forEach((fold) => fold.addEventListener("toggle", check));
    check();
  };

  document.querySelectorAll(".friend-picker").forEach((picker) => {
    let refresh = null;
    const initialize = (autoCheck = true) => {
      if (refresh) return refresh;
      const recordKey = picker.dataset.recordKey || picker.dataset.accountId;
      const form = picker.closest("form");
      const textarea = form?.querySelector(".targets-textarea");
      const search = picker.querySelector(".friend-search-input");
      const list = picker.querySelector(".friend-picker-list");
      const summary = picker.querySelector(".friend-picker-summary");
      let friends = parseJson(`friends-cache-${recordKey}`);
      const isGroupTarget = (value) => /^群聊[:：]/.test(String(value || "").trim());
      let selected = new Set(parseJson(`selected-targets-${recordKey}`).filter((target) => !isGroupTarget(target)));
      let lastRender = "", searchTimer = null, visibleCount = 100;
      const more = pageControls(list);
      let finishCache = () => {};
      const updateSummary = () => { if (summary) summary.textContent = `已选 ${selected.size} 人`; };
      const render = () => {
        const query = String(search?.value || "").trim().toLowerCase();
        const all = [...new Set([...selected, ...friends])];
        const matches = all.filter((name) => name.toLowerCase().includes(query));
        const names = matches.slice(0, visibleCount);
        more.hidden = matches.length <= visibleCount;
        more.textContent = `再显示 ${Math.min(100, Math.max(0, matches.length - visibleCount))} 人（共 ${matches.length} 人）`;
        updateSummary();
        const signature = JSON.stringify([names, names.map((name) => selected.has(name))]);
        if (signature === lastRender) return;
        lastRender = signature;
        list.textContent = "";
        if (!names.length) {
          const empty = document.createElement("div");
          empty.className = "friend-picker-empty";
          empty.textContent = all.length ? "没有匹配的好友。" : "点击“刷新好友列表”后再选择目标。";
          list.appendChild(empty);
          return;
        }
        const fragment = document.createDocumentFragment();
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
            if (textarea) textarea.value = [...selected].join("\n");
            label.classList.toggle("selected", checkbox.checked);
            updateSummary();
            lastRender = "";
          });
          label.append(text, checkbox);
          fragment.appendChild(label);
        });
        list.appendChild(fragment);
      };
      textarea?.addEventListener("input", () => {
        selected = new Set(String(textarea.value || "").replaceAll(",", "\n").split(/\r?\n/).map((item) => item.trim()).filter((item) => item && !isGroupTarget(item)));
        render();
      });
      search?.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        visibleCount = 100;
        searchTimer = window.setTimeout(render, 80);
      });
      more.addEventListener("click", () => { visibleCount += 100; render(); });
      search?.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
      render();
      refresh = refreshController({
        button: picker.querySelector(".friend-refresh-button"), status: picker.querySelector(".friend-picker-status"),
        url: picker.dataset.refreshUrl, csrf: picker.dataset.csrfToken,
        apply: (job) => { finishCache(); friends = job.friends || friends; render(); },
        progress: (job) => job.stage === "collecting" ? `正在读取好友列表…已采集 ${job.collected || 0} 个` : "正在启动并读取好友列表…",
        autoCheck,
      });
      finishCache = mountCache(picker, (data) => { friends = data.friends || []; render(); });
      return refresh;
    };
    picker.refreshFriends = () => initialize(false)(true);
    initializeWhenVisible(picker, initialize);
  });

  const batchButton = document.querySelector("[data-refresh-all-friends]");
  const batchSummary = document.querySelector("[data-refresh-all-friends-summary]");
  batchButton?.addEventListener("click", async () => {
    const pickers = [...document.querySelectorAll(".friend-picker")];
    if (!pickers.length) {
      if (batchSummary) { batchSummary.hidden = false; batchSummary.textContent = "还没有账号，请先完成登录绑定。"; }
      return;
    }
    const results = [];
    batchButton.disabled = true;
    if (batchSummary) { batchSummary.hidden = false; batchSummary.textContent = `正在刷新 ${pickers.length} 个账号…`; }
    try {
      for (const picker of pickers) {
        if (typeof picker.refreshFriends === "function") results.push(await picker.refreshFriends());
      }
    } finally { batchButton.disabled = false; }
    if (!batchSummary) return;
    const ok = results.filter((item) => item.ok).length;
    const pending = results.filter((item) => item.pending).length;
    const failed = results.filter((item) => !item.ok && !item.pending);
    batchSummary.textContent = failed.length || pending
      ? `成功 ${ok} 个，待核对 ${pending} 个，未完成 ${failed.length} 个。${failed.map((item) => item.message).join("；")}`
      : `全部成功（${ok} 个账号）`;
  });

  document.querySelectorAll(".group-picker").forEach((picker) => {
    let refresh = null;
    const initialize = () => {
      if (refresh) return refresh;
      const recordKey = picker.dataset.recordKey || picker.dataset.accountId;
      const list = picker.querySelector(".group-picker-list");
      const editor = picker.closest(".group-target-editor");
      const bindings = parseJson(`group-bindings-${recordKey}`, {});
      let groups = parseJson(`groups-cache-${recordKey}`);
      const selected = new Set(Object.values(bindings).map(String));
      const names = new Map(Object.entries(bindings).map(([target, conversationId]) => [String(conversationId), target.replace(/^群聊[:：]/, "")]));
      const search = document.createElement("input");
      search.type = "search";
      search.className = "friend-search-input group-search-input";
      search.placeholder = "搜索群名或编号";
      search.setAttribute("aria-label", "搜索群名或编号");
      const summary = document.createElement("p");
      summary.className = "friend-picker-summary";
      const submittedSelection = document.createElement("div");
      submittedSelection.hidden = true;
      list.before(search, summary, submittedSelection);
      let searchTimer = null, visibleCount = 100;
      const more = pageControls(list);
      let finishCache = () => {};
      const updateSummary = () => {
        summary.textContent = `已选 ${selected.size} 个群`;
        submittedSelection.textContent = "";
        [...selected].sort().forEach((conversationId) => {
          const input = document.createElement("input");
          input.type = "hidden";
          input.name = "group_bind";
          input.value = conversationId;
          submittedSelection.appendChild(input);
        });
      };
      const render = () => {
        list.textContent = "";
        updateSummary();
        const known = new Map();
        groups.forEach((group) => {
          const conversationId = String(group.conversationId || "");
          if (!conversationId) return;
          names.set(conversationId, String(group.name || "未命名群"));
          known.set(conversationId, {name: names.get(conversationId), conversationId});
        });
        const available = new Set(known.keys());
        selected.forEach((conversationId) => {
          if (!known.has(conversationId)) known.set(conversationId, {name: names.get(conversationId) || "已选群", conversationId});
        });
        const query = search.value.trim().toLowerCase();
        const fragment = document.createDocumentFragment();
        const matches = [...known.values()].filter((group) => !query || `${group.name} ${group.conversationId}`.toLowerCase().includes(query));
        more.hidden = matches.length <= visibleCount;
        more.textContent = `再显示 ${Math.min(100, Math.max(0, matches.length - visibleCount))} 个群（共 ${matches.length} 个群）`;
        matches.slice(0, visibleCount).forEach((group) => {
          const label = document.createElement("label"), box = document.createElement("input"), text = document.createElement("span");
          box.type = "checkbox";
          box.dataset.groupChoice = "";
          box.value = group.conversationId;
          box.checked = selected.has(group.conversationId);
          text.textContent=`${group.name}（${String(group.conversationId)}）`;
          if (!available.has(group.conversationId)) {
            const note = document.createElement("small");
            note.textContent = "本次未读取到，已保留选择；可取消";
            text.append(" ", note);
          }
          box.addEventListener("change", () => {
            if (box.checked) selected.add(group.conversationId);
            else selected.delete(group.conversationId);
            updateSummary();
          });
          label.append(box, text);
          fragment.appendChild(label);
        });
        list.appendChild(fragment);
        if (!list.childElementCount) {
          const empty = document.createElement("p");
          empty.className = "friend-picker-empty";
          empty.textContent = query ? "没有匹配的群。" : "刷新群列表后，可以选择需要发送的群。";
          list.appendChild(empty);
        }
      };
      search.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        visibleCount = 100;
        searchTimer = window.setTimeout(render, 80);
      });
      more.addEventListener("click", () => { visibleCount += 100; render(); });
      search.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
      render();
      refresh = refreshController({
        button: editor?.querySelector(".group-refresh-button"), status: editor?.querySelector("[data-group-status]"),
        url: picker.dataset.refreshUrl, csrf: picker.dataset.csrfToken,
        apply: (job) => {
          finishCache();
          groups = job.groups || groups;
          const notices = picker.querySelector(".group-picker-notices");
          if (notices) {
            notices.textContent = "";
            (job.targets || []).filter((target) => target.status !== "ok").forEach((target) => {
              const pill = document.createElement("span");
              pill.className = `pill ${target.status === "missing" ? "danger" : "warning"}`;
              pill.textContent = target.status === "missing" ? "未找到此群" : `已改名为 ${target.name || ""}`;
              notices.appendChild(pill);
            });
          }
          render();
        },
        progress: () => "正在读取群列表…当前选择已保留。",
      });
      finishCache = mountCache(picker, (data) => { groups = data.groups || []; render(); });
      return refresh;
    };
    initializeWhenVisible(picker, initialize);
  });
})();

(() => {
  const renderAvailableIcons = () => {
    if (window.lucide) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  };
  window.addEventListener("DOMContentLoaded", renderAvailableIcons);
  document.querySelector("[data-icon-library]")?.addEventListener("load", renderAvailableIcons, {once: true});
  if (document.readyState !== "loading") renderAvailableIcons();
})();

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
