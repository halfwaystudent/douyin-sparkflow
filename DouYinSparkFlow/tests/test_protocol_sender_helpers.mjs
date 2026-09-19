import assert from "node:assert/strict";
import crypto from "node:crypto";
import test from "node:test";

import {
  SDK_BUNDLES,
  buildTargetLookup,
  isSuccessfulSendResult,
  mergeConversationCache,
  resolveTargetMapping,
  sendMessages,
  sha256Hex,
  verifyBundleBytes,
} from "../core/protocol_sender.mjs";
import { casefold } from "../core/unicode_casefold.mjs";


test("every remote bundle has a pinned sha256", () => {
  assert.ok(SDK_BUNDLES.length > 0);
  for (const bundle of SDK_BUNDLES) {
    assert.match(bundle.url, /^https:\/\//);
    assert.match(bundle.sha256, /^[0-9a-f]{64}$/);
  }
});


test("only statusCode zero is a successful protocol send", () => {
  assert.equal(isSuccessfulSendResult({ success: true, statusCode: 0 }), true);
  assert.equal(isSuccessfulSendResult({ success: true, statusCode: 1 }), false);
  assert.equal(isSuccessfulSendResult({ success: false, statusCode: 0 }), false);
  assert.equal(isSuccessfulSendResult({ success: true }), false);
});


test("stable identity resolves the matching conversation", () => {
  const lookup = buildTargetLookup([
    {
      nickname: "Alice",
      secUid: "sec-1",
      peerUserId: "1001",
      conversationId: "conversation-1",
    },
    {
      nickname: "Alice",
      secUid: "sec-2",
      peerUserId: "1002",
      conversationId: "conversation-2",
    },
  ]);

  const resolved = resolveTargetMapping(lookup, "Alice", { secUid: "sec-2" });

  assert.equal(resolved.mapping.conversationId, "conversation-2");
});


test("ambiguous nickname without stable identity is rejected", () => {
  const lookup = buildTargetLookup([
    { nickname: "Alice", secUid: "sec-1", peerUserId: "1001" },
    { nickname: "Alice", secUid: "sec-2", peerUserId: "1002" },
  ]);

  const resolved = resolveTargetMapping(lookup, "Alice", {});

  assert.equal(resolved.mapping, null);
  assert.equal(resolved.reason, "ambiguous_target");
});

test("nickname lookup normalizes case and width", () => {
  const lookup = buildTargetLookup([
    {
      nickname: "Ａlice",
      secUid: "sec-1",
      peerUserId: "1001",
      conversationId: "conversation-1",
    },
  ]);

  const resolved = resolveTargetMapping(lookup, "alice", {});

  assert.equal(resolved.mapping.conversationId, "conversation-1");
  assert.equal(resolved.reason, "unique_nickname");
});

test("nickname lookup uses Unicode case folding", () => {
  const lookup = buildTargetLookup([
    {
      nickname: "Straße",
      secUid: "sec-1",
      peerUserId: "1001",
      conversationId: "conversation-1",
    },
  ]);

  const resolved = resolveTargetMapping(lookup, "STRASSE", {});

  assert.equal(resolved.mapping.conversationId, "conversation-1");
  assert.equal(resolved.reason, "unique_nickname");

  const combiningLookup = buildTargetLookup([
    {
      nickname: "a\u0345",
      conversationId: "conversation-2",
    },
    {
      nickname: "\u1fd3",
      conversationId: "conversation-3",
    },
  ]);
  assert.equal(
    resolveTargetMapping(combiningLookup, "a\u03b9", {}).mapping
      .conversationId,
    "conversation-2",
  );
  assert.equal(
    resolveTargetMapping(
      combiningLookup,
      "\u03b9\u0308\u0301",
      {},
    ).mapping.conversationId,
    "conversation-3",
  );

  const fullFoldLookup = buildTargetLookup([
    { nickname: "\u01f0", conversationId: "conversation-4" },
    { nickname: "\u1fb3", conversationId: "conversation-5" },
  ]);
  assert.equal(
    resolveTargetMapping(
      fullFoldLookup,
      "j\u030c",
      {},
    ).mapping.conversationId,
    "conversation-4",
  );
  assert.equal(
    resolveTargetMapping(
      fullFoldLookup,
      "\u03b1\u03b9",
      {},
    ).mapping.conversationId,
    "conversation-5",
  );
});

test("casefold map preserves Python Unicode 16 behavior", () => {
  assert.equal(casefold("\u01f0"), "j\u030c");
  assert.equal(casefold("\u1fb3"), "\u03b1\u03b9");
  assert.equal(casefold("\ua7ce"), "\ua7ce");
  assert.equal(casefold("\ua7f1"), "\ua7f1");
  assert.equal(casefold("\u{16ea0}"), "\u{16ea0}");
  assert.equal(casefold("A\u030a"), "\u00e5");
  assert.equal(casefold("\u1ae9\u0734"), "\u1ae9\u0734");
  assert.equal(casefold("\u0734\u1ae9"), "\u0734\u1ae9");
  assert.equal(casefold("\u1f82\u0300"), "\u1f02\u03b9\u0300");
});

test("casefold matches the Python Unicode 16 scalar checksum", () => {
  const hash = crypto.createHash("sha256");
  for (let cp = 0; cp <= 0x10ffff; cp += 1) {
    if (cp >= 0xd800 && cp <= 0xdfff) continue;
    hash.update(casefold(String.fromCodePoint(cp)), "utf8");
  }

  assert.equal(
    hash.digest("hex"),
    "dd53abf13ed8264c625c02b09ecbd4d7dd0f1b8d0abeddc910ac95d574d4a692",
  );
});

test("casefold matches the Python Unicode 16 combining checksum", () => {
  const hash = crypto.createHash("sha256");
  for (let cp = 0; cp <= 0x10ffff; cp += 1) {
    if (cp >= 0xd800 && cp <= 0xdfff) continue;
    hash.update(casefold(String.fromCodePoint(cp) + "\u0300"), "utf8");
  }

  assert.equal(
    hash.digest("hex"),
    "e32c2f6781017ddaa3e0218ecaf41ec2f34b7be9d62150f03220b948124548f5",
  );
});


test("peerUserId-only cache entries survive deduplication", () => {
  const merged = mergeConversationCache([], [
    {
      nickname: "PeerOnly",
      peerUserId: "1001",
      secUid: "",
      conversationId: "conversation-1",
    },
  ]);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].peerUserId, "1001");
});


