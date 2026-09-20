// What the surface says while he is reading. Run with node against the
// installed app/surface.js.
const HOME = process.env.DIABLO_HOME || (process.env.HOME + '/.diablo');
const S = await import(`file://${HOME}/app/surface.js`);

let pass = 0, fail = 0;
const ok = (label, cond, detail = '') => {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; console.log(`  FAIL ${label}${detail ? '  — ' + detail : ''}`); }
};

const user = t => ({ role: 'user', content: t });
const him = t => ({ role: 'assistant', content: t });
const ASKED = [user('solve this question')];
const ANSWERED = [user('solve this question'), him('The third row, at 6300.')];

console.log('\nWhile he is looking');
ok('looking has a line',
   S.screenStatus({ phase: 'looking', line: 'Looking at Preview…' }).line
   === 'Looking at Preview…');
ok('reading has one too',
   S.screenStatus({ phase: 'reading', line: 'Reading Preview…' }).line
   === 'Reading Preview…');
ok('understanding names what he found',
   S.screenStatus({ phase: 'understood', line: 'Reading Question 4…' }).line
   === 'Reading Question 4…');
ok('no screen work, no line', S.screenStatus(null) === null);
ok('a phase the page does not know is ignored, not shown',
   S.screenStatus({ phase: 'arbitrating', line: 'x' }) === null,
   'an unknown phase must not leak an internal word into the window');

console.log('\nWhat that makes the moment');
let p = S.presentation({ state: 'processing', turns: ASKED,
                         screen: { phase: 'reading', line: 'Reading Preview…' } });
ok('reading is its own kind, not thinking', p.kind === 'reading', p.kind);
ok('  and it stays on the compact surface', p.level === S.INTERACTION, p.level);
ok('reading outranks a tool name',
   S.presentation({ state: 'processing', turns: ASKED, activeTool: 'web_search',
                    screen: { phase: 'reading', line: 'Reading Preview…' } }).kind
   === 'reading');
ok('with no screen work it is thinking as before',
   S.presentation({ state: 'processing', turns: ASKED }).kind === 'thinking');
ok('a finished read does not hold the surface in reading',
   S.presentation({ state: 'idle', turns: ANSWERED,
                    screen: { phase: 'done', source: 'From Preview' } }).kind === 'short');

console.log('\nWhen he could not read it');
const failed = { phase: 'failed', line: "I couldn't capture Preview." };
ok('a failed read is a fault', S.screenStatus(failed).failed === true);
ok('  and it is said calmly, on the compact surface',
   S.presentation({ state: 'idle', turns: ASKED, screen: failed }).level === S.INTERACTION);
ok('  as an error kind',
   S.presentation({ state: 'idle', turns: ASKED, screen: failed }).kind === 'error');
ok('  carrying the sentence the runtime wrote',
   S.screenStatus(failed).line === "I couldn't capture Preview.");
ok('a failure with no sentence still says something',
   S.screenStatus({ phase: 'failed' }).line.length > 0);
ok('a failure never shows a source line', S.screenStatus(failed).source === '');

console.log('\nWhere the answer came from');
ok('a finished read offers its provenance',
   S.screenStatus({ phase: 'done', source: 'From Preview — worksheet.pdf' }).source
   === 'From Preview — worksheet.pdf');
ok('  and no status line with it',
   S.screenStatus({ phase: 'done', source: 'From Preview' }).line === '');
ok('a read still running offers none',
   S.screenStatus({ phase: 'reading', line: 'Reading Preview…' }).source === '');
ok('a read that found nothing to name offers none',
   S.screenStatus({ phase: 'done' }).source === '');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
