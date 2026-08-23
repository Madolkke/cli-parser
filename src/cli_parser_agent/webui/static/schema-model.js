(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.SchemaModel = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";
  const TYPES = ["string", "integer", "number", "boolean", "object", "array"];
  const FIELD_RE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;
  const COMMON = ["title", "description", "enum"];
  const TYPE_KEYS = {
    string: ["minLength", "maxLength"],
    integer: ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"],
    number: ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"],
    boolean: [],
    object: ["properties", "required", "additionalProperties"],
    array: ["items", "minItems", "maxItems"],
  };
  function createNode(type) {
    if (type === "object") return { type, properties: {}, additionalProperties: false };
    if (type === "array") return { type, items: createNode("string") };
    return { type };
  }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function changeType(node, type) {
    if (!TYPES.includes(type)) throw new Error("不支持的类型");
    const next = createNode(type);
    for (const key of COMMON) if (Object.prototype.hasOwnProperty.call(node, key)) next[key] = clone(node[key]);
    return next;
  }
  function normalise(schema) {
    const rootNode = normaliseNode(schema);
    if (rootNode.type !== "object") throw new Error("根节点必须是 object");
    if (schema.$schema) rootNode.$schema = schema.$schema;
    return rootNode;
  }
  function normaliseNode(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) || !TYPES.includes(value.type)) throw new Error("每个节点都必须声明受支持的 type");
    const node = createNode(value.type);
    for (const key of COMMON) if (Object.prototype.hasOwnProperty.call(value, key)) node[key] = clone(value[key]);
    for (const key of TYPE_KEYS[value.type]) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
      if (key === "properties") {
        node.properties = {};
        for (const [name, child] of Object.entries(value.properties || {})) node.properties[name] = normaliseNode(child);
      } else if (key === "items") node.items = normaliseNode(value.items);
      else if (key === "required") { if (Array.isArray(value.required) && value.required.length) node.required = [...value.required]; }
      else if (key === "additionalProperties") node.additionalProperties = false;
      else node[key] = value[key];
    }
    return node;
  }
  function validate(schema) {
    const errors = [];
    if (!schema || schema.type !== "object") errors.push({ path: "/type", message: "根节点必须是 object" });
    walk(schema, "", errors);
    return errors;
  }
  function walk(node, path, errors) {
    if (!node || typeof node !== "object" || !TYPES.includes(node.type)) { errors.push({ path: path || "/", message: "节点类型无效" }); return; }
    if (node.enum !== undefined) {
      if (!Array.isArray(node.enum) || node.enum.length === 0) errors.push({ path: path + "/enum", message: "枚举至少需要一个值" });
      if (["object", "array"].includes(node.type)) errors.push({ path: path + "/enum", message: "对象和数组不能设置枚举" });
    }
    if (node.type === "object") {
      const names = Object.keys(node.properties || {});
      for (const name of names) {
        const childPath = path + "/properties/" + escapePointer(name);
        if (!FIELD_RE.test(name) || name.length > 120) errors.push({ path: childPath, message: "字段名必须是 ASCII snake_case，且不超过 120 个字符" });
        walk(node.properties[name], childPath, errors);
      }
      for (const name of node.required || []) if (!names.includes(name)) errors.push({ path: path + "/required", message: "必填字段 " + name + " 不存在" });
    }
    if (node.type === "array") walk(node.items, path + "/items", errors);
    for (const [min, max] of [["minLength", "maxLength"], ["minItems", "maxItems"], ["minimum", "maximum"]]) {
      if (node[min] !== undefined && node[max] !== undefined && Number(node[min]) > Number(node[max])) errors.push({ path: path || "/", message: min + " 不能大于 " + max });
    }
  }
  function escapePointer(value) { return value.replace(/~/g, "~0").replace(/\//g, "~1"); }
  function utf8Bytes(value) { return new TextEncoder().encode(value).length; }
  function validateInputs(outputs, maxBytes) {
    return outputs.map((value, index) => {
      if (!value.trim()) return { index, message: "输入 " + (index + 1) + " 不能为空" };
      if (utf8Bytes(value) > maxBytes) return { index, message: "输入 " + (index + 1) + " 超过 1 MiB" };
      return null;
    }).filter(Boolean);
  }
  return { TYPES, FIELD_RE, TYPE_KEYS, createNode, changeType, clone, normalise, validate, validateInputs, utf8Bytes };
});
