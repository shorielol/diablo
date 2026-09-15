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

import { mascot, AGENTS, byId, index as indexRoster, agent as pickAgent } from './mascot.js';

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

/* ─── Chrome ─────────────────────────────────────────────────────────────── */

const STATE_WORDS = {
  cold: 'Asleep', init: 'Waking', idle: 'Listening for you',
  listening: 'Listening', processing: 'Thinking', responding: 'Speaking',
  error: 'Something broke', shutdown: 'Shutting down',
};

/* The sections that are not a conversation. They used to be tabs across the
   top, which put five equal-weight destinations in front of someone who came
   here to say one sentence. They are now three small links at the foot of the
   rail, where a settings link belongs. */
const SECTIONS = [['trace', 'Trace'], ['health', 'Health'], ['settings', 'Settings']];

function greeting(hour) {
  if (hour < 5) return 'Still up';
  if (hour < 12) return 'Morning';
  if (hour < 17) return 'Afternoon';
  if (hour < 22) return 'Evening';
  return 'Late one';
}

/* "3m", "2h", "Yesterday". Absolute timestamps in a list of conversations are
   noise: what you want to know is whether this was just now or a while ago. */
function ago(seconds) {
  if (!seconds) return '';
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return 'now';
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h`;
  if (delta < 172800) return 'Yesterday';
  return `${Math.floor(delta / 86400)}d`;
}

/* ─── The rail ───────────────────────────────────────────────────────────────
   Diablo, and whatever is actually going on.

   This used to be one row per agent, always, whether that agent had ever done
   anything or not — six names sitting there looking like a cast list. The
   question you arrive with is "what has it been doing", and a fixed list of
   characters cannot answer it: five of the six rows are furniture, and the one
   that matters looks exactly like the other five.

   So Diablo is the only permanent row, because Diablo is the only permanent
   entity — it has every capability the others do and is always there to talk
   to. Everything below it is work: one row per dispatched task, newest first,
   labelled with the task rather than the character, because "Research Delhi
   apprenticeships" tells you what happened and "Vassago" does not. The demon
   is still there in the face and the colour, which is where identity belongs
   when the text has a job to do. */

export const RailRow = memo(({ agent, title, line, when, busy, active, onPick }) => html`
  <button class="convo" data-active=${active} data-busy=${busy}
          style=${{ '--accent': agent.accent }} onClick=${onPick}>
    <span class="unread" aria-hidden="true" />
    <span class="face"><${Mascot} agent=${agent} size=${38} wave=${false} /></span>
    <span class="meta">
      <span class="top">
        <span class="nm">${title}</span>
        <span class="when">${busy ? 'now' : when}</span>
      </span>
      <span class="line">${line}</span>
    </span>
  </button>`);

/* Pulled out of the component and exported so it can be tested against real
   job payloads. The version that lived inside useMemo could only be checked by
   looking at the window, and "looks right" is how the last UI bug survived. */
export function railRows(roster, jobs, liveAgents) {
  const index = roster && roster.length ? indexRoster(roster) : byId;
  const who = id => index[id] || byId[id] || byId.diablo;
  const live = liveAgents || {};
  const all = jobs || [];
  const out = [];

  /* Diablo first, and always. Its line is whatever it is doing now, or the
     last thing it finished on its own — not a sub-agent's work, which has its
     own row further down. */
  const diabloJob = all.find(j => !(j.parts || []).length
                                  && (j.agent || 'diablo') === 'diablo');
  out.push({
    key: 'diablo',
    agent: who('diablo'),
    title: who('diablo').name,
    line: live.diablo || diabloJob?.summary || diabloJob?.task || who('diablo').blurb,
    when: ago(diabloJob?.finished_at || diabloJob?.created_at),
    busy: !!live.diablo,
    pick: 'diablo',
  });

  /* Then the work. A fan-out becomes one row per agent that took a piece,
     because that is what you would want to open — the piece, not the job. */
  for (const job of all) {
    const parts = (job.parts || []).length
      ? job.parts.map(p => ({ id: p.agent, task: p.task, summary: p.summary,
                              status: p.status,
                              when: p.finished_at || job.created_at }))
      : [{ id: job.agent || 'diablo', task: job.task, summary: job.summary,
           status: job.status, when: job.finished_at || job.created_at }];
    for (const [i, part] of parts.entries()) {
      if (part.id === 'diablo' && parts.length === 1) continue;   // already above
      const agent = who(part.id);
      const running = live[part.id];
      out.push({
        key: `${job.id}:${i}`,
        agent,
        title: part.task || `${agent.name}'s work`,
        line: running || part.summary
              || (part.status === 'running' ? `${agent.name} is on it`
                                            : `${agent.name} · ${part.status || 'queued'}`),
        when: ago(part.when),
        busy: !!running,
        pick: part.id,
      });
    }
  }
  return out;
}

