(() => {
  const start = () => {
    const panel = document.querySelector("[data-login-interaction]");
    const root = document.getElementById("login-desktop-controls");
    if (!panel || !root) return;
    const image = panel.querySelector("[data-interaction-image]");
    const status = panel.querySelector("[data-interaction-status]");
    const input = panel.querySelector("[data-interaction-text]");
    const send = panel.querySelector("[data-interaction-send]");
    const expand = panel.querySelector("[data-interaction-expand]");
    const refresh = panel.querySelector("[data-interaction-refresh]");
    const keys = Array.from(panel.querySelectorAll("[data-interaction-key]"));
    let stopped = false, frameBusy = false, objectUrl = "", dragging = false;
    let timer = null, epoch = 0, ready = false, failures = 0, statusUntil = 0, textPending = false;
    let lastPoint = null, pendingMove = null, moving = false, queue = Promise.resolve(), pendingActions = 0;
    let wheelTimer = null, wheelSending = false, pendingWheel = 0, lastActivity = Date.now();
    const requests = new Set();
    const ticket = () => root.dataset.workspaceActive === "1" ? root.dataset.workspaceTicket || "" : "";
    const visible = () => !stopped && panel.open && document.visibilityState === "visible";
    const eligible = () => visible() && Boolean(ticket());
    const current = (heldTicket, heldEpoch) => eligible() && heldTicket === ticket() && heldEpoch === epoch;
    const message = (text, hold = 0) => { status.textContent = text; statusUntil = hold ? Date.now() + hold : 0; };
    const setReady = (value) => {
      ready = value;
      input.disabled = !value;
      send.disabled = !value || textPending;
      keys.forEach((button) => { button.disabled = !value; });
    };
    const clear = () => {
      input.value = "";
      image.hidden = true;
      image.removeAttribute("src");
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = "";
      dragging = false;
      moving = false;
      pendingMove = null;
      pendingWheel = 0;
      textPending = false;
      wheelSending = false;
      setReady(false);
    };
    const pause = () => {
      epoch += 1;
      clearTimeout(timer);
      clearTimeout(wheelTimer);
      timer = wheelTimer = null;
      pendingActions = 0;
      queue = Promise.resolve();
      requests.forEach((request) => request.abort());
      clear();
      panel.removeAttribute("aria-busy");
    };
    const request = async (operation, value, heldTicket, heldEpoch) => {
      if (!current(heldTicket, heldEpoch)) return null;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      requests.add(controller);
      const form = new FormData();
      form.set("csrf_token", root.dataset.csrfToken || "");
      form.set("ticket", heldTicket);
      if (value) form.set("action", JSON.stringify(value));
      try {
        const response = await fetch(`/login-desktop/interaction/${operation}`, {
          method: "POST", body: form, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!current(heldTicket, heldEpoch)) return null;
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          if (!current(heldTicket, heldEpoch)) return null;
          if (response.status === 423) {
            pause();
            root.dataset.workspaceActive = "0";
            message("工作区已释放，请重新检查工作区后继续。");
            root.dispatchEvent(new Event("login-interaction-lease-lost"));
            return null;
          }
          throw new Error(data.error || "登录窗口暂时不可用");
        }
        const result = operation === "screen" ? await response.blob() : await response.json();
        return current(heldTicket, heldEpoch) ? result : null;
      } finally {
        clearTimeout(timeout);
        requests.delete(controller);
      }
    };
    const schedule = (delay = 0) => {
      clearTimeout(timer);
      timer = eligible() ? setTimeout(frame, delay) : null;
    };
    const action = (value) => {
      const heldTicket = ticket(), heldEpoch = epoch;
      if (!eligible() || !ready) {
        message("请先等待当前登录窗口显示，再点击窗口中的输入框。");
        return Promise.resolve(false);
      }
      lastActivity = Date.now();
      pendingActions += 1;
      clearTimeout(timer);
      const task = queue.catch(() => {}).then(async () => {
        if (!current(heldTicket, heldEpoch)) return false;
        return Boolean(await request("action", value, heldTicket, heldEpoch));
      }).finally(() => {
        if (heldEpoch === epoch) {
          pendingActions -= 1;
          if (!pendingActions && !dragging) schedule(150);
        }
      });
      task.catch((error) => {
        if (current(heldTicket, heldEpoch)) message(error.name === "AbortError"
          ? "未收到操作结果，请先核对窗口变化，不要重复输入。"
          : error.message, 8000);
      });
      queue = task;
      return task;
    };
    const frame = async () => {
      timer = null;
      if (!eligible() || frameBusy) return;
      if (pendingActions || dragging) { schedule(250); return; }
      const heldTicket = ticket(), heldEpoch = epoch;
      frameBusy = true;
      panel.setAttribute("aria-busy", "true");
      if (!ready && Date.now() >= statusUntil) message("正在读取登录窗口…网络较慢时需要几秒，请稍候。");
      try {
        const blob = await request("screen", null, heldTicket, heldEpoch);
        if (!blob || !current(heldTicket, heldEpoch)) return;
        const next = URL.createObjectURL(blob);
        const previous = objectUrl;
        image.src = next;
        try { await image.decode(); }
        catch (error) { URL.revokeObjectURL(next); throw error; }
        if (!current(heldTicket, heldEpoch)) { URL.revokeObjectURL(next); return; }
        objectUrl = next;
        image.hidden = false;
        if (previous) URL.revokeObjectURL(previous);
        failures = 0;
        setReady(true);
        if (Date.now() >= statusUntil) message("点击或拖动窗口完成验证；输入密码或验证码前，先点击窗口里的输入框。");
      } catch (error) {
        if (current(heldTicket, heldEpoch)) {
          failures += 1;
          setReady(false);
          message(error.name === "AbortError" ? "画面读取超时，正在重试。也可以点“更新画面”。" : `${error.message}，正在重试。`, 3000);
        }
      } finally {
        frameBusy = false;
        if (heldEpoch === epoch) panel.removeAttribute("aria-busy");
        if (eligible()) schedule(heldEpoch !== epoch ? 0 : failures
          ? Math.min(10000, 1500 * 2 ** (failures - 1))
          : Date.now() - lastActivity < 10000 ? 1200 : 3000);
      }
    };
    const point = (event) => {
      const rect = image.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(image.naturalWidth - 1, (event.clientX - rect.left) * image.naturalWidth / rect.width)),
        y: Math.max(0, Math.min(image.naturalHeight - 1, (event.clientY - rect.top) * image.naturalHeight / rect.height)),
      };
    };
    const move = async () => {
      if (moving) return;
      moving = true;
      const heldEpoch = epoch;
      try {
        while (heldEpoch === epoch && pendingMove && dragging) {
          const next = pendingMove;
          pendingMove = null;
          await action({type: "pointer", phase: "move", ...next});
        }
      } catch { /* The request handler displays the error. */ }
      finally { if (heldEpoch === epoch) moving = false; }
    };
    image.addEventListener("pointerdown", (event) => {
      if (!eligible() || !ready || event.button !== 0) return;
      event.preventDefault();
      dragging = true;
      lastPoint = point(event);
      image.setPointerCapture(event.pointerId);
      action({type: "pointer", phase: "down", ...lastPoint});
    });
    image.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      lastPoint = point(event);
      pendingMove = lastPoint;
      move();
    });
    const release = (event) => {
      if (!dragging) return;
      lastPoint = point(event);
      dragging = false;
      pendingMove = null;
      action({type: "pointer", phase: "up", ...lastPoint});
    };
    image.addEventListener("pointerup", release);
    image.addEventListener("pointercancel", release);
    image.addEventListener("contextmenu", (event) => event.preventDefault());
    image.addEventListener("dragstart", (event) => event.preventDefault());
    const flushWheel = () => {
      wheelTimer = null;
      if (wheelSending || !pendingWheel || !eligible() || !ready) return;
      const dy = pendingWheel, heldEpoch = epoch;
      pendingWheel = 0;
      wheelSending = true;
      action({type: "wheel", dy}).catch(() => {}).finally(() => {
        if (heldEpoch !== epoch) return;
        wheelSending = false;
        if (pendingWheel) wheelTimer = setTimeout(flushWheel, 80);
      });
    };
    image.addEventListener("wheel", (event) => {
      if (!eligible() || !ready) return;
      event.preventDefault();
      pendingWheel = Math.max(-800, Math.min(800, pendingWheel + event.deltaY));
      if (!wheelTimer && !wheelSending) wheelTimer = setTimeout(flushWheel, 80);
    }, {passive: false});
    send.addEventListener("click", async () => {
      const text = input.value;
      if (!text || send.disabled) return;
      const heldEpoch = epoch;
      textPending = true;
      send.disabled = true;
      input.value = "";
      try {
        if (await action({type: "text", text})) message("已输入到登录窗口，核对无误后再点窗口中的确认按钮。", 8000);
      } catch { /* Never restore or replay sensitive input after a failed request. */ }
      finally { if (heldEpoch === epoch) { textPending = false; send.disabled = !ready; } }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); send.click(); }
    });
    keys.forEach((button) => button.addEventListener("click", () => action({type: "key", key: button.dataset.interactionKey})));
    const expanded = (value) => {
      panel.classList.toggle("interaction-expanded", value);
      expand?.setAttribute("aria-expanded", String(value));
      if (expand) expand.textContent = value ? "还原窗口" : "放大窗口";
    };
    expand?.addEventListener("click", () => expanded(!panel.classList.contains("interaction-expanded")));
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && panel.classList.contains("interaction-expanded")) { expanded(false); expand?.focus(); }
    });
    refresh?.addEventListener("click", () => { failures = 0; statusUntil = 0; schedule(); });
    const wake = () => {
      if (!visible()) return;
      if (!ticket()) {
        setReady(false);
        message("先点“添加新账号”或“打开登录工作区”，轮到你后即可操作。");
        return;
      }
      failures = 0;
      statusUntil = 0;
      lastActivity = Date.now();
      schedule();
    };
    panel.addEventListener("toggle", () => {
      if (panel.open) wake();
      else { expanded(false); pause(); }
    });
    let lastTicket = ticket();
    new MutationObserver(() => {
      const next = ticket();
      if (next !== lastTicket) {
        lastTicket = next;
        pause();
        wake();
      }
    }).observe(root, {attributes: true, attributeFilter: ["data-workspace-ticket", "data-workspace-active"]});
    document.addEventListener("visibilitychange", () => {
      pause();
      if (document.visibilityState === "visible") wake();
      else message("已暂停画面更新。回到本页后会先检查工作区，再显示最新窗口。");
    });
    window.addEventListener("pagehide", () => { stopped = true; pause(); });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) { stopped = false; pause(); wake(); }
    });
    setReady(false);
    wake();
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();
