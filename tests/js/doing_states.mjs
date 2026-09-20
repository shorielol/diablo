// What the surface shows while Diablo is doing something. Run with node
// against the installed app/surface.js.
const HOME = process.env.DIABLO_HOME || (process.env.HOME + '/.diablo');
const S = await import(`file://${HOME}/app/surface.js`);

let pass = 0, fail = 0;
const ok = (label, cond, detail = '') => {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; console.log(`  FAIL ${label}${detail ? '  — ' + detail : ''}`); }
};

const user = t => ({ role: 'user', content: t });
const ASKED = [user('save this')];
const p = extra => S.presentation({ state: 'idle', turns: ASKED, ...extra });

const TARGETING = { phase: 'targeting', line: 'Finding Save' };
const ASKING = { phase: 'asking', line: 'Click Save? That changes something you’d have to put back. Shall I?' };
const EXECUTING = { phase: 'executing', line: 'Click Save…' };
const VERIFYING = { phase: 'verifying', line: 'Checking' };
const DONE = { phase: 'done', line: 'Done' };
const FAILED = { phase: 'failed', line: "I couldn't verify that.", fault: true };
const BLOCKED = { phase: 'blocked', line: "I can't safely identify the target." };
const AMBIG = { phase: 'ambiguous', line: 'I found two possible Save buttons. Which one?' };

console.log('\nThe phases the page knows');
ok('targeting has a line', S.doingStatus(TARGETING).line === 'Finding Save');
ok('executing has one too', S.doingStatus(EXECUTING).line === 'Click Save…');
ok('checking is its own line', S.doingStatus(VERIFYING).line === 'Checking');
ok('no task, nothing shown', S.doingStatus(null) === null);
ok('a phase the page does not know is ignored',
   S.doingStatus({ phase: 'arbitrating', line: 'x' }) === null,
   'an internal word must never leak into the window');

console.log('\nAsking needs an answer, not a status line');
ok('asking is marked as asking', S.doingStatus(ASKING).asking === true);
ok('ambiguity asks too', S.doingStatus(AMBIG).asking === true);
ok('executing does not', S.doingStatus(EXECUTING).asking === false);
ok('a question becomes its own kind', p({ doing: ASKING }).kind === 'permission');
ok('  and it outranks a screen read',
   p({ doing: ASKING, screen: { phase: 'reading', line: 'Reading Preview…' } }).kind
   === 'permission');
ok('  and outranks a lesson step',
   p({ doing: ASKING, lesson: { phase: 'guiding', line: 'Step 1 of 2 · This is Save' } }).kind
   === 'permission', 'nothing moves until it is answered');
ok('ambiguity is a question, not a guess', p({ doing: AMBIG }).kind === 'permission');

console.log('\nActing and checking read differently');
ok('executing is its own kind', p({ doing: EXECUTING }).kind === 'doing');
ok('verifying is its own kind too', p({ doing: VERIFYING }).kind === 'doing');
ok('  and their lines differ',
   S.doingStatus(EXECUTING).line !== S.doingStatus(VERIFYING).line,
   'sending the event is not the result happening');

console.log('\nHow it ends');
ok('a failure is a fault', S.doingStatus(FAILED).fault === true);
ok('  drawn as an error', p({ doing: FAILED }).kind === 'error');
ok('  and never reads as success', !/done/i.test(S.doingStatus(FAILED).line));
ok('being refused is NOT a fault', S.doingStatus(BLOCKED).fault === false,
   'a refusal is an answer, not a crash');
ok('  so it is not drawn as an error', p({ doing: BLOCKED }).kind !== 'error');
ok('done is over', S.doingStatus(DONE).over === true);
ok('  and no longer asking', S.doingStatus(DONE).asking === false);

console.log('\nThe surface stays compact');
for (const [name, d] of [['targeting', TARGETING], ['asking', ASKING],
                         ['executing', EXECUTING], ['verifying', VERIFYING],
                         ['failed', FAILED], ['blocked', BLOCKED]]) {
  ok(`  ${name} stays on the compact surface`,
     p({ doing: d }).level === S.INTERACTION, p({ doing: d }).level);
}
ok('with no task it behaves as before',
   S.presentation({ state: 'processing', turns: ASKED }).kind === 'thinking');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
