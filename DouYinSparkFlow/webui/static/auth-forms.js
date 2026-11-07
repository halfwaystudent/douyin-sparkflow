(() => {
  if (document.body.hasAttribute("data-auth-theme")) {
    const root = document.documentElement;
    const system = window.matchMedia?.("(prefers-color-scheme: dark)");
    const storedTheme = () => {
      try { return localStorage.getItem("sparkflow-theme"); }
      catch { return null; }
    };
    const followSystem = () => {
      const stored = storedTheme();
      root.dataset.theme = stored === "dark" || stored === "light" ? stored : system?.matches ? "dark" : "light";
    };
    followSystem();
    system?.addEventListener?.("change", followSystem);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const next = root.dataset.theme === "dark" ? "light" : "dark";
        root.dataset.theme = next;
        try { localStorage.setItem("sparkflow-theme", next); } catch {}
      });
    });
  }
  const token = new URLSearchParams(window.location.hash.slice(1)).get("invite");
  const invitation = document.querySelector('[name="invitation_token"]');
  if (token && invitation) {
    invitation.value = token;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  document.querySelectorAll("form[data-auth-form]").forEach((form) => {
    form.querySelectorAll('input[type="password"]:not([name="invitation_token"]):not([name="bootstrap_token"])').forEach((input) => {
      const wrapper = document.createElement("div");
      wrapper.className = "password-input";
      input.before(wrapper);
      wrapper.append(input);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "password-toggle";
      toggle.textContent = "显示";
      toggle.setAttribute("aria-label", "显示密码");
      toggle.setAttribute("aria-pressed", "false");
      toggle.addEventListener("click", () => {
        const visible = input.type === "password";
        input.type = visible ? "text" : "password";
        toggle.textContent = visible ? "隐藏" : "显示";
        toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
        toggle.setAttribute("aria-pressed", String(visible));
        input.focus();
      });
      wrapper.append(toggle);
    });
    let submitting = false;
    const reset = () => {
      submitting = false;
      form.removeAttribute("aria-busy");
      form.querySelectorAll('[type="submit"]').forEach((button) => { button.disabled = false; });
    };
    window.addEventListener("pageshow", reset);
    form.addEventListener("submit", (event) => {
      if (submitting) { event.preventDefault(); return; }
      submitting = true;
      form.setAttribute("aria-busy", "true");
      form.querySelectorAll('[type="submit"]').forEach((button) => { button.disabled = true; });
    });
  });
  document.querySelector("[data-copy-invitation]")?.addEventListener("click", async () => {
    const input = document.querySelector("[data-invitation-url]");
    const status = document.querySelector("[data-invitation-copy-status]");
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = "邀请链接已复制。";
    } catch {
      input.focus(); input.select();
      status.textContent = "请手动复制已选中的邀请链接。";
    }
  });
})();
