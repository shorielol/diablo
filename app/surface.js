/* ─── Presentation ──────────────────────────────────────────────────────────
   Which of Diablo's surfaces the page is, and what kind of moment it shows.

   Three levels, one object (skills/surface.py has the geometry):
     presence     the orb alone; the page is not on screen
     interaction  one exchange: what you asked, what he said, the line to type
     work         a larger, temporary surface when the content needs room

   The page decides the level from what it is showing, because only the page
   knows how long an answer is or whether three things are running. The shell
   decides what that level means on this screen, and the motion system moves
   between them. Everything here is pure and exported for the node tests. */

export const INTERACTION = 'interaction';
export const WORK = 'work';

/* The same numbers as skills/surface.py; test_surface checks they agree. */
export const INTERACTION_MAX_HEIGHT = 440;

/* An answer longer than a short paragraph, or one with structure (a list, a
   fence, several paragraphs), wants the work surface. */
export const LONG_CHARS = 560;

const live = jobs => (jobs || []).filter(j => j.status === 'running' || j.status === 'queued');

/* The last thing you said, and everything he has said since. He can speak
   more than once for one request ("on it", then the answer). */
export function lastExchange(turns) {
  const all = turns || [];
  let i = all.length - 1;
  while (i >= 0 && all[i].role !== 'user') i--;
  const ask = i >= 0 ? all[i].content : '';
  const answers = all.slice(i + 1).filter(t => t.role !== 'user').map(t => t.content);
  return { ask, answer: answers.join('\n\n') };
}

export function isLong(text) {
  const t = String(text || '');
  if (t.length > LONG_CHARS) return true;
  if (t.includes('```')) return true;
  const lines = t.split('\n');
  if (lines.filter(l => /^\s*([-*•]|\d+[.)])\s+/.test(l)).length >= 3) return true;
  return lines.filter(l => l.trim()).length >= 6;
}

/* What kind of moment this is. Order is priority. */
export function kindOf({ state, turns, jobs, activeTool, flash } = {}) {
  if (state === 'error') return 'error';
  if (flash && flash.kind === 'failed') return 'error';
  const running = live(jobs);
  if (running.some(j => (j.parts || []).length > 1)) return 'multistep';
  if (state === 'listening') return 'listening';
  if (state === 'processing' || state === 'preparing') return activeTool ? 'status' : 'thinking';
  if (flash && flash.kind === 'success') return 'success';
  const { ask, answer } = lastExchange(turns);
  if (!ask && !answer) return running.length ? 'status' : 'empty';
  if (!answer) return 'thinking';
  if (isLong(answer)) return 'long';
  if (/\?\s*$/.test(answer.trim())) return 'clarify';
  return 'short';
}

/* The level. The person's own choice (expand, collapse) wins over the
   content; anything that is not the conversation is always the work
   surface, because trace, health, settings and another agent's page are
   pages, not exchanges. */
export function presentation({ view = 'home', picked = null, pinned = null,
                               overflow = false, ...facts } = {}) {
  const kind = kindOf(facts);
  let level = (kind === 'long' || kind === 'multistep' || overflow) ? WORK : INTERACTION;
  if (pinned === WORK || pinned === INTERACTION) level = pinned;
  if (view !== 'home' || (picked && picked !== 'diablo')) level = WORK;
  return { level, kind };
}

/* ─── The shell ─────────────────────────────────────────────────────────────
   Messages to the native window. Numbered, so the shell can drop one that
   arrives after a newer one. Outside Diablo's window (a browser, the tests)
   there is no handler and nothing is sent. */

/* Numbered from the clock, not from zero: a page that reloads (a WebKit
   content process restarting, a refresh) must still count upward, or the
   shell would take its reports for stale ones and never resize again. */
let seq = Date.now();
let last = '';

function post(message) {
  const handler = globalThis.webkit?.messageHandlers?.surface;
  if (!handler) return false;
  handler.postMessage(message);
  return true;
}

export function reportLevel(level, height, kind) {
  const key = `${level}:${Math.ceil(height)}:${kind}`;
  if (key === last) return false;
  last = key;
  seq += 1;
  return post({ seq, level, height: Math.ceil(height), kind });
}

/* After the surface has folded away: the next report must be sent even if it
   says the same thing, because the shell is starting from nothing. */
export function forgetReports() { last = ''; }

export function reportEngaged(engaged) { return post({ engaged: !!engaged }); }
export function requestClose() { return post({ close: true }); }
export function requestDismiss(reason) { return post({ dismiss: reason || 'done' }); }
