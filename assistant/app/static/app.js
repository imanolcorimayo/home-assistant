// Global chat modal. Lives on every page (included from base.html). Talks to
// POST /chat/stream (multipart: text + attachments → SSE over fetch) and renders
// the agent's progress live: each tool call as it runs, then the reply.
// family_id/member_id come from the session server-side. A conversation is a
// chat_session whose id the server returns on the first message; we keep it in
// localStorage so the open thread survives reloads.
(function () {
  const $ = (id) => document.getElementById(id);
  const fab = $("chat-fab"), modal = $("chat-modal"), log = $("chat-log");
  const form = $("chat-form"), input = $("chat-input"), send = $("chat-send");
  const expandBtn = $("chat-expand"), newBtn = $("chat-new"), historyBtn = $("chat-history");
  const drawer = $("chat-sessions"), drawerList = $("chat-sessions-list"), drawerClose = $("chat-sessions-close");
  const attachBtn = $("chat-attach"), micBtn = $("chat-mic"), fileInput = $("chat-file");
  const attachBox = $("chat-attachments");
  const recBox = $("chat-recording"), recTime = $("chat-rec-time"), recStop = $("chat-rec-stop"), recCancel = $("chat-rec-cancel");
  if (!fab || !modal) return;

  // Limits — mirror the server (config.py). Keep quality high, payloads small.
  const MAX_IMAGES = 3, MAX_AUDIOS = 2, MAX_AUDIO_SEC = 120, MAX_IMAGE_BYTES = 5 * 1024 * 1024;

  const firstName = modal.dataset.name || "";
  const STORE_KEY = "assistant.chat.session";
  let sessionId = localStorage.getItem(STORE_KEY) || null;
  let started = false, busy = false;
  let pending = [];   // [{kind, mime, blob, name, url}]

  const FRIENDLY = {
    look_up_transactions: "Buscando movimientos", add_expense: "Registrando gasto",
    add_income: "Registrando ingreso", edit_expense: "Corrigiendo movimiento",
    delete_transaction: "Borrando movimiento", restore_transaction: "Restaurando movimiento",
    list_recurring: "Revisando recurrentes", add_recurring_charge: "Creando recurrente",
    update_recurring_charge_tool: "Actualizando recurrente", pay_recurring: "Registrando pago",
    create_category: "Creando categoría", create_categories: "Creando categorías",
    create_budget: "Definiendo presupuesto", create_account: "Creando cuenta",
  };
  const friendly = (n) => FRIENDLY[n] || n;

  // ── rendering ──────────────────────────────────────────────────────────────
  function render(el, text) {
    el.textContent = "";
    const lines = (text || "").split("\n");
    lines.forEach((line, i) => {
      const bullet = /^\s*-\s+/.test(line);
      const row = document.createElement(bullet ? "div" : "span");
      if (bullet) { row.className = "pl-3 -indent-3"; line = "• " + line.replace(/^\s*-\s+/, ""); }
      for (const p of line.split(/(\*\*[^*]+\*\*)/g)) {
        if (p.startsWith("**") && p.endsWith("**")) {
          const b = document.createElement("strong"); b.textContent = p.slice(2, -2); row.appendChild(b);
        } else { row.appendChild(document.createTextNode(p)); }
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

  // A "me" bubble that can show attachment thumbnails above the text.
  function myBubble(text, atts) {
    const wrap = document.createElement("div");
    wrap.className = "self-end flex flex-col items-end gap-1 max-w-[85%]";
    if (atts && atts.length) {
      const strip = document.createElement("div");
      strip.className = "flex flex-wrap gap-1 justify-end";
      atts.forEach((a) => {
        if (a.kind === "image") {
          const img = document.createElement("img");
          img.src = a.url; img.className = "w-20 h-20 object-cover rounded-lg border border-line";
          strip.appendChild(img);
        } else {
          const chip = document.createElement("div");
          chip.className = "text-xs rounded-lg border border-line bg-surface px-2 py-1 text-muted";
          chip.textContent = "🎤 audio";
          strip.appendChild(chip);
        }
      });
      wrap.appendChild(strip);
    }
    if (text) {
      const el = document.createElement("div");
      el.className = "px-3 py-2 rounded-2xl rounded-br-sm bg-ink text-white text-sm leading-snug whitespace-pre-wrap break-words";
      render(el, text);
      wrap.appendChild(el);
    }
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

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
        const s = document.createElement("div"); s.textContent = "● " + friendly(name) + "…";
        steps.appendChild(s); stepEls[name] = s;
        body.textContent = friendly(name) + "…"; log.scrollTop = log.scrollHeight;
      },
      toolDone(name, ok) { const s = stepEls[name]; if (s) s.textContent = (ok ? "✓ " : "✕ ") + friendly(name); },
      reply(text) { body.classList.remove("text-muted", "italic"); render(body, text); log.scrollTop = log.scrollHeight; },
      error(text, onRetry) {
        body.classList.remove("italic", "text-muted"); body.classList.add("text-expense");
        body.textContent = text + " ";
        if (onRetry) {
          const b = document.createElement("button");
          b.className = "underline font-medium"; b.textContent = "Reintentar";
          b.addEventListener("click", () => { wrap.remove(); onRetry(); });
          body.appendChild(b);
        }
      },
    };
  }

  // ── attachments ────────────────────────────────────────────────────────────
  function renderAttachments() {
    attachBox.innerHTML = "";
    if (!pending.length) { attachBox.classList.add("hidden"); attachBox.classList.remove("flex"); return; }
    attachBox.classList.remove("hidden"); attachBox.classList.add("flex");
    pending.forEach((a, i) => {
      const cell = document.createElement("div");
      cell.className = "relative";
      if (a.kind === "image") {
        const img = document.createElement("img");
        img.src = a.url; img.className = "w-16 h-16 object-cover rounded-lg border border-line";
        cell.appendChild(img);
      } else {
        const chip = document.createElement("div");
        chip.className = "w-16 h-16 grid place-items-center rounded-lg border border-line bg-bg text-xs text-muted text-center px-1";
        chip.textContent = "🎤 " + (a.secs ? a.secs + "s" : "audio");
        cell.appendChild(chip);
      }
      const x = document.createElement("button");
      x.type = "button";
      x.className = "absolute -top-1.5 -right-1.5 w-5 h-5 grid place-items-center rounded-full bg-ink text-white text-xs";
      x.textContent = "×";
      x.addEventListener("click", () => { URL.revokeObjectURL(a.url); pending.splice(i, 1); renderAttachments(); });
      cell.appendChild(x);
      attachBox.appendChild(cell);
    });
  }

  // Downscale an image to <=1600px and re-encode as JPEG (quota + size).
  function processImage(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const max = 1600, scale = Math.min(1, max / Math.max(img.width, img.height));
        const w = Math.round(img.width * scale), h = Math.round(img.height * scale);
        const c = document.createElement("canvas"); c.width = w; c.height = h;
        c.getContext("2d").drawImage(img, 0, 0, w, h);
        c.toBlob((blob) => {
          URL.revokeObjectURL(img.src);
          if (!blob) return reject();
          resolve({ kind: "image", mime: "image/jpeg", blob, name: "imagen.jpg", url: URL.createObjectURL(blob) });
        }, "image/jpeg", 0.85);
      };
      img.onerror = reject;
      img.src = URL.createObjectURL(file);
    });
  }

  attachBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    for (const file of fileInput.files) {
      if (pending.filter((a) => a.kind === "image").length >= MAX_IMAGES) {
        alert(`Máximo ${MAX_IMAGES} imágenes.`); break;
      }
      try {
        const a = await processImage(file);
        if (a.blob.size > MAX_IMAGE_BYTES) { alert("La imagen es demasiado grande."); continue; }
        pending.push(a); renderAttachments();
      } catch (e) { alert("No se pudo procesar la imagen."); }
    }
    fileInput.value = "";
  });

  // ── audio recording ──────────────────────────────────────────────────────
  let recorder = null, recChunks = [], recTimer = null, recSecs = 0;

  function pickAudioMime() {
    const types = ["audio/mp4", "audio/ogg", "audio/webm"];
    for (const t of types) if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
    return "";
  }

  micBtn.addEventListener("click", async () => {
    if (pending.filter((a) => a.kind === "audio").length >= MAX_AUDIOS) { alert(`Máximo ${MAX_AUDIOS} audios.`); return; }
    if (!navigator.mediaDevices || !window.MediaRecorder) { alert("Tu navegador no permite grabar audio."); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { alert("No se pudo acceder al micrófono."); return; }
    const mime = pickAudioMime();
    recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    recChunks = []; recSecs = 0;
    recorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      recBox.classList.add("hidden"); recBox.classList.remove("flex");
      clearInterval(recTimer);
      if (recorder._cancelled) return;
      const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
      pending.push({ kind: "audio", mime: blob.type, blob, name: "audio", secs: recSecs, url: URL.createObjectURL(blob) });
      renderAttachments();
    };
    recorder._cancelled = false;
    recorder.start();
    recBox.classList.remove("hidden"); recBox.classList.add("flex");
    recTime.textContent = "0:00";
    recTimer = setInterval(() => {
      recSecs++;
      recTime.textContent = Math.floor(recSecs / 60) + ":" + String(recSecs % 60).padStart(2, "0");
      if (recSecs >= MAX_AUDIO_SEC) recorder.stop();   // auto-stop at the cap
    }, 1000);
  });
  recStop.addEventListener("click", () => { if (recorder && recorder.state !== "inactive") recorder.stop(); });
  recCancel.addEventListener("click", () => { if (recorder) { recorder._cancelled = true; recorder.stop(); } });

  // ── greeting / sessions ─────────────────────────────────────────────────────
  function greet() { bubble(`Hola ${firstName}. Contame un gasto o ingreso y lo registro, o preguntame algo. También podés mandarme la foto de un ticket o un audio.`, "bot"); started = true; }

  async function loadSession(id) {
    drawer.classList.add("hidden"); log.innerHTML = "";
    let count = 0;
    try {
      const r = await fetch(`/chat/sessions/${id}/messages`);
      const { messages } = await r.json();
      for (const m of messages) { bubble(m.text, m.who); count++; }
    } catch (e) { /* fall through */ }
    if (count === 0) greet(); else started = true;
    sessionId = id; localStorage.setItem(STORE_KEY, id); input.focus();
  }

  function startNew() {
    sessionId = null; localStorage.removeItem(STORE_KEY); started = false;
    pending.forEach((a) => URL.revokeObjectURL(a.url)); pending = []; renderAttachments();
    log.innerHTML = ""; drawer.classList.add("hidden"); greet(); input.focus();
  }

  async function open() {
    modal.classList.remove("hidden");
    if (!started) { if (sessionId) await loadSession(sessionId); else greet(); }
    input.focus();
  }
  const close = () => modal.classList.add("hidden");

  fab.addEventListener("click", open);
  $("chat-close").addEventListener("click", close);
  newBtn.addEventListener("click", startNew);
  drawerClose.addEventListener("click", () => drawer.classList.add("hidden"));

  historyBtn.addEventListener("click", async () => {
    drawer.classList.remove("hidden");
    drawerList.innerHTML = `<div class="text-muted text-sm p-2">Cargando...</div>`;
    try {
      const r = await fetch("/chat/sessions");
      const { sessions } = await r.json();
      drawerList.innerHTML = "";
      if (!sessions || !sessions.length) { drawerList.innerHTML = `<div class="text-muted text-sm p-2">No hay conversaciones todavía.</div>`; return; }
      for (const s of sessions) {
        const btn = document.createElement("button");
        btn.className = "text-left px-3 py-2 rounded-lg hover:bg-bg border border-transparent hover:border-line";
        const when = s.last_ts ? new Date(s.last_ts).toLocaleDateString() : "";
        const title = document.createElement("div"); title.className = "text-sm truncate"; title.textContent = s.title || "Conversación";
        const meta = document.createElement("div"); meta.className = "text-xs text-muted"; meta.textContent = `${s.runs} mensaje(s) · ${when}`;
        btn.append(title, meta);
        btn.addEventListener("click", () => loadSession(s.session_id));
        drawerList.appendChild(btn);
      }
    } catch (e) { drawerList.innerHTML = `<div class="text-expense text-sm p-2">No se pudieron cargar.</div>`; }
  });

  expandBtn.addEventListener("click", () => {
    ["md:inset-6", "md:bottom-6", "md:right-6", "md:w-[420px]", "md:h-[640px]", "md:max-h-[85vh]"]
      .forEach((c) => modal.classList.toggle(c));
  });

  // ── auto-grow textarea ──────────────────────────────────────────────────────
  function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 128) + "px"; }
  input.addEventListener("input", grow);

  // ── send (multipart → SSE) ──────────────────────────────────────────────────
  async function sendPayload(text, atts) {
    busy = true; send.disabled = true;
    const live = liveBubble();
    try {
      const fd = new FormData();
      fd.append("message", text);
      fd.append("session_id", sessionId || "");
      atts.forEach((a) => fd.append("files", a.blob, a.name));
      const r = await fetch("/chat/stream", { method: "POST", body: fd });
      if (!r.ok || !r.body) {
        let msg = "Error al enviar.";
        try { const j = await r.json(); if (j.error) msg = j.error; } catch (e) {}
        live.error(msg, () => sendPayload(text, atts)); return;
      }
      const reader = r.body.getReader(), dec = new TextDecoder();
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
          if (ev.type === "start") { sessionId = ev.session_id; localStorage.setItem(STORE_KEY, sessionId); }
          else if (ev.type === "tool") live.tool(ev.name);
          else if (ev.type === "tool_done") live.toolDone(ev.name, ev.ok);
          else if (ev.type === "reply") { live.reply(ev.text); if (ev.session_id) { sessionId = ev.session_id; localStorage.setItem(STORE_KEY, sessionId); } }
          else if (ev.type === "error") live.error(ev.message, () => sendPayload(text, atts));
        }
      }
    } catch (err) {
      live.error("Error de conexión.", () => sendPayload(text, atts));
    } finally {
      busy = false; send.disabled = false; input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) return;
    const text = input.value.trim();
    if (!text && !pending.length) return;
    started = true;
    const atts = pending;            // capture for this send (and retry)
    myBubble(text, atts);
    input.value = ""; input.style.height = "auto";
    pending = []; renderAttachments();
    sendPayload(text, atts);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
})();
