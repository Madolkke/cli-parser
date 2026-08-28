(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.Highlight = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  /* Zero-dependency syntax highlighting for the WebUI artifact viewers.
   * The tokenizers are pure string functions so Node unit tests can cover
   * them; DOM assembly only happens in the browser entry point. */

  const MAX_HIGHLIGHT_CHARS = 256 * 1024;

  const JSON_RULES = [
    { kind: "ws", re: /\s+/y },
    { kind: "string", re: /"(?:[^"\\\r\n]|\\(?:["\\\/bfnrt]|u[0-9a-fA-F]{4}))*"/y },
    { kind: "number", re: /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y },
    { kind: "literal", re: /(?:true|false|null)/y },
    { kind: "punct", re: /[{}\[\],:]/y },
  ];

  /* Classify strings: a string token directly followed (ignoring spaces) by
   * ":" is an object key.  Returns tokens as {kind, value} pairs. */
  function tokenizeJson(source) {
    const tokens = [];
    let offset = 0;
    while (offset < source.length) {
      let matched = false;
      for (const rule of JSON_RULES) {
        rule.re.lastIndex = offset;
        const match = rule.re.exec(source);
        if (match && match[0].length > 0) {
          tokens.push({ kind: rule.kind, value: match[0] });
          offset += match[0].length;
          matched = true;
          break;
        }
      }
      if (!matched) {
        // Unparseable tail (truncated stream, half-written string…): emit it
        // verbatim as plain text instead of looping forever.
        tokens.push({ kind: "plain", value: source.slice(offset) });
        break;
      }
    }
    for (let index = 0; index < tokens.length; index += 1) {
      if (tokens[index].kind !== "string") continue;
      let next = index + 1;
      while (next < tokens.length && tokens[next].kind === "ws") next += 1;
      if (tokens[next] && tokens[next].kind === "punct" && tokens[next].value === ":") {
        tokens[index].kind = "key";
      }
    }
    return tokens;
  }

  const TTP_TAG = /\{\{[^{}]*\}\}/y;
  const TTP_XML = /<\/?[A-Za-z_][\w.-]*(?:\s+[^<>{}]*?)?\/?>/y;

  function tokenizeTagBody(body) {
    /* "{{ name | filter(...) }}" → delimiter, name, remainder. */
    const tokens = [{ kind: "tag-delim", value: "{{" }];
    const rest = body.slice(2, -2);
    const match = /^(\s*)([A-Za-z_][\w.]*)/.exec(rest);
    if (match) {
      if (match[1]) tokens.push({ kind: "tag-args", value: match[1] });
      tokens.push({ kind: "tag-name", value: match[2] });
      tokens.push({ kind: "tag-args", value: rest.slice(match[0].length) });
    } else {
      tokens.push({ kind: "tag-args", value: rest });
    }
    tokens.push({ kind: "tag-delim", value: "}}" });
    return tokens;
  }

  function tokenizeTtp(source) {
    const tokens = [];
    let offset = 0;
    while (offset < source.length) {
      const from = offset;
      TTP_TAG.lastIndex = offset;
      const tag = TTP_TAG.exec(source);
      if (tag && tag.index === offset) {
        tokens.push(...tokenizeTagBody(tag[0]));
        offset += tag[0].length;
        continue;
      }
      TTP_XML.lastIndex = offset;
      const xml = TTP_XML.exec(source);
      if (xml && xml.index === offset) {
        tokens.push({ kind: "xml-tag", value: xml[0] });
        offset += xml[0].length;
        continue;
      }
      // Plain literal text up to the next "{{" or XML-ish tag start.
      const nextTag = /\{\{|<\/?[A-Za-z_]/.exec(source.slice(offset));
      const length = nextTag ? nextTag.index : source.length - offset;
      tokens.push({ kind: "text", value: source.slice(offset, offset + length) });
      offset += length;
      if (offset === from) {
        tokens.push({ kind: "text", value: source.slice(offset) });
        break;
      }
    }
    return tokens;
  }

  function tokenSpans(source, language) {
    if (language === "ttp") return tokenizeTtp(source);
    if (language === "json") return tokenizeJson(source);
    return [{ kind: "plain", value: source }];
  }

  function fillCodeElement(pre, source, language) {
    pre.replaceChildren();
    pre.classList.toggle("is-highlighted", source.length <= MAX_HIGHLIGHT_CHARS);
    if (source.length > MAX_HIGHLIGHT_CHARS) {
      pre.textContent = source;
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const token of tokenSpans(source, language)) {
      if (token.kind === "plain" || token.kind === "text" || token.kind === "ws") {
        fragment.append(token.value);
        continue;
      }
      const span = document.createElement("span");
      span.className = "hl-" + token.kind;
      span.textContent = token.value;
      fragment.append(span);
    }
    pre.append(fragment);
  }

  return {
    MAX_HIGHLIGHT_CHARS,
    tokenizeJson,
    tokenizeTtp,
    tokenSpans,
    fillCodeElement,
  };
});