function Rail({ roster, jobs, liveAgents, picked, onPick, view, setView, state }) {
  const [query, setQuery] = useState('');

  const rows = useMemo(() => railRows(roster, jobs, liveAgents),
                       [roster, jobs, liveAgents]);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter(r =>
      r.agent.name.toLowerCase().includes(needle) ||
      String(r.title).toLowerCase().includes(needle) ||
      String(r.line).toLowerCase().includes(needle));
  }, [rows, query]);

  return html`
    <aside class="rail">
      <div class="railhead">
        <span class="wordmark">DIABLO</span>
        <span class="dot" data-state=${state} title=${STATE_WORDS[state] || state} />
      </div>

      <div class="searchrow">
        <label class="search">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2.2" aria-hidden="true">
            <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
          </svg>
          <input value=${query} onInput=${e => setQuery(e.target.value)}
                 placeholder="Search" aria-label="Search tasks and agents" spellcheck="false" />
        </label>
        <button class="plus" onClick=${() => { setQuery(''); onPick(null); }}
                title="Start fresh" aria-label="Start a new conversation">
          <svg width="15" height="15" viewBox="0 0 24 24" stroke="currentColor"
               stroke-width="2.2" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        </button>
      </div>

      <div class="convos" role="list">
        ${shown.length === 0
          ? html`<p class="railempty">Nothing by that name.</p>`
          : shown.map(r => html`
            <${RailRow} key=${r.key} agent=${r.agent} title=${r.title} line=${r.line}
               when=${r.when} busy=${r.busy}
               active=${picked === r.pick && view === 'home'}
               onPick=${() => { setView('home'); onPick(r.pick); }} />`)}
        ${shown.length === 1 && !query
          ? html`<p class="railempty">Ask for something that takes a while and it
                   will appear here while it runs.</p>`
          : null}
      </div>

      <nav class="railfoot" aria-label="Sections">
        ${SECTIONS.map(([id, label]) => html`
          <button key=${id} class="chip" data-active=${view === id}
                  onClick=${() => setView(view === id ? 'home' : id)}>${label}</button>`)}
      </nav>
    </aside>`;
}

/* ─── Talk ───────────────────────────────────────────────────────────────── */

const Turn = memo(({ role, content }) => html`
  <div class=${`turn ${role === 'user' ? 'you' : ''}`}>
    <div>
      <div class="who">${role === 'user' ? 'You' : 'Diablo'}</div>
      <div class="bubble">${content}</div>
    </div>
  </div>`);

/* ─── The stage ──────────────────────────────────────────────────────────────
   The character, large, with the last thing he said above him. Before he has
   said anything there is a greeting and one line telling you how to start,
   and that is the entire screen — because on the first run the only thing
   worth knowing is that you can just talk to it. */

/* The greeting arrives a word at a time, each one unblurring as it lands.
   The idea is reactbits' Blur Text; the implementation is eight lines of CSS
   rather than a dependency, because this app has no build step and a text
   effect is not worth acquiring one.

   Justified because of how often it is seen: the greeting shows once, when
   the window opens on an empty conversation. Anything you look at a hundred
   times a day should not animate at all — but a first impression can afford
   240 milliseconds. */
const BlurIn = memo(({ text }) => html`
  <span class="blurin" aria-label=${text}>
    ${text.split(' ').map((word, i) => html`
      <span key=${i} style=${{ animationDelay: `${i * 70}ms` }} aria-hidden="true">
        ${word}${i < text.split(' ').length - 1 ? '\u00A0' : ''}</span>`)}
  </span>`);

