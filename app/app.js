/*
  D.I.A.B.L.O. — the app.

  React from local files, htm instead of JSX, no build step and no node_modules.
  That is not minimalism for its own sake: this window has to open instantly
  from a cold Mac and keep working with no network, and a toolchain buys
  neither.

  The whole UI is driven by one server-sent-events stream. Nothing polls except
  a slow safety-net refresh, because a voice assistant's state changes in
  bursts — three transitions in two seconds, then nothing for ten minutes — and
  polling either misses the burst or wastes the silence.
*/

import { mascot, AGENTS, byId, agentForTool } from './mascot.js';

const { createElement, useState, useEffect, useRef, useCallback, useMemo, memo } = React;
const html = htm.bind(createElement);
const root = ReactDOM.createRoot(document.getElementById('root'));

const api = {
  async get(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${path} -> ${response.status}`);
    return response.json();
  },
  async post(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return response.json().catch(() => ({}));
  },
};

/* ─── Icons ──────────────────────────────────────────────────────────────────
   Inline SVG, never emoji. Emoji render differently on every OS, cannot be
   recoloured, and read out loud by a screen reader as their unicode name. */

const Icon = memo(({ name, size = 18 }) => {
  const paths = {
    send: 'M3.4 20.4 20.85 12 3.4 3.6 3.4 10.2 15.4 12 3.4 13.8Z',
    mic: 'M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-3.1A7 7 0 0 0 19 11Z',
    stop: 'M6 6h12v12H6z',
    trash: 'M6 7h12l-1 13H7L6 7Zm3-3h6l1 2H8l1-2Z',
  };
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24"
      fill="currentColor" aria-hidden="true" focusable="false">
      <path d=${paths[name]} /></svg>`;
});

/* ─── The character ──────────────────────────────────────────────────────────
   dangerouslySetInnerHTML is safe here and nowhere else in this file: the
   markup comes from mascot.js, which builds it from a fixed template and a
   colour, never from anything a user or a model produced. */

const Mascot = memo(({ agent, size = 96, wave = true }) => {
  const svg = useMemo(
    () => mascot({ accent: agent.accent, eye: agent.eye, mouth: agent.mouth, size, wave }),
    [agent.accent, agent.eye, agent.mouth, size, wave]);
  return html`<div class="mascot" style=${{ width: size, height: size }}
      dangerouslySetInnerHTML=${{ __html: svg }} />`;
});

/* ─── Title bar ──────────────────────────────────────────────────────────── */

const STATE_WORDS = {
  cold: 'Asleep', init: 'Waking', idle: 'Listening for you',
  listening: 'Listening', processing: 'Thinking', responding: 'Speaking',
  error: 'Something broke', shutdown: 'Shutting down',
};

const TABS = [
  ['talk', 'Talk'], ['agents', 'Agents'], ['trace', 'Trace'], ['health', 'Health'],
  ['settings', 'Settings'],
];

const TitleBar = memo(({ state, tab, setTab, busy }) => html`
  <header class="titlebar">
    <span class="wordmark">DIABLO</span>
    <nav class="tabs" role="tablist" aria-label="Sections">
      ${TABS.map(([id, label]) => html`
        <button key=${id} class="tab" role="tab" aria-selected=${tab === id}
                onClick=${() => setTab(id)}>
          ${label}
          ${id === 'agents' && busy > 0
            ? html`<span class="badge" aria-label=${`${busy} running`}>${busy}</span>` : null}
        </button>`)}
    </nav>
    <span class="spacer" />
    <div class="state" data-state=${state} role="status" aria-live="polite">
      <span class="dot" />
      ${STATE_WORDS[state] || state}
    </div>
  </header>`);

/* ─── Talk ───────────────────────────────────────────────────────────────── */

const Turn = memo(({ role, content }) => html`
  <div class=${`turn ${role === 'user' ? 'you' : ''}`}>
    <div>
      <div class="who">${role === 'user' ? 'You' : 'Diablo'}</div>
      <div class="bubble">${content}</div>
    </div>
  </div>`);

