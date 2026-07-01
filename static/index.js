// SRA Dashboard v2.0 — Lógica principal
// Cobre: Pesquisa, Chat, Histórico, Feedback, Obsidian Sync, Anexo, Exportação, API Keys

// ─── Estado Global ────────────────────────────────────────────────────────────
const state = {
  mode: 'research',
  currentReport: null,
  currentQuery: null,
  chatMessages: [],
  isLoading: false,
  pendingAttachment: null,
};

// ─── Helpers de Settings (localStorage) ──────────────────────────────────────
function getStoredSettings() {
  return {
    api_key: localStorage.getItem('sra_api_key') || null,
    provider: localStorage.getItem('sra_provider') || null,
    model: localStorage.getItem('sra_model') || null,
  };
}

// ─── Inicialização ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadReportsList();
  checkAgentHealth();
  loadSettingsIntoUI();

  document.getElementById('search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      startResearch();
    }
  });
});

function loadSettingsIntoUI() {
  const saved = getStoredSettings();
  if (saved.model) {
    const sel = document.getElementById('model-select');
    const exists = Array.from(sel.options).some(o => o.value === saved.model);
    if (!exists) {
      const opt = document.createElement('option');
      opt.value = saved.model;
      opt.textContent = saved.model;
      sel.appendChild(opt);
    }
    sel.value = saved.model;
  }
}

// ─── Health Check ─────────────────────────────────────────────────────────────
async function checkAgentHealth() {
  try {
    const res = await fetch('/health');
    const data = await res.json();
    const dot = document.getElementById('status-dot');
    const label = document.getElementById('status-label');
    if (data.status === 'ok') {
      dot.style.background = '#22c55e';
      label.textContent = 'Online';
    } else {
      dot.style.background = '#f59e0b';
      label.textContent = 'Degradado';
    }
  } catch {
    document.getElementById('status-dot').style.background = '#ef4444';
    document.getElementById('status-label').textContent = 'Offline';
  }
}

// ─── Modo: Pesquisa ARES ──────────────────────────────────────────────────────
async function startResearch() {
  if (state.isLoading) return;
  const query = document.getElementById('search-input').value.trim();
  if (!query) { flashInput(); return; }

  state.isLoading = true;
  state.currentQuery = query;

  showProgressBar();
  hideReport();
  setProgressStep(1, 'Analisando intenção da query...');

  const progressInterval = simulateProgress();

  try {
    const settings = getStoredSettings();
    const resBody = { query };
    if (settings.api_key) resBody.api_key = settings.api_key;
    if (settings.provider) resBody.provider = settings.provider;

    const res = await fetch('/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(resBody),
    });

    clearInterval(progressInterval);

    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    const data = await res.json();

    if (data.error) throw new Error(data.error);

    setProgressStep(9, 'Relatório gerado com sucesso!');
    await sleep(400);

    state.currentReport = data.report;
    renderReport(data.report);
    loadReportsList();
  } catch (err) {
    clearInterval(progressInterval);
    showError(`Erro na pesquisa: ${err.message}`);
  } finally {
    state.isLoading = false;
    hideProgressBar();
  }
}

function simulateProgress() {
  const steps = [
    { step: 1, label: 'Analisando intenção da query...' },
    { step: 2, label: 'Expandindo queries (8-12 variações)...' },
    { step: 3, label: 'Planejando fontes por prioridade...' },
    { step: 4, label: 'Buscando em paralelo (GitHub, Reddit, HN, ArXiv)...' },
    { step: 5, label: 'Ranqueando e filtrando resultados...' },
    { step: 6, label: 'Detectando gaps de informação...' },
    { step: 7, label: 'Re-pesquisando gaps encontrados...' },
    { step: 8, label: 'Sintetizando entidades e dados...' },
  ];
  let i = 0;
  return setInterval(() => {
    if (i < steps.length) {
      setProgressStep(steps[i].step, steps[i].label);
      i++;
    }
  }, 11000);
}

