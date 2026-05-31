// Global chat modal. Lives on every page (included from base.html). Talks to
// POST /chat/message; family_id/member_id come from the session server-side.
// A conversation is a chat_session: the server returns its id on the first
// message, we send it back on every following one. New/resume via the header.
(function () {
  const fab = document.getElementById("chat-fab");
  const modal = document.getElementById("chat-modal");
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");
  const expandBtn = document.getElementById("chat-expand");
  const newBtn = document.getElementById("chat-new");
  const historyBtn = document.getElementById("chat-history");
  const drawer = document.getElementById("chat-sessions");
  const drawerList = document.getElementById("chat-sessions-list");
  const drawerClose = document.getElementById("chat-sessions-close");
  if (!fab || !modal) return;

  const firstName = modal.dataset.name || "";
  let sessionId = null;   // null = a fresh, not-yet-saved conversation
  let started = false;    // have we shown anything in this conversation yet?

  // Minimal markdown: **bold**. Inserted as text nodes (no HTML injection).
  function render(el, text) {
    for (const p of text.split(/(\*\*[^*]+\*\*)/g)) {
      if (p.startsWith("**") && p.endsWith("**")) {
        const b = document.createElement("strong");
        b.textContent = p.slice(2, -2);
        el.appendChild(b);
      } else {
        el.appendChild(document.createTextNode(p));
      }
    }
  }

  function bubble(text, who) {
    const el = document.createElement("div");
    const base = "px-3 py-2 rounded-2xl max-w-[85%] text-sm leading-snug whitespace-pre-wrap break-words";
    el.className = who === "me"
      ? base + " self-end bg-ink text-white rounded-br-sm"
      : base + " self-start bg-surface border border-line rounded-bl-sm";
    if (who === "pending") {
      el.className = base + " self-start bg-surface border border-line text-muted italic";
    }
    render(el, text);
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function greet() {
    bubble(`Hola ${firstName}. Contame un gasto o ingreso y lo registro. ` +
           `Por ejemplo: "gasté 20 en el super".`, "bot");
    started = true;
  }

  // Reset to a blank conversation (new thread on the next message sent).
  function startNew() {
    sessionId = null;
    started = false;
    log.innerHTML = "";
    drawer.classList.add("hidden");
    greet();
    input.focus();
  }

  function open() {
    modal.classList.remove("hidden");
    if (!started) greet();
    input.focus();
  }
  function close() { modal.classList.add("hidden"); }

  fab.addEventListener("click", open);
  document.getElementById("chat-close").addEventListener("click", close);
  newBtn.addEventListener("click", startNew);
  drawerClose.addEventListener("click", () => drawer.classList.add("hidden"));

  // Sessions drawer: list this member's previous conversations.
  historyBtn.addEventListener("click", async () => {
    drawer.classList.remove("hidden");
    drawerList.innerHTML = `<div class="text-muted text-sm p-2">Cargando...</div>`;
    try {
      const r = await fetch("/chat/sessions");
      const { sessions } = await r.json();
      drawerList.innerHTML = "";
      if (!sessions || !sessions.length) {
        drawerList.innerHTML = `<div class="text-muted text-sm p-2">No hay conversaciones todavía.</div>`;
        return;
      }
      for (const s of sessions) {
        const btn = document.createElement("button");
        btn.className = "text-left px-3 py-2 rounded-lg hover:bg-bg border border-transparent hover:border-line";
        const when = s.last_ts ? new Date(s.last_ts).toLocaleDateString() : "";
        btn.innerHTML =
          `<div class="text-sm truncate">${escapeHtml(s.title || "Conversación")}</div>` +
          `<div class="text-xs text-muted">${s.runs} mensaje(s) · ${when}</div>`;
        btn.addEventListener("click", () => loadSession(s.session_id));
        drawerList.appendChild(btn);
      }
    } catch (err) {
      drawerList.innerHTML = `<div class="text-expense text-sm p-2">No se pudieron cargar.</div>`;
    }
  });

  async function loadSession(id) {
    drawer.classList.add("hidden");
    log.innerHTML = "";
    try {
      const r = await fetch(`/chat/sessions/${id}/messages`);
      const { messages } = await r.json();
      for (const m of messages) bubble(m.text, m.who);
    } catch (err) {
      bubble("No se pudo abrir la conversación.", "bot");
    }
    sessionId = id;
    started = true;
    input.focus();
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // Expand toggle: on desktop, swap the small docked panel for a large centered one.
  expandBtn.addEventListener("click", () => {
    modal.classList.toggle("md:inset-6");
    modal.classList.toggle("md:bottom-6");
    modal.classList.toggle("md:right-6");
    modal.classList.toggle("md:w-[420px]");
    modal.classList.toggle("md:h-[640px]");
    modal.classList.toggle("md:max-h-[85vh]");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    started = true;
    bubble(text, "me");
    input.value = "";
    send.disabled = true;
    const pending = bubble("escribiendo...", "pending");
    try {
      const r = await fetch("/chat/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      const data = await r.json();
      pending.remove();
      if (data.session_id) sessionId = data.session_id;
      bubble(data.reply || data.error || "(sin respuesta)", "bot");
    } catch (err) {
      pending.remove();
      bubble("Error de conexión. Probá de nuevo.", "bot");
    } finally {
      send.disabled = false;
      input.focus();
    }
  });

  // Enter sends, Shift+Enter newlines.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
})();
