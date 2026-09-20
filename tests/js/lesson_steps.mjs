// What the surface says while he is teaching. Run with node against the
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
const ASKED = [user('show me where settings is')];
const STEP = { phase: 'guiding', line: 'Step 2 of 4 · This is Export', index: 2, total: 4 };
const WAIT = { phase: 'waiting', line: 'Step 2 of 4 · Go ahead' };
const BAD = { phase: 'unverified', line: "That didn't seem to change anything.", fault: true };

console.log('\nBeing shown a step');
ok('a step has a caption', S.lessonStatus(STEP).line === 'Step 2 of 4 · This is Export');
ok('  and it is not a fault', S.lessonStatus(STEP).fault === false);
ok('waiting is marked as waiting', S.lessonStatus(WAIT).waiting === true);
ok('no lesson, no caption', S.lessonStatus(null) === null);
ok('a finished lesson stops captioning', S.lessonStatus({ phase: 'done' }) === null);
ok('a cancelled one too', S.lessonStatus({ phase: 'cancelled' }) === null);
ok('a phase the page does not know is ignored',
   S.lessonStatus({ phase: 'reanchoring', line: 'x' }) === null,
   'an unknown phase must not leak an internal word into the window');

console.log('\nWhat that makes the moment');
let p = S.presentation({ state: 'idle', turns: ASKED, lesson: STEP });
ok('teaching is its own kind', p.kind === 'guiding', p.kind);
ok('  and it stays on the compact surface', p.level === S.INTERACTION, p.level);
ok('a step outranks a screen read',
   S.presentation({ state: 'processing', turns: ASKED, lesson: STEP,
                    screen: { phase: 'reading', line: 'Reading Preview…' } }).kind
   === 'guiding', 'the step is what the person is doing right now');
ok('a step outranks a tool name',
   S.presentation({ state: 'processing', turns: ASKED, activeTool: 'web_search',
                    lesson: STEP }).kind === 'guiding');
ok('with no lesson it behaves as before',
   S.presentation({ state: 'processing', turns: ASKED }).kind === 'thinking');

console.log('\nWhen the step did not take');
ok('not-verified is a fault', S.lessonStatus(BAD).fault === true);
ok('  shown as an error kind',
   S.presentation({ state: 'idle', turns: ASKED, lesson: BAD }).kind === 'error');
ok('  said calmly on the compact surface',
   S.presentation({ state: 'idle', turns: ASKED, lesson: BAD }).level === S.INTERACTION);
ok('  and never reads as success',
   !/done|success/i.test(S.lessonStatus(BAD).line));

console.log('\nA long answer does not drag the lesson onto the work surface');
const LONG = [user('guide me'), him('A'.repeat(700))];
ok('teaching stays compact even with a long answer',
   S.presentation({ state: 'idle', turns: LONG, lesson: STEP }).level === S.INTERACTION,
   'the screen is the explanation; the surface is its caption');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
