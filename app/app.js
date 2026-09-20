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
import { presentation, lastExchange, reportLevel, reportEngaged, requestClose,
         requestDismiss, forgetReports, INTERACTION, WORK,
         INTERACTION_MAX_HEIGHT } from './surface.js';

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
   recoloured, and read out loud by a screen reader as their unicode name.

   One family: a 24-unit grid, 1.8 strokes, round ends, drawn at 16 in the
   chrome. Filled and stroked icons side by side was the old set, and it read
   as two sets. Stop is the one filled shape, because a stop square is
   recognised by its mass. */

const ICONS = {
  send:   'M12 19V5M5.5 11.5 12 5l6.5 6.5',
  stop:   'M7.5 7.5h9v9h-9z',
  trash:  'M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.9 12.1a1.6 1.6 0 0 0 1.6 1.4h6a1.6 1.6 0 0 0 1.6-1.4L17.5 7',
  search: 'M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13ZM20 20l-4.8-4.8',
  plus:   'M12 5v14M5 12h14',
  back:   'M14.5 18 8.5 12l6-6',
  more:   'M5.5 12h.01M12 12h.01M18.5 12h.01',
  // Two corners opening out, and closing back in: larger, and back to one exchange.
  expand:   'M14 5h5v5M10 19H5v-5M19 5l-5.5 5.5M5 19l5.5-5.5',
  collapse: 'M13.5 5.5v5h5M10.5 18.5v-5h-5M19 5l-5.5 5.5M5 19l5.5-5.5',
};

const Icon = memo(({ name, size = 16 }) => html`
  <svg width=${size} height=${size} viewBox="0 0 24 24" aria-hidden="true" focusable="false"
       fill=${name === 'stop' ? 'currentColor' : 'none'} stroke="currentColor"
       stroke-width=${name === 'more' ? 3.2 : 1.8} stroke-linecap="round" stroke-linejoin="round">
    <path d=${ICONS[name]} /></svg>`);

/* ─── The character ──────────────────────────────────────────────────────────
   dangerouslySetInnerHTML is safe here and nowhere else in this file: the
   markup comes from mascot.js, which builds it from a fixed template and a
   colour, never from anything a user or a model produced. */

const Mascot = memo(({ agent, size = 96, wave = true, crop = '' }) => {
  const svg = useMemo(
    // Diablo's own mark is the text colour; a sub-agent's front is its accent.
    () => mascot({ accent: agent.id === 'diablo' ? null : agent.accent,
                   eye: agent.eye, mouth: agent.mouth,
                   size, wave, crop }),
    [agent.id, agent.accent, agent.eye, agent.mouth, size, wave, crop]);
  return html`<div class="mascot" style=${{ width: size, height: size }}
      dangerouslySetInnerHTML=${{ __html: svg }} />`;
});

/* Every size a mark is drawn at in here, in one table, and never large: the
   orb on the desktop is Diablo, and the surface comes out of him, so the
   surface does not draw him again. The others' marks say who a piece of
   work belongs to, and whom you are addressing. */
const MASCOT = {
  row: 28,        // a row in the task list
  inline: 16,     // one line of work in flight; the head of the command line
  portrait: 44,   // the top of another agent's page
};

/* ─── Chrome ─────────────────────────────────────────────────────────────── */

/* One word each, and nothing at all when he is idle.
   "Listening for you" sat in the corner permanently, which is a caption
   telling you the application is running. The empty string is the design. */
const STATE_WORDS = {
  cold: '', init: 'Waking', idle: '',
  listening: 'Listening', processing: 'Thinking',
  // The answer exists and the sound has not started. Named, because the
  // alternative is showing "Speaking" through a synthesis round trip.
  preparing: 'Thinking', responding: 'Speaking',
  error: 'Something broke', shutdown: 'Closing',
};

/* The sections that are not a conversation. They used to be tabs across the
   top, which put five equal-weight destinations in front of someone who came
   here to say one sentence. They are now three small links at the foot of the
   rail, where a settings link belongs. */
