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