function Talk({ turns, state, onSend, onInterrupt, onReset }) {
  const [draft, setDraft] = useState('');
  const areaRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }); }, [turns.length]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    onSend(text);
    setDraft('');
    // Reset the auto-grow, or the box stays tall after a long message.
    if (areaRef.current) areaRef.current.style.height = 'auto';
  }, [draft, onSend]);

  const onKeyDown = useCallback((event) => {
    // Enter sends, Shift+Enter is a newline. The opposite of a chat app is
    // always wrong and people have muscle memory for this one.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }, [send]);

  const grow = useCallback((event) => {
    setDraft(event.target.value);
    const el = event.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
  }, []);

  const speaking = state === 'responding';

  return html`
    <${React.Fragment}>
      <div class="pane">
        ${turns.length === 0
          ? html`
            <div class="empty">
              <${Mascot} agent=${byId.diablo} size=${120} />
              <div>
                <h2>At your service.</h2>
                <p>Say <strong>"Hey Diablo"</strong> out loud, or type below.
                   Everything runs on this Mac.</p>
              </div>
            </div>`
          : html`
            <div class="thread">
              ${turns.map((t, i) => html`<${Turn} key=${i} role=${t.role} content=${t.content} />`)}
              <div ref=${endRef} />
            </div>`}
      </div>

      <div class="composer">
        <textarea ref=${areaRef} value=${draft} onInput=${grow} onKeyDown=${onKeyDown}
                  rows="1" placeholder="Ask Diablo something…"
                  aria-label="Message to Diablo" />
        ${speaking
          ? html`<button class="iconbtn" data-active="true" onClick=${onInterrupt}
                     title="Stop speaking" aria-label="Stop speaking">
                   <${Icon} name="stop" /></button>`
          : html`<button class="iconbtn" onClick=${send} disabled=${!draft.trim()}
                     title="Send" aria-label="Send message">
                   <${Icon} name="send" /></button>`}
        <button class="iconbtn" onClick=${onReset} title="Forget this conversation"
                aria-label="Forget this conversation"><${Icon} name="trash" /></button>
      </div>
    <//>`;
}

/* ─── Agents ─────────────────────────────────────────────────────────────── */

const AgentCard = memo(({ agent, busy, status }) => html`
  <article class="agent" data-busy=${busy} style=${{ '--accent': agent.accent }}>
    <div class="av"><${Mascot} agent=${agent} size=${46} wave=${false} /></div>
    <div>
      <div class="nm">${agent.name}</div>
      <div class="rl">${agent.role}</div>
      <div class="st">${status || agent.blurb}</div>
    </div>
  </article>`);

function Agents({ jobs, activeTool }) {
  const running = jobs.filter(j => j.status === 'running' || j.status === 'queued');
  const live = activeTool ? agentForTool(activeTool) : null;

  return html`
    <div class="pane">
      <div class="agents">
        ${AGENTS.map(agent => html`
          <${AgentCard} key=${agent.id} agent=${agent}
             busy=${live?.id === agent.id}
             status=${live?.id === agent.id ? `Running ${activeTool}` : null} />`)}
      </div>

      <div class="card" style=${{ marginTop: 'var(--s5)' }}>
        <h3>Work queue</h3>
        ${jobs.length === 0
          ? html`<div class="row"><span class="k">Nothing has been asked of it yet.</span></div>`
          : jobs.slice(0, 12).map(job => html`
            <div class="row" key=${job.id}>
              <span class="k">${job.task}</span>
              <span class="pill" data-tone=${
                job.status === 'done' ? 'ok'
                : job.status === 'interrupted' || job.status === 'error' ? 'bad' : 'warn'}>
                ${job.status}</span>
            </div>`)}
      </div>
      ${running.length > 0 ? html`
        <p style=${{ marginTop: 'var(--s3)', fontSize: 12, color: 'var(--faint)' }}>
          ${running.length} running in the background. You can keep talking.</p>` : null}
    </div>`;
}

/* ─── Trace ──────────────────────────────────────────────────────────────── */

const TraceEvent = memo(({ ev }) => {
  const detail = Object.entries(ev)
    .filter(([k, v]) => !['ts', 'turn', 'stage', 'ms'].includes(k) && v !== null && v !== '')
    .map(([k, v]) => `${k}=${v}`).join('  ');
  return html`
    <div class="ev" data-stage=${ev.stage}>
      <span class="ms">${typeof ev.ms === 'number' ? Math.round(ev.ms) + 'ms' : ''}</span>
      <span class="st">${ev.stage}</span>
      <span class="dt" title=${detail}>${detail}</span>
    </div>`;
});

function Trace({ turns }) {
  if (!turns.length) {
    return html`<div class="pane"><div class="empty">
      <h2>Nothing recorded yet.</h2>
      <p>Every decision Diablo makes is written here — what it understood,
         which guard fired, and where the time went.</p></div></div>`;
  }
  return html`
    <div class="pane">
      ${turns.slice().reverse().map((chain, i) => html`
        <div class="turnblock" key=${i}>
          <div class="head">
            ${new Date((chain[0]?.ts || 0) * 1000).toLocaleTimeString()} · turn ${chain[0]?.turn}
          </div>
          ${chain.map((ev, j) => html`<${TraceEvent} key=${j} ev=${ev} />`)}
        </div>`)}
    </div>`;
}

/* ─── Health ─────────────────────────────────────────────────────────────── */

function Health({ state }) {
  const subsystems = state.subsystems || {};
  const perms = state.permissions || {};
  return html`
    <div class="pane">
      <div class="grid two">
        ${Object.entries(subsystems).map(([name, detail]) => html`
          <div class="card" key=${name}>
            <h3>${name.replace('_', ' ')}</h3>
            <div class="row">
              <span class="k">Status</span>
              <span class="pill" data-tone=${detail.ready ? 'ok' : 'warn'}>
                ${detail.ready ? 'ready' : 'degraded'}</span>
            </div>
            ${Object.entries(detail)
              .filter(([k, v]) => k !== 'ready' && v !== null && v !== undefined && v !== '')
              .slice(0, 5)
              .map(([k, v]) => html`
                <div class="row" key=${k}>
                  <span class="k">${k.replace(/_/g, ' ')}</span>
                  <span class="v">${String(v).slice(0, 40)}</span>
                </div>`)}
          </div>`)}
      </div>

      <div class="card" style=${{ marginTop: 'var(--s4)' }}>
        <h3>Permissions</h3>
        ${Object.entries(perms).map(([name, granted]) => html`
          <div class="row" key=${name}>
            <span class="k">${name}</span>
            <span class="pill" data-tone=${granted ? 'ok' : 'bad'}>
              ${granted ? 'granted' : 'denied'}</span>
          </div>`)}
        ${perms.accessibility === false ? html`
          <p style=${{ marginTop: 'var(--s2)', fontSize: 12, color: 'var(--faint)' }}>
            Accessibility is only needed to point at buttons that have no text.
            Everything else works without it.</p>` : null}
      </div>
    </div>`;
}

/* ─── Settings ───────────────────────────────────────────────────────────── */

function Settings({ settings, onSave }) {
  const [draft, setDraft] = useState(() =>
    Object.fromEntries(Object.entries(settings).map(([k, v]) => [k, v.value])));
  const [saved, setSaved] = useState('');

  const save = useCallback(async () => {
    const result = await onSave(draft);
    setSaved(result?.needs_restart?.length
      ? `Saved. Restart needed for: ${result.needs_restart.join(', ')}`
      : 'Saved.');
    setTimeout(() => setSaved(''), 4000);
  }, [draft, onSave]);

  return html`
    <div class="pane">
      <div style=${{ maxWidth: 460 }}>
        ${Object.entries(settings).map(([key, field]) => html`
          <div class="field" key=${key}>
            <label for=${`f-${key}`}>${field.label}</label>
            <div class="help">${field.help}</div>
            ${field.type === 'text'
              ? html`<input id=${`f-${key}`} type="text" value=${draft[key] ?? ''}
                       onInput=${e => setDraft(d => ({ ...d, [key]: e.target.value }))} />`
              : html`
                <div class="steps" role="group" aria-labelledby=${`f-${key}`}>
                  ${[0, 1, 2, 3].map(n => html`
                    <button key=${n} aria-pressed=${Number(draft[key]) === n}
                            onClick=${() => setDraft(d => ({ ...d, [key]: n }))}>
                      ${['None', 'A little', 'Some', 'A lot'][n]}
                    </button>`)}
                </div>`}
          </div>`)}

        <div style=${{ display: 'flex', alignItems: 'center', marginTop: 'var(--s5)' }}>
          <button class="btn" data-variant="primary" onClick=${save}>Save</button>
          ${saved ? html`<span class="saved">${saved}</span>` : null}
        </div>
      </div>
    </div>`;
}

/* ─── App ────────────────────────────────────────────────────────────────── */

const VALID_TABS = ['talk', 'agents', 'trace', 'health', 'settings'];

function App() {
  // The tab lives in the URL fragment, so a particular view can be linked to
  // and a reload comes back where you were rather than at the start.
  const [tab, setTabState] = useState(() => {
    const fromHash = window.location.hash.replace('#', '');
    return VALID_TABS.includes(fromHash) ? fromHash : 'talk';
  });
  const setTab = useCallback((next) => {
    setTabState(next);
    window.location.hash = next;
  }, []);

  useEffect(() => {
    const onHash = () => {
      const next = window.location.hash.replace('#', '');
      if (VALID_TABS.includes(next)) setTabState(next);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const [state, setState] = useState({ state: 'cold', subsystems: {} });
  const [turns, setTurns] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [trace, setTrace] = useState([]);
  const [settings, setSettings] = useState(null);
  const [activeTool, setActiveTool] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const all = await api.get('/api/everything');
      setState(all.state);
      setTurns(all.conversation || []);
      setJobs(all.jobs || []);
      setTrace(all.trace || []);
      setSettings(all.settings || null);
    } catch (e) {
      setState(s => ({ ...s, state: 'error', last_error: String(e) }));
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // The event stream. One connection, reconnecting on its own, because the
  // assistant restarting must not leave the window silently stale.
  useEffect(() => {
    let source, retry;
    const connect = () => {
      source = new EventSource('/events');
      source.onmessage = (message) => {
        let payload;
        try { payload = JSON.parse(message.data); } catch { return; }

        if (payload.event === 'state_changed') {
          setState(s => ({ ...s, state: payload.new_state }));
          // A finished turn is the cheapest moment to pick up everything else.
          if (payload.new_state === 'idle') refresh();
        } else if (payload.event === 'tool') {
          setActiveTool(payload.tool);
        } else if (payload.event === 'transcript_ready') {
          setTurns(t => [...t, { role: 'user', content: payload.text }]);
        } else if (payload.event === 'response_ready') {
          setTurns(t => [...t, { role: 'assistant', content: payload.text }]);
        } else if (payload.event === 'conversation_reset') {
          setTurns([]);
        } else if (payload.event === 'health_update') {
          setState(payload);
        }
      };
      source.onerror = () => {
        source.close();
        retry = setTimeout(connect, 1500);
      };
    };
    connect();
    return () => { source?.close(); clearTimeout(retry); };
  }, [refresh]);

  // A slow safety net. If an event is ever missed the window is stale for at
  // most twenty seconds rather than until someone notices.
  useEffect(() => {
    const timer = setInterval(refresh, 20000);
    return () => clearInterval(timer);
  }, [refresh]);

  const send = useCallback(async (text) => {
    setTurns(t => [...t, { role: 'user', content: text }]);
    await api.post('/api/say', { text });
  }, []);

  const interrupt = useCallback(() => api.post('/api/control', { action: 'interrupt' }), []);
  const reset = useCallback(async () => {
    await api.post('/api/control', { action: 'reset' });
    setTurns([]);
  }, []);
  const saveSettings = useCallback((values) => api.post('/api/settings', values), []);

  const busy = useMemo(
    () => jobs.filter(j => j.status === 'running' || j.status === 'queued').length, [jobs]);

  return html`
    <div class="app">
      <${TitleBar} state=${state.state} tab=${tab} setTab=${setTab} busy=${busy} />
      <div class="body">
        ${tab === 'talk' ? html`<${Talk} turns=${turns} state=${state.state}
             onSend=${send} onInterrupt=${interrupt} onReset=${reset} />` : null}
        ${tab === 'agents' ? html`<${Agents} jobs=${jobs} activeTool=${activeTool} />` : null}
        ${tab === 'trace' ? html`<${Trace} turns=${trace} />` : null}
        ${tab === 'health' ? html`<${Health} state=${state} />` : null}
        ${tab === 'settings' && settings
          ? html`<${Settings} settings=${settings} onSave=${saveSettings} />` : null}
      </div>
    </div>`;
}

root.render(html`<${App} />`);
