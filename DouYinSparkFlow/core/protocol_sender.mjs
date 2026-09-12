#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { Blob } from "node:buffer";
import { pathToFileURL } from "node:url";

export const SDK_BUNDLES = [
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/lib-polyfill.f81f86eb.js",
    sha256: "9076c568e86629b229e3f81f649d1a83e67d9645c3ba574406c3e6b2a804bc80",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/lib-router.5ab9ff10.js",
    sha256: "bbf164fe655433eaccfd346d87853b151f64a6391882c56dbc50a449e42095c8",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/2105.f8d74876.js",
    sha256: "a802f1c74efda6c83b3d26fd84b66980d88ea9bf12e87de1fbef4c6c4741bc4e",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/douyin_creator_data_old.2f971672.js",
    sha256: "77579ca1db8bd7ee874b27d6eb0691cdb36ef7eb11fb77c53cc81462e719d846",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/argus-builder-strategy.5a053c46.js",
    sha256: "1e8859043f62711f662f89710ee5e1c2c870af27d55dc174a2d7b09b70e1a121",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/7676.a4cd4900.js",
    sha256: "38cd5b2be2436fc9ce38b51b53045e4a8a2f05d9a581626b1969ed93722adab0",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/4916.56c33d22.js",
    sha256: "91e689e17d16f61386604daef544cfcb187fd02abc6885db1e5baf0f08381a7f",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/8198.b5c0b108.js",
    sha256: "56f7d2477591f3dd4951288354ad71d750a06ab5d7636adfc5640d323e29530d",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/4168.b2e72401.js",
    sha256: "d33a99a60aca01c0aa269adf112d4e49fc2436d734dd49fd209fa1faf1ec5a40",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/7771.d27d1891.js",
    sha256: "a96f33881f8fcd329aaa42546b324285efdb1f258419fa259be6bacde25c7c25",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/6682.2a991dfb.js",
    sha256: "4a63e6279be76182cb937f56587ebc77db91ba328579c238f84d4c8e4330a867",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/361.4fc40815.js",
    sha256: "02160b7dabc6a8437adb4f417b3975ef1e03058eae87bd72b36a1d8ba20bba78",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/pages-chat.c817de31.js",
    sha256: "59ef0c6329bc5606cbf0a003d0836eaff56224caf0627820d5a8fcf11fa5c482",
  },
];

const CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat";
const USER_AGENT =
  (process.env.SPARKFLOW_PROTOCOL_USER_AGENT ||
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36").trim();

function noop() {}

function toCookieString(cookies) {
  return (cookies || [])
    .filter((item) => item?.name && item?.value !== undefined)
    .map((item) => `${item.name}=${item.value}`)
    .join("; ");
}

function normalizeNickname(value) {
  return String(value || "").trim();
}

function stableNow() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function toNonNegativeInteger(value, fallback = 0) {
  const parsed = Number.parseInt(value, 10);
  if (Number.isNaN(parsed) || parsed < 0) {
    return fallback;
  }
  return parsed;
}

function normalizeSendStrategy(raw = {}) {
  const intervalMin = toNonNegativeInteger(raw.messageIntervalSecondsMin, 0);
  const intervalMax = Math.max(intervalMin, toNonNegativeInteger(raw.messageIntervalSecondsMax, intervalMin));
  return {
    messageIntervalSecondsMin: intervalMin,
    messageIntervalSecondsMax: intervalMax,
  };
}

function randomBetweenInclusive(min, max) {
  if (max <= min) {
    return min;
  }
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

const SEND_MESSAGE_STATUS_NAMES = {
  0: "Succeeded",
  1: "UserNotInConversation",
  2: "CheckConversationNotPass",
  3: "CheckMessageNotPass",
  4: "CheckMessageNotPassButSelfVisible",
  5: "UserHasBeenBlock",
};

function sendMessageStatusName(statusCode) {
  if (statusCode === null || statusCode === undefined) {
    return "";
  }
  return SEND_MESSAGE_STATUS_NAMES[Number(statusCode)] || "Unknown";
}

export function isSuccessfulSendResult(sendResult) {
  return (
    Boolean(sendResult?.success) &&
    Number(sendResult?.statusCode) === 0
  );
}

function publicSendResultSummary(sendResult) {
  if (!sendResult || typeof sendResult !== "object") {
    return {};
  }
  const summary = {};
  for (const key of ["success", "statusCode", "statusMsg", "checkCode", "checkMsg", "errorCode", "errorMsg"]) {
    if (sendResult[key] !== undefined) {
      summary[key] = sendResult[key];
    }
  }
  summary.rawKeys = Object.keys(sendResult).sort();
  return summary;
}

async function readStdinJson() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) {
    throw new Error("Missing JSON payload on stdin");
  }
  return JSON.parse(raw);
}

