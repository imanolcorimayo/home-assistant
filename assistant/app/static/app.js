// Global chat modal. Lives on every page (included from base.html). Talks to
// POST /chat/message; family_id/member_id come from the session server-side.
(function () {
  const fab = document.getElementById("chat-fab");
  const modal = document.getElementById("chat-modal");
  const panel = document.getElementById("chat-panel");
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");
  const expandBtn = document.getElementById("chat-expand");
  if (!fab || !modal) return;

  const firstName = modal.dataset.name || "";
  let greeted = false;

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

  function open() {
    modal.classList.remove("hidden");
    if (!greeted) {
      bubble(`Hola ${firstName}. Contame un gasto o ingreso y lo registro. ` +
             `Por ejemplo: "gasté 20 en el super".`, "bot");
      greeted = true;
    }
    input.focus();
  }
  function close() { modal.classList.add("hidden"); }

  fab.addEventListener("click", open);
  document.getElementById("chat-close").addEventListener("click", close);

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
    bubble(text, "me");
    input.value = "";
    send.disabled = true;
    const pending = bubble("escribiendo...", "pending");
    try {
      const r = await fetch("/chat/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await r.json();
      pending.remove();
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
