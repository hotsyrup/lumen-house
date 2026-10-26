import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdtempSync, readFileSync, readdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const files = folder => readdirSync(folder, {withFileTypes:true}).flatMap(entry =>
  entry.isDirectory() ? files(join(folder, entry.name)) : [join(folder, entry.name)]);

test('actual raw Pages upload excludes payment review UI and trace', t => {
  const folder = mkdtempSync(join(tmpdir(), 'house-payment-artifact-'));
  t.after(() => rmSync(folder, {recursive:true, force:true}));
  const archive = join(folder, 'artifact.tar');
  execFileSync('tar', ['-cf', archive, '--exclude=.git', '--exclude=.github', '-C', root, '.']);
  const names = execFileSync('tar', ['-tf', archive], {encoding:'utf8'}).split('\n');
  assert.equal(names.some(name => /payment-request-review|(?:^|\/)trace\.json$/.test(name)), false);
  assert.equal(names.some(name => /(?:\.py[co]|\.(?:sqlite3?|db|pem|key)|\.log)$|(?:^|\/)__pycache__\//i.test(name)), false);
  assert.equal(names.includes('./exchange/payment_requests/core.py'), true);
});

test('reviewed payment source contains no private native source, installation paths or recognizable secrets', () => {
  for (const path of files(join(root, 'exchange', 'payment_requests'))) {
    const value = readFileSync(path, 'utf8');
    assert.equal(/\/Users\/[A-Za-z0-9]|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|[sr]k_live_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})\b/.test(value), false, path);
  }
  assert.equal(createHash('sha256').update(readFileSync(join(root, 'exchange/production-adapter.mjs'))).digest('hex'),
    'f4037ec5a2c203fa28c6c85fa598b28740075c620710556323f0354df0fe6fb8');
  assert.equal(files(root).some(path => /(?:^|\/)(?:pilot_server|fulfillment_workflow|fulfillment_cdp|pilot_admin)\.py$/.test(path)), false);
});
