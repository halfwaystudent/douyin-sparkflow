(() => {
  const start = () => {
    const panel = document.querySelector("[data-login-interaction]");
    const root = document.getElementById("login-desktop-controls");
    if (!panel || !root) return;
    const image = panel.querySelector("[data-interaction-image]");
    const status = panel.querySelector("[data-interaction-status]");
    const input = panel.querySelector("[data-interaction-text]");
    const send = panel.querySelector("[data-interaction-send]");
    let stopped = false, frameBusy = false, objectUrl = "", dragging = false;
    let timer = null, ticking = false;
    let lastPoint = null, pendingMove = null, moving = false, queue = Promise.resolve();
    const requests = new Set();
    const ticket = () => root.dataset.workspaceActive === "1" ? root.dataset.workspaceTicket || "" : "";
    const message = (text) => { status.textContent = text; };
    const clear = () => {
      input.value = "";
      image.hidden = true;
      image.removeAttribute("src");
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = "";
      dragging = false;
      pendingMove = null;
    };
    const request = async (operation, action, heldTicket) => {
      if (!heldTicket || heldTicket !== ticket()) throw new Error("请先申请登录工作区");
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      requests.add(controller);
      const form = new FormData();
      form.set("csrf_token", root.dataset.csrfToken || "");
      form.set("ticket", heldTicket);
      if (action) form.set("action", JSON.stringify(action));
      try {
        const response = await fetch(`/login-desktop/interaction/${operation}`, {
          method: "POST", body: form, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          if (response.status === 423) {
            clear();
            root.dispatchEvent(new Event("login-interaction-lease-lost"));
          }
          throw new Error(data.error || "登录窗口暂时不可用");
        }
        return operation === "screen" ? await response.blob() : await response.json();
      } finally {
        clearTimeout(timeout);
        requests.delete(controller);
      }
    };
    const action = (value) => {
      const heldTicket = ticket();
      queue = queue.catch(() => {}).then(async () => {
        if (stopped || !panel.open || heldTicket !== ticket()) return;
        await request("action", value, heldTicket);
      });
      queue.catch((error) => message(error.name === "AbortError" ? "操作超时，请稍后重试" : error.message));
      return queue;
    };
    const frame = async () => {
      if (stopped || frameBusy || !panel.open || document.visibilityState !== "visible") return;
      const heldTicket = ticket();
      if (!heldTicket) {
        clear();
        message("先点“添加新账号”或“打开登录工作区”，轮到你后即可操作。");
        return;
      }
      frameBusy = true;
      try {
        const blob = await request("screen", null, heldTicket);
        if (stopped || !panel.open || heldTicket !== ticket()) return;
        const next = URL.createObjectURL(blob);
        const previous = objectUrl;
        objectUrl = next;
        image.src = next;
        await image.decode().catch(() => {});
        if (stopped || !panel.open || heldTicket !== ticket()) { clear(); return; }
        image.hidden = false;
        if (previous) URL.revokeObjectURL(previous);
        message("直接点击或拖动窗口；输入验证码或密码时，先点击对应输入框，再使用下方输入栏。");
      } catch (error) {
        if (!stopped) message(error.name === "AbortError" ? "窗口响应较慢，正在等待下一次更新" : error.message);
      } finally { frameBusy = false; }
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
      try {
        while (pendingMove && dragging) {
          const next = pendingMove;
          pendingMove = null;
          await action({type: "pointer", phase: "move", ...next});
        }
      } catch { /* The request handler displays the error. */ }
      finally { moving = false; }
    };
    image.addEventListener("pointerdown", (event) => {
      if (!ticket() || event.button !== 0) return;
      event.preventDefault();
      dragging = true;
      lastPoint = point(event);
      image.setPointerCapture(event.pointerId);
      action({type: "pointer", phase: "down", ...lastPoint});
      tick();
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
    image.addEventListener("wheel", (event) => {
      if (!ticket()) return;
      event.preventDefault();
      action({type: "wheel", dy: Math.max(-800, Math.min(800, event.deltaY))});
    }, {passive: false});
    send.addEventListener("click", async () => {
      const text = input.value;
      if (!text || send.disabled) return;
      send.disabled = true;
      input.value = "";
      try {
        await action({type: "text", text});
        message("已输入到登录窗口，确认无误后点窗口中的提交按钮。");
      } catch { /* Do not restore sensitive input after a failed request. */ }
      finally { send.disabled = false; }
    });
    panel.querySelectorAll("[data-interaction-key]").forEach((button) => {
      button.addEventListener("click", () => action({type: "key", key: button.dataset.interactionKey}));
    });
    panel.querySelector("[data-interaction-expand]").addEventListener("click", () => {
      panel.classList.toggle("interaction-expanded");
    });
    panel.addEventListener("toggle", () => {
      if (panel.open) tick();
      else {
        panel.classList.remove("interaction-expanded");
        clear();
        requests.forEach((request) => request.abort());
      }
    });
    const tick = async () => {
      if (stopped || ticking) return;
      ticking = true;
      clearTimeout(timer);
      try { await frame(); }
      finally {
        ticking = false;
        if (!stopped) timer = setTimeout(tick, dragging ? 250 : 1200);
      }
    };
    let lastTicket = ticket();
    new MutationObserver(() => {
      const current = ticket();
      if (current !== lastTicket) {
        lastTicket = current;
        clear();
        requests.forEach((request) => request.abort());
        tick();
      }
    }).observe(root, {attributes: true, attributeFilter: ["data-workspace-ticket", "data-workspace-active"]});
    tick();
    window.addEventListener("pagehide", () => {
      stopped = true;
      clearTimeout(timer);
      clear();
      requests.forEach((request) => request.abort());
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();
