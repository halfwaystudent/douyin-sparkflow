(() => {
  const forms = [...document.querySelectorAll("form[data-editor-form]")];
  if (!forms.length) return;
  const states = new Map();
  let leaving = false;
  const fingerprint = (form) => JSON.stringify([...new FormData(form)]
    .filter(([key]) => !["csrf_token", "edit_revision"].includes(key))
    .map(([key, value]) => {
      if (["targets", "group_targets", "messages"].includes(key)) {
        value = String(value).split(/\r?\n/).map((line) => line.trim()).filter(Boolean).join("\n");
      }
      return [key, String(value)];
    })
    .sort(([leftKey, leftValue], [rightKey, rightValue]) => leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue)));
  const dirty = (form) => fingerprint(form) !== states.get(form).saved;
  const hasChanges = () => forms.some((form) => dirty(form) || states.get(form).pending);
  window.sparkflowEditorWarning = () => hasChanges() ? "当前页面还有未保存或正在保存的修改，离开后请先核对保存结果。" : "";
  window.addEventListener("sparkflow:leave-approved", () => { leaving = true; });
  window.addEventListener("pageshow", () => { leaving = false; });
  window.addEventListener("beforeunload", (event) => {
    if (leaving || !hasChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const confirmLeave = () => {
    const warning = window.sparkflowEditorWarning();
    if (!warning || window.confirm(`${warning}\n仍然离开吗？`)) { leaving = true; return true; }
    return false;
  };
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href);
    if (!["http:", "https:"].includes(url.protocol)) return;
    if (url.origin === location.origin && url.pathname === location.pathname && url.search === location.search && url.hash) return;
    if (!confirmLeave()) event.preventDefault();
  });
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented || states.has(event.target)) return;
    if (!confirmLeave()) { event.preventDefault(); event.stopImmediatePropagation(); }
  });

  forms.forEach((form) => {
    const state = { saved: form.dataset.editorUnsaved === "1" ? null : fingerprint(form), pending: false };
    states.set(form, state);
    const status = form.querySelector("[data-editor-state]");
    const feedback = form.querySelector("[data-editor-feedback]");
    const review = form.querySelector("[data-editor-review]");
    const buttons = [...form.querySelectorAll('[type="submit"]')];
    const labels = [...form.querySelectorAll("[data-editor-save-label]")];
    const originalLabels = labels.map((node) => node.textContent);
    const updateStatus = () => {
      if (status) status.textContent = state.pending ? "正在保存…" : dirty(form) ? "有未保存修改" : "";
      const badge = form.closest("details.fold")?.querySelector("[data-account-draft]");
      if (badge) {
        badge.hidden = !state.pending && !dirty(form);
        badge.textContent = state.pending ? "保存中" : "未保存";
      }
    };
    const show = (text, failed = false) => {
      if (!feedback) return;
      feedback.hidden = false;
      feedback.classList.toggle("editor-error", failed);
      feedback.textContent = text;
    };
    form.addEventListener("input", () => { leaving = false; updateStatus(); });
    form.addEventListener("change", () => { leaving = false; updateStatus(); });
    updateStatus();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.pending || !form.reportValidity()) return;
      const body = new FormData(form);
      const submitted = fingerprint(form);
      state.pending = true;
      form.setAttribute("aria-busy", "true");
      buttons.forEach((button) => { button.disabled = true; });
      labels.forEach((node) => { node.textContent = "正在保存…"; });
      updateStatus();
      if (review) review.hidden = true;
      const controller = new AbortController();
      const deadline = window.setTimeout(() => controller.abort(), 45000);
      try {
        const response = await fetch(form.action, {
          method: "POST", body, credentials: "same-origin", cache: "no-store", signal: controller.signal,
          headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
        });
        if (response.redirected || response.status === 401) throw new Error("平台登录状态已失效。请先重新登录，再核对保存结果；本页输入仍保留。");
        if (response.status === 403 && !(response.headers.get("content-type") || "").includes("json")) {
          const denied = new Error("页面凭据或账号权限已变化，本次修改未保存。输入仍在，请重新打开账号页核对。");
          denied.review = true;
          throw denied;
        }
        const data = await response.json();
        if (!response.ok || data.ok !== true) {
          const error = new Error(data.error || "暂时无法保存，请稍后重试。你的输入已保留。");
          error.review = response.status === 409 || response.status >= 500;
          throw error;
        }
        const revision = form.querySelector('[name="edit_revision"]');
        if (revision && typeof data.edit_revision === "string") revision.value = data.edit_revision;
        state.saved = submitted;
        const currentDirty = dirty(form);
        show(`${data.message || "设置已保存。"}${currentDirty ? " 保存期间新增的修改还未提交。" : ""}`);
        const card = form.closest("details.fold");
        if (card && data.account) {
          const name = card.querySelector("[data-account-saved-name]");
          const count = card.querySelector("[data-account-target-count]");
          if (name) name.textContent = data.account.username;
          if (count) count.textContent = data.account.target_count;
          const enabled = card.querySelector("[data-account-enabled]");
          if (enabled) {
            enabled.textContent = data.account.enabled ? "已启用" : "已停用";
            enabled.classList.toggle("success", Boolean(data.account.enabled));
            enabled.classList.toggle("warning", !data.account.enabled);
          }
          const next = form.querySelector("[data-editor-next]");
          if (next) next.hidden = !data.account.target_count;
        }
        const nextRun = form.querySelector("[data-editor-next-run]");
        if (nextRun && "automatic_enabled" in data) {
          nextRun.hidden = false;
          nextRun.textContent = data.automatic_enabled && data.next_run_display
            ? `下一次安排：${data.next_run_display}（${body.get("timezone")}）`
            : "自动发送已暂停，需要时可以手动发送。";
        }
      } catch (error) {
        const transport = error.name === "AbortError" || error instanceof TypeError || error instanceof SyntaxError;
        show(transport ? "没有收到完整保存结果，服务器可能仍在处理。输入已保留，请先核对已保存内容再重试。" : error.message, true);
        if (review) review.hidden = !(transport || error.review || /登录/.test(error.message));
      } finally {
        window.clearTimeout(deadline);
        state.pending = false;
        form.removeAttribute("aria-busy");
        buttons.forEach((button) => { button.disabled = false; });
        labels.forEach((node, index) => { node.textContent = originalLabels[index]; });
        updateStatus();
      }
    });
  });
})();

(() => {
  document.querySelectorAll("form[data-merge-duplicate-form]").forEach((form) => {
    const target = form.querySelector("select[data-target-account-ref]");
    const generation = form.querySelector("input[data-target-login-generation]");
    if (!target || !generation) return;

    const syncTargetGeneration = () => {
      generation.value = target.selectedOptions[0]?.dataset.loginGeneration || "";
    };
    syncTargetGeneration();
    target.addEventListener("change", syncTargetGeneration);
    form.addEventListener("submit", syncTargetGeneration, true);
  });
})();

