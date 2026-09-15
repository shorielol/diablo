// The rail, against real job payloads. Run with node.
// app.js reads the React UMD global at module scope, so stand one up first.
// Only the hooks it destructures are needed; railRows itself is pure.
const noop = () => {};
globalThis.React = { createElement: noop, useState: noop, useEffect: noop,
  useRef: noop, useCallback: noop, useMemo: noop, memo: (f) => f };
globalThis.htm = { bind: () => (() => null) };
globalThis.ReactDOM = { createRoot: () => ({ render: noop }) };
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null, addEventListener: noop,
  documentElement: { style: { setProperty: noop } } };
const HOME = process.env.DIABLO_HOME
  || (process.env.HOME + '/.diablo');
const { railRows } = await import(`file://${HOME}/app/app.js`);

let pass = 0, fail = 0;
const ok = (label, cond, detail = '') => {
  if (cond) { pass++; console.log(`  ok   ${label}`); }
  else { fail++; console.log(`  FAIL ${label}${detail ? '  — ' + detail : ''}`); }
};

const now = Date.now() / 1000;

console.log('\nNothing has happened yet');
let rows = railRows([], [], {});
ok('only Diablo is listed', rows.length === 1, `${rows.length} rows`);
ok('and it is Diablo', rows[0].pick === 'diablo' && rows[0].title === 'Diablo');
ok('no demon appears unprompted',
   !rows.some(r => ['asmo', 'marbas', 'vassago', 'seere', 'levi'].includes(r.pick)));

console.log('\nOne task, dispatched to one agent');
rows = railRows([], [{ id: 'j1', agent: 'vassago', task: 'Research Delhi apprenticeships',
                       status: 'running', created_at: now - 5 }], { vassago: 'searching the web' });
ok('a row appears for the task', rows.length === 2, `${rows.length} rows`);
ok('titled with the task, not the character',
   rows[1].title === 'Research Delhi apprenticeships', rows[1].title);
ok('the live status is the line', rows[1].line === 'searching the web', rows[1].line);
ok('and it shows as busy', rows[1].busy === true);
ok('picking it opens that agent', rows[1].pick === 'vassago');
ok('the face is still the demon', rows[1].agent.id === 'vassago');

console.log('\nThe task finishes');
rows = railRows([], [{ id: 'j1', agent: 'vassago', task: 'Research Delhi apprenticeships',
                       status: 'done', summary: 'Four firms hiring, sources named.',
                       created_at: now - 40, finished_at: now - 5 }], {});
ok('the row stays as recent work', rows.length === 2);
ok('the line becomes the answer', rows[1].line === 'Four firms hiring, sources named.');
ok('no longer busy', rows[1].busy === false);
ok('and it is stamped', typeof rows[1].when === 'string' && rows[1].when.length > 0, rows[1].when);

console.log('\nA fan-out across three agents');
rows = railRows([], [{ id: 'j2', task: 'compare two things and play music',
    created_at: now - 20,
    parts: [{ agent: 'vassago', task: 'compare two things', status: 'done', summary: 'A beats B.' },
            { agent: 'seere', task: 'play music', status: 'running' },
            { agent: 'marbas', task: 'check the files', status: 'queued' }] }], { seere: 'opening Music' });
ok('one row per piece, plus Diablo', rows.length === 4, `${rows.length} rows`);
ok('each carries its own piece',
   rows[1].title === 'compare two things' && rows[2].title === 'play music');
ok('a queued piece says so', rows[3].line.includes('queued'), rows[3].line);
ok('a running piece shows its live line', rows[2].line === 'opening Music');
ok('keys are unique so React can tell them apart',
   new Set(rows.map(r => r.key)).size === rows.length);

console.log('\nDiablo does not get two rows');
rows = railRows([], [{ id: 'j3', agent: 'diablo', task: 'what time is it',
                       status: 'done', summary: 'Ten past four.', created_at: now - 3 }], {});
ok('a solo Diablo job folds into the permanent row', rows.length === 1, `${rows.length} rows`);
ok('and becomes its line', rows[0].line === 'Ten past four.', rows[0].line);

console.log('\nDiablo inside a fan-out still gets its piece');
rows = railRows([], [{ id: 'j4', task: 'two things', created_at: now,
    parts: [{ agent: 'diablo', task: 'first thing', status: 'done', summary: 'done' },
            { agent: 'levi', task: 'second thing', status: 'done', summary: 'also done' }] }], {});
ok('both pieces are listed', rows.length === 3, `${rows.length} rows`);

console.log('\nThe hidden safety agents stay hidden');
rows = railRows([], [{ id: 'j5', agent: 'vassago', task: 'anything', status: 'done',
                       created_at: now }], {});
ok('scout, scribe and warden are nowhere in the rail',
   !rows.some(r => ['scout', 'scribe', 'warden'].includes(r.pick)));

console.log(`\n${pass}/${pass + fail} passed`);
process.exit(fail ? 1 : 0);