export function sha256Hex(body) {
  const buffer = Buffer.isBuffer(body) ? body : Buffer.from(body);
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

export function verifyBundleBytes(body, expectedSha256) {
  const actual = sha256Hex(body);
  const expected = String(expectedSha256 || "").toLowerCase();
  if (!expected || actual !== expected) {
    throw new Error(
      `Bundle integrity check failed: expected=${expected || "missing"} actual=${actual}`,
    );
  }
  return actual;
}

async function ensureBundles(cacheDir) {
  await fs.promises.mkdir(cacheDir, { recursive: true });
  for (const bundle of SDK_BUNDLES) {
    const { url, sha256 } = bundle;
    const filename = url.split("/").at(-1);
    const filePath = path.join(cacheDir, filename);
    if (fs.existsSync(filePath)) {
      const cached = await fs.promises.readFile(filePath);
      try {
        verifyBundleBytes(cached, sha256);
        continue;
      } catch {
        // A stale or modified cache entry is replaced only after the download verifies.
      }
    }
    const response = await fetch(url, { headers: { "User-Agent": USER_AGENT } });
    if (!response.ok) {
      throw new Error(`Failed to download SDK bundle ${url}: ${response.status}`);
    }
    const body = Buffer.from(await response.arrayBuffer());
    verifyBundleBytes(body, sha256);
    await fs.promises.writeFile(filePath, body);
  }
}

function createWebpackRequire(bundleDir, cookieString) {
  const modules = {};
  const cache = {};

  function requireModule(id) {
    if (cache[id]) {
      return cache[id].exports;
    }
    if (!modules[id]) {
      throw new Error(`Missing webpack module ${id}`);
    }
    const module = { exports: {} };
    cache[id] = module;
    modules[id].call(module.exports, module, module.exports, requireModule);
    return module.exports;
  }

  requireModule.d = (exports, definition) => {
    for (const key of Object.keys(definition)) {
      if (!Object.prototype.hasOwnProperty.call(exports, key)) {
        Object.defineProperty(exports, key, {
          enumerable: true,
          get: definition[key],
        });
      }
    }
  };
  requireModule.o = (obj, prop) => Object.prototype.hasOwnProperty.call(obj, prop);
  requireModule.r = (exports) => {
    if (typeof Symbol !== "undefined" && Symbol.toStringTag) {
      Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
    }
    Object.defineProperty(exports, "__esModule", { value: true });
  };
  requireModule.n = (mod) => {
    const getter = mod && mod.__esModule ? () => mod.default : () => mod;
    requireModule.d(getter, { a: getter });
    return getter;
  };
  requireModule.g = globalThis;
  requireModule.hmd = (module) => module;
  requireModule.nmd = (module) => module;

  const chunkArray = [];
  chunkArray.push = (chunk) => Object.assign(modules, chunk[1]);

  const fakeElement = () => ({
    style: {},
    setAttribute: noop,
    appendChild: noop,
    removeChild: noop,
    addEventListener: noop,
    removeEventListener: noop,
    getContext: () => ({}),
  });
  const documentRef = {
    cookie: cookieString,
    referrer: CREATOR_CHAT_URL,
    createElement: fakeElement,
    getElementsByTagName: () => [],
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: noop,
    removeEventListener: noop,
    body: { appendChild: noop, removeChild: noop },
    head: { appendChild: noop, removeChild: noop },
    documentElement: { style: {} },
  };
  function XMLHttpRequestStub() {
    this.open = noop;
    this.setRequestHeader = noop;
    this.send = noop;
  }
  function WebSocketStub() {
    this.readyState = 1;
    this.send = noop;
    this.close = noop;
  }

  const context = {
    self: { webpackChunkdouyin_creator_data: chunkArray },
    window: {},
    globalThis: null,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Buffer,
    TextDecoder,
    TextEncoder,
    Blob,
    document: documentRef,
    navigator: {
      userAgent: USER_AGENT,
      language: "en-US",
      cookieEnabled: true,
      onLine: true,
      platform: "Linux x86_64",
      sendBeacon: undefined,
      appName: "Netscape",
    },
    location: {
      href: CREATOR_CHAT_URL,
      protocol: "https:",
      search: "",
      pathname: "/creator-micro/data/following/chat",
      hostname: "creator.douyin.com",
    },
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    performance: { now: () => Date.now() },
    fetch,
    XMLHttpRequest: XMLHttpRequestStub,
    WebSocket: WebSocketStub,
    URL,
    URLSearchParams,
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    crypto,
  };
  context.window = context;
  context.globalThis = context;

  for (const entry of fs.readdirSync(bundleDir).filter((name) => name.endsWith(".js")).sort()) {
    const code = fs.readFileSync(path.join(bundleDir, entry), "utf8");
    try {
      vm.runInNewContext(code, context, { filename: entry });
    } catch {
      // Some bundles execute browser-only entrypoints after registering modules.
    }
  }

  return requireModule;
}

class ProtocolError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "ProtocolError";
    this.details = details;
  }
}

