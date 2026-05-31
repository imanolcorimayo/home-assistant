// Global chat modal. Lives on every page (included from base.html). Talks to
// POST /chat/stream (SSE over fetch) and renders the agent's progress live:
// each tool call as it runs, then the final reply. family_id/member_id come
// from the session server-side. A conversation is a chat_session whose id the
// server returns on the first message; we keep it in localStorage so the open
// thread survives page reloads.
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
  const STORE_KEY = "assistant.chat.session";
  let sessionId = localStorage.getItem(STORE_KEY) || null;
  let started = false;   // anything shown in the current conversation yet?
  let busy = false;

  // Human-readable label per tool, for the live progress line.
  const FRIENDLY = {
    look_up_transactions: "Buscando movimientos",
    add_expense: "Registrando gasto",
    add_income: "Registrando ingreso",
    edit_expense: "Corrigiendo movimiento",
    delete_transaction: "Borrando movimiento",
    restore_transaction: "Restaurando movimiento",
    list_recurring: "Revisando recurrentes",
    add_recurring_charge: "Creando recurrente",
    update_recurring_charge_tool: "Actualizando recurrente",
    pay_recurring: "Registrando pago",
    create_category: "Creando categoría",
    create_categories: "Creando categorías",
    create_budget: "Definiendo presupuesto",
    create_account: "Creando cuenta",
  };
  const friendly = (n) => FRIENDLY[n] || n;

  // ── rendering ────────────────────────────────────────────────────────────
  // Mini-markdown: **bold** inline + "- " bullets, inserted as text nodes only.
  function render(el, text) {
    el.textContent = "";
    const lines = (text || "").split("\n");
    lines.forEach((line, i) => {
      const bullet = /^\s*-\s+/.test(line);
      const row = document.createElement(bullet ? "div" : "span");
      if (bullet) { row.className = "pl-3 -indent-3"; line = "• " + line.replace(/^\s*-\s+/, ""); }
      for (const p of line.split(/(\*\*[^*]+\*\*)/g)) {
        if (p.startsWith("**") && p.endsWith("**")) {
          const b = document.createElement("strong");
          b.textContent = p.slice(2, -2);
          row.appendChild(b);
        } else {
          row.appendChild(document.createTextNode(p));
        }
      }
      el.appendChild(row);
      if (!bullet && i < lines.length - 1) el.appendChild(document.createTextNode("\n"));
    });
  }

  function bubble(text, who) {
    const el = document.createElement("div");
    const base = "px-3 py-2 rounded-2xl max-w-[85%] text-sm leading-snug whitespace-pre-wrap break-words";
    el.className = who === "me"
      ? base + " self-end bg-ink text-white rounded-br-sm"
      : base + " self-start bg-surface border border-line rounded-bl-sm";
    render(el, text);
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  // The live "thinking" element: a steps list + a reply slot, in one bot bubble.
  function liveBubble() {
    const wrap = document.createElement("div");
    wrap.className = "self-start max-w-[85%] w-full";
    const steps = document.createElement("div");
    steps.className = "hidden flex-col gap-0.5 mb-1 text-xs text-muted";
    const body = document.createElement("div");
    body.className = "px-3 py-2 rounded-2xl rounded-bl-sm bg-surface border border-line text-sm leading-snug whitespace-pre-wrap break-words text-muted italic";
    body.textContent = "Pensando…";
    wrap.append(steps, body);
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    const stepEls = {};
    return {
      tool(name) {
        steps.classList.remove("hidden"); steps.classList.add("flex");
        const s = document.createElement("div");
        s.textContent = "● " + friendly(name) + "…";
        steps.appendChild(s);
        stepEls[name] = s;
        body.textContent = friendly(name) + "…";
        log.scrollTop = log.scrollHeight;
      },
      toolDone(name, ok) {
        const s = stepEls[name];
        if (s) s.textContent = (ok ? "✓ " : "✕ ") + friendly(name);
      },
      reply(text) {
        body.classList.remove("text-muted", "italic");
        render(body, text);
        log.scrollTop = log.scrollHeight;
      },
      error(text) {
        body.classList.remove("italic");
        body.classList.add("text-expense");
        body.textContent = text;
      },
    };
  }

  // ── greeting ──────────────────────────────────────────────────────────────
  function greet() {
    bubble(`Hola ${firstName}. Contame un gasto o ingreso y lo registro, o preguntame algo.`, "bot");
    started = true;
  }

  // ── session open / new / resume ──────────────────────────────────────────
  async function loadSession(id) {
    drawer.classList.add("hidden");
    log.innerHTML = "";
    let count = 0;
    try {
      const r = await fetch(`/chat/sessions/${id}/messages`);
      const { messages } = await r.json();
      for (const m of messages) { bubble(m.text, m.who); count++; }
    } catch (e) { /* fall through to greet */ }
    if (count === 0) { greet(); } else { started = true; }
    sessionId = id;
    localStorage.setItem(STORE_KEY, id);
    input.focus();
  }

  function startNew() {
    sessionId = null;
    localStorage.removeItem(STORE_KEY);
    started = false;
    log.innerHTML = "";
    drawer.classList.add("hidden");
    greet();
    input.focus();
  }

  async function open() {
    modal.classList.remove("hidden");
    if (!started) {
      if (sessionId) { await loadSession(sessionId); }
      else { greet(); }
    }
    input.focus();
  }
  const close = () => modal.classList.add("hidden");

  fab.addEventListener("click", open);
  document.getElementById("chat-close").addEventListener("click", close);
  newBtn.addEventListener("click", startNew);
  drawerClose.addEventListener("click", () => drawer.classList.add("hidden"));

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
        const title = document.createElement("div");
        title.className = "text-sm truncate"; title.textContent = s.title || "Conversación";
        const meta = document.createElement("div");
        meta.className = "text-xs text-muted"; meta.textContent = `${s.runs} mensaje(s) · ${when}`;
        btn.append(title, meta);
        btn.addEventListener("click", () => loadSession(s.session_id));
        drawerList.appendChild(btn);
      }
    } catch (e) {
      drawerList.innerHTML = `<div class="text-expense text-sm p-2">No se pudieron cargar.</div>`;
    }
  });

  // Expand toggle: swap the small docked panel for a large one (desktop).
  expandBtn.addEventListener("click", () => {
    ["md:inset-6", "md:bottom-6", "md:right-6", "md:w-[420px]", "md:h-[640px]", "md:max-h-[85vh]"]
      .forEach((c) => modal.classList.toggle(c));
  });

  // ── send (SSE over fetch) ─────────────────────────────────────────────────
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (busy) return;
    const text = input.value.trim();
    if (!text) return;
    started = true;
    bubble(text, "me");
    input.value = "";
    busy = true; send.disabled = true;
    const live = liveBubble();
    try {
      const r = await fetch("/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      if (!r.ok || !r.body) throw new Error("bad response");
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (!chunk.startsWith("data: ")) continue;
          const ev = JSON.parse(chunk.slice(6));
          if (ev.type === "start") {
            sessionId = ev.session_id; localStorage.setItem(STORE_KEY, sessionId);
          } else if (ev.type === "tool") {
            live.tool(ev.name);
          } else if (ev.type === "tool_done") {
            live.toolDone(ev.name, ev.ok);
          } else if (ev.type === "reply") {
            live.reply(ev.text);
            if (ev.session_id) { sessionId = ev.session_id; localStorage.setItem(STORE_KEY, sessionId); }
          } else if (ev.type === "error") {
            live.error(ev.message);
          }
        }
      }
    } catch (err) {
      live.error("Error de conexión. Probá de nuevo.");
    } finally {
      busy = false; send.disabled = false; input.focus();
    }
  });

  // Enter sends, Shift+Enter newlines.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
})();
