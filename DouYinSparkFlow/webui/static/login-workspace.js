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
  let lastHeartbeatAt = 0, browserLoginLabel = "", desktopState = {};
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
  const showBindingResult = (account, message, { pending = false, uncertain = false, failed = false } = {}) => {
    if (!bindingResult) return;
    bindingResult.hidden = false;
    bindingResult.querySelector("[data-binding-result-title]").textContent = uncertain ? "保存结果待核对" : failed ? "账号已保存，核验未通过" : pending ? "账号已保存，登录待确认" : "账号已保存";
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
        showBindingResult(current, message, { pending: state !== "verified" || current.identity_state !== "verified", failed: state === "login_required" || state === "identity_conflict" || current.identity_state === "conflict" });
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

  const reusableAccountSession = () => desktopState.profile_mode === "account" && desktopState.reusable_session === true;
  const updateDesktopState = (desktop) => {
    if (!desktop || typeof desktop !== "object") return;
    // Status replies are snapshots, not deltas. Merging let a prior ticket's
    // logged_in:true survive a new ticket, a false/unknown result, or a rescan.
    desktopState = { ...desktop };
    if (desktopState.profile_mode === "account" && desktopState.reusable_session === true) {
      document.querySelectorAll("[data-refresh-login-qr] span").forEach((label) => { label.textContent = "重新扫码"; });
    }
  };
  const clearDesktopState = () => {
    desktopState = {};
    browserLoginLabel = "";
  };
  const reusableAccountHint = () => reusableAccountSession() ? "沿用账号浏览器环境，可保存登录态。" : "";

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

  const renderWorkspace = (next, desktop = null) => {
    if (!pageActive) return;
    const previousState = workspace.state;
    const previousTicket = workspace.ticket;
    workspace = next || { state: "closed", active: false, position: 0, ticket: "" };
    if (workspace.state !== previousState || workspace.ticket !== previousTicket) workspaceVersion += 1;
    publishWorkspace();
    if (workspace.state !== previousState || workspace.ticket !== previousTicket) {
      cancelQr();
      if (workspace.ticket !== previousTicket) {
        qrStoppedTicket = "";
        clearDesktopState();
        setQrButtons("刷新二维码");
      }
      scheduleStatus(workspace.state === "queued" || workspace.state === "resetting" ? 4000 : workspace.state === "active" ? 20000 : 30000);
    }
    // Bind a fresh desktop snapshot only after retiring the previous ticket.
    // Applying it before clearDesktopState erased the successful open response.
    if (desktop) updateDesktopState(desktop);
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
        if (desktopState.logged_in) {
          setQrButtons(reusableAccountSession() ? "重新扫码" : "刷新二维码");
          if (qrStatus) qrStatus.textContent = `${reusableAccountHint()} 当前浏览器已登录，请核对并保存登录态。`;
        } else if (canUseWorkspace()) {
          refreshLoginQr(100);
        }
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
            : `${reusableAccountHint()} 登录工作区已分配给当前会话，剩余 ${remaining} 秒。完成扫码后请保存登录态。`,
          tone,
        );
      }
      return;
    }
    setStatus("登录工作区当前未分配给本会话（空闲或由其他会话占用）。点“添加新账号”，或在账号页选择“重新登录”。");
  };

  let qrPollStartedAt = 0;
  let qrStoppedTicket = "";
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
    if (stopped) qrStoppedTicket = workspace.ticket;
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
    if (!qrImage || workspace.state !== "active" || !workspace.active || !canUseWorkspace() || bindingNeedsReview || (workspace.ticket && qrStoppedTicket === workspace.ticket)) return;
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
          if (data.logged_in || data.state === "qr_logged_in" || data.state === "logged_in") {
            updateDesktopState(data);
            if (qrStatus) qrStatus.textContent = data.message || `${reusableAccountHint()} 当前浏览器已登录，请保存登录态。`;
            setQrButtons(reusableAccountSession() ? "重新扫码" : "刷新二维码");
            return;
          }
          if (data.retryable === false) {
            if (qrStatus) qrStatus.textContent = data.message || data.error || "登录页未能生成二维码，已停止自动重试。";
            setQrButtons("已停止，点此重试", { stopped: true });
            return;
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
          if (data.retryable === false || data.recovery?.exhausted) {
            if (qrStatus) qrStatus.textContent = data.message || data.recovery?.message || "登录页恢复失败，已停止自动重试。";
            setQrButtons("已停止，点此重试", { stopped: true });
            return;
          }
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
      renderWorkspace(data.workspace, data.desktop || data);
      if (workspace.state === "active" && workspace.active) {
        if (Object.prototype.hasOwnProperty.call(data, "logged_in") && !data.logged_in) browserLoginLabel = "";
        loadFrame();
        if (qrImage?.hidden && !qrRequestPending && !bindingNeedsReview && !desktopState.logged_in) refreshLoginQr(100);
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
        renderWorkspace(data.workspace, data.desktop);
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
    qrStoppedTicket = "";
    cancelQr();
    clearDesktopState();
    const progress = root.querySelector("[data-login-progress]");
    if (progress) progress.textContent = "第 2 步：正在生成新的二维码，请扫码完成登录。";
    setStatus("正在重新生成二维码，请扫码完成登录。", "warning");
    setQrButtons("读取中…", { busy: true });
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
          showSaveProgress(operation, data.message || "服务器仍在处理，请保留当前页面。");
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
      showBindingResult(data.account, data.verification_error || "账号已保存，但登录验证没有通过，请在账号页重新登录。", { pending: true, failed: true });
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
    renderWorkspace(trackWorkspace(data.workspace), data.desktop || null);
    if (openFrame && data.state !== "queued" && canUseWorkspace()) loadFrame();
    if (pollQr && !desktopState.logged_in && data.state !== "queued" && workspace.active && previousTicket === workspace.ticket && qrImage?.hidden) refreshLoginQr(100);
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