function extractCookieMap(cookies) {
  const items = {};
  for (const item of cookies || []) {
    if (item?.name) {
      items[item.name] = item.value ?? "";
    }
  }
  return items;
}

function buildCreatorHeaders(cookieString, cookieMap, referer = CREATOR_CHAT_URL) {
  return {
    "User-Agent": USER_AGENT,
    Referer: referer,
    Origin: "https://creator.douyin.com",
    Accept: "application/json, text/javascript",
    "Content-Type": "application/x-www-form-urlencoded",
    Cookie: cookieString,
    "x-tt-passport-csrf-token":
      cookieMap.passport_csrf_token || cookieMap.passport_csrf_token_default || "",
  };
}

function buildImHeaders(cookieString) {
  return {
    "User-Agent": USER_AGENT,
    Referer: CREATOR_CHAT_URL,
    Origin: "https://creator.douyin.com",
    Cookie: cookieString,
  };
}

async function fetchJson(url, options = {}) {
  const timeoutMs = options.timeoutMs || 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    return { response, text, data };
  } finally {
    clearTimeout(timer);
  }
}

async function fetchSessionIdentity(cookieString, cookieMap) {
  const headers = buildCreatorHeaders(cookieString, cookieMap);
  const params = new URLSearchParams({
    aid: "2906",
    app_name: "aweme_creator_platform",
    device_platform: "web",
    referer: "",
    user_agent: USER_AGENT,
    cookie_enabled: "true",
    screen_width: "1280",
    screen_height: "720",
    browser_language: "en-US@posix",
    browser_platform: "Linux x86_64",
    browser_name: "Mozilla",
    browser_version: USER_AGENT,
    browser_online: "true",
    timezone_name: "Asia/Shanghai",
  });
  const { response, data, text } = await fetchJson(
    `https://creator.douyin.com/aweme/v1/creator/im/user_token/?${params.toString()}`,
    { headers },
  );
  if (!response.ok || data?.status_code !== 0 || !data?.user_id) {
    throw new ProtocolError("Failed to resolve creator IM session identity", {
      status: response.status,
      body: text,
    });
  }
  return {
    userId: String(data.user_id),
    sessionToken: String(data.token || ""),
  };
}

