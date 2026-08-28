"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const highlight = require("../../src/cli_parser_agent/webui/static/highlight.js");

function join(tokens) {
  return tokens.map((token) => token.value).join("");
}

test("tokenizes JSON keys, strings, numbers, and literals", () => {
  const tokens = highlight.tokenizeJson('{"host": "r1", "mtu": 1500, "up": true, "note": null}');
  assert.deepEqual(
    tokens.filter((token) => token.kind === "key").map((token) => token.value),
    ['"host"', '"mtu"', '"up"', '"note"'],
  );
  assert.deepEqual(
    tokens.filter((token) => token.kind === "string").map((token) => token.value),
    ['"r1"'],
  );
  assert.deepEqual(
    tokens.filter((token) => token.kind === "number").map((token) => token.value),
    ["1500"],
  );
  assert.deepEqual(
    tokens.filter((token) => token.kind === "literal").map((token) => token.value),
    ["true", "null"],
  );
  assert.equal(join(tokens), '{"host": "r1", "mtu": 1500, "up": true, "note": null}');
});

test("handles escaped characters inside strings without splitting", () => {
  const tokens = highlight.tokenizeJson('{"a": "x\\"y\\\\z", "b": "\\u00e9"}');
  assert.deepEqual(
    tokens.filter((token) => token.kind === "string").map((token) => token.value),
    ['"x\\"y\\\\z"', '"\\u00e9"'],
  );
});

test("emits truncated input as a plain tail instead of hanging", () => {
  const tokens = highlight.tokenizeJson('{"a": "unterminated');
  assert.equal(join(tokens), '{"a": "unterminated');
  assert.equal(tokens[tokens.length - 1].kind, "plain");
});

test("classifies a string followed by whitespace and colon as a key", () => {
  const tokens = highlight.tokenizeJson('{ "spaced key" : 1 }');
  const stringToken = tokens.find((token) => token.kind === "key" || token.kind === "string");
  assert.equal(stringToken.kind, "key");
  assert.equal(stringToken.value, '"spaced key"');
});

test("tokenizes TTP template tags and XML groups", () => {
  const source = '<group name="interfaces">\n{{ interface }} is up {{ mtu | ORPHRASE }}\n</group>';
  const tokens = highlight.tokenizeTtp(source);
  assert.deepEqual(
    tokens.filter((token) => token.kind === "xml-tag").map((token) => token.value),
    ['<group name="interfaces">', "</group>"],
  );
  assert.deepEqual(
    tokens.filter((token) => token.kind === "tag-name").map((token) => token.value),
    ["interface", "mtu"],
  );
  assert.equal(join(tokens), source);
});

test("keeps filter arguments inside the tag args token", () => {
  const tokens = highlight.tokenizeTtp('{{ ip | PHRASE("a b") }}');
  const names = tokens.filter((token) => token.kind === "tag-name").map((token) => token.value);
  const args = tokens.filter((token) => token.kind === "tag-args").map((token) => token.value).join("");
  assert.deepEqual(names, ["ip"]);
  assert.equal(args.trim(), '| PHRASE("a b")');
});

test("renders literal text without braces as a single text token", () => {
  const tokens = highlight.tokenizeTtp("plain line\nanother line");
  assert.deepEqual(tokens, [{ kind: "text", value: "plain line\nanother line" }]);
});

test("tokenSpans falls back to plain for unknown languages", () => {
  assert.deepEqual(highlight.tokenSpans("hello", "text"), [{ kind: "plain", value: "hello" }]);
});