function renderReport(markdown) {
  const container = document.getElementById('report-container');
  const content = document.getElementById('report-content');

  marked.setOptions({ breaks: true, gfm: true });
  content.innerHTML = marked.parse(markdown);

  if (window.hljs) {
    content.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
  }

  container.classList.remove('hidden');
  container.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ─── Modo: Chat Direto ────────────────────────────────────────────────────────
async function sendChatMessage() {
  if (state.isLoading) return;
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;

  input.value = '';

  let userContent = message;
  let displayMessage = message;

  if (state.pendingAttachment) {
    const att = state.pendingAttachment;
    userContent = `[Documento Anexado: ${att.name}]\n"""\n${att.content}\n"""\n\nMensagem do Usuário: ${message}`;
    displayMessage = `📎 **${escapeHtml(att.name)}**\n\n${message}`;
    clearAttachment();
  }

  addChatMessage('user', displayMessage);

  const model = document.getElementById('model-select').value;
  state.chatMessages.push({ role: 'user', content: userContent });
  state.isLoading = true;

  const assistantBubble = addChatMessage('assistant', '');
  assistantBubble.textContent = '...';

  const settings = getStoredSettings();
  const chatBody = {
    model: model === 'auto' ? undefined : model,
    messages: state.chatMessages,
    system_prompt: 'Você é um assistente de pesquisa especializado. Responda em PT-BR com precisão e objetividade.',
  };
  if (settings.api_key) chatBody.api_key = settings.api_key;
  if (settings.provider) chatBody.provider = settings.provider;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(chatBody),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let fullResponse = '';

    assistantBubble.textContent = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.decode(value, { stream: true });
      const lines = text.split('\n');
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const chunk = line.slice(6);
        if (chunk === '[DONE]') break;
        if (chunk.startsWith('[ERROR]')) {
          assistantBubble.textContent = chunk;
          break;
        }
        fullResponse += chunk;
        assistantBubble.innerHTML = marked.parse(fullResponse);
        scrollChatToBottom();
      }
    }

    state.chatMessages.push({ role: 'assistant', content: fullResponse });
  } catch (err) {
    assistantBubble.textContent = `Erro: ${err.message}`;
  } finally {
    state.isLoading = false;
    scrollChatToBottom();
  }
}