async function fetchIdentitySecurityToken(cookieString, cookieMap) {
  const headers = buildCreatorHeaders(cookieString, cookieMap);
  const params = new URLSearchParams({
    scene: "im_send_msg",
    auto_retry_req: "0",
    skip_verify: "0",
    identity_token_force_get_tag: "0",
    passport_jssdk_version: "5.1.4",
    passport_jssdk_type: "lite",
    is_from_ttaccountsdk: "1",
    aid: "2906",
    language: "zh",
    account_app_language: "en-US",
    id_token_version: "2.1.5",
  });
  const { response, data, text } = await fetchJson(
    `https://creator.douyin.com/passport/safe/get_identity_security_token/?${params.toString()}`,
    { headers },
  );
  if (!response.ok || data?.message !== "success" || !data?.data?.identity_security_token) {
    throw new ProtocolError("Failed to resolve identity security token", {
      status: response.status,
      body: text,
    });
  }
  return {
    identitySecurityHeader: JSON.stringify({ token: data.data.identity_security_token }),
    realDeviceId: String(data.data.device_id || ""),
  };
}

async function fetchProfileNickname(cookieString, secUid) {
  const url =
    "https://www.douyin.com/aweme/v1/web/user/profile/other/?" +
    new URLSearchParams({ sec_user_id: secUid }).toString();
  const { response, data, text } = await fetchJson(url, {
    headers: {
      "User-Agent": USER_AGENT,
      Referer: `https://www.douyin.com/user/${secUid}`,
      Cookie: cookieString,
      Accept: "application/json, text/javascript",
    },
  });
  if (!response.ok || data?.status_code !== 0) {
    return "";
  }
  return normalizeNickname(data?.user?.nickname);
}

function stringifyMaybeLong(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "bigint") {
    return String(value);
  }
  if (typeof value.toString === "function" && value.toString !== Object.prototype.toString) {
    return value.toString();
  }
  return String(value);
}

function selectPeerParticipant(conversation, selfUserId) {
  const participants = conversation?.firstPageParticipant?.participants || [];
  for (const participant of participants) {
    const currentUserId = stringifyMaybeLong(participant?.user_id);
    if (currentUserId && currentUserId !== selfUserId) {
      return participant;
    }
  }
  return null;
}

async function createProtocolClient({ bundleDir, cookieString, cookieMap, userId }) {
  const requireModule = createWebpackRequire(bundleDir, cookieString);
  const sdk = requireModule(61724);
  const { BytedIM } = requireModule(26440);

  class AdditionalParamsPlugin extends sdk.BasePlugin {
    install() {}

    async sendPacket(packet) {
      packet.device_id = 0;
      packet.device_platform = "douyin_creator";
      packet.headers = {
        ...(packet.headers || {}),
        aid_new: 2906,
        app_name: "douyin_creator",
      };
      return packet;
    }
  }

  class NodeHttpClient extends sdk.IMHttpClient {
    async send(url, method, body) {
      const fullUrl = /^https?:/i.test(url)
        ? url
        : `${String(this.option.apiUrl).replace(/\/$/, "")}/${String(url).replace(/^\//, "")}`;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(fullUrl, {
          method,
          headers: this.headers,
          body: body ? Buffer.from(body) : undefined,
          signal: controller.signal,
        });
        return await response.arrayBuffer();
      } finally {
        clearTimeout(timer);
      }
    }

    sendByBeacon() {
      return false;
    }
  }

  const client = new BytedIM(
    {
      appId: 2906,
      fpId: 9,
      appKey: "e1bd35ec9db7b8d846de66ed140b1ad9",
      service: 5,
      apiUrl: "https://imapi.douyin.com",
      frontierUrl: "wss://frontier-im.douyin.com/ws/v2",
      inboxType: 1,
      token: "",
      userId,
      deviceId: userId,
      authType: sdk.im_proto.AuthType.SESSION_AUTH,
      devicePlatform: "douyin_pc",
      timeout: 20000,
      acceptIncorrectInboxType: true,
      biz: "douyin_creator",
      withCredentials: false,
      httpHeaders: buildImHeaders(cookieString),
      headers: {},
      webSocketLevel: sdk.WebSocketLevel.PushOnly,
      debug: false,
      http: (ctx) => new NodeHttpClient(ctx),
    },
    [AdditionalParamsPlugin],
  );

  const initResult = await client.init();
  if (initResult !== sdk.InitResult.Succeeded) {
    throw new ProtocolError("Protocol IM init did not succeed", { initResult });
  }

  return { client };
}