test("peerUserId-only cache upgrades when secUid appears", () => {
  const merged = mergeConversationCache(
    [
      {
        nickname: "Alice",
        peerUserId: "1001",
        secUid: "",
        conversationId: "conversation-1",
      },
    ],
    [
      {
        nickname: "Alice",
        peerUserId: "1001",
        secUid: "sec-1",
        conversationId: "conversation-1",
      },
    ],
  );

  assert.equal(merged.length, 1);
  assert.equal(merged[0].secUid, "sec-1");
  const lookup = buildTargetLookup(merged);
  const resolved = resolveTargetMapping(lookup, "Alice", {});
  assert.equal(resolved.mapping.conversationId, "conversation-1");
  assert.notEqual(resolved.reason, "ambiguous_target");
});

test("cache merge does not clear non-empty stable identity fields", () => {
  const merged = mergeConversationCache(
    [
      {
        nickname: "Alice",
        peerUserId: "1001",
        secUid: "sec-1",
        conversationId: "conversation-old",
      },
    ],
    [
      {
        nickname: "Alice",
        peerUserId: "",
        secUid: "sec-1",
        conversationId: "conversation-new",
      },
    ],
  );

  assert.equal(merged.length, 1);
  assert.equal(merged[0].peerUserId, "1001");
  assert.equal(merged[0].secUid, "sec-1");
  assert.equal(merged[0].conversationId, "conversation-new");
});


