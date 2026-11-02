(() => {
  const supported = window.isSecureContext && window.PublicKeyCredential && navigator.credentials;
  const decode = (value) => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), (char) => char.charCodeAt(0));
  const encode = (value) => btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const optionsFor = (options, registration) => {
    options.challenge = decode(options.challenge);
    if (registration) options.user.id = decode(options.user.id);
    for (const key of ["allowCredentials", "excludeCredentials"]) {
      if (options[key]) options[key].forEach((item) => { item.id = decode(item.id); });
    }
    return options;
  };
  const serialize = (credential) => {
    if (typeof credential.toJSON === "function") return credential.toJSON();
    const response = { clientDataJSON: encode(credential.response.clientDataJSON) };
    for (const key of ["attestationObject", "authenticatorData", "signature", "userHandle"]) {
      if (credential.response[key]) response[key] = encode(credential.response[key]);
    }
    if (credential.response.getTransports) response.transports = credential.response.getTransports();
    return { id: credential.id, rawId: encode(credential.rawId), type: credential.type,
      response, clientExtensionResults: credential.getClientExtensionResults() };
  };
  const post = async (url, body) => {
    const response = await fetch(url, { method: "POST", body, credentials: "same-origin" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "操作失败，请重试。");
    return result;
  };
  document.querySelectorAll("[data-passkey-login], [data-passkey-register]").forEach((button) => {
    const registration = button.hasAttribute("data-passkey-register");
    const container = button.closest("[data-passkey-panel]");
    const status = container.querySelector("[data-passkey-status]");
    if (!supported) {
      button.disabled = true;
      status.textContent = "当前浏览器不支持通行密钥，请使用密码登录。";
      return;
    }
    button.addEventListener("click", async () => {
      button.disabled = true;
      status.textContent = "请在设备上完成验证…";
      try {
        const body = new FormData();
        if (registration) {
          body.set("csrf_token", container.dataset.csrfToken);
          body.set("current_password", container.querySelector('[name="passkey_current_password"]').value);
        } else {
          const username = document.querySelector('form[action="/login"] [name="username"]').value.trim();
          if (!username) throw new Error("请先填写用户名。");
          body.set("username", username);
        }
        const base = registration ? "/account/passkeys/" : "/auth/passkeys/";
        const challenge = await post(base + "options", body);
        const publicKey = optionsFor(challenge.options, registration);
        const credential = registration ? await navigator.credentials.create({ publicKey }) : await navigator.credentials.get({ publicKey });
        if (!credential) throw new Error("验证已取消。");
        body.delete("current_password");
        body.set("challenge_id", challenge.challenge_id);
        body.set("credential", JSON.stringify(serialize(credential)));
        if (registration) body.set("label", container.querySelector('[name="passkey_label"]').value);
        const result = await post(base + "verify", body);
        if (registration) window.location.reload();
        else window.location.assign(result.redirect);
      } catch (error) {
        status.textContent = error.name === "NotAllowedError" ? "验证已取消或超时，可以重试或使用密码。" : error.message;
      } finally {
        button.disabled = false;
        const password = container.querySelector('[name="passkey_current_password"]');
        if (password) password.value = "";
      }
    });
  });
  document.querySelectorAll("[data-passkey-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("移除这个通行密钥？密码登录仍可使用。")) return;
      const body = new FormData();
      body.set("credential_id", button.dataset.passkeyDelete);
      body.set("csrf_token", button.dataset.csrfToken);
      button.disabled = true;
      try {
        await post("/account/passkeys/delete", body);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        window.alert(error.message);
      }
    });
  });
})();
