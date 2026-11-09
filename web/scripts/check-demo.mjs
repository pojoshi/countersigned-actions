// Drives the built demo (demo/index.html) in jsdom: clicks through scenarios and asserts on the DOM.
import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(process.argv[2] ?? join(root, 'demo/index.html'), 'utf8');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
vc.on('error', (e) => errors.push(String(e)));
// jsdom lacks TextEncoder, which every browser has.
const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/', virtualConsole: vc, beforeParse(w) { w.TextEncoder = TextEncoder; } });
const { document } = dom.window;
const $ = (id) => document.getElementById(id);
const click = (id) => $(id).dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
const stamp = () => document.querySelector('#decision .stamp')?.textContent;
const final = () => document.querySelector('#decision .final')?.textContent ?? '';

let failed = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : '  -> ' + extra}`);
  if (!cond) failed++;
};

check('page boots without script errors', errors.length === 0, errors.join(' | '));
check('every scenario has a button', document.querySelectorAll('.scn').length === 25, String(document.querySelectorAll('.scn').length));

click('scS00');
check("'delete 500 customer records' is held for a human, with risk and evidence", stamp() === 'Needs a human' && $('decision').textContent.includes('risk, out of 100') && $('decision').textContent.includes('Missing information'), stamp());
click('resetBtn');
click('scS01');
check('a small verified erasure is allowed and runs', stamp() === 'Allowed' && final().startsWith('Ran'), stamp() + final());
check('evidence cites the verified ticket', $('decision').textContent.includes('TKT-1001 is an open erasure_request'));

click('scS08');
check("'delete 10' of a 600-row segment is not trusted", stamp() === 'Refused' && $('decision').textContent.includes('600'), $('decision').textContent.slice(0, 200));

click('scS09');
check('an invented ticket is sent back with what to fix', stamp() === 'Sent back' && $('decision').textContent.includes('TKT-9999 does not exist'));

// S01 already erased that segment; a second claim of 3 records no longer matches the data
click('scS15');
check('claiming records that are already gone is sent back', stamp() === 'Sent back', stamp());
click('resetBtn');
click('scS15');
check('a hard delete waits for a human', stamp() === 'Needs a human' && document.querySelectorAll('.q-item').length === 1);
document.querySelector('.q-item .approve').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('approving it runs exactly that action', final().startsWith('Ran') && document.querySelectorAll('.q-item').length === 0, final());

click('scS13');
check('splitting 60 refunds: 5 run, the rest are held', $('decision').textContent.includes('5 of 60 ran on their own'), $('decision').textContent.slice(0, 160));

click('scS22');
check('prod scale-down at noon is deferred', stamp() === 'Not yet');
$('timeSelect').value = '3'; $('timeSelect').dispatchEvent(new dom.window.Event('change', { bubbles: true }));
check('moving into the change window releases it (replicas changed)', document.getElementById('worldPanel').textContent.includes('web (prod)9') || document.getElementById('worldPanel').textContent.includes('web (prod)8'), $('worldPanel').textContent.slice(-200));

// audit rows expand to show inputs, evidence, reasoning and outcome
[...document.querySelectorAll('#auditBody tr.clickable')].find((r) => r.children[1].textContent === 'decision').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
const detail = document.querySelector('#auditBody tr.detail pre')?.textContent ?? '';
check('an audit row expands to the stored decision', detail.includes('"inputs"') && detail.includes('"evidence"') && detail.includes('"outcome"') && detail.includes('"reasons"'), detail.slice(0, 120));
// kill switch
click('killToggle');
click('scS01');
check('kill switch refuses new work', stamp() === 'Refused' && $('decision').textContent.includes('kill switch is on'));
click('killToggle');

// tamper
click('verifyBtn');
check('chain verifies', $('verifyResult').textContent.startsWith('The chain is intact'), $('verifyResult').textContent);
$('tamperSeq').value = '3';
click('tamperBtn');
click('verifyBtn');
check('editing entry 3 breaks the chain there', $('verifyResult').textContent.includes('Broken at entry #3'), $('verifyResult').textContent);
check('the broken row is highlighted', document.querySelectorAll('#auditBody tr.bad').length === 1);

// custom proposal with a smuggled field
$('customJson').value = JSON.stringify({ operation: 'payments.transfer', params: { to_account: 'A', amount_cents: 100, reason: 'x' }, approved: true });
$('agentSelect').value = 'finance-bot';
click('submitCustom');
check('a smuggled approval field is rejected', stamp() === 'Rejected' && $('decision').textContent.includes('approved'), $('decision').textContent.slice(0, 160));

click('resetBtn');
check('start over clears the log to one entry', document.querySelectorAll('#auditBody tr').length === 1);

console.log(failed ? `\n${failed} check(s) failed` : '\nall demo checks passed');
process.exit(failed ? 1 : 0);