test("linked cache entry reconciles already split peer and sec rows", () => {
  const merged = mergeConversationCache(
    [
      {
        nickname: "Alice",
        peerUserId: "1001",
        secUid: "",
        conversationId: "conversation-peer",
      },
      {
        nickname: "Alice",
        peerUserId: "",
        secUid: "sec-1",
        conversationId: "conversation-sec",
      },
    ],
    [
      {
        nickname: "Alice",
        peerUserId: "1001",
        secUid: "sec-1",
        conversationId: "conversation-merged",
      },
    ],
  );

  assert.equal(merged.length, 1);
  assert.equal(merged[0].peerUserId, "1001");
  assert.equal(merged[0].secUid, "sec-1");
  const lookup = buildTargetLookup(merged);
  const resolved = resolveTargetMapping(lookup, "Alice", {});
  assert.equal(resolved.mapping.conversationId, "conversation-merged");
  assert.notEqual(resolved.reason, "ambiguous_target");
});


test("peerUserId is used when secUid is unavailable", () => {
  const lookup = buildTargetLookup([
    {
      nickname: "PeerOnly",
      peerUserId: "1001",
      secUid: "",
      conversationId: "conversation-1",
    },
  ]);

  const resolved = resolveTargetMapping(lookup, "PeerOnly", {
    peerUserId: "1001",
  });

  assert.equal(resolved.mapping.conversationId, "conversation-1");
});


test("bundle verification rejects a mismatched sha256", () => {
  const body = Buffer.from("not-the-expected-body", "utf8");

  assert.equal(sha256Hex(body).length, 64);
  assert.throws(
    () => verifyBundleBytes(body, "0".repeat(64)),
    /integrity/i,
  );
});

test("a target exception preserves earlier successful sends", async () => {
  const client = {
    updateSendMessageHeaders() {},
    getConversation({ conversationId }) {
      return { conversationId };
    },
    async createMessage({ content }) {
      return { text: JSON.parse(content).text };
    },
    async sendMessage({ message }) {
      if (message.text === "second") {
        throw new Error("second target failed");
      }
      return { success: true, statusCode: 0, statusMsg: "ok" };
    },
  };
  const cacheEntries = [
    {
      nickname: "Alice",
      secUid: "sec-1",
      conversationId: "conversation-1",
    },
    {
      nickname: "Bob",
      secUid: "sec-2",
      conversationId: "conversation-2",
    },
  ];
  const targetIdentities = {
    Alice: { secUid: "sec-1" },
    Bob: { secUid: "sec-2" },
  };

  const result = await sendMessages({
    client,
    cacheEntries,
    messagesByTarget: { Alice: "first", Bob: "second" },
    targetIdentities,
    dryRun: false,
    cookieString: "",
    cookieMap: {},
    sendStrategy: {
      messageIntervalSecondsMin: 0,
      messageIntervalSecondsMax: 0,
    },
    identityOverride: {
      identitySecurityHeader: "token",
      realDeviceId: "device",
    },
  });

  assert.equal(result.sent.length, 2);
  assert.equal(result.sent[0].success, true);
  assert.equal(result.sent[1].success, false);
  assert.equal(result.sent[1].statusName, "exception");
});

test("a conversation lookup exception preserves earlier sends", async () => {
  const client = {
    updateSendMessageHeaders() {},
    getConversation({ conversationId }) {
      if (conversationId === "conversation-2") {
        throw new Error("conversation lookup exploded");
      }
      return { conversationId };
    },
    async createMessage({ content }) {
      return { text: JSON.parse(content).text };
    },
    async sendMessage() {
      return { success: true, statusCode: 0, statusMsg: "ok" };
    },
  };
  const cacheEntries = [
    {
      nickname: "Alice",
      secUid: "sec-1",
      conversationId: "conversation-1",
    },
    {
      nickname: "Bob",
      secUid: "sec-2",
      conversationId: "conversation-2",
    },
  ];

  const result = await sendMessages({
    client,
    cacheEntries,
    messagesByTarget: { Alice: "first", Bob: "second" },
    targetIdentities: {
      Alice: { secUid: "sec-1" },
      Bob: { secUid: "sec-2" },
    },
    dryRun: false,
    cookieString: "",
    cookieMap: {},
    sendStrategy: {
      messageIntervalSecondsMin: 0,
      messageIntervalSecondsMax: 0,
    },
    identityOverride: {
      identitySecurityHeader: "token",
      realDeviceId: "device",
    },
  });

  assert.equal(result.sent.length, 2);
  assert.equal(result.sent[0].success, true);
  assert.equal(result.sent[1].success, false);
  assert.equal(result.sent[1].statusName, "exception");
});

