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

