// Absolute paths: page is served at the site root, API lives under /api.
const ENDPOINT = "/api/message";
const MEDIA_ENDPOINT = "/api/media";

const $messages = document.getElementById("messages");
const $form = document.getElementById("composer");
const $input = document.getElementById("input");
const $send = document.getElementById("send");
const $attach = document.getElementById("attach");
const $mic = document.getElementById("mic");
const $imageInput = document.getElementById("image-input");

function addBubble(text, who, opts = {}) {
  const el = document.createElement("div");
  el.className = `bubble ${who}` + (opts.thinking ? " thinking" : "");
  el.textContent = text;
  $messages.appendChild(el);
  $messages.scrollTop = $messages.scrollHeight;
  return el;
}

async function send(text) {
  addBubble(text, "user");
  const placeholder = addBubble("procesando…", "bot", { thinking: true });

  try {
    const r = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    placeholder.classList.remove("thinking");
    placeholder.textContent = data.reply || "(sin respuesta)";
  } catch (err) {
    placeholder.classList.remove("thinking");
    placeholder.textContent = `error: ${err.message}`;
  }
}

async function sendMedia(blob, filename, label) {
  addBubble(label, "user");
  const placeholder = addBubble("procesando…", "bot", { thinking: true });
  try {
    const fd = new FormData();
    fd.append("file", blob, filename);
    const r = await fetch(MEDIA_ENDPOINT, { method: "POST", body: fd });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    placeholder.classList.remove("thinking");
    placeholder.textContent = data.reply || "(sin respuesta)";
  } catch (err) {
    placeholder.classList.remove("thinking");
    placeholder.textContent = `error: ${err.message}`;
  }
}

$form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $input.value.trim();
  if (!text) return;
  $input.value = "";
  send(text);
});

// --- image (camera / file) ---
$attach.addEventListener("click", () => $imageInput.click());
$imageInput.addEventListener("change", () => {
  const f = $imageInput.files[0];
  if (f) sendMedia(f, f.name || "foto.jpg", "📷 (foto)");
  $imageInput.value = "";
});

// --- voice note (MediaRecorder) ---
let mediaRecorder = null;
let chunks = [];

$mic.addEventListener("click", async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    // Prefer formats Gemini accepts (ogg/mp4) over webm, which it rejects.
    const preferred = ["audio/ogg;codecs=opus", "audio/mp4", "audio/mpeg"];
    const supported = preferred.find(
      (t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)
    );
    mediaRecorder = new MediaRecorder(stream, supported ? { mimeType: supported } : undefined);
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      $mic.classList.remove("recording");
      const type = mediaRecorder.mimeType || "audio/webm";
      const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      const blob = new Blob(chunks, { type });
      sendMedia(blob, `nota.${ext}`, "🎤 (nota de voz)");
    };
    mediaRecorder.start();
    $mic.classList.add("recording");
  } catch (err) {
    addBubble(`no pude acceder al micrófono: ${err.message}`, "bot");
  }
});

addBubble("Mandame un gasto o ingreso por texto, foto del ticket, o nota de voz.", "bot");