test("group chat entries survive deduplication and merge by conversationId", () => {
  const merged = mergeConversationCache(
    [
      {
        nickname: "测试群聊",
        conversationId: "group-1001",
        conversationShortId: "1001",
        isGroup: true,
        conversationType: 2,
      },
    ],
    [
      {
        nickname: "测试群聊(新名)",
        conversationId: "group-1001",
        conversationShortId: "1001",
        isGroup: true,
        conversationType: 2,
      },
      {
        nickname: "二群",
        conversationId: "group-1002",
        conversationShortId: "1002",
        isGroup: true,
        conversationType: 2,
      },
    ],
  );

  assert.equal(merged.length, 2);
  const group1 = merged.find((entry) => entry.conversationId === "group-1001");
  assert.ok(group1);
  assert.equal(group1.nickname, "测试群聊(新名)");
  assert.equal(group1.isGroup, true);
  assert.equal(group1.conversationType, 2);

  const group2 = merged.find((entry) => entry.conversationId === "group-1002");
  assert.ok(group2);
  assert.equal(group2.nickname, "二群");
});

test("group chat resolves by nickname and stable conversationId", () => {
  const lookup = buildTargetLookup([
    {
      nickname: "家庭群",
      conversationId: "conv-family",
      isGroup: true,
    },
    {
      nickname: "朋友群",
      conversationId: "conv-friends",
      isGroup: true,
    },
  ]);

  // Resolve by nickname
  const resolvedByNick = resolveTargetMapping(lookup, "家庭群", {});
  assert.ok(resolvedByNick.mapping);
  assert.equal(resolvedByNick.mapping.conversationId, "conv-family");
  assert.equal(resolvedByNick.reason, "unique_nickname");

  // Resolve by conversationId
  const resolvedById = resolveTargetMapping(lookup, "改名后的群", {
    conversationId: "conv-friends",
  });
  assert.ok(resolvedById.mapping);
  assert.equal(resolvedById.mapping.conversationId, "conv-friends");
  assert.equal(resolvedById.reason, "stable_conversation_id");
});

test("sendMessages successfully dispatches to group conversations", async () => {
  const sentMessages = [];
  const client = {
    updateSendMessageHeaders() {},
    getConversation({ conversationId }) {
      return { conversationId, type: 2 };
    },
    async createMessage({ content, conversation }) {
      return { text: JSON.parse(content).text, conversation };
    },
    async sendMessage({ message }) {
      sentMessages.push(message);
      return { success: true, statusCode: 0, statusMsg: "ok" };
    },
  };
  const cacheEntries = [
    {
      nickname: "火花互续群",
      conversationId: "conv-spark-group",
      isGroup: true,
    },
  ];

  const result = await sendMessages({
    client,
    cacheEntries,
    messagesByTarget: { 火花互续群: "✨今日群聊火花+1" },
    targetIdentities: { 火花互续群: { conversationId: "conv-spark-group" } },
    dryRun: false,
    cookieString: "",
    cookieMap: {},
    sendStrategy: {
      messageIntervalSecondsMin: 0,
      messageIntervalSecondsMax: 0,
    },
    identityOverride: {
      identitySecurityHeader: "token",
      realDeviceId: "device",
    },
  });

  assert.equal(result.sent.length, 1);
  assert.equal(result.sent[0].success, true);
  assert.equal(result.sent[0].target, "火花互续群");
  assert.equal(result.sent[0].conversationId, "conv-spark-group");
  assert.equal(sentMessages.length, 1);
  assert.equal(sentMessages[0].text, "✨今日群聊火花+1");
});

