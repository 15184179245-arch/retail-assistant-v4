/** Early preload, defense in depth alongside Node's Host-owned permissions.
 * This is not an OS sandbox. No untrusted code or online Provider is admitted.
 */
import {createRequire, syncBuiltinESMExports} from 'node:module';
import {readFileSync} from 'node:fs';
if (!['linux','darwin'].includes(process.platform) || process.arch !== 'x64') throw new Error('pi_launch_platform_denied');
const require = createRequire(import.meta.url);
// Node constructs pipe-backed stdio lazily with net.Socket. Materialize only
// these inherited descriptors before denying constructors; no SDK is loaded.
void process.stdin; void process.stdout; void process.stderr;
function denied(): never { throw new Error('pi_launch_capability_denied'); }
function replace(module: string, names: readonly string[]): void {
  const target: Record<string, unknown> = require(module) as Record<string, unknown>;
  for (const name of names) {
    if (!(name in target)) continue;
    Object.defineProperty(target, name, {value: denied, enumerable: true, writable: false, configurable: false});
  }
}
// Never late-install after importing SDK or provider code. Node permissions
// independently forbid child/worker/native capabilities and non-private writes.
replace('node:child_process', ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork']);
replace('node:worker_threads', ['Worker']);
// The initialized stdio objects retain Socket's prototype (and constructor).
// Deny its network entry too so those objects cannot reconnect to a peer.
const socketPrototype = (require('node:net') as {Socket: {prototype: object}}).Socket.prototype;
Object.defineProperty(socketPrototype, 'connect', {value: denied, writable: false, configurable: false});
replace('node:net', ['connect','createConnection','createServer','Socket','Server']);
replace('node:tls', ['connect','createServer','TLSSocket','Server']);
replace('node:http', ['request','get','createServer','ClientRequest','Server','Agent']);
replace('node:https', ['request','get','createServer','Server','Agent']);
replace('node:http2', ['connect','createServer','createSecureServer']);
replace('node:dgram', ['createSocket','Socket']);
replace('node:dns', ['lookup','lookupService','resolve','resolve4','resolve6','resolveAny','resolveCaa',
  'resolveCname','resolveMx','resolveNaptr','resolveNs','resolvePtr','resolveSoa','resolveSrv','resolveTxt','reverse','Resolver']);
replace('node:dns/promises', ['lookup','lookupService','resolve','resolve4','resolve6','resolveAny','resolveCaa',
  'resolveCname','resolveMx','resolveNaptr','resolveNs','resolvePtr','resolveSoa','resolveSrv','resolveTxt','reverse','Resolver']);
for (const name of ['fetch','WebSocket','EventSource']) {
  Object.defineProperty(globalThis, name, {value: denied, configurable: false, writable: false});
}
Object.defineProperty(process, 'dlopen', {value: denied, configurable: false, writable: false});
syncBuiltinESMExports();
// The requested manifest only selects a label for identical denied capabilities.
// main independently admits its digest, complete profile and loaded sources.
let online = false;
let family = 'readonly';
if (process.argv.length === 4 && process.argv[2]?.endsWith('/pi-release.json')) {
  const manifest = JSON.parse(readFileSync(process.argv[2], 'utf8')) as Record<string,unknown>;
  if ((manifest['profile'] === `host-pi-source-node24-${process.platform}-v1` && manifest['wire_protocol'] === 'v4.pi-source/1')
      || (manifest['profile'] === `host-pi-source-node24-${process.platform}-online-v1` && manifest['wire_protocol'] === 'v4.pi-source/2')) family = 'source';
  if ((manifest['profile'] === `host-pi-discovery-node24-${process.platform}-v1` && manifest['wire_protocol'] === 'v4.pi-discovery/1')
      || (manifest['profile'] === `host-pi-discovery-node24-${process.platform}-online-v1` && manifest['wire_protocol'] === 'v4.pi-discovery/2')) family = 'discovery';
  if ((manifest['profile'] === `host-pi-memory-node24-${process.platform}-v1` && manifest['wire_protocol'] === 'v4.pi-memory/1')
      || (manifest['profile'] === `host-pi-memory-node24-${process.platform}-online-v1` && manifest['wire_protocol'] === 'v4.pi-memory/2')) family = 'memory';
  online = manifest['profile'] === `host-pi-${family}-node24-${process.platform}-online-v1`
    && manifest['wire_protocol'] === `v4.pi-${family}/2`;
}
const streaming=process.argv.length>=3&&JSON.parse(readFileSync(process.argv[2]!, 'utf8'))['profile']===`host-pi-readonly-node24-${process.platform}-online-stream-v1`;
const resultProfile=process.argv.length>=3&&JSON.parse(readFileSync(process.argv[2]!, 'utf8'))['profile']===`host-pi-readonly-node24-${process.platform}-online-stream-result-v1`;
Object.defineProperty(globalThis, Symbol.for('v4.pi.readonly.guard'), {
  value: Object.freeze({installed: true, profile: `host-pi-${family}-node24-${process.platform}-${resultProfile ? 'online-stream-result-v1' : streaming ? 'online-stream-v1' : online ? 'online-v1' : 'v1'}`}),
  configurable: false, writable: false,
});