const Bubble = memo(({ text, speaking }) => html`
  <div class="bubble-hero" data-speaking=${speaking}>
    <span class="bubmic"><${Icon} name="mic" size=${15} /></span>
    <span class="bubtext">${text}</span>
  </div>`);

function Stage({ turns, state, agent, roster, jobs, liveAgents, address,
                 onSend, onInterrupt, onReset }) {
  const [draft, setDraft] = useState('');
  const areaRef = useRef(null);
  const endRef = useRef(null);
  const speaking = state === 'responding';

  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }); }, [turns.length]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    onSend(text, agent?.id || null);
    setDraft('');
    if (areaRef.current) areaRef.current.style.height = 'auto';
  }, [draft, onSend, agent]);

  const onKeyDown = useCallback((event) => {
    // Enter sends, Shift+Enter is a newline. The opposite is always wrong and
    // people have muscle memory for this one.
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
  }, [send]);

  const grow = useCallback((event) => {
    setDraft(event.target.value);
    const el = event.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 132) + 'px';
  }, []);

  /* An agent picked in the rail is who you are now talking to: their work
     above, and the composer below aimed at them. Saying their name out loud
     does the same thing, so the two ways in agree. */
  if (agent && agent.id !== 'diablo') {
    const mine = jobs.filter(j => j.agent === agent.id
      || (j.parts || []).some(p => p.agent === agent.id));
    const theirs = turns.filter(t => t.agent === agent.id || t.role === 'user');
    return html`
      <div class="stage" style=${{ '--accent': agent.accent }}>
        <div class="agenthead" style=${{ '--accent': agent.accent }}>
          <${Mascot} agent=${agent} size=${64} wave=${false} />
          <div>
            <h1>${agent.name}</h1>
            <p class="role">${agent.role}</p>
            <p class="blurb">${liveAgents[agent.id] || agent.blurb}</p>
          </div>
        </div>
        <div class="pane">
          ${mine.length === 0
            ? html`<p class="quiet">${agent.name} has not been asked for anything yet.
                     Say something that needs ${agent.role.toLowerCase()} and it will
                     land here.</p>`
            : mine.slice(0, 20).map(job => html`
              <article class="jobcard" key=${job.id}>
                <p class="task">${job.task}</p>
                ${(job.parts || []).filter(p => p.agent === agent.id).map((p, i) => html`
                  <p class="answer" key=${i}>${p.summary || '…'}</p>`)}
                ${(job.parts || []).length === 0 && job.summary
                  ? html`<p class="answer">${job.summary}</p>` : null}
                <p class="stamp">${ago(job.finished_at || job.created_at)}
                  <span class="pill" data-tone=${job.status === 'done' ? 'ok' : 'warn'}>
                    ${job.status}</span></p>
              </article>`)}
        </div>

        <div class="composer">
          <textarea ref=${areaRef} value=${draft} onInput=${grow} onKeyDown=${onKeyDown}
                    rows="1" placeholder=${`Ask ${agent.name} something…`}
                    aria-label=${`Message to ${agent.name}`} />
          <button class="iconbtn" onClick=${send} disabled=${!draft.trim()}
                  title="Send" aria-label=${`Send to ${agent.name}`}>
            <${Icon} name="send" /></button>
        </div>
      </div>`;
  }

  const last = [...turns].reverse().find(t => t.role === 'assistant');

  return html`
    <div class="stage">
      <${Running} jobs=${jobs} roster=${roster.length ? indexRoster(roster) : byId} />
      ${turns.length === 0
        ? html`
          <div class="hero">
            <${Mascot} agent=${byId.diablo} size=${188} />
            <${Bubble} speaking=${speaking}
               text=${last?.content || "Hey Diablo. I'm listening."} />
            <h1 class="greet"><${BlurIn} text=${`${greeting(new Date().getHours())}, ${address}.`} /></h1>
            <p class="hint">Say <b>"Hey Diablo"</b> out loud, or type below.
               Everything runs on this Mac.</p>
          </div>`
        : html`
          <div class="pane">
            <div class="thread">
              ${turns.map((t, i) => html`
                <${Turn} key=${i} role=${t.role} content=${t.content} />`)}
              <div ref=${endRef} />
            </div>
          </div>`}

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
        ${turns.length > 0
          ? html`<button class="iconbtn" onClick=${onReset}
                     title="Forget this conversation"
                     aria-label="Forget this conversation">
                   <${Icon} name="trash" /></button>`
          : null}
      </div>
    </div>`;
}

