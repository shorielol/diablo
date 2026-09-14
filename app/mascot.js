/*
  D.I.A.B.L.O. — the agents, drawn.

  One character, four temperaments. Each agent is the same mascot with a
  different accent streak, eye colour and expression, because four unrelated
  characters in a 46px avatar read as noise — the eye needs a constant to
  notice the variable.

  Drawn rather than drawn-from: the brief came with a reference image of what
  looks like a licensed anime character, so this is an original figure in the
  same register — black suit, pale face, gold eyes, a streak of colour through
  dark hair, and a grin that is slightly too pleased with itself.

  Pure SVG strings. No sprite sheet, no image requests, no dependency; they
  scale to any size and recolour by argument, which is what an avatar in six
  places at three sizes actually needs.
*/

const PALETTE = {
  hair:   '#15151A',
  hairLo: '#0B0B0E',
  skin:   '#EFE0D2',
  skinLo: '#DCC7B4',
  suit:   '#17171C',
  suitLo: '#0E0E12',
  shirt:  '#F4F4F6',
  glove:  '#FFFFFF',
  mouth:  '#FFFFFF',
  gum:    '#8E1526',
};

/*
  Expressions. The mouth is the whole personality here — the eyes barely
  change between agents, and the mouth changes everything.
*/
const MOUTHS = {
  // Wide, toothy, pleased with itself. The default.
  grin: `
    <path d="M43 70 Q60 88 77 70 Q60 77 43 70 Z" fill="${PALETTE.gum}"/>
    <path d="M43 70 Q60 87 77 70 Z" fill="${PALETTE.mouth}"/>
    <path d="M49 72.4 L49 79.5 M55 73.6 L55 82.6 M65 73.6 L65 82.6 M71 72.4 L71 79.5"
          stroke="${PALETTE.hairLo}" stroke-width="1.1" opacity="0.55"/>`,
  // Small, closed, attentive.
  calm: `<path d="M52 74 Q60 80 68 74" stroke="${PALETTE.hairLo}" stroke-width="2"
          stroke-linecap="round" fill="none"/>`,
  // Flat. Not amused.
  flat: `<path d="M51 76 L69 76" stroke="${PALETTE.hairLo}" stroke-width="2"
          stroke-linecap="round"/>`,
  // One-sided. Thinking about it.
  smirk: `<path d="M50 76 Q60 80 70 72" stroke="${PALETTE.hairLo}" stroke-width="2"
          stroke-linecap="round" fill="none"/>`,
};

/**
 * One mascot, as an SVG string.
 *
 * @param {object} opts
 * @param {string} opts.accent  the streak colour — what tells agents apart
 * @param {string} opts.eye     iris colour
 * @param {string} opts.mouth   key of MOUTHS
 * @param {number} opts.size    rendered px
 * @param {boolean} opts.wave   include the raised gloved hand
 */
