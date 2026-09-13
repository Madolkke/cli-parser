"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const model = require("../../src/cli_parser_agent/webui/static/schema-model.js");

test("normalise preserves the complete supported nested subset", () => {
  const schema = {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    title: "Inventory",
    properties: {
      hostname: { type: "string", minLength: 1, maxLength: 64, enum: ["r1", "r2"] },
      interfaces: {
        type: "array", minItems: 1, maxItems: 10,
        items: {
          type: "object",
          properties: { name: { type: "string" }, mtu: { type: "integer", minimum: 0 } },
          required: ["name"], additionalProperties: false,
        },
      },
    },
    required: ["hostname"], additionalProperties: false,
  };
  assert.deepEqual(model.normalise(schema), schema);
  assert.deepEqual(model.validate(schema), []);
});

test("changeType keeps common metadata and removes incompatible structure", () => {
  const source = { type: "object", title: "Node", properties: { name: { type: "string" } }, additionalProperties: false };
  assert.deepEqual(model.changeType(source, "array"), {
    type: "array", title: "Node", items: { type: "string" },
  });
});

test("renameProperty rejects duplicates without changing the source", () => {
  const properties = { hostname: { type: "string" }, status: { type: "string" } };
  const result = model.renameProperty(properties, "status", "hostname");
  assert.deepEqual(result, { ok: false, reason: "duplicate" });
  assert.deepEqual(properties, { hostname: { type: "string" }, status: { type: "string" } });
});

test("renameProperty returns a new property map for a valid rename", () => {
  const properties = { hostname: { type: "string" }, status: { type: "string" } };
  const result = model.renameProperty(properties, "status", "state");
  assert.equal(result.ok, true);
  assert.deepEqual(result.properties, { hostname: { type: "string" }, state: { type: "string" } });
  assert.ok(Object.prototype.hasOwnProperty.call(properties, "status"));
});

test("validation reports field names, required paths, and invalid ranges", () => {
  const schema = {
    type: "object",
    properties: { BadName: { type: "string", minLength: 5, maxLength: 2 } },
    required: ["missing"], additionalProperties: false,
  };
  const messages = model.validate(schema).map((item) => item.message);
  assert.ok(messages.some((message) => message.includes("snake_case")));
  assert.ok(messages.some((message) => message.includes("必填字段")));
  assert.ok(messages.some((message) => message.includes("minLength")));
});

test("input validation uses UTF-8 bytes and preserves input indexes", () => {
  assert.deepEqual(model.validateInputs(["ok", ""], 4), [{ index: 1, message: "输入 2 不能为空" }]);
  assert.deepEqual(model.validateInputs(["你好"], 5), [{ index: 0, message: "输入 1 超过 1 MiB" }]);
  assert.equal(model.utf8Bytes("你好"), 6);
});


test("keyword, soft keyword and TTP reserved name rules preserve editing data", () => {
  const schema = (name, child = { type: "string" }) => ({ type: "object", properties: { [name]: child }, additionalProperties: false });
  for (const name of model.PYTHON_KEYWORDS) {
    const input = schema(name);
    const before = structuredClone(input);
    assert.ok(model.validate(input).length > 0, name);
    assert.deepEqual(input, before);
    assert.deepEqual(model.normalise(input), before);
  }
  for (const name of ["match", "case", "type", "id", "format", "device_class", "ipv4_address", "a".repeat(120)]) {
    assert.deepEqual(model.validate(schema(name)), [], name);
  }
  for (const name of ["字段", "1_port", "_port", "port_", "port__name", "_", "Port", "port-name", "port\n", "class\n", "a".repeat(121)]) {
    assert.ok(model.validate(schema(name)).length > 0, name);
  }
  for (const type of ["string", "integer", "number", "boolean"]) {
    assert.ok(model.validate(schema("ignore", { type })).some(e => e.message.includes("TTP")));
  }
  assert.deepEqual(model.validate(schema("ignore", schema("value"))), []);
  assert.deepEqual(model.validate(schema("ignore", { type: "array", items: { type: "string" } })), []);
  const nested = schema("items", { type: "array", items: schema("class") });
  assert.deepEqual(model.validate(nested).map(e => e.path), ["/properties/items/items/properties/class"]);
  assert.deepEqual(model.validate(schema("value", { type: "string", description: "class", enum: ["for"] })), []);
});
