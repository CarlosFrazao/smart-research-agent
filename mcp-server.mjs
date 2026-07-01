#!/usr/bin/env node
// Ponte STDIO-to-SSE para conectar a IDE (STDIO) ao Smart Research Agent rodando no Docker (SSE)

import readline from 'node:readline';

const SSE_URL = 'http://127.0.0.1:3458/mcp/sse';
const HOST = 'http://127.0.0.1:3458';
const MAX_BUFFER_SIZE = 50;

async function main() {
  let postUrl = null;
  const pendingMsgs = [];

  async function sendPost(url, payload) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
      });
      if (!res.ok) {
        console.error(`[RESEARCH BRIDGE] Falha ao enviar: HTTP ${res.status}`);
      }
    } catch (err) {
      console.error(`[RESEARCH BRIDGE] Erro de rede ao enviar: ${err.message}`);
    }
  }

  async function connectSSE(attempt = 1) {
    postUrl = null;
    console.error(`[RESEARCH BRIDGE] Conectando ao Smart Research Agent SSE (Tentativa ${attempt})...`);

    try {
      const response = await fetch(SSE_URL);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      console.error('[RESEARCH BRIDGE] Conexao SSE estabelecida com sucesso!');
      attempt = 1;

      let buffer = '';
      const decoder = new TextDecoder('utf-8');

      for await (const chunk of response.body) {
        buffer += decoder.decode(chunk, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;

          if (trimmed.startsWith('data:')) {
            const dataContent = trimmed.slice(5).trim();
            if (!postUrl && dataContent.startsWith('/') && dataContent.includes('messages')) {
              postUrl = `${HOST}${dataContent}`;
              console.error(`[RESEARCH BRIDGE] Sessao mapeada. postUrl: ${postUrl}`);
              while (pendingMsgs.length > 0) {
                sendPost(postUrl, pendingMsgs.shift());
              }
            } else if (dataContent) {
              process.stdout.write(dataContent + '\n');
            }
          }
        }
      }
    } catch (err) {
      console.error(`[RESEARCH BRIDGE] Desconectado: ${err.message}`);
    }

    const delay = Math.min(15000, 1000 * Math.pow(2, attempt - 1)) + Math.random() * 1000;
    await new Promise(resolve => setTimeout(resolve, delay));
    connectSSE(attempt + 1);
  }

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false,
  });

  rl.on('line', (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    if (postUrl) {
      sendPost(postUrl, trimmed);
    } else {
      if (pendingMsgs.length >= MAX_BUFFER_SIZE) pendingMsgs.shift();
      pendingMsgs.push(trimmed);
    }
  });

  rl.on('close', () => process.exit(0));

  connectSSE();
}

main().catch(err => {
  console.error('[RESEARCH BRIDGE] Falha critica:', err);
  process.exit(1);
});
