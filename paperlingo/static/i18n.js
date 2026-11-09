"use strict";

// UI-only localization. Research text and submitted values are never rewritten.
const I18n = (() => {
  const english = window.PaperLingoEnglish;
  const storageKey = "paperlingo_language";
  const valid = value => value === "ko" || value === "en";
  const preferred = () => (navigator.language || "ko").toLowerCase().startsWith("ko") ? "ko" : "en";
  let language = preferred();
  try { const saved = localStorage.getItem(storageKey); if (valid(saved)) language = saved; } catch { /* Storage is optional. */ }
  const texts = new Map(), attributes = new Map();
  let cleanupQueued = false;
  const message = (key, ...args) => ({ key, args, toString() { return resolve(this); } });
  const join = (values, separator = "") => ({ values, separator, toString() { return resolve(this); } });
  const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const patterns = Object.keys(english).filter(key => /\{\d+\}/.test(key) && /[가-힣]/.test(key))
    .sort((a, b) => b.replace(/\{\d+\}/g, "").length - a.replace(/\{\d+\}/g, "").length).map(key => {
    const parts = key.split(/(\{\d+\})/g), indices = [];
    const source = parts.map(part => /^\{\d+\}$/.test(part) ? (indices.push(Number(part.slice(1, -1))), "([\\s\\S]*?)") : escape(part)).join("");
    return { key, indices, regex: new RegExp(`^${source}$`) };
  });
  function translateServer(value) {
    if (language === "ko" || !/[가-힣]/.test(value)) return value;
    if (Object.hasOwn(english, value)) return english[value];
    for (const pattern of patterns) {
      const match = value.match(pattern.regex);
      if (match) {
        const args = []; pattern.indices.forEach((index, position) => { args[index] = match[position + 1]; });
        return format(english[pattern.key], args);
      }
    }
    // Validation errors may include a JSON path before a known server message.
    const prefix = value.match(/^(predictions\[\d+\](?:\.questions\[\d+\])?: )(.*)$/s);
    if (prefix) return prefix[1] + translateServer(prefix[2]);
    return value;
  }
  function format(template, args) {
    return template.replace(/\{(\d+)\}/g, (match, index) => index < args.length ? resolve(args[index], false) : match);
  }
  function resolve(value, translateString = true) {
    if (typeof value === "function") return resolve(value(), translateString);
    if (value && typeof value === "object") {
      if ("values" in value) return value.values.map(item => resolve(item)).join(value.separator);
      if ("key" in value) return format(language === "en" ? english[value.key] ?? translateServer(value.key) : value.key, value.args);
    }
    const text = value == null ? "" : String(value);
    return translateString ? translateServer(text) : text;
  }
  function cleanup() {
    cleanupQueued = false;
    for (const element of texts.keys()) if (!element.isConnected) texts.delete(element);
    for (const element of attributes.keys()) if (!element.isConnected) attributes.delete(element);
  }
  function queueCleanup() {
    if (!cleanupQueued) { cleanupQueued = true; queueMicrotask(cleanup); }
  }
  function textNode(value) {
    const text = document.createTextNode(resolve(value));
    texts.set(text, value); queueCleanup(); return text;
  }
  function setText(element, value) { element.replaceChildren(textNode(value)); }
  function setAttribute(element, name, value) {
    if (!attributes.has(element)) attributes.set(element, new Map());
    attributes.get(element).set(name, value);
    element.setAttribute(name, resolve(value)); queueCleanup();
  }
  function sourceText(element) {
    return join([...element.childNodes].map(child => child.nodeType === Node.TEXT_NODE ? texts.get(child) ?? child.data : sourceText(child)));
  }
  function refresh() {
    document.documentElement.lang = language;
    cleanup();
    for (const [text, value] of texts) text.data = resolve(value);
    for (const [element, entries] of attributes) for (const [name, value] of entries) element.setAttribute(name, resolve(value));
    document.querySelectorAll("[data-language]").forEach(button => {
      const active = button.dataset.language === language;
      button.setAttribute("aria-pressed", String(active)); button.classList.toggle("active", active);
    });
  }
  function setLanguage(value, persist = true) {
    if (!valid(value)) return;
    language = value;
    if (persist) { try { localStorage.setItem(storageKey, value); } catch { /* Keep the current tab usable. */ } }
    refresh();
  }
  function init() {
    const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const text = walker.currentNode;
      if (!text.parentElement || text.parentElement.closest("script,style,pre,textarea,[data-i18n-skip]")) continue;
      if (!/[가-힣]/.test(text.data)) continue;
      const raw = text.data, trimmed = raw.trim();
      texts.set(text, join([raw.slice(0, raw.indexOf(trimmed)), message(trimmed), raw.slice(raw.indexOf(trimmed) + trimmed.length)]));
    }
    document.querySelectorAll("[placeholder],[title],[aria-label]").forEach(element => {
      if (element.closest("[data-i18n-skip]")) return;
      for (const name of ["placeholder", "title", "aria-label"]) {
        const value = element.getAttribute(name);
        if (value && /[가-힣]/.test(value)) setAttribute(element, name, message(value));
      }
    });
    document.querySelectorAll("[data-language]").forEach(button => button.addEventListener("click", () => setLanguage(button.dataset.language)));
    document.querySelectorAll('a[href="/api/schema"]').forEach(link => setAttribute(link, "href", () => `/api/schema?lang=${language}`));
    window.addEventListener("storage", event => {
      if (event.key === storageKey || event.key === null) setLanguage(valid(event.newValue) ? event.newValue : preferred(), false);
    });
    refresh();
  }
  return { init, message, join, resolve, textNode, setText, setAttribute, sourceText, setLanguage,
    get language() { return language; }, get locale() { return language === "ko" ? "ko-KR" : "en-US"; } };
})();
const msg = I18n.message;
