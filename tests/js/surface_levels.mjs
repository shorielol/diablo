// Which surface, for what. Run with node against the installed app/surface.js.
const HOME = process.env.DIABLO_HOME || (process.env.HOME + '/.diablo');
const S = await import(`file://${HOME}/app/surface.js`);

let pass = 0, fail = 0;
const ok = (label, cond, detail = '') => {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; console.log(`  FAIL ${label}${detail ? '  — ' + detail : ''}`); }
};

const user = t => ({ role: 'user', content: t });
const him = t => ({ role: 'assistant', content: t });
const SHORT = [user('What is the capital of Australia?'), him('Canberra.')];
const LONG_TEXT = 'A'.repeat(700);
const LIST = him('Three ways:\n1. one thing\n2. another thing\n3. a third thing');

console.log('\nOne exchange stays compact');
let p = S.presentation({ state: 'idle', turns: SHORT });
ok('a short answer is the compact surface', p.level === S.INTERACTION, p.level);
ok('  and it knows what it is', p.kind === 'short', p.kind);
ok('nothing said yet is still compact',
   S.presentation({ state: 'idle', turns: [] }).level === S.INTERACTION);
ok('  and that kind is empty',
   S.presentation({ state: 'idle', turns: [] }).kind === 'empty');
ok('a question back to you is compact and named',
   S.presentation({ state: 'idle', turns: [user('open the budget'), him('Which one: a or b?')] }).kind
   === 'clarify');

console.log('\nWork the content needs room for');
ok('a long answer opens the work surface',
   S.presentation({ state: 'idle', turns: [user('how?'), him(LONG_TEXT)] }).level === S.WORK);
ok('  by its length', S.isLong(LONG_TEXT));
ok('a list of steps counts as long', S.isLong(LIST.content));
ok('code does too', S.isLong('here:\n```js\nconst a = 1;\n```'));
ok('one paragraph does not', !S.isLong('Canberra. Sydney is bigger, which is why people guess it.'));
const parts = [{ id: 'j', status: 'running', parts: [{ agent: 'asmo' }, { agent: 'marbas' }] }];
p = S.presentation({ state: 'idle', turns: SHORT, jobs: parts });
ok('several things at once open the work surface', p.level === S.WORK, p.level);
ok('  and that is multistep', p.kind === 'multistep', p.kind);
ok('an exchange too tall for the compact surface moves up',
   S.presentation({ state: 'idle', turns: SHORT, overflow: true }).level === S.WORK);

console.log('\nStates of the moment');
ok('thinking', S.presentation({ state: 'processing', turns: [user('hi')] }).kind === 'thinking');
ok('using a tool', S.presentation({ state: 'processing', turns: [user('hi')], activeTool: 'web_search' }).kind
   === 'status');
ok('listening', S.presentation({ state: 'listening', turns: SHORT }).kind === 'listening');
ok('a fault', S.presentation({ state: 'error', turns: SHORT }).kind === 'error');
ok('  a failed job is a fault too',
   S.presentation({ state: 'idle', turns: SHORT, flash: { kind: 'failed' } }).kind === 'error');
ok('done', S.presentation({ state: 'idle', turns: SHORT, flash: { kind: 'success' } }).kind === 'success');
ok('  and stays compact to say so',
   S.presentation({ state: 'idle', turns: SHORT, flash: { kind: 'success' } }).level === S.INTERACTION);
ok('a fault is never louder than compact',
   S.presentation({ state: 'error', turns: SHORT }).level === S.INTERACTION);

console.log('\nYour choice, and the pages');
ok('asking for it larger wins over a short answer',
   S.presentation({ state: 'idle', turns: SHORT, pinned: S.WORK }).level === S.WORK);
ok('collapsing it wins over a long answer',
   S.presentation({ state: 'idle', turns: [user('how?'), him(LONG_TEXT)], pinned: S.INTERACTION }).level
   === S.INTERACTION);
ok('settings is a page, so the work surface',
   S.presentation({ view: 'settings', state: 'idle', turns: [] }).level === S.WORK);
ok("another agent's work is a page too",
   S.presentation({ picked: 'vassago', state: 'idle', turns: SHORT }).level === S.WORK);
ok('Diablo himself is not',
   S.presentation({ picked: 'diablo', state: 'idle', turns: SHORT }).level === S.INTERACTION);

console.log('\nThe exchange shown is the last one');
const many = [user('first'), him('one'), user('second'), him('two'), him('and more')];
const ex = S.lastExchange(many);
ok('the latest question', ex.ask === 'second', ex.ask);
ok('everything he said after it', ex.answer === 'two\n\nand more', JSON.stringify(ex.answer));
ok('nothing said yet is empty, not undefined', S.lastExchange([]).ask === '');

console.log('\nReports to the shell');
ok('outside the window nothing is posted', S.reportLevel(S.INTERACTION, 100, 'short') === false);
const sent = [];
globalThis.webkit = { messageHandlers: { surface: { postMessage: m => sent.push(m) } } };
S.forgetReports();
S.reportLevel(S.INTERACTION, 120, 'short');
S.reportLevel(S.INTERACTION, 120, 'short');
S.reportLevel(S.WORK, 0, 'long');
ok('the same report is not sent twice', sent.length === 2, `${sent.length} sent`);
ok('each is numbered upward', sent[1].seq > sent[0].seq);
ok('numbered from the clock, so a reload still counts up', sent[0].seq > 1e12, String(sent[0].seq));
ok('heights are whole points', Number.isInteger(sent[0].height));
S.requestClose(); S.requestDismiss('success'); S.reportEngaged(true);
ok('closing, dismissing and engagement each go over', sent.length === 5, `${sent.length}`);
ok('  and say what they are',
   sent[2].close === true && sent[3].dismiss === 'success' && sent[4].engaged === true);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
