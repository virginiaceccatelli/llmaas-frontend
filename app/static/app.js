/* Shared helpers. Every page loads this before its own inline script.
   Nothing here knows the broker's address: the BFF at this same origin is
   the only thing the browser ever talks to. */

let ME = null;

/* The CSRF token is fetched with the session, never stored in localStorage:
   it should live exactly as long as the tab does. */
export async function me() {
  const r = await fetch('/api/me', { headers: { 'Accept': 'application/json' } });
  ME = await r.json();
  return ME;
}

export async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'Accept': 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && ME && ME.csrf) headers['X-CSRF-Token'] = ME.csrf;

  const r = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) {           // session expired under us
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('not signed in');
  }
  const text = await r.text();
  const data = text ? JSON.parse(text) : null;
  if (!r.ok) throw new Error(detail(data) || `HTTP ${r.status}`);
  return data;
}

export function detail(data) {
  if (!data) return '';
  if (typeof data.detail === 'string') return data.detail;
  // FastAPI validation errors arrive as a list of objects.
  if (Array.isArray(data.detail)) {
    return data.detail.map((e) => `${(e.loc || []).slice(1).join('.')}: ${e.msg}`).join('; ');
  }
  return '';
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

export function toast(el, message, kind = '') {
  if (!el) return;
  el.textContent = message;
  el.className = 'banner ' + kind + (message ? '' : ' hidden');
}

/* Inline SVG, not glyphs: symbols like ⚿ and ⏻ have no coverage in the
   default Windows UI font and render as empty boxes. */
const svg = (d) =>
  `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

const ICON = {
  chat: svg('<path d="M14 7.5c0 2.9-2.7 5.2-6 5.2-.8 0-1.6-.1-2.3-.3L2 13.5l1.3-2.4C2.5 10.2 2 8.9 2 7.5 2 4.6 4.7 2.3 8 2.3s6 2.3 6 5.2Z"/>'),
  key: svg('<circle cx="5.6" cy="10.4" r="2.9"/><path d="m7.7 8.3 5.6-5.6M11.2 4.8l1.6 1.6M13.3 2.7l1.5 1.5"/>'),
  usage: svg('<path d="M3 13.2V9M8 13.2V3.4M13 13.2V6.6"/>'),
  compose: svg('<path d="M13 7.8v4A1.7 1.7 0 0 1 11.3 13.5h-7A1.7 1.7 0 0 1 2.6 11.8v-7A1.7 1.7 0 0 1 4.3 3h4"/><path d="m11.6 2.3 2.1 2.1-4.9 4.9-2.6.5.5-2.6 4.9-4.9Z"/>'),
  out: svg('<path d="M6.2 13.6H3.7a1.7 1.7 0 0 1-1.7-1.7V4.1a1.7 1.7 0 0 1 1.7-1.7h2.5"/><path d="m10.6 11.2 3.4-3.2-3.4-3.2M14 8H6.1"/>'),
};

/* --- the rail ----------------------------------------------------------
   Rendered here rather than repeated in four HTML files. `withChats` adds
   the conversation list, which only the chat page has any use for. */
export function mountShell({ user, withChats = false }) {
  const tab = (href, ico, text) =>
    `<a href="${href}"${href === location.pathname ? ' aria-current="page"' : ''}>` +
    `<span class="ico">${ICON[ico]}</span>${text}</a>`;

  document.getElementById('rail').innerHTML = `
    <div class="rail-head">
      <a class="brand" href="/">
        <span class="mark">W</span>
        <span class="name">WIIT <b>LLMaaS</b></span>
      </a>
      <span class="spacer"></span>
      ${withChats ? `<button class="icon-btn" id="newchat" title="New chat">${ICON.compose}</button>` : ''}
    </div>

    <nav class="nav">
      ${tab('/', 'chat', 'Chat')}
      ${tab('/keys', 'key', 'API keys')}
      ${tab('/usage', 'usage', 'Usage')}
    </nav>

    ${withChats ? `
      <div class="chats" id="chats">
        <div class="rail-label">Conversations</div>
        <div id="chatlist"></div>
      </div>` : '<div class="spacer"></div>'}

    <div class="rail-foot">
      <span class="avatar-sm">${esc((user || '?').slice(0, 2))}</span>
      <span class="who" title="${esc(user || '')}">${esc(user || '')}</span>
      <button class="icon-btn" id="signout" title="Sign out">${ICON.out}</button>
    </div>`;

  document.getElementById('signout').onclick = async () => {
    try { await api('/api/logout', { method: 'POST' }); } finally { location.href = '/login'; }
  };
}

/* --- conversation store ------------------------------------------------
   Chat history lives on the SERVER, one history per user, reached through the
   BFF at /api/conversations. It used to live in this browser's localStorage,
   which is per-BROWSER and not per-user: two people signing in to the same
   browser shared one history, and one person on two devices had two. Neither
   is a multi-tenant product, so it moved.

   That means the platform now stores prompt and completion text — see the
   comment above `conversations` in db/init.sql for what that obliges you to
   do. The inference path still logs nothing.

   Every method that touches the server is async. `newId` and `title` are pure
   and stay synchronous. */
export const store = {
  async all() {
    try {
      return await api('/api/conversations');
    } catch {
      // Signed out, or the broker is down. An empty rail is the right
      // degradation: never invent history.
      return [];
    }
  },
  async get(id) {
    try {
      return await api(`/api/conversations/${encodeURIComponent(id)}`);
    } catch { return null; }
  },
  async upsert(chat) {
    // Whole-array replace, so a retried save cannot duplicate a turn.
    return api(`/api/conversations/${encodeURIComponent(chat.id)}`, {
      method: 'PUT',
      body: { title: chat.title, messages: chat.messages },
    });
  },
  async remove(id) {
    return api(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
  },
  newId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 7); },
  title(messages) {
    const first = messages.find((m) => m.role === 'user');
    if (!first) return 'New chat';
    const t = first.content.replace(/\s+/g, ' ').trim();
    return t.length > 42 ? t.slice(0, 42) + '…' : t;
  },
};

/* Reads an SSE stream from fetch(). onEvent gets each parsed `data:` payload;
   `[DONE]` ends it. This is the standard OpenAI stream, relayed untouched by
   the BFF, so nothing here is specific to our broker. */
export async function readSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop();                 // keep the partial frame
    for (const frame of frames) {
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') return;
        try { onEvent(JSON.parse(payload)); } catch { /* keepalive or noise */ }
      }
    }
  }
}
