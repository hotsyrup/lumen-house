import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
// Public baseline 9c87ced2213cdbf306c70a3100c83d354bc41a21. These exact ten
// existing files must survive the packaging correction without a rebuild.
const publicFiles = {
  '.nojekyll': '01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b',
  'CNAME': 'f1ee71e3df601ddff207833c6d7d861ed9ff595d63a6e1fea5934102c27ad901',
  'README.md': '018de40b53645fb94721da260d966b7d2078b61a6378c054a443eb4bd0376369',
  'assets/machine-relic-004.svg': '7e6424dc61e7cf9486a96abd841e98d14cd2752521a7d41a146eeec63353aba7',
  'index.html': 'b8dd3b51bc66623410310feec8b4d0623296d8a568396429122d6f2014361bbd',
  'script.js': '2e710c9aa007d9ecde6b2c224483cb499f08eba18512053db025a99f178dfe3b',
  'skills/lumen-house/LICENSE': 'a01122e620ece4045bfc7c90c42c7b794fcc0d6f2d5600064a1b767ca5f5bff9',
  'skills/lumen-house/README.md': '706ddd566e65b948879f6a4e58909397c192e15d4ce86df50ccd49608574ab63',
  'skills/lumen-house/SKILL.md': '68cfc15d20bc11b7f2e890436085f5ee583c973e7fb3f127177a7728ada4a817',
  'styles.css': '99c3c1ff90aa0fd6a622ca41ec9e6385ca752937a4e85af378abfb38ca2f1b82',
};
const reviewFiles = {
  'index.html': '6c1fdb9c01b3f3f660fec1bd224a9d5dd1bfc52503900868fb0794b72d37629c',
  'ui.mjs': 'be0a40f380ecd4cb81e773625a42e93294f04fa8ab022f87700ef2c5e7c3ba86',
  'style.css': '72c4b52d7d787109ca3e1963b23c2f6dfc337497fc3062517b39cf5b742518ac',
};

test('observed raw Pages artifact excludes review UI and preserves existing public bytes', t => {
  // The deployed uploader archives '.', excluding only .git and .github.
  // Keep this an actual archive test: Worker gates and Jekyll rules cannot
  // protect files accidentally included by that independent static publisher.
  assert.equal(existsSync(join(root, 'exchange', '.data')), false, 'Use a clean source checkout without local demo data');
  const temporary = mkdtempSync(join(tmpdir(), 'house-pages-artifact-'));
  t.after(() => rmSync(temporary, {recursive: true, force: true}));
  const artifact = join(temporary, 'artifact.tar');
  const extracted = join(temporary, 'extracted');
  mkdirSync(extracted);
  execFileSync('tar', ['-cf', artifact, '--exclude=.git', '--exclude=.github', '-C', root, '.']);
  const entries = execFileSync('tar', ['-tf', artifact], {encoding: 'utf8'}).split('\n').filter(Boolean).map(path => path.replace(/^\.\//, ''));
  assert.equal(entries.some(path => path === '.github' || path.startsWith('.github/')), false);
  for (const name of Object.keys(reviewFiles)) assert.equal(entries.includes('exchange/' + name), false, name + ' must not ship in Pages');
  assert.equal(entries.includes('exchange/core.mjs'), true, 'Reviewed public source remains intentionally public');
  execFileSync('tar', ['-xf', artifact, '-C', extracted]);
  for (const [name, expected] of Object.entries(publicFiles)) {
    assert.equal(sha256(readFileSync(join(extracted, name))), expected, 'Existing public bytes changed: ' + name);
  }
  const reviewHashes = new Set(Object.values(reviewFiles));
  for (const path of entries.filter(path => path && !path.endsWith('/'))) {
    assert.equal(reviewHashes.has(sha256(readFileSync(join(extracted, path)))), false, 'Review asset copied into Pages under another name: ' + path);
  }
});

test('relocated local review assets preserve their exact reviewed bytes', () => {
  for (const [name, expected] of Object.entries(reviewFiles)) {
    assert.equal(sha256(readFileSync(join(root, '.github', 'exchange-review', name))), expected, name + ' changed during relocation');
    assert.equal(existsSync(join(root, 'exchange', name)), false, 'Old static review path remains: ' + name);
  }
});
