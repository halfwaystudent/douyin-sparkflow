import assert from "node:assert/strict";
import test from "node:test";

import {
  SDK_BUNDLES,
  buildTargetLookup,
  isSuccessfulSendResult,
  resolveTargetMapping,
  sha256Hex,
  verifyBundleBytes,
} from "../core/protocol_sender.mjs";


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


test("bundle verification rejects a mismatched sha256", () => {
  const body = Buffer.from("not-the-expected-body", "utf8");

  assert.equal(sha256Hex(body).length, 64);
  assert.throws(
    () => verifyBundleBytes(body, "0".repeat(64)),
    /integrity/i,
  );
});
