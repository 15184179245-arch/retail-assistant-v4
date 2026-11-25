// Discover source suites, not stale output from an older test build.
import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const compiled = readdirSync(new URL('test/', root))
  .filter(name => name.endsWith('.test.ts'))
  .sort()
  .map(name => 'dist/test/' + name.replace(/\.ts$/, '.js'));
if (!compiled.length) throw new Error('No TypeScript test suites found');
// Other .mjs tests belong to the separately pinned Linux legacy release.
const result = spawnSync(process.execPath, ['--test', ...compiled, 'test/invalid-tool-recovery.test.mjs'],
  { cwd: fileURLToPath(root), stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