const SECTIONS = [['trace', 'Trace'], ['health', 'Health'], ['settings', 'Settings']];
const SECTION_TITLES = Object.fromEntries(SECTIONS);

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
    <span class="face">
      <${Mascot} agent=${agent} size=${MASCOT.row} wave=${false} crop="head" />
      <span class="unread" aria-hidden="true" />
    </span>
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
      <div class="railhead" />

      <div class="searchrow">
        <label class="search">
          <${Icon} name="search" size=${14} />
          <input value=${query} onInput=${e => setQuery(e.target.value)}
                 placeholder="Search" aria-label="Search tasks and agents" spellcheck="false" />
        </label>
        <button class="tool plus" onClick=${() => { setQuery(''); onPick(null); }}
                title="Start fresh" aria-label="Start a new conversation">
          <${Icon} name="plus" /></button>
      </div>

      <div class="convos" role="list">
        ${shown.length === 0
          ? html`<p class="railempty">Nothing by that name.</p>`
          : shown.map(r => html`
            <${RailRow} key=${r.key} agent=${r.agent} title=${r.title} line=${r.line}
               when=${r.when} busy=${r.busy}
               active=${picked === r.pick && view === 'home'}
               onPick=${() => { setView('home'); onPick(r.pick); }} />`)}
      </div>

      <nav class="railfoot" aria-label="Sections">
        ${SECTIONS.map(([id, label]) => html`
          <button key=${id} class="chip" data-active=${view === id}
                  onClick=${() => setView(view === id ? 'home' : id)}>${label}</button>`)}
      </nav>
    </aside>`;
}

/* ─── Talk ───────────────────────────────────────────────────────────────────
   No bubbles, no avatars, no names. What you asked is a quiet line, set like
   the heading of what follows; what he said is the text under it. The orb
   below the surface is who is speaking, so nothing in here needs to say it
   again — except to screen readers, which are told in words. */

const Turn = memo(({ role, content }) => html`
  <div class=${`turn ${role === 'user' ? 'you' : 'him'}`}>
    <span class="sr">${role === 'user' ? 'You:' : 'Diablo:'}</span>
    <p class="text">${content}</p>
  </div>`);

/* The window's own title row. Shares the row with the traffic lights. */
const TopBar = memo(({ children, tools }) => html`
  <header class="topbar">
    <div class="title">${children}</div>
    <div class="tools">${tools}</div>
  </header>`);

/* Everything that is not the exchange itself, behind one control. A native
   disclosure rather than a hand-rolled popover: it opens from the keyboard
   without any code here. */
function Menu({ items, onToggle }) {
  const ref = useRef(null);
  return html`
    <details class="menu" ref=${ref}
             onToggle=${e => onToggle && onToggle(e.currentTarget.open)}>
      <summary class="tool" title="More" aria-label="More"><${Icon} name="more" /></summary>
      <div class="menulist" role="menu">
        ${items.map(item => html`
          <button key=${item.id} role="menuitem" data-active=${!!item.active}
                  aria-pressed=${!!item.active}
                  onClick=${() => { if (ref.current) ref.current.open = false; item.onPick(); }}>
            ${item.label}</button>`)}
      </div>
    </details>`;
}

/* A tool's name, said the way a person would. */
const toolWords = tool => String(tool || '').replace(/[_-]+/g, ' ').trim();

/* One line for the moment, when the moment needs one. Never colour alone:
   the words carry it, and red is kept for the one line that is a fault. */
export function statusLine(kind, tool, flash, running) {
  switch (kind) {
    case 'listening': return 'Listening…';
    case 'thinking':  return 'Thinking…';
    case 'status':    return tool ? `Using ${toolWords(tool)}…`
                                  : running ? `Working on ${running === 1 ? 'it' : `${running} things`}…` : 'Working…';
    case 'multistep': return `${running} things running`;
    case 'error':     return flash?.kind === 'failed' ? "That didn't work."
                                                      : 'Something went wrong. Try again, or open Health.';
    case 'success':   return 'Done.';
    default:          return '';
  }
}

/* The compact surface's body: the last exchange and the moment it is in. */
const Exchange = memo(({ ask, answer, kind, line }) => {
  if (kind === 'empty') return null;
  const showAnswer = answer && kind !== 'thinking' && kind !== 'listening';
  return html`
    <section class="exchange">
      ${ask ? html`<p class="ask"><span class="sr">You: </span>${ask}</p>` : null}
      ${showAnswer
        ? html`<div class="answer"><span class="sr">Diablo: </span><p class="text">${answer}</p></div>`
        : null}
      ${line ? html`<p class="line" data-kind=${kind} role="status" aria-live="polite">${line}</p>` : null}
    </section>`;
});

/* Nothing said yet, on the larger surface. One line, at the foot where his
   next one would appear. The compact surface has not even this: the line
   you type into says it. */
const Empty = memo(() => html`
  <div class="blank">
    <p class="hint">Hold <kbd>⌃</kbd> <kbd>⌥</kbd> anywhere to talk, or type below.</p>
  </div>`);

/* The command line. Diablo is the orb just below it, so it carries no face
   of its own; addressing one of the others puts their mark at its head, the
   one place identity is information. Enter sends and Shift+Enter is a new
   line; the opposite is always wrong and people have muscle memory for it. */
function Command({ agent, state, kind, target, speaking, onSend, onInterrupt, onDraft, autoFocus }) {
  const [draft, setDraft] = useState('');
  const areaRef = useRef(null);
  // The line says what he is doing while he is doing it; a fault is said
  // calmly above it, not shouted from the field you type into.
  const word = ['listening', 'processing', 'preparing', 'responding'].includes(state)
    ? STATE_WORDS[state] : '';

  useEffect(() => { if (autoFocus) areaRef.current?.focus(); }, [autoFocus]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    onSend(text, target);
    setDraft('');
    onDraft && onDraft(false);
    if (areaRef.current) areaRef.current.style.height = 'auto';
  }, [draft, onSend, target, onDraft]);

  const onKeyDown = useCallback((event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
  }, [send]);

  const grow = useCallback((event) => {
    setDraft(event.target.value);
    onDraft && onDraft(!!event.target.value.trim());
    const el = event.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 132) + 'px';
  }, [onDraft]);

  const other = agent && agent.id !== 'diablo';
  const placeholder = other ? `Ask ${agent.name} something`
    : word ? `${word}…`
    : kind === 'clarify' ? 'Answer Diablo'
    : 'Ask, or hold ⌃⌥ to talk';

  return html`
    <div class="command">
      ${other ? html`<span class="to" title=${agent.name}>
                       <${Mascot} agent=${agent} size=${MASCOT.inline} wave=${false} /></span>` : null}
      <span class="sr" role="status" aria-atomic="true">${word ? `Diablo: ${word}` : ''}</span>
      <textarea ref=${areaRef} value=${draft} onInput=${grow} onKeyDown=${onKeyDown}
                rows="1" placeholder=${placeholder}
                aria-label=${`Message to ${other ? agent.name : 'Diablo'}`} />
      ${speaking
        ? html`<button class="send" data-active="true" onClick=${onInterrupt}
                   title="Stop speaking" aria-label="Stop speaking">
                 <${Icon} name="stop" /></button>`
        : html`<button class="send" onClick=${send} disabled=${!draft.trim()}
                   title="Send" aria-label=${`Send to ${other ? agent.name : 'Diablo'}`}>
                 <${Icon} name="send" /></button>`}
    </div>`;
}

/* ─── The compact surface ────────────────────────────────────────────────────
   One exchange and the line to type into, as tall as that and no taller.
   The first row is the window's title row: it is where the surface can be
   dragged from, and it holds the two ways out of it — larger, or more. */

function Interaction({ exchange, kind, line, permissions, onPermissions, menu,
                       onExpand, command, bodyRef }) {
  return html`
    <div class="stage" data-level="interaction">
      <${TopBar} tools=${html`
        <button class="tool" onClick=${onExpand}
                title="Open larger" aria-label="Open larger"><${Icon} name="expand" /></button>
        ${menu}`} />
      <div class="body" ref=${bodyRef}>
        <${Needs} perms=${permissions || {}} onPermissions=${onPermissions} />
        <${Exchange} ask=${exchange.ask} answer=${exchange.answer} kind=${kind} line=${line} />
      </div>
      ${command}
    </div>`;
}

/* ─── The work surface ───────────────────────────────────────────────────────
   The same surface with room: the whole conversation, whatever is running,
   and the pages that are not the conversation. Temporary — the control that
   brings it back to one exchange is the first one in its title row. */

function Work({ turns, agent, jobs, liveAgents, roster, permissions, onPermissions,
               menu, onCollapse, onReset, onHome, line, kind, command }) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }); }, [turns.length]);

  const collapse = html`
    <button class="tool" onClick=${onCollapse} title="Back to one exchange"
            aria-label="Back to one exchange"><${Icon} name="collapse" /></button>`;

  /* An agent picked from the tasks is who you are now talking to: their work
     above, and the command line aimed at them, with their mark on it. */
  if (agent && agent.id !== 'diablo') {
    const mine = jobs.filter(j => j.agent === agent.id
      || (j.parts || []).some(p => p.agent === agent.id));
    return html`
      <div class="stage" data-level="work" style=${{ '--accent': agent.accent }}>
        <${TopBar} tools=${html`${menu}`}>
          <button class="back" onClick=${onHome} aria-label="Back to Diablo">
            <${Icon} name="back" /> Diablo</button>
        <//>
        <div class="pane">
          <div class="intro">
            <${Mascot} agent=${agent} size=${MASCOT.portrait} wave=${false} crop="head" />
            <div>
              <h1>${agent.name}</h1>
              <p class="role">${agent.role}</p>
              <p class="blurb">${liveAgents[agent.id] || agent.blurb}</p>
            </div>
          </div>
          <div class="works">
            ${mine.length === 0
              ? html`<p class="quiet">${agent.name} has not been asked for anything yet.</p>`
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
        </div>
        ${command}
      </div>`;
  }

  return html`
    <div class="stage" data-level="work">
      <${TopBar} tools=${html`
        ${collapse}
        ${turns.length > 0
          ? html`<button class="tool" onClick=${onReset}
                     title="Forget this conversation"
                     aria-label="Forget this conversation">
                   <${Icon} name="trash" /></button>`
          : null}
        ${menu}`}>Diablo<//>
      <${Needs} perms=${permissions || {}} onPermissions=${onPermissions} />
      <${Running} jobs=${jobs} roster=${roster.length ? indexRoster(roster) : byId} />
      ${turns.length === 0
        ? html`<${Empty} />`
        : html`
          <div class="pane">
            <div class="thread">
              ${turns.map((t, i) => html`
                <${Turn} key=${i} role=${t.role} content=${t.content} />`)}
              ${line && kind !== 'multistep'
                ? html`<p class="line" data-kind=${kind} role="status" aria-live="polite">${line}</p>`
                : null}
              <div ref=${endRef} />
            </div>
          </div>`}
      ${command}
    </div>`;
}

/* A page that is not the conversation: its name in the title row, and the
   way back. */
function Section({ title, setView, menu, children }) {
  return html`
    <div class="stage" data-level="work">
      <${TopBar} tools=${menu}>
        <button class="back" onClick=${() => setView('home')} aria-label="Back to Diablo">
          <${Icon} name="back" /> Diablo</button>
        <span class="sub">${title}</span>
      <//>
      ${children}
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
      ${live.slice(0, 2).map(job => {
        const parts = (job.parts && job.parts.length)
          ? job.parts
          : [{ agent: job.agent || 'diablo', task: job.task, status: job.status }];
        return html`
          <div class="runjob" key=${job.id}>
            ${parts.map((part, i) => {
              const who = pickAgent(part.agent, roster);
              return html`
                <div class="runpart" key=${i} style=${{ '--accent': who.accent }}
                     data-status=${part.status || 'queued'}>
                  <span class="runface"><${Mascot} agent=${who} size=${MASCOT.inline} wave=${false} crop="head" /></span>
                  <span class="runtask">${part.task}</span>
                </div>`;
            })}
            <span class="runrule" aria-hidden="true"><i /></span>
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
          <div class="tbhead">
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

/* Asking for a permission, in one place.
   The compact request in the conversation and the diagnostic page both do
   exactly this, and two copies of it would drift the moment macOS changes its
   mind about when it shows a dialog. */
function useGrant(onPermissions) {
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

  return { grant, asking, note, setNote };
}

/* What Diablo cannot do without each one, said the way he would say it.
   Not "SCREEN RECORDING: DENIED" — the user does not need the name of the
   toggle, they need to know what is missing and why he is asking. */
const PERM_ASK = {
  screen: 'I need screen access to see what you are seeing.',
  mic: 'I need the microphone to hear you.',
  accessibility: 'Accessibility lets me point at buttons that have no text '
               + 'label. Everything else works without it.',
};

/* The button says what pressing it does. Built from its own table rather
   than "Allow " + the diagnostic label, which produced "Allow Screen
   recording" — a stray capital in the middle of a sentence, which is the
   kind of detail that makes an interface feel unfinished. */
const PERM_BUTTON = {
  screen: 'Allow screen access',
  mic: 'Allow microphone',
  accessibility: 'Allow accessibility',
};

/* The order matters: one request at a time, most blocking first. Three
   stacked permission prompts on first run is the onboarding clutter the
   product is supposed to avoid. */
const PERM_ORDER = ['screen', 'mic', 'accessibility'];

const Needs = memo(({ perms, onPermissions }) => {
  const { grant, asking, note } = useGrant(onPermissions);
  /* Strictly false, never merely absent. The snapshot reports null for a
     permission macOS will not disclose without being asked, and "I cannot
     tell" is not a reason to interrupt someone with a request. Only a
     definite denial earns the row. */
  const missing = PERM_ORDER.find(name => perms[name] === false);
  if (!missing) return null;
  return html`
    <section class="needs" role="status">
      <p class="why">${PERM_ASK[missing]}</p>
      <div class="act">
        <button class="grantbtn" disabled=${asking === missing}
                onClick=${() => grant(missing)}>
          ${asking === missing ? 'Asking\u2026' : (PERM_BUTTON[missing] || 'Allow')}
        </button>
        ${note ? html`<span class="grantnote">${note}</span>` : null}
      </div>
    </section>`;
});

function Health({ state, onPermissions }) {
  const subsystems = state.subsystems || {};
  const perms = state.permissions || {};
  const { grant, asking, note, setNote } = useGrant(onPermissions);

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
const VALID_VIEWS = ['home', 'tasks', 'trace', 'health', 'settings'];

/* Tasks: what has been handed off, one row per piece of work. It used to be
   a sidebar that stood there whether or not anything had happened; now it
   is a page you open when there is something in it. Wide work surfaces
   still show the rail beside the conversation. */
function Tasks({ roster, jobs, liveAgents, onPick }) {
  const rows = useMemo(() => railRows(roster, jobs, liveAgents).slice(1),
                       [roster, jobs, liveAgents]);
  return html`
    <div class="pane">
      <div class="convos tasks" role="list">
        ${rows.length === 0
          ? html`<p class="quiet">Nothing has been handed off yet.</p>`
          : rows.map(r => html`
            <${RailRow} key=${r.key} agent=${r.agent} title=${r.title} line=${r.line}
               when=${r.when} busy=${r.busy} active=${false}
               onPick=${() => onPick(r.pick)} />`)}
      </div>
    </div>`;
}

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
    setViewState('home');
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

  // The surface. `pinned` is the person's own choice of level, held until
  // it folds away; `overflow` is the compact surface finding its exchange
  // too tall for it; `flash` is a finished job, said once.
  const [pinned, setPinned] = useState(null);
  const [overflow, setOverflow] = useState(false);
  const [flash, setFlash] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const appRef = useRef(null);
  const bodyRef = useRef(null);

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
          if (payload.new_state !== 'processing') setActiveTool(null);
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
        } else if (payload.event === 'job_done' || payload.event === 'job_failed') {
          setLiveAgents({});
          setFlash({ kind: payload.event === 'job_done' ? 'success' : 'failed',
                     text: payload.summary || '', at: Date.now() });
          refresh();
        } else if (payload.event === 'permissions') {
          setState(s => ({ ...s, permissions: payload.permissions }));
        } else if (payload.event === 'transcript_ready') {
          setFlash(null);
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
    setFlash(null);
    setTurns(t => [...t, { role: 'user', content: text }]);
    await api.post('/api/say', agent ? { text, agent } : { text });
  }, []);

  const interrupt = useCallback(() => api.post('/api/control', { action: 'interrupt' }), []);
  const reset = useCallback(async () => {
    await api.post('/api/control', { action: 'reset' });
    setTurns([]);
    setFlash(null);
  }, []);
  const saveSettings = useCallback((values) => api.post('/api/settings', values), []);

  const rosterById = useMemo(() => indexRoster(roster), [roster]);
  const agent = picked ? pickAgent(picked, rosterById) : null;
  const other = agent && agent.id !== 'diablo';
  const running = useMemo(
    () => jobs.filter(j => j.status === 'running' || j.status === 'queued').length, [jobs]);

  /* ─── Which surface, and what it says ─────────────────────────────────── */

  const { level, kind } = presentation({ view, picked, pinned, overflow,
                                         state: state.state, turns, jobs, activeTool, flash });
  const exchange = useMemo(() => {
    const ex = lastExchange(turns);
    return kind === 'success' && !ex.answer && flash?.text ? { ...ex, answer: flash.text } : ex;
  }, [turns, kind, flash]);
  const line = statusLine(kind, activeTool, flash, running);

  // A new exchange starts from the compact surface again.
  useEffect(() => { setOverflow(false); }, [exchange.ask]);

  // The stylesheet sizes the compact surface to its content.
  useEffect(() => { document.documentElement.dataset.level = level; }, [level]);

  // Tell the shell the level, and at the compact level how tall it is.
  useEffect(() => {
    const measure = () => {
      const app = appRef.current;
      if (!app) return;
      if (level !== INTERACTION) {
        // The work surface follows its content too, so an answer that needs
        // 600 points does not open a window of 900. What it cannot fit, it
        // scrolls; what it does not need, it does not take.
        const pane = document.querySelector('.pane');
        reportLevel(level, app.offsetHeight + (pane ? pane.scrollHeight - pane.clientHeight : 0),
                    kind);
        return;
      }
      const body = bodyRef.current;
      const natural = app.offsetHeight + (body ? body.scrollHeight - body.clientHeight : 0);
      if (natural > INTERACTION_MAX_HEIGHT && pinned !== INTERACTION && !overflow) {
        setOverflow(true);
        return;
      }
      // An open menu needs room below the title row to be read.
      reportLevel(INTERACTION, Math.max(natural, menuOpen ? 212 : 0), kind);
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (appRef.current) observer.observe(appRef.current);
    if (bodyRef.current) for (const child of bodyRef.current.children) observer.observe(child);
    const thread = document.querySelector('.thread');
    if (thread) observer.observe(thread);
    return () => observer.disconnect();
  }, [level, kind, pinned, overflow, menuOpen, exchange, line]);

  // Someone reading, hovering or typing: the surface stays. Hovering means
  // the pointer moved inside it; a surface that appears under a resting
  // pointer has not been touched, and must still fold away.
  useEffect(() => { reportEngaged(hovered || drafting || menuOpen); },
            [hovered, drafting, menuOpen]);

  // "Done", said once he has finished saying it, then the surface folds away
  // unless someone is using it. The shell owns the wait.
  useEffect(() => {
    if (kind === 'success' && state.state === 'idle') requestDismiss('success');
  }, [kind, state.state, flash?.at]);

  // Folded away: the next opening starts from the one exchange, at home.
  useEffect(() => {
    window.diabloSurface = {
      closed: () => {
        setPinned(null);
        setOverflow(false);
        setMenuOpen(false);
        setViewState('home');
        setPicked(null);
        writeHash('home', null);
        forgetReports();
      },
    };
  }, [writeHash]);

  // Opened for typing: the line is ready without a click.
  useEffect(() => {
    const onFocus = () => document.querySelector('.command textarea')?.focus();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  // Escape steps back one level at a time: out of a menu, out of a page,
  // out of the larger surface, and finally back into him. ⌘W folds it away.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') {
        const open = document.querySelectorAll('details[open]');
        if (open.length) { open.forEach(d => { d.open = false; }); return; }
        if (view !== 'home') setView('home');
        else if (other) onPick(null);
        else if (level === WORK) setPinned(INTERACTION);
        else requestClose();
        event.preventDefault();
      } else if (event.key === 'w' && event.metaKey) {
        requestClose();
        event.preventDefault();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [view, other, level, setView, onPick]);

  const menuItems = [
    jobs.length ? { id: 'tasks', label: 'Tasks', active: view === 'tasks',
                    onPick: () => setView(view === 'tasks' ? 'home' : 'tasks') } : null,
    ...SECTIONS.map(([id, label]) => ({ id, label, active: view === id,
                                        onPick: () => setView(view === id ? 'home' : id) })),
    turns.length ? { id: 'forget', label: 'Forget this conversation', onPick: reset } : null,
  ].filter(Boolean);
  const menu = html`<${Menu} items=${menuItems} onToggle=${setMenuOpen} />`;

  const command = html`
    <${Command} agent=${other ? agent : byId.diablo} state=${other ? 'idle' : state.state}
       kind=${kind} target=${other ? agent.id : null}
       speaking=${!other && state.state === 'responding'}
       onSend=${send} onInterrupt=${interrupt} onDraft=${setDrafting} />`;

  let main;
  if (level === INTERACTION) {
    main = html`
      <${Interaction} exchange=${exchange} kind=${kind} line=${line}
         permissions=${state.permissions} onPermissions=${refresh}
         menu=${menu} onExpand=${() => setPinned(WORK)} command=${command}
         bodyRef=${bodyRef} />`;
  } else if (view === 'home') {
    main = html`
      <${Work} turns=${turns} agent=${agent} jobs=${jobs} liveAgents=${liveAgents}
         roster=${roster} permissions=${state.permissions} onPermissions=${refresh}
         menu=${menu} onCollapse=${() => setPinned(INTERACTION)} onReset=${reset}
         onHome=${() => onPick(null)} line=${line} kind=${kind} command=${command} />`;
  } else {
    main = html`
      <${Section} title=${SECTION_TITLES[view] || 'Tasks'} setView=${setView} menu=${menu}>
        ${view === 'tasks'
          ? html`<${Tasks} roster=${roster.length ? roster : AGENTS} jobs=${jobs}
                   liveAgents=${liveAgents} onPick=${onPick} />` : null}
        ${view === 'trace' ? html`<${Trace} turns=${trace} />` : null}
        ${view === 'health'
          ? html`<${Health} state=${state} onPermissions=${refresh} />` : null}
        ${view === 'settings' && settings
          ? html`<${Settings} settings=${settings} onSave=${saveSettings} />` : null}
      <//>`;
  }

  return html`
    <div class="app" ref=${appRef} data-view=${view} data-level=${level} data-kind=${kind}
         onMouseMove=${hovered ? undefined : () => setHovered(true)}
         onMouseLeave=${() => setHovered(false)}>
      ${level === WORK
        ? html`<${Rail} roster=${roster.length ? roster : AGENTS} jobs=${jobs}
                 liveAgents=${liveAgents} picked=${picked} onPick=${onPick}
                 view=${view} setView=${setView} state=${state.state} />`
        : null}
      <main class="main">${main}</main>
    </div>`;
}

root.render(html`<${App} />`);