async function buildConversationCache({
  client,
  selfUserId,
  cookieString,
  existingCache = [],
  targetNames = [],
}) {
  const cachedBySecUid = new Map(
    (existingCache || []).filter((entry) => entry?.secUid).map((entry) => [entry.secUid, entry]),
  );
  const wantedTargets = new Set((targetNames || []).map(normalizeNickname).filter(Boolean));
  const matchedTargets = new Set();
  const conversations = await client.getConversationListOnline();
  const cacheEntries = [];

  for (const conversation of conversations) {
    if (conversation?.type !== 1) {
      continue;
    }

    const peer = selectPeerParticipant(conversation, selfUserId);
    if (!peer) {
      continue;
    }

    const peerUserId = stringifyMaybeLong(peer.user_id);
    const secUid = peer.sec_uid || "";
    if (!peerUserId || !secUid) {
      continue;
    }

    let nickname = normalizeNickname(cachedBySecUid.get(secUid)?.nickname);
    if (!nickname) {
      try {
        nickname = await fetchProfileNickname(cookieString, secUid);
      } catch {
        nickname = "";
      }
    }

    cacheEntries.push({
      nickname,
      peerUserId,
      secUid,
      conversationId: conversation.id,
      conversationShortId: conversation.shortId,
      updatedAt: stableNow(),
    });

    if (nickname && wantedTargets.has(nickname)) {
      matchedTargets.add(nickname);
      if (matchedTargets.size === wantedTargets.size) {
        break;
      }
    }
  }

  const deduped = new Map();
  for (const entry of existingCache || []) {
    if (!entry?.nickname || !entry?.secUid) {
      continue;
    }
    deduped.set(entry.secUid, entry);
  }
  for (const entry of cacheEntries) {
    if (!entry.nickname) {
      continue;
    }
    deduped.set(entry.secUid, entry);
  }
  return Array.from(deduped.values()).sort((left, right) =>
    left.nickname.localeCompare(right.nickname, "zh-CN"),
  );
}

function addUniqueLookupEntry(lookup, key, entry) {
  const normalized = String(key || "").trim();
  if (!normalized) {
    return;
  }
  if (lookup.has(normalized) && lookup.get(normalized) !== entry) {
    lookup.set(normalized, null);
    return;
  }
  lookup.set(normalized, entry);
}


export function buildTargetLookup(cacheEntries) {
  const byNickname = new Map();
  const bySecUid = new Map();
  const byPeerUserId = new Map();
  for (const entry of cacheEntries) {
    const key = normalizeNickname(entry.nickname);
    addUniqueLookupEntry(byNickname, key, entry);
    addUniqueLookupEntry(bySecUid, entry.secUid, entry);
    addUniqueLookupEntry(byPeerUserId, entry.peerUserId, entry);
  }
  return { byNickname, bySecUid, byPeerUserId };
}


export function resolveTargetMapping(lookup, target, identity = {}) {
  if (identity.ambiguous) {
    return { mapping: null, reason: "ambiguous_target" };
  }
  const secUid = String(identity.secUid || "").trim();
  if (secUid) {
    const mapping = lookup.bySecUid.get(secUid);
    return {
      mapping: mapping || null,
      reason: mapping ? "stable_sec_uid" : "stable_identity_not_found",
    };
  }
  const peerUserId = String(identity.peerUserId || "").trim();
  if (peerUserId) {
    const mapping = lookup.byPeerUserId.get(peerUserId);
    return {
      mapping: mapping || null,
      reason: mapping ? "stable_peer_user_id" : "stable_identity_not_found",
    };
  }

  const nickname = normalizeNickname(target);
  const mapping = lookup.byNickname.get(nickname);
  if (mapping) {
    return { mapping, reason: "unique_nickname" };
  }
  return {
    mapping: null,
    reason: lookup.byNickname.has(nickname)
      ? "ambiguous_target"
      : "conversation_not_found",
  };
}

