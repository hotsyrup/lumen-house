import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {lstatSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const forbiddenArtifactPath = /(?:^|\/)(?:__pycache__|\.private|\.data|\.wrangler|node_modules|evidence|frozen-house|disabled-release-candidate|rollback-assets|review-assets)(?:\/|$)|(?:\.py[co]|\.(?:sqlite(?:3)?|db)(?:[.-].*)?|\.log|\.pem|\.key)$/i;
const forbiddenPrivateFile = /(?:^|\/)(?:\.env(?:\..*)?|wrangler[^/]*|pilot-config\.json|house-worker\.[cm]?js|worker\.[cm]?js|FINAL-ACTIVE-VERSION\.json|POST-DEPLOY[^/]*|DEPLOYMENT-[^/]*)$/i;

function filesIn(directory) {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? filesIn(path) : [path];
  });
}

test('actual raw Pages artifact excludes controls review assets, trace and private runtime material', t => {
  // Replay the independent uploader's actual exclusions. Source-control ignore
  // rules do not protect an untracked local DB or trace from a raw tar upload.
  const temporary = mkdtempSync(join(tmpdir(), 'house-controls-artifact-'));
  t.after(() => rmSync(temporary, {recursive: true, force: true}));
  const artifact = join(temporary, 'artifact.tar');
  const extracted = join(temporary, 'extracted');
  mkdirSync(extracted);
  execFileSync('tar', ['-cf', artifact, '--exclude=.git', '--exclude=.github', '-C', root, '.']);
  const entries = execFileSync('tar', ['-tf', artifact], {encoding: 'utf8'})
    .split('\n').filter(Boolean).map(path => path.replace(/^\.\//, ''));
  assert.equal(entries.some(path => path === '.github/' || path.startsWith('.github/')), false);
  for (const path of entries) {
    assert.equal(forbiddenArtifactPath.test(path), false, 'Private/generated artifact entry: ' + path);
    assert.equal(forbiddenPrivateFile.test(path), false, 'Private runtime/config artifact entry: ' + path);
    assert.equal(/(?:^|\/)trace\.json$/i.test(path), false, 'Review trace must stay excluded: ' + path);
    assert.equal(/(?:^|\/)exchange-controls-review(?:\/|$)/.test(path), false, 'Review UI must stay excluded: ' + path);
  }
  assert.equal(entries.includes('exchange/controls/core.py'), true, 'Reviewed control source must remain inspectable');
  assert.equal(entries.includes('exchange/controls/service.py'), true, 'Reviewed native seam must remain inspectable');
  execFileSync('tar', ['-xf', artifact, '-C', extracted]);
  const reviewHashes = new Set(filesIn(join(root, '.github', 'exchange-controls-review'))
    .filter(path => /\.(?:html|css|mjs|json)$/.test(path)).map(path => sha256(readFileSync(path))));
  for (const path of entries.filter(path => path && !path.endsWith('/'))) {
    const extractedPath = join(extracted, path);
    assert.equal(lstatSync(extractedPath).isSymbolicLink(), false, 'Unexpected artifact symlink: ' + path);
    assert.equal(reviewHashes.has(sha256(readFileSync(extractedPath))), false, 'Review asset copied under another name: ' + path);
  }
});

test('controls sources contain no private installation paths or recognizable production credentials', () => {
  for (const path of filesIn(join(root, 'exchange', 'controls'))) {
    assert.equal(forbiddenArtifactPath.test(path.slice(root.length + 1)), false, 'Generated/private control file: ' + path);
    const source = readFileSync(path, 'utf8');
    assert.equal(/\/Users\/[A-Za-z0-9]/.test(source), false, 'Private Mac path in reviewed source: ' + path);
    assert.equal(/["']?account_id["']?\s*[:=]\s*["'][0-9a-f]{32}["']/i.test(source), false, 'Provider account ID in reviewed source: ' + path);
    assert.equal(/\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|[sr]k_live_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})\b/.test(source), false, 'Recognizable production credential in reviewed source: ' + path);
    assert.equal(/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/.test(source), false, 'Private key in reviewed source: ' + path);
  }
});

test('production-only exchange import graph remains the exact unchanged disabled passthrough', () => {
  const source = readFileSync(join(root, 'exchange', 'production-adapter.mjs'));
  assert.equal(sha256(source), 'f4037ec5a2c203fa28c6c85fa598b28740075c620710556323f0354df0fe6fb8');
  const code = source.toString('utf8').replace(/\/\/[^\n]*/g, '');
  assert.equal(/\bimport\s*(?:[({*]|["'])|\bexport\s+[^;]*\bfrom\s*["']/.test(code), false,
    'Production graph must have no controls, local server, native storage or UI dependencies');
  assert.equal(/\b(?:process|storage|EXCHANGE_ENABLED|LUMEN_HOUSE_NATIVE_SOURCE)\b/.test(code), false,
    'Production passthrough must not inspect controls, flags or storage');
});