/* ─── Work in flight ─────────────────────────────────────────────────────────
   A strip above the conversation, only while something is actually running.
   This is the only place a fan-out is visible as a fan-out — three names, three
   errands, three states, all moving at once — and it disappears the moment the
   last one lands, because a queue with nothing in it is not worth a row. */

const Running = memo(({ jobs, roster }) => {
  const live = jobs.filter(j => j.status === 'running' || j.status === 'queued');
  if (!live.length) return null;
  return html`
    <div class="running" role="status" aria-live="polite">
      ${live.slice(0, 3).map(job => {
        const parts = (job.parts && job.parts.length)
          ? job.parts
          : [{ agent: job.agent || 'diablo', task: job.task, status: job.status }];
        return html`
          <div class="runjob" key=${job.id}>
            ${parts.map((part, i) => {
              const who = pickAgent(part.agent, roster);
              return html`
                <span class="runpart" key=${i} style=${{ '--accent': who.accent }}
                      data-status=${part.status || 'queued'}>
                  <span class="runface"><${Mascot} agent=${who} size=${20} wave=${false} /></span>
                  <span class="runwho">${who.name}</span>
                  <span class="runtask">${part.task}</span>
                </span>`;
            })}
          </div>`;
      })}
    </div>`;
});

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

const PERM_LABELS = {
  mic: 'Microphone', screen: 'Screen recording', accessibility: 'Accessibility',
};

