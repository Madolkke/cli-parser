"use strict";

/* Shared UI primitives: SVG icons, toasts, modal confirm dialogs and time
 * formatting.  UMD-free sibling of schema-model.js: browser-only, no tests
 * import it, so a plain window global is enough. */

const UI = (() => {
  /* ------------------------------------------------------------------ icons */

  const ICON_PATHS = {
    menu: "M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5",
    refresh: "M16.023 9.348h4.992V4.356m0 4.992-3.181-3.183a8.25 8.25 0 0 0-13.803 3.7M4.031 9.865v4.99m0 0h4.99m-4.99 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7",
    close: "M6 18 18 6M6 6l12 12",
    plus: "M12 4.5v15m7.5-7.5h-15",
    trash: "M14.74 9l-.346 9m-4.788 0L9.26 9M18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0",
    copy: "M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 0 1-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9 9 0 0 1 1.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 0 0-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 0 1-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 0 0-3.375-3.375h-1.5a1.125 1.125 0 0 1-1.125-1.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H9.75",
    check: "m4.5 12.75 6 6 9-13.5",
    chevron: "m8.25 4.5 7.5 7.5-7.5 7.5",
    chevronDown: "m4.5 8.25 7.5 7.5 7.5-7.5",
    play: "M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 0 1 0 1.971l-11.54 6.347a1.125 1.125 0 0 1-1.667-.985V5.653Z",
    stop: "M6 6h12v12H6z",
    search: "m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z",
    brain: "M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456Z",
    chat: "M8.625 12a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H8.25m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0H12m4.125 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm0 0h-.375M21 12c0 4.556-4.03 8.25-9 8.25a9.764 9.764 0 0 1-2.555-.337A5.972 5.972 0 0 1 5.41 20.97a5.969 5.969 0 0 1-.474-.065 4.48 4.48 0 0 0 .978-2.025c.09-.457-.133-.901-.467-1.226C3.93 16.178 3 14.189 3 12c0-4.556 4.03-8.25 9-8.25s9 3.694 9 8.25Z",
    wrench: "M11.42 15.17 17.25 21A2.652 2.652 0 0 0 21 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 1 1-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 0 0 4.486-6.336l-3.276 3.277a3.004 3.004 0 0 1-2.25-2.25l3.276-3.276a4.5 4.5 0 0 0-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437 1.745-1.437",
    clock: "M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
    history: "M12 8.25v-4.5m0 4.5 4.5-1.5M12 8.25l-3 1.5m9-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  };

  function svgIcon(name, size = 16) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", String(size));
    svg.setAttribute("height", String(size));
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICON_PATHS[name] || ICON_PATHS.chevron);
    svg.append(path);
    return svg;
  }

  function hydrateIcons(root = document) {
    root.querySelectorAll("[data-icon]").forEach((element) => {
      const name = element.dataset.icon;
      const size = Number(element.dataset.iconSize) || 16;
      element.replaceChildren(svgIcon(name, size));
    });
  }

  /* ------------------------------------------------------------------ toast */

  let toastHost = null;
  let modalSequence = 0;

  function ensureToastHost() {
    if (toastHost) return toastHost;
    toastHost = document.createElement("div");
    toastHost.className = "toast-host";
    toastHost.setAttribute("role", "status");
    toastHost.setAttribute("aria-live", "polite");
    document.body.append(toastHost);
    return toastHost;
  }

  function toast(message, kind = "info", timeoutMs = 3600) {
    const host = ensureToastHost();
    const item = document.createElement("div");
    item.className = "toast toast-" + kind;
    const icon = svgIcon(kind === "success" ? "check" : kind === "error" ? "close" : "history", 15);
    icon.classList.add("toast-icon");
    const text = document.createElement("span");
    text.className = "toast-text";
    text.textContent = message;
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "toast-dismiss";
    dismiss.setAttribute("aria-label", "关闭通知");
    dismiss.append(svgIcon("close", 13));
    item.append(icon, text, dismiss);
    const remove = () => {
      item.classList.add("is-leaving");
      setTimeout(() => item.remove(), 160);
    };
    dismiss.onclick = remove;
    host.append(item);
    if (host.children.length > 4) host.firstElementChild.remove();
    if (timeoutMs > 0) setTimeout(remove, timeoutMs);
    return item;
  }

  /* ---------------------------------------------------------- confirm dialog */

  let dialogStack = [];

  function confirmDialog({ title, body, confirmLabel = "确定", cancelLabel = "取消", danger = false }) {
    return new Promise((resolve) => {
      const previousFocus = document.activeElement;
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      const dialog = document.createElement("div");
      dialog.className = "modal";
      dialog.setAttribute("role", "alertdialog");
      dialog.setAttribute("aria-modal", "true");
      const titleId = "modal-title-" + (++modalSequence);
      dialog.setAttribute("aria-labelledby", titleId);
      const heading = document.createElement("h3");
      heading.id = titleId;
      heading.textContent = title;
      const message = document.createElement("p");
      message.textContent = body;
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancelButton = document.createElement("button");
      cancelButton.type = "button";
      cancelButton.className = "btn";
      cancelButton.textContent = cancelLabel;
      const confirmButton = document.createElement("button");
      confirmButton.type = "button";
      confirmButton.className = "btn " + (danger ? "btn-danger-solid" : "btn-primary");
      confirmButton.textContent = confirmLabel;
      actions.append(cancelButton, confirmButton);
      dialog.append(heading, message, actions);
      overlay.append(dialog);
      const finish = (result) => {
        overlay.remove();
        dialogStack = dialogStack.filter((entry) => entry.overlay !== overlay);
        document.body.classList.toggle("modal-open", dialogStack.length > 0);
        if (previousFocus && previousFocus.isConnected) previousFocus.focus();
        resolve(result);
      };
      cancelButton.onclick = () => finish(false);
      confirmButton.onclick = () => finish(true);
      overlay.addEventListener("mousedown", (event) => {
        if (event.target === overlay) finish(false);
      });
      const focusables = [cancelButton, confirmButton];
      dialog.addEventListener("keydown", (event) => {
        if (event.key === "Escape") { event.stopPropagation(); finish(false); }
        else if (event.key === "Tab") {
          const nodes = focusables;
          const first = nodes[0];
          const last = nodes[nodes.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      });
      document.body.append(overlay);
      document.body.classList.add("modal-open");
      dialogStack.push({ overlay });
      confirmButton.focus();
    });
  }

  /* ------------------------------------------------------------------- time */

  function formatDuration(totalSeconds) {
    const seconds = Math.max(0, Number(totalSeconds) || 0);
    if (seconds < 60) return seconds.toFixed(1) + " 秒";
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    if (minutes < 60) return minutes + " 分 " + rest + " 秒";
    const hours = Math.floor(minutes / 60);
    return hours + " 时 " + (minutes % 60) + " 分";
  }

  function relativeTime(value, now = Date.now()) {
    const date = new Date(value);
    const time = date.getTime();
    if (Number.isNaN(time)) return "";
    const deltaSeconds = Math.round((now - time) / 1000);
    if (deltaSeconds < 45) return "刚刚";
    if (deltaSeconds < 90) return "1 分钟前";
    const minutes = Math.round(deltaSeconds / 60);
    if (minutes < 60) return minutes + " 分钟前";
    const hours = Math.round(minutes / 60);
    if (hours < 24) return hours + " 小时前";
    const days = Math.round(hours / 24);
    if (days < 7) return days + " 天前";
    return date.toLocaleDateString(undefined, { month: "2-digit", day: "2-digit" });
  }

  return { svgIcon, hydrateIcons, toast, confirmDialog, formatDuration, relativeTime };
})();

if (typeof window !== "undefined") window.UI = UI;