function addChatMessage(role, content) {
  const container = document.getElementById('chat-messages');
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}`;
  if (content) bubble.innerHTML = marked.parse(content);
  container.appendChild(bubble);
  scrollChatToBottom();
  return bubble;
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
}

// ─── Histórico de Relatórios ──────────────────────────────────────────────────
async function loadReportsList() {
  try {
    const res = await fetch('/api/reports');
    const data = await res.json();
    const list = document.getElementById('reports-list');

    if (!data.reports || data.reports.length === 0) {
      list.innerHTML = '<div class="no-reports">Nenhum relatório ainda</div>';
      return;
    }

    list.innerHTML = data.reports.map(r => {
      const name = formatReportName(r.filename);
      return `<div class="report-item" onclick="loadReport('${escapeAttr(r.filename)}')" title="${escapeAttr(r.filename)}">
        <span class="report-icon">📄</span>
        <span class="report-name">${escapeHtml(name)}</span>
      </div>`;
    }).join('');
  } catch {
    document.getElementById('reports-list').innerHTML = '<div class="no-reports">Erro ao carregar</div>';
  }
}

async function loadReport(filename) {
  try {
    const res = await fetch(`/api/reports/${encodeURIComponent(filename)}`);
    if (!res.ok) throw new Error('Relatório não encontrado');
    const markdown = await res.text();
    state.currentReport = markdown;
    state.currentQuery = filename;
    setMode('research');
    hideProgressBar();
    renderReport(markdown);
  } catch (err) {
    showError(`Erro ao carregar relatório: ${err.message}`);
  }
}

function formatReportName(filename) {
  return filename
    .replace(/^\d{4}-\d{2}-\d{2}-/, '')
    .replace(/\.md$/, '')
    .replace(/-/g, ' ')
    .substring(0, 32);
}

// ─── Feedback ─────────────────────────────────────────────────────────────────
async function sendFeedback(type) {
  if (!state.currentQuery) return;
  try {
    const signal = type === 'positive' ? 'helpful' : 'not_helpful';
    await fetch('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: state.currentQuery, signal }),
    });
    const btnId = type === 'positive' ? 'btn-feedback-up' : 'btn-feedback-down';
    flashButton(btnId);
  } catch { /* silently fail */ }
}

// ─── Obsidian Sync ─────────────────────────────────────────────────────────────
async function saveToObsidian() {
  if (!state.currentQuery) { alert('Nenhum relatório ativo.'); return; }
  try {
    const res = await fetch('/api/obsidian-sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: state.currentQuery }),
    });
    const data = await res.json();
    if (data.synced) {
      alert(`Salvo no Obsidian:\n${data.destination}`);
    } else {
      alert(`Falha ao salvar no Obsidian.\n${data.error || 'Verifique OBSIDIAN_VAULT_PATH no .env'}`);
    }
  } catch { alert('Sem conexão com o backend.'); }
}

// ─── Utilitários UI ───────────────────────────────────────────────────────────
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById(`panel-${mode}`).classList.add('active');
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(`btn-${mode}`).classList.add('active');
}

function showProgressBar() {
  document.getElementById('progress-bar-container').classList.remove('hidden');
}

function hideProgressBar() {
  document.getElementById('progress-bar-container').classList.add('hidden');
}

function hideReport() {
  document.getElementById('report-container').classList.add('hidden');
}

function setProgressStep(step, label) {
  document.querySelectorAll('.step').forEach(s => {
    const n = parseInt(s.dataset.step, 10);
    s.className = 'step' + (n < step ? ' done' : n === step ? ' active' : '');
  });
  const pct = Math.round((step / 9) * 100);
  document.getElementById('progress-fill').style.width = `${pct}%`;
  document.getElementById('progress-label').textContent = label;
}

function showError(msg) {
  const container = document.getElementById('report-container');
  container.classList.remove('hidden');
  document.getElementById('report-content').innerHTML =
    `<div class="error-box">⚠️ ${escapeHtml(msg)}</div>`;
}

function suggestQuery(q) {
  document.getElementById('search-input').value = q;
  document.getElementById('search-input').focus();
}

function copyReport() {
  if (state.currentReport) {
    navigator.clipboard.writeText(state.currentReport).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = state.currentReport;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    });
  }
}

function flashInput() {
  const input = document.getElementById('search-input');
  const box = document.getElementById('search-box');
  box.style.borderColor = '#ef4444';
  setTimeout(() => { box.style.borderColor = ''; }, 700);
  input.focus();
}

function flashButton(id) {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.style.transform = 'scale(1.25)';
  btn.style.borderColor = '#22c55e';
  setTimeout(() => {
    btn.style.transform = '';
    btn.style.borderColor = '';
  }, 400);
}

function scrollChatToBottom() {
  const container = document.getElementById('chat-messages');
  container.scrollTop = container.scrollHeight;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttr(str) {
  return String(str).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ─── Anexo de Arquivo (BLOCO 2) ───────────────────────────────────────────────
function handleFileAttach(event) {
  const file = event.target.files[0];
  if (!file) return;
  event.target.value = '';

  const reader = new FileReader();
  reader.onload = (e) => {
    state.pendingAttachment = { name: file.name, content: e.target.result };
    renderAttachmentPreview(file.name);
  };
  reader.onerror = () => showAttachmentError(file.name);
  reader.readAsText(file, 'utf-8');
}

function renderAttachmentPreview(filename) {
  const preview = document.getElementById('attachment-preview');
  preview.classList.remove('hidden');
  preview.innerHTML = `
    <span class="attachment-chip">
      📎 <strong>${escapeHtml(filename)}</strong>
      <button class="attachment-remove" onclick="clearAttachment()" title="Remover anexo">✕</button>
    </span>`;
}

function clearAttachment() {
  state.pendingAttachment = null;
  const preview = document.getElementById('attachment-preview');
  preview.classList.add('hidden');
  preview.innerHTML = '';
}

function showAttachmentError(filename) {
  const preview = document.getElementById('attachment-preview');
  preview.classList.remove('hidden');
  preview.innerHTML = `<span class="attachment-chip error">⚠️ Falha ao ler ${escapeHtml(filename)} <button class="attachment-remove" onclick="clearAttachment()">✕</button></span>`;
}

// ─── Exportação (BLOCO 3) ─────────────────────────────────────────────────────
function downloadReport() {
  if (!state.currentReport) return;
  const slug = (state.currentQuery || 'relatorio').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 60);
  const blob = new Blob([state.currentReport], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${slug}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function downloadChat() {
  if (state.chatMessages.length === 0) { alert('Nenhuma mensagem na conversa.'); return; }
  let text = `# Histórico de Conversa — SRA Dashboard\n\n`;
  text += `> Exportado em ${new Date().toLocaleString('pt-BR')}\n\n---\n\n`;
  state.chatMessages.forEach(msg => {
    const role = msg.role === 'user' ? 'Você' : 'Assistente';
    text += `### ${role}:\n${msg.content}\n\n---\n\n`;
  });
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `sra-conversa-${new Date().toISOString().slice(0, 10)}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ─── Modal de Settings (BLOCO 4) ─────────────────────────────────────────────
function openSettings() {
  const saved = getStoredSettings();
  document.getElementById('settings-provider').value = saved.provider || '';
  document.getElementById('settings-api-key').value = saved.api_key || '';
  document.getElementById('settings-model').value = saved.model || '';
  document.getElementById('settings-modal').classList.remove('hidden');
}

function closeSettings() {
  document.getElementById('settings-modal').classList.add('hidden');
}

function saveSettings() {
  const provider = document.getElementById('settings-provider').value.trim();
  const apiKey = document.getElementById('settings-api-key').value.trim();
  const model = document.getElementById('settings-model').value.trim();

  if (apiKey) {
    localStorage.setItem('sra_api_key', apiKey);
  } else {
    localStorage.removeItem('sra_api_key');
  }

  if (provider) {
    localStorage.setItem('sra_provider', provider);
  } else {
    localStorage.removeItem('sra_provider');
  }

  if (model) {
    localStorage.setItem('sra_model', model);
    const sel = document.getElementById('model-select');
    const exists = Array.from(sel.options).some(o => o.value === model);
    if (!exists) {
      const opt = document.createElement('option');
      opt.value = model;
      opt.textContent = model;
      sel.appendChild(opt);
    }
    sel.value = model;
  } else {
    localStorage.removeItem('sra_model');
  }

  closeSettings();
}
