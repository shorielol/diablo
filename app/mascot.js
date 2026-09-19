/*
  D.I.A.B.L.O. — the mark, for the window.

  Two strokes: a short vertical source, and one arc — the front — facing out
  from it. Something that is there, and the signal leaving it. The same mark
  make_icon.py draws for the floating presence, the menu bar and the app icon,
  in the same proportions, so the window and the desktop show one thing.

  Diablo's mark takes the text colour, so it follows light and dark with the
  page. A sub-agent's front is drawn in that agent's accent: the roster stays
  readable at a glance without anyone needing a face.

  Pure SVG strings: no requests, no dependency, any size.
*/

// The mark's geometry, in its own unit square centred on (0, 0). The same
// numbers as SOURCE_X, SOURCE_HALF, FRONT_RADIUS and FRONT_SPAN in make_icon.py.
const SOURCE_X = -0.16;
const SOURCE_HALF = 0.22;
const FRONT_RADIUS = 0.30;
const FRONT_SPAN = 52;

/* Heavier at small sizes, so the two strokes stay two strokes. */
function weightFor(size) {
  if (size <= 18) return 0.125;
  if (size <= 32) return 0.105;
  return 0.09;
}

/**
 * The mark, as an SVG string.
 *
 * @param {object} opts
 * @param {string|null} opts.accent  a sub-agent's colour for the front; null
 *                                   draws the whole mark in the text colour
 * @param {number} opts.size         rendered px
 *
 * `eye`, `mouth`, `wave` and `crop` are accepted and ignored: they belonged to
 * the character this replaced, and callers still pass them.
 */
export function mascot({ accent = null, size = 96 } = {}) {
  const w = weightFor(size);
  const a = FRONT_SPAN * Math.PI / 180;
  const fx = (SOURCE_X + FRONT_RADIUS * Math.cos(a)).toFixed(4);
  const fy = (FRONT_RADIUS * Math.sin(a)).toFixed(4);
  const front = accent || 'currentColor';
  return `
<svg viewBox="-0.72 -0.72 1.44 1.44" width="${size}" height="${size}" role="img"
     aria-label="Diablo" xmlns="http://www.w3.org/2000/svg"
     fill="none" stroke-linecap="round" stroke-width="${w}">
  <line x1="${SOURCE_X}" y1="${-SOURCE_HALF}" x2="${SOURCE_X}" y2="${SOURCE_HALF}"
        stroke="currentColor"/>
  <path d="M ${fx} ${-fy} A ${FRONT_RADIUS} ${FRONT_RADIUS} 0 0 1 ${fx} ${fy}"
        stroke="${front}"/>
</svg>`;
}

/*
  The roster.

  Diablo is the one you talk to. It does not do the work itself — it hands the
  pieces out and collects what comes back, and the others are who it hands them
  to. Names are Goetic, picked for what each one was said to do rather than for
  sounding sinister: Asmodeus knew things, Marbas answered of hidden things and
  the mechanical arts, Vassago declared what was lost, Seere went anywhere in an
  instant, and Leviathan was simply the deep one.

  This list mirrors skills/roster.py, which is the source of truth and is
  served at /api/agents. It is duplicated here only so the window has something
  to draw in the moment before the first fetch lands.

  There used to be three others on this panel — a reader, a writer and a guard.
  They were never agents. They were a drawing of the permission gate, and that
  gate is not a thing to put on a card: it runs on every mutating call whatever
  name is on the job, and it is not optional, not selectable, and not the
  user's business to manage. It is still there. It just is not scenery.
*/
export const AGENTS = [
  { id: 'diablo',  name: 'Diablo',  role: 'The one you talk to',
    accent: '#E0233C', eye: '#F2C230', mouth: 'grin',
    blurb: 'Listens, decides, and hands the work out.' },
  { id: 'asmo',    name: 'Asmo',    role: 'Finds things out',
    accent: '#8B5CF6', eye: '#D6BCFA', mouth: 'smirk',
    blurb: 'The web, encyclopaedias, and anything you want looked up.' },
  { id: 'marbas',  name: 'Marbas',  role: 'Papers and numbers',
    accent: '#46C46B', eye: '#B6EEC6', mouth: 'calm',
    blurb: 'Reads, writes, moves and totals whatever is on disk.' },
  { id: 'vassago', name: 'Vassago', role: 'Watches the screen',
    accent: '#3DA9FC', eye: '#8FD3FF', mouth: 'calm',
    blurb: 'Window capture, reading text off the glass, pointing at things.' },
  { id: 'seere',   name: 'Seere',   role: 'Runs the errands',
    accent: '#F2913D', eye: '#FFD8A8', mouth: 'grin',
    blurb: 'Apps, music, volume, messages. In and out quickly.' },
  { id: 'levi',    name: 'Levi',    role: 'The deep one',
    accent: '#D93AA8', eye: '#FFC2E9', mouth: 'flat',
    blurb: 'Wakes the larger model when the small one is out of its depth.' },
];

export const byId = Object.fromEntries(AGENTS.map(a => [a.id, a]));

/** Index a roster fetched from /api/agents, falling back to the built-in one. */
export function index(list) {
  if (!Array.isArray(list) || !list.length) return byId;
  return Object.fromEntries(list.map(a => [a.id, a]));
}

/** Look one up by id without ever returning undefined. */
export function agent(id, roster) {
  return (roster || byId)[id] || byId.diablo;
}