async function sendMessages({
  client,
  cacheEntries,
  messagesByTarget,
  targetIdentities,
  dryRun,
  cookieString,
  cookieMap,
  sendStrategy,
}) {
  if (!dryRun) {
    const identity = await fetchIdentitySecurityToken(cookieString, cookieMap);
    client.updateSendMessageHeaders({
      identity_security_token: identity.identitySecurityHeader,
      identity_security_device_id: identity.realDeviceId,
      identity_security_aid: "2906",
    });
  }

  const lookup = buildTargetLookup(cacheEntries);
  const resolved = [];
  const unresolved = [];
  const sent = [];
  const normalizedStrategy = normalizeSendStrategy(sendStrategy);

  for (const [target, message] of Object.entries(messagesByTarget)) {
    const { mapping, reason } = resolveTargetMapping(
      lookup,
      target,
      targetIdentities?.[target] || {},
    );
    if (!mapping) {
      unresolved.push({ target, reason });
      continue;
    }

    const conversation = client.getConversation({ conversationId: mapping.conversationId });
    if (!conversation) {
      unresolved.push({ target, reason: "conversation_not_loaded", mapping });
      continue;
    }

    resolved.push({
      target,
      nickname: mapping.nickname,
      peerUserId: mapping.peerUserId,
      conversationId: mapping.conversationId,
      conversationShortId: mapping.conversationShortId,
    });

    let delayBeforeSendSeconds = 0;
    if (!dryRun && sent.length > 0 && normalizedStrategy.messageIntervalSecondsMax > 0) {
      delayBeforeSendSeconds = randomBetweenInclusive(
        normalizedStrategy.messageIntervalSecondsMin,
        normalizedStrategy.messageIntervalSecondsMax,
      );
      if (delayBeforeSendSeconds > 0) {
        await sleep(delayBeforeSendSeconds * 1000);
      }
    }

    const payload = JSON.stringify({ text: message, aweType: 774 });
    const messageObject = await client.createMessage({
      type: 7,
      content: payload,
      conversation,
      insert: false,
    });

    if (dryRun) {
      sent.push({
        target,
        dryRun: true,
        message,
        payload,
        conversationId: mapping.conversationId,
        delayBeforeSendSeconds,
      });
      continue;
    }

    const sendResult = await client.sendMessage({ message: messageObject });
    const statusCode = sendResult?.statusCode ?? null;
    sent.push({
      target,
      dryRun: false,
      message,
      success: isSuccessfulSendResult(sendResult),
      statusCode,
      statusName: sendMessageStatusName(statusCode),
      statusMsg: sendResult?.statusMsg ?? "",
      sendResultSummary: publicSendResultSummary(sendResult),
      conversationId: mapping.conversationId,
      delayBeforeSendSeconds,
      sentAt: stableNow(),
    });
  }

  return { resolved, unresolved, sent };
}

async function main() {
  const payload = await readStdinJson();
  const repoRoot = payload.repoRoot || process.cwd();
  const bundleDir = path.join(repoRoot, ".im_sdk_cache");
  await ensureBundles(bundleDir);

  const account = payload.account || {};
  const cookieString = toCookieString(account.cookies);
  const cookieMap = extractCookieMap(account.cookies);
  const { userId } = await fetchSessionIdentity(cookieString, cookieMap);
  const { client } = await createProtocolClient({
    bundleDir,
    cookieString,
    cookieMap,
    userId,
  });

  const cacheEntries = await buildConversationCache({
    client,
    selfUserId: userId,
    cookieString,
    existingCache: account.protocol_targets_cache || [],
    targetNames: Object.keys(payload.messagesByTarget || {}),
  });
  const execution = await sendMessages({
    client,
    cacheEntries,
    messagesByTarget: payload.messagesByTarget || {},
    targetIdentities: payload.targetIdentities || {},
    dryRun: Boolean(payload.dryRun),
    cookieString,
    cookieMap,
    sendStrategy: payload.sendStrategy || {},
  });

  try {
    console.log(
      JSON.stringify(
        {
          ok: true,
          username: account.username || "",
          userId,
          dryRun: Boolean(payload.dryRun),
          protocol_targets_cache: cacheEntries,
          ...execution,
        },
        null,
        2,
      ),
    );
  } finally {
    await client.dispose();
  }
}

const isMain =
  Boolean(process.argv[1]) &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;

if (isMain) {
  main().catch((error) => {
    console.log(
      JSON.stringify(
        {
          ok: false,
          error: error?.message || String(error),
          details: error?.details || {},
          stack: error?.stack || "",
        },
        null,
        2,
      ),
    );
    process.exit(1);
  });
}