export function mascot({ accent = '#E0233C', eye = '#F2C230',
                         mouth = 'grin', size = 96, wave = true } = {}) {
  const id = Math.random().toString(36).slice(2, 8);
  return `
<svg viewBox="0 0 120 120" width="${size}" height="${size}" role="img"
     aria-label="Diablo" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <clipPath id="head-${id}"><ellipse cx="60" cy="54" rx="32" ry="31"/></clipPath>
    <linearGradient id="suit-${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${PALETTE.suit}"/>
      <stop offset="1" stop-color="${PALETTE.suitLo}"/>
    </linearGradient>
  </defs>

  <!-- Body first -->
  <path d="M14 120 Q17 99 40 91 L60 100 L80 91 Q103 99 106 120 Z"
        fill="url(#suit-${id})"/>
  <path d="M47 90 L60 108 L73 90 L66 87 L60 97 L54 87 Z" fill="${PALETTE.shirt}"/>
  <path d="M60 100 L65.5 105 L63 119 L57 119 L54.5 105 Z" fill="${accent}"/>

  <!-- Neck -->
  <path d="M52 74 L52 93 Q60 99 68 93 L68 74 Z" fill="${PALETTE.skinLo}"/>

  <!-- Face -->
  <ellipse cx="60" cy="54" rx="32" ry="31" fill="${PALETTE.skin}"/>

  <g clip-path="url(#head-${id})">
    <!-- Hair as ONE solid mass with a scalloped fringe. The previous version
         built it from separate sweeps and left a pale band across the brow
         that read as a bandage. A single filled path cannot have gaps. -->
    <path d="M22 14 H98 V44
             Q90 60 81 47 Q71 62 61 48 Q51 62 42 47 Q32 60 22 46 Z"
          fill="${PALETTE.hair}"/>
    <!-- Streaks, inside the hair and stopping at the fringe -->
    <path d="M45 16 Q41 30 44.5 47 Q50.5 33 50 16 Z" fill="${accent}"/>
    <path d="M64 15 Q67 29 62.5 45 Q57 31 59 15 Z" fill="${eye}" opacity="0.92"/>
    <!-- Side panels, so the hair frames the face instead of capping it -->
    <path d="M22 30 H34 V86 H22 Z" fill="${PALETTE.hair}"/>
    <path d="M86 30 H98 V86 H86 Z" fill="${PALETTE.hair}"/>
    <path d="M22 30 H30 V86 H22 Z" fill="${PALETTE.hairLo}"/>
  </g>

  <!-- Eyes: wider than tall, which reads as a face. Taller than wide reads
       as goggles, which is what the first attempt looked like. -->
  <ellipse cx="47" cy="59" rx="10" ry="8.6" fill="${PALETTE.hairLo}"/>
  <ellipse cx="73" cy="59" rx="10" ry="8.6" fill="${PALETTE.hairLo}"/>
  <!-- The upper lid, angled inward. This is the whole expression. -->
  <path d="M36 54 Q47 47 58 55 L58 50 Q47 42 36 49 Z" fill="${PALETTE.hair}"/>
  <path d="M84 54 Q73 47 62 55 L62 50 Q73 42 84 49 Z" fill="${PALETTE.hair}"/>
  <ellipse cx="47" cy="60" rx="3.2" ry="5.6" fill="${eye}"/>
  <ellipse cx="73" cy="60" rx="3.2" ry="5.6" fill="${eye}"/>
  <ellipse cx="47" cy="60" rx="1.1" ry="4.2" fill="${PALETTE.hairLo}"/>
  <ellipse cx="73" cy="60" rx="1.1" ry="4.2" fill="${PALETTE.hairLo}"/>
  <circle cx="49.6" cy="56.4" r="1.6" fill="#fff" opacity="0.95"/>
  <circle cx="75.6" cy="56.4" r="1.6" fill="#fff" opacity="0.95"/>

  ${MOUTHS[mouth] || MOUTHS.grin}

  ${wave ? `
  <!-- A gloved hand, resting against the shoulder rather than floating in
       space beside it. Four fingers and a thumb, at this size, is enough. -->
  <g>
    <path d="M88 118 Q84 104 88 94 Q90 88 95 89 Q99 90 97.5 96
             Q100 89 104.5 90.5 Q108 92 105.5 99
             Q109 96 112 99 Q115 102 110 108
             Q108 116 100 119 Z" fill="${PALETTE.glove}"/>
    <path d="M94 106 Q100 101 107 103" stroke="${PALETTE.skinLo}" stroke-width="1"
          fill="none" opacity="0.6"/>
  </g>` : ''}
</svg>`;
}

/*
  The roster.

  Diablo is the one you talk to. The other three are what it becomes when it
  goes away to do something, and they exist because a background task that
  reports as "Diablo" is indistinguishable from the Diablo you are currently
  speaking to. Giving the work a face makes "what is it doing" answerable at a
  glance, which is the entire point of the agents panel.
*/
export const AGENTS = [
  {
    id: 'diablo', name: 'Diablo', role: 'The one you talk to',
    accent: '#E0233C', eye: '#F2C230', mouth: 'grin',
    blurb: 'Listens, decides, answers. Everything starts here.',
  },
  {
    id: 'scout', name: 'Scout', role: 'Reads the screen',
    accent: '#3DA9FC', eye: '#8FD3FF', mouth: 'calm',
    blurb: 'Window capture, OCR, and pointing at things.',
    tools: ['read_screen', 'explain_screen', 'list_windows', 'point_at', 'find_on_screen'],
  },
  {
    id: 'scribe', name: 'Scribe', role: 'Handles your files',
    accent: '#46C46B', eye: '#B6EEC6', mouth: 'smirk',
    blurb: 'Reading, writing, moving, and the arithmetic in between.',
    tools: ['read_file', 'write_file', 'append_file', 'move_file', 'find_files',
            'search_text', 'make_folder', 'python_snippet', 'create_presentation'],
  },
  {
    id: 'warden', name: 'Warden', role: 'Guards the risky ones',
    accent: '#F2C230', eye: '#FFE6A8', mouth: 'flat',
    blurb: 'Deletes, messages and shell. Asks first, every time.',
    tools: ['delete_file', 'run_shell', 'send_whatsapp', 'send_imessage', 'set_volume'],
  },
];

/** Which agent owns a given tool. Used to put a face on live activity. */
export function agentForTool(tool) {
  if (!tool) return AGENTS[0];
  for (const agent of AGENTS) {
    if (agent.tools && agent.tools.includes(tool)) return agent;
  }
  return AGENTS[0];
}

export const byId = Object.fromEntries(AGENTS.map(a => [a.id, a]));