function Health({ state, onPermissions }) {
  const subsystems = state.subsystems || {};
  const perms = state.permissions || {};
  const [asking, setAsking] = useState(null);
  const [note, setNote] = useState('');

  /* macOS decides whether the dialog appears, and it only ever appears once
     per app. After that the honest thing is to open the exact pane and say so,
     which is what the reply from the server tells us happened. */
  const grant = useCallback(async (name) => {
    setAsking(name); setNote('');
    try {
      const result = await api.post('/api/control', { action: `grant:${name}` });
      if (result.granted) {
        setNote(`${PERM_LABELS[name] || name} granted.`);
      } else if (result.opened_settings) {
        setNote('macOS would not show the dialog again, so I have opened the '
              + 'pane. Switch Diablo on there, then press Check now.');
      } else {
        setNote(result.error || 'No answer yet. Press Check now once you have decided.');
      }
      if (result.permissions || result.granted !== undefined) onPermissions?.();
    } catch (e) {
      setNote(String(e));
    } finally {
      setAsking(null);
    }
  }, [onPermissions]);

  const recheck = useCallback(async () => {
    setNote('');
    await api.post('/api/control', { action: 'recheck' });
    onPermissions?.();
  }, [onPermissions]);

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
            <span class="k">${PERM_LABELS[name] || name}</span>
            <span class="perm">
              <span class="pill" data-tone=${granted ? 'ok' : 'bad'}>
                ${granted ? 'granted' : 'denied'}</span>
              ${granted ? null : html`
                <button class="ghost sm" disabled=${asking === name}
                        onClick=${() => grant(name)}>
                  ${asking === name ? 'Asking...' : 'Grant'}</button>`}
            </span>
          </div>`)}

        ${note ? html`<p class="note">${note}</p>` : null}

        ${perms.accessibility === false ? html`
          <p class="note">
            Accessibility is only used to point at buttons that have no text
            label. Everything else — the microphone, reading the screen, the
            files — works without it.</p>` : null}

        <div class="row" style=${{ marginTop: 'var(--s3)' }}>
          <span class="k">Checked every 30 seconds</span>
          <button class="ghost sm" onClick=${recheck}>Check now</button>
        </div>
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

/* Two pieces of navigation, not five. `view` is home or one of the three
   utility sections; `picked` is which agent's work the stage is showing, and
   null means the conversation itself. Both live in the URL fragment so a
   reload comes back where you were. */
const VALID_VIEWS = ['home', 'trace', 'health', 'settings'];

function App() {
  const [view, setViewState] = useState(() => {
    const fromHash = (window.location.hash.replace('#', '').split('/')[0]) || 'home';
    return VALID_VIEWS.includes(fromHash) ? fromHash : 'home';
  });
  const [picked, setPicked] = useState(() =>
    window.location.hash.split('/')[1] || null);

  const writeHash = useCallback((nextView, nextPicked) => {
    window.location.hash = nextPicked ? `${nextView}/${nextPicked}` : nextView;
  }, []);
  const setView = useCallback((next) => {
    setViewState(next);
    setPicked(p => { writeHash(next, next === 'home' ? p : null); return next === 'home' ? p : null; });
  }, [writeHash]);
  const onPick = useCallback((id) => {
    setPicked(id);
    writeHash('home', id);
  }, [writeHash]);

  useEffect(() => {
    const onHash = () => {
      const [v, who] = window.location.hash.replace('#', '').split('/');
      if (VALID_VIEWS.includes(v || 'home')) setViewState(v || 'home');
      setPicked(who || null);
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
  const [roster, setRoster] = useState(AGENTS);
  // Who is working right now, and on what. Keyed by agent id so a card can
  // light up the instant its piece starts rather than on the next poll.
  const [liveAgents, setLiveAgents] = useState({});

  const refresh = useCallback(async () => {
    try {
      const all = await api.get('/api/everything');
      setState(all.state);
      setTurns(all.conversation || []);
      setJobs(all.jobs || []);
      setTrace(all.trace || []);
      setSettings(all.settings || null);
      if (all.agents?.length) setRoster(all.agents);
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
        } else if (payload.event === 'agent_started') {
          setLiveAgents(a => ({ ...a, [payload.agent]: payload.task }));
        } else if (payload.event === 'agent_done') {
          setLiveAgents(a => { const next = { ...a }; delete next[payload.agent]; return next; });
        } else if (payload.event === 'job_started') {
          setLiveAgents(a => ({ ...a, [payload.agent || 'diablo']: payload.task }));
          refresh();
        } else if (payload.event === 'job_done') {
          setLiveAgents({});
          refresh();
        } else if (payload.event === 'permissions') {
          setState(s => ({ ...s, permissions: payload.permissions }));
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

  const send = useCallback(async (text, agent) => {
    setTurns(t => [...t, { role: 'user', content: text }]);
    await api.post('/api/say', agent ? { text, agent } : { text });
  }, []);

  const interrupt = useCallback(() => api.post('/api/control', { action: 'interrupt' }), []);
  const reset = useCallback(async () => {
    await api.post('/api/control', { action: 'reset' });
    setTurns([]);
  }, []);
  const saveSettings = useCallback((values) => api.post('/api/settings', values), []);

  const busy = useMemo(
    () => jobs.filter(j => j.status === 'running' || j.status === 'queued').length, [jobs]);

  const address = settings?.address?.value || 'Sir';
  const rosterById = useMemo(() => indexRoster(roster), [roster]);
  const agent = picked ? pickAgent(picked, rosterById) : null;

  return html`
    <div class="app" data-view=${view}>
      <${Rail} roster=${roster.length ? roster : AGENTS} jobs=${jobs}
         liveAgents=${liveAgents} picked=${picked} onPick=${onPick}
         view=${view} setView=${setView} state=${state.state} />

      <main class="main">
        <div class="draghandle" />
        ${view === 'home'
          ? html`<${Stage} turns=${turns} state=${state.state} agent=${agent}
               roster=${roster} jobs=${jobs} liveAgents=${liveAgents}
               address=${address} onSend=${send} onInterrupt=${interrupt}
               onReset=${reset} />`
          : null}
        ${view === 'trace' ? html`<${Trace} turns=${trace} />` : null}
        ${view === 'health'
          ? html`<${Health} state=${state} onPermissions=${refresh} />` : null}
        ${view === 'settings' && settings
          ? html`<${Settings} settings=${settings} onSave=${saveSettings} />` : null}
      </main>
    </div>`;
}

root.render(html`<${App} />`);
