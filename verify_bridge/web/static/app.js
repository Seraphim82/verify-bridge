(() => {
  "use strict";

  const state = {
    envKeys: {},
    defaults: {},
    referenceFiles: [],
    deliverableFiles: [],
    currentRunId: null,
    eventSource: null,
    lastResult: null,
    subs: {},
    pendingRequest: null,
  };

  const $ = (id) => document.getElementById(id);

  const els = {
    creatorProvider: $("creator-provider"),
    creatorModel: $("creator-model"),
    creatorKey: $("creator-key"),
    creatorUrl: $("creator-url"),
    creatorCli: $("creator-cli"),
    creatorEnvHint: $("creator-env-hint"),
    verifierProvider: $("verifier-provider"),
    verifierModel: $("verifier-model"),
    verifierKey: $("verifier-key"),
    verifierUrl: $("verifier-url"),
    verifierCli: $("verifier-cli"),
    verifierEnvHint: $("verifier-env-hint"),
    bridgeVisual: $("bridge-visual"),
    liveStatus: $("live-status"),
    liveMessage: $("live-message"),
    maxIter: $("max-iter"),
    stagnation: $("stagnation"),
    allowStructured: $("allow-structured"),
    includeAcceptable: $("include-acceptable"),
    markdownInput: $("markdown-input"),
    mdFileInput: $("md-file-input"),
    refFilesInput: $("ref-files-input"),
    outFilesInput: $("out-files-input"),
    refFilesList: $("ref-files-list"),
    outFilesList: $("out-files-list"),
    btnPreview: $("btn-preview"),
    btnStart: $("btn-start"),
    btnStop: $("btn-stop"),
    btnClear: $("btn-clear"),
    btnClosePreview: $("btn-close-preview"),
    btnExport: $("btn-export"),
    previewPanel: $("preview-panel"),
    previewPacket: $("preview-packet"),
    previewAudit: $("preview-audit"),
    errorBanner: $("error-banner"),
    finalBanner: $("final-banner"),
    timeline: $("timeline"),
    chatPanel: $("chat-panel"),
    chatRolePill: $("chat-role-pill"),
    chatTitle: $("chat-title"),
    chatInstructions: $("chat-instructions"),
    chatCopy: $("chat-copy"),
    chatOpen: $("chat-open"),
    chatPrompt: $("chat-prompt"),
    chatFilesStep: $("chat-files-step"),
    chatFiles: $("chat-files"),
    chatReplyNum: $("chat-reply-num"),
    chatReply: $("chat-reply"),
    chatUploadWrap: $("chat-upload-wrap"),
    chatUpload: $("chat-upload"),
    chatSend: $("chat-send"),
  };

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function updateAgentFields(role) {
    const provider = els[`${role}Provider`].value;
    const container = document.querySelector(`.agent-fields[data-role="${role}"]`);
    const modelField = container.querySelector(".field-model");
    const keyField = container.querySelector(".field-key");
    const urlField = container.querySelector(".field-url");
    const cliField = container.querySelector(".field-cli");
    const subField = container.querySelector(".field-sub");
    const chatField = container.querySelector(".field-chat");
    const envHint = els[`${role}EnvHint`];

    const isApi = provider === "gemini" || provider === "openai" || provider === "anthropic";
    const isSub = provider.startsWith("sub_");
    modelField.classList.toggle("hidden", !isApi && !isSub);
    subField.classList.toggle("hidden", !isSub);
    chatField.classList.toggle("hidden", provider !== "chat");
    if (isSub) {
      els[`${role}Model`].placeholder = "opcional · deixe vazio para o padrão da assinatura";
      renderSubStatus(role);
    }
    keyField.classList.toggle("hidden", !isApi);
    urlField.classList.toggle("hidden", provider !== "openai");
    cliField.classList.toggle("hidden", provider !== "cli");

    if (isApi && state.defaults[provider]) {
      els[`${role}Model`].placeholder = state.defaults[provider].model || "";
      if (!els[`${role}Model`].value) {
        els[`${role}Model`].value = state.defaults[provider].model || "";
      }
      if (provider === "openai" && !els[`${role}Url`].value) {
        els[`${role}Url`].value = state.defaults.openai.base_url || "";
      }
    }

    if (isApi && state.envKeys[provider]) {
      envHint.textContent = "✓ Chave detectada no ambiente";
    } else if (isSub) {
      envHint.textContent = "Sem chave de API";
    } else if (provider === "chat") {
      envHint.textContent = "Você cola no chat";
    } else {
      envHint.textContent = "";
    }
  }

  function renderFileChips(listEl, files, role) {
    if (!files.length) {
      listEl.className = "file-chips empty-chips";
      listEl.textContent =
        role === "ref" ? "Nenhum arquivo de referência" : "Nenhum arquivo de entrega";
      return;
    }
    listEl.className = "file-chips";
    listEl.innerHTML = "";
    files.forEach((f, idx) => {
      const chip = document.createElement("div");
      chip.className = "file-chip";
      if (f.mime && f.mime.startsWith("image/")) {
        const img = document.createElement("img");
        img.src = `data:${f.mime};base64,${f.data_b64}`;
        img.alt = f.name;
        chip.appendChild(img);
      }
      const span = document.createElement("span");
      span.textContent = f.name;
      chip.appendChild(span);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.title = "Remover";
      btn.textContent = "×";
      btn.addEventListener("click", () => {
        files.splice(idx, 1);
        renderFileChips(listEl, files, role);
      });
      chip.appendChild(btn);
      listEl.appendChild(chip);
    });
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const res = String(reader.result || "");
        const b64 = res.includes(",") ? res.split(",", 2)[1] : res;
        resolve({ name: file.name, mime: file.type || "application/octet-stream", data_b64: b64 });
      };
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  async function handleFileUpload(inputEl, targetList, listEl, role) {
    const selected = Array.from(inputEl.files || []);
    for (const file of selected) {
      const item = await readFileAsBase64(file);
      targetList.push(item);
    }
    inputEl.value = "";
    renderFileChips(listEl, targetList, role);
  }

  function collectAgentConfig(role) {
    return {
      provider: els[`${role}Provider`].value,
      model: els[`${role}Model`].value.trim(),
      api_key: els[`${role}Key`].value.trim(),
      base_url: els[`${role}Url`].value.trim(),
      cli_cmd: els[`${role}Cli`].value.trim(),
      chat: $(`${role}-chat`).value,
    };
  }

  // ---------------- Assinaturas ----------------
  function subToolId(role) {
    return els[`${role}Provider`].value.replace(/^sub_/, "");
  }

  function renderSubStatus(role) {
    const box = $(`${role}-sub-status`);
    const loginBtn = $(`${role}-sub-login`);
    const info = state.subs[subToolId(role)];
    box.className = "sub-status";
    if (!info) {
      box.textContent = "Verificando assinatura...";
      loginBtn.classList.add("hidden");
      return;
    }
    if (info.logged_in) {
      box.classList.add("ok");
      box.textContent = `✓ ${info.label}: ${info.detail}`;
      loginBtn.classList.add("hidden");
    } else {
      box.classList.add("warn");
      box.textContent = `${info.label}: ${info.detail}`;
      loginBtn.classList.remove("hidden");
      loginBtn.textContent = info.installed ? `Entrar (${info.plan})` : "Como instalar";
    }
  }

  async function loadSubscriptions(refresh = false) {
    try {
      const resp = await fetch(`/api/subscriptions${refresh ? "?refresh=1" : ""}`);
      const data = await resp.json();
      state.subs = {};
      for (const t of data.tools || []) state.subs[t.id] = t;
    } catch {
      // mantem estado anterior
    }
    ["creator", "verifier"].forEach((role) => {
      if (els[`${role}Provider`].value.startsWith("sub_")) renderSubStatus(role);
    });
  }

  async function startSubLogin(role) {
    const toolId = subToolId(role);
    hideBanners();
    try {
      const resp = await fetch(`/api/subscriptions/${toolId}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await resp.json();
      if (!resp.ok) {
        showError(data.error || "Não foi possível iniciar o login.");
        return;
      }
      if (data.url) window.open(data.url, "_blank", "noopener");
      $(`${role}-sub-status`).textContent = data.message || "Conclua o login e volte para esta tela.";
      // Consulta o status por ate 3 minutos enquanto o usuario faz login.
      for (let i = 0; i < 60; i += 1) {
        await new Promise((r) => setTimeout(r, 3000));
        await loadSubscriptions(true);
        if (state.subs[toolId] && state.subs[toolId].logged_in) break;
      }
    } catch (err) {
      showError(`Falha ao iniciar login: ${err}`);
    }
  }

  // ---------------- Modo Chat ----------------
  function showChatPanel(req) {
    state.pendingRequest = req;
    const isVerifier = req.role === "verifier";
    els.chatRolePill.className = `pill ${isVerifier ? "pill-verifier" : "pill-creator"}`;
    els.chatRolePill.textContent = isVerifier ? "IA 2 · VERIFICADORA" : "IA 1 · CRIADORA";
    els.chatTitle.textContent = `Sua vez: leve este pedido ao ${req.chat_label}`;
    els.chatInstructions.textContent = req.instructions || "";
    els.chatPrompt.textContent = req.prompt || "";
    els.chatOpen.href = req.chat_url || "#";
    els.chatOpen.textContent = `Abrir ${req.chat_label}`;

    els.chatFiles.innerHTML = "";
    const files = req.files || [];
    els.chatFilesStep.classList.toggle("hidden", files.length === 0);
    els.chatReplyNum.textContent = files.length ? "3" : "2";
    for (const f of files) {
      const a = document.createElement("a");
      a.className = "btn btn-ghost btn-sm";
      a.href = f.url;
      a.download = f.name;
      a.textContent = `⬇ ${f.name}`;
      els.chatFiles.appendChild(a);
    }
    els.chatReply.value = "";
    els.chatUpload.value = "";
    els.chatUploadWrap.classList.toggle("hidden", !req.expects_files);
    els.chatSend.disabled = false;
    els.chatPanel.classList.remove("hidden");
    els.chatPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function hideChatPanel() {
    state.pendingRequest = null;
    els.chatPanel.classList.add("hidden");
  }

  async function copyChatPrompt() {
    const text = els.chatPrompt.textContent || "";
    try {
      await navigator.clipboard.writeText(text);
      els.chatCopy.textContent = "✓ Copiado";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(els.chatPrompt);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      els.chatCopy.textContent = "Selecionado: use Cmd/Ctrl+C";
    }
    setTimeout(() => (els.chatCopy.textContent = "Copiar pedido"), 2500);
  }

  async function sendChatReply() {
    const req = state.pendingRequest;
    if (!req || !state.currentRunId) return;
    const files = [];
    for (const file of Array.from(els.chatUpload.files || [])) {
      const item = await readFileAsBase64(file);
      files.push({ name: item.name, data_b64: item.data_b64 });
    }
    const text = els.chatReply.value;
    if (!text.trim() && !files.length) {
      showError("Cole a resposta do chat (ou anexe o arquivo revisado) antes de enviar.");
      return;
    }
    els.chatSend.disabled = true;
    try {
      const resp = await fetch(`/api/runs/${state.currentRunId}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: req.request_id, text, files }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        els.chatSend.disabled = false;
        showError(data.error || "Resposta não aceita.");
        return;
      }
      hideBanners();
      hideChatPanel();
    } catch (err) {
      els.chatSend.disabled = false;
      showError(`Falha ao enviar resposta: ${err}`);
    }
  }

  function collectPayload() {
    return {
      creator: collectAgentConfig("creator"),
      verifier: collectAgentConfig("verifier"),
      markdown: els.markdownInput.value,
      reference_files: state.referenceFiles.map((f) => ({ name: f.name, data_b64: f.data_b64 })),
      deliverable_files: state.deliverableFiles.map((f) => ({ name: f.name, data_b64: f.data_b64 })),
      max_iterations: parseInt(els.maxIter.value, 10) || 5,
      stagnation_limit: parseInt(els.stagnation.value, 10) || 2,
      allow_structured: els.allowStructured.checked,
      include_acceptable: els.includeAcceptable.checked,
    };
  }

  function setStatus(kind, label, message) {
    els.liveStatus.className = `status-badge status-${kind}`;
    els.liveStatus.textContent = label;
    els.liveMessage.textContent = message || "";
    els.bridgeVisual.classList.toggle("active", kind === "running");
  }

  function setRunningUI(running) {
    els.btnStart.classList.toggle("hidden", running);
    els.btnStop.classList.toggle("hidden", !running);
    els.btnPreview.disabled = running;
  }

  function showError(msg) {
    els.errorBanner.textContent = msg;
    els.errorBanner.classList.remove("hidden");
  }

  function hideBanners() {
    els.errorBanner.classList.add("hidden");
    els.finalBanner.classList.add("hidden");
  }

  function renderRound(data) {
    const empty = els.timeline.querySelector(".empty-timeline");
    if (empty) empty.remove();

    const card = document.createElement("article");
    card.className = "round-card";

    const rep = data.relatorio;
    const san = data.sanitizacao || {};
    const counts = rep ? rep.contagem : { critico: 0, moderado: 0, aceitavel: 0 };
    const statusLabel = rep ? (rep.aprovado ? "APROVADO" : "REPROVADO") : "ERRO";
    const statusClass = rep ? (rep.aprovado ? "sev-aceitavel" : "sev-critico") : "sev-critico";

    let html = `
      <div class="round-header">
        <span class="round-title">Rodada ${data.iteracao}</span>
        <div class="counts">
          <span class="sev-badge ${statusClass}">${statusLabel}</span>
          <span class="sev-badge sev-critico">Crítico: ${counts.critico}</span>
          <span class="sev-badge sev-moderado">Moderado: ${counts.moderado}</span>
          <span class="sev-badge sev-aceitavel">Aceitável: ${counts.aceitavel}</span>
        </div>
      </div>
    `;

    // Imagens / artefatos lado a lado
    const imgPreviews = (data.previews || []).filter((p) => p.url);
    if (imgPreviews.length) {
      html += `<div class="artifact-compare">`;
      for (const p of imgPreviews) {
        html += `
          <div class="artifact-thumb">
            <img src="${escapeHtml(p.url)}" alt="${escapeHtml(p.label)}" />
            <span>${escapeHtml(p.metadata)}</span>
          </div>
        `;
      }
      html += `</div>`;
    }

    // 1. Pacote cru enviado pela Ponte ao Verificador
    const removedTotal =
      (san.removed_sections ? san.removed_sections.length : 0) +
      (san.removed_code_blocks || 0) +
      (san.removed_commentary_lines || 0) +
      (san.rejected_files ? san.rejected_files.length : 0);

    html += `
      <div class="bubble bubble-bridge">
        <div class="bubble-head">
          <span>🛡️ PONTE CEGA → IA VERIFICADORA (Pacote Cru)</span>
          <span>${removedTotal} item(ns) filtrado(s)</span>
        </div>
        <pre class="code-box">${escapeHtml(data.raw_packet_text || "")}</pre>
        <details>
          <summary>Ver auditoria local do que foi removido/bloqueado (não enviado ao verificador)</summary>
          <pre class="code-box">${escapeHtml(JSON.stringify(san, null, 2))}</pre>
        </details>
      </div>
    `;

    // 2. Resposta da IA Verificadora
    if (data.erro) {
      html += `
        <div class="bubble bubble-verifier">
          <div class="bubble-head"><span>🔍 IA VERIFICADORA · ERRO</span></div>
          <div>${escapeHtml(data.erro)}</div>
        </div>
      `;
    } else if (rep) {
      let critHtml = "";
      if (rep.criterios && rep.criterios.length) {
        critHtml = `<div class="criteria-list">` + rep.criterios.map((c) => {
          const cls = c.atendido ? "crit-pass" : "crit-fail";
          const icon = c.atendido ? "✓" : "✗";
          return `<span class="crit-pill ${cls}">${icon} ${escapeHtml(c.id)} (${escapeHtml(c.peso)}): ${escapeHtml(c.requisito)}</span>`;
        }).join("") + `</div>`;
      }

      let findingsHtml = "";
      if (rep.falhas && rep.falhas.length) {
        findingsHtml = `<div class="finding-list">` + rep.falhas.map((f) => `
          <div class="finding-item">
            <div class="finding-top">
              <span class="sev-badge sev-${escapeHtml(f.severidade)}">${escapeHtml(f.severidade)}</span>
              <strong>${escapeHtml(f.acao)}</strong>
              ${f.evidencia ? `<code>${escapeHtml(f.evidencia)}</code>` : ""}
            </div>
            <div class="finding-meta">
              <strong>Observado:</strong> ${escapeHtml(f.observado)} ·
              <strong>Esperado:</strong> ${escapeHtml(f.esperado)}
            </div>
          </div>
        `).join("") + `</div>`;
      } else {
        findingsHtml = `<p class="finding-meta">Nenhuma falha encontrada. Pedido atendido integralmente.</p>`;
      }

      html += `
        <div class="bubble bubble-verifier">
          <div class="bubble-head"><span>🔍 IA VERIFICADORA → PONTE</span></div>
          ${critHtml}
          ${findingsHtml}
        </div>
      `;
    }

    // 3. Prompt enviado de volta para a IA Criadora
    if (data.prompt_correcao && rep && !rep.aprovado) {
      html += `
        <div class="bubble bubble-creator">
          <div class="bubble-head">
            <span>🤖 PONTE → IA CRIADORA (Prompt Direto de Correção)</span>
            <button class="btn btn-ghost btn-sm btn-copy-prompt" type="button">Copiar prompt</button>
          </div>
          <pre class="code-box">${escapeHtml(data.prompt_correcao)}</pre>
        </div>
      `;
    }

    card.innerHTML = html;

    const copyBtn = card.querySelector(".btn-copy-prompt");
    if (copyBtn && data.prompt_correcao) {
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(data.prompt_correcao);
          copyBtn.textContent = "Copiado!";
          setTimeout(() => { copyBtn.textContent = "Copiar prompt"; }, 1500);
        } catch {
          copyBtn.textContent = "Falha ao copiar";
        }
      });
    }

    els.timeline.appendChild(card);
    card.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  function finishRun(result) {
    hideChatPanel();
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    state.currentRunId = null;
    state.lastResult = result;
    setRunningUI(false);
    els.btnExport.classList.remove("hidden");

    if (result.aprovado) {
      setStatus("approved", "APROVADO", `Concluído em ${result.total_iteracoes} rodada(s).`);
      els.finalBanner.className = "banner banner-approved";
      els.finalBanner.textContent =
        `✓ Verificação APROVADA após ${result.total_iteracoes} rodada(s) — sem erros críticos nem moderados.`;
      els.finalBanner.classList.remove("hidden");
    } else {
      const motivo = result.motivo_parada || "encerrado";
      setStatus("failed", `PARADO (${motivo})`, result.erro || "");
      els.finalBanner.className = "banner banner-warning";
      els.finalBanner.textContent =
        `Loop encerrado (${motivo}) após ${result.total_iteracoes} rodada(s). ${result.erro || ""}`.trim();
      els.finalBanner.classList.remove("hidden");
    }
  }

  async function previewFilter() {
    hideBanners();
    try {
      const resp = await fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectPayload()),
      });
      const data = await resp.json();
      if (!resp.ok) {
        showError(data.error || "Falha ao testar filtro.");
        return;
      }
      els.previewPacket.textContent = data.raw_packet_text || "";
      els.previewAudit.textContent = JSON.stringify(data.sanitization || {}, null, 2);
      els.previewPanel.classList.remove("hidden");
    } catch (err) {
      showError(`Erro ao consultar filtro: ${err}`);
    }
  }

  async function startConversation() {
    hideBanners();
    els.previewPanel.classList.add("hidden");
    els.btnExport.classList.add("hidden");
    els.timeline.innerHTML = "";
    state.lastResult = null;

    setRunningUI(true);
    setStatus("running", "Iniciando...", "Preparando ponte entre as duas IAs...");

    try {
      const resp = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectPayload()),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setRunningUI(false);
        setStatus("failed", "Erro na entrada", data.error || "");
        showError(data.error || "Não foi possível iniciar.");
        return;
      }

      state.currentRunId = data.run_id;
      const es = new EventSource(`/api/runs/${data.run_id}/events`);
      state.eventSource = es;

      es.addEventListener("status", (ev) => {
        const payload = JSON.parse(ev.data);
        const phaseLabels = {
          sanitizing: "Filtrando entrada",
          verifying: "IA 2 Verificando",
          revising: "IA 1 Corrigindo",
          cancelling: "Parando...",
        };
        setStatus("running", phaseLabels[payload.phase] || "Em andamento", payload.message);
      });

      es.addEventListener("awaiting_input", (ev) => {
        const req = JSON.parse(ev.data);
        setStatus("running", "Aguardando você", `Leve o pedido ao ${req.chat_label} e cole a resposta.`);
        showChatPanel(req);
      });

      es.addEventListener("input_received", () => {
        hideChatPanel();
      });

      es.addEventListener("iteration", (ev) => {
        renderRound(JSON.parse(ev.data));
      });

      es.addEventListener("done", (ev) => {
        finishRun(JSON.parse(ev.data));
      });

      es.onerror = () => {
        if (state.currentRunId) {
          es.close();
          setRunningUI(false);
          setStatus("failed", "Conexão perdida", "O servidor local parou de responder.");
        }
      };
    } catch (err) {
      setRunningUI(false);
      setStatus("failed", "Erro", String(err));
      showError(`Falha ao iniciar conversa: ${err}`);
    }
  }

  async function stopConversation() {
    if (!state.currentRunId) return;
    try {
      await fetch(`/api/runs/${state.currentRunId}/stop`, { method: "POST" });
    } catch {
      // ignora se já encerrou
    }
  }

  function exportReport() {
    if (!state.lastResult) return;
    const blob = new Blob([JSON.stringify(state.lastResult, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "verify-bridge-relatorio.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function init() {
    try {
      const resp = await fetch("/api/env");
      const info = await resp.json();
      state.envKeys = info.env_keys || {};
      state.defaults = info.defaults || {};
    } catch {
      // segue com defaults vazios
    }

    els.creatorProvider.addEventListener("change", () => {
      els.creatorModel.value = "";
      updateAgentFields("creator");
    });
    els.verifierProvider.addEventListener("change", () => {
      els.verifierModel.value = "";
      updateAgentFields("verifier");
    });

    els.mdFileInput.addEventListener("change", async () => {
      const file = els.mdFileInput.files && els.mdFileInput.files[0];
      if (file) {
        els.markdownInput.value = await file.text();
      }
      els.mdFileInput.value = "";
    });

    els.refFilesInput.addEventListener("change", () =>
      handleFileUpload(els.refFilesInput, state.referenceFiles, els.refFilesList, "ref")
    );
    els.outFilesInput.addEventListener("change", () =>
      handleFileUpload(els.outFilesInput, state.deliverableFiles, els.outFilesList, "out")
    );

    els.btnPreview.addEventListener("click", previewFilter);
    els.btnStart.addEventListener("click", startConversation);
    els.btnStop.addEventListener("click", stopConversation);
    els.btnExport.addEventListener("click", exportReport);
    els.btnClosePreview.addEventListener("click", () => els.previewPanel.classList.add("hidden"));
    els.btnClear.addEventListener("click", () => {
      els.markdownInput.value = "";
      state.referenceFiles = [];
      state.deliverableFiles = [];
      renderFileChips(els.refFilesList, state.referenceFiles, "ref");
      renderFileChips(els.outFilesList, state.deliverableFiles, "out");
      els.previewPanel.classList.add("hidden");
      hideBanners();
    });

    ["creator", "verifier"].forEach((role) => {
      $(`${role}-sub-login`).addEventListener("click", () => startSubLogin(role));
      $(`${role}-sub-refresh`).addEventListener("click", () => loadSubscriptions(true));
    });
    els.chatCopy.addEventListener("click", copyChatPrompt);
    els.chatSend.addEventListener("click", sendChatReply);

    updateAgentFields("creator");
    updateAgentFields("verifier");
    loadSubscriptions();
  }

  init();
})();
