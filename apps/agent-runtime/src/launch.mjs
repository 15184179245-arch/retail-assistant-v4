// Importable trusted assembly, not a CLI or a user-selected provider loader.
// Host already verifies the entire closure before spawn. Child rechecks only
// its digest, fixed bootstrap/module set and SDK identity, not the full closure.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {serve} from './main.mjs';
import {SDK,ProtocolError} from './protocol.mjs';
const fail=code=>{throw new ProtocolError(code);};
const hash=b=>createHash('sha256').update(b).digest('hex');
const roles={preload:'guard/preload.mjs',bootstrap:'fixtures/launch_bootstrap.mjs',launch:'candidate/apps/agent-runtime/src/launch.mjs',main:'candidate/apps/agent-runtime/src/main.mjs',protocol:'candidate/apps/agent-runtime/src/protocol.mjs',runtime:'candidate/apps/agent-runtime/src/runtime.mjs',package:'package.json'};
export function verifyLaunch(){
 const root=process.env.SPIKE_ROOT,rt=process.env.SPIKE_RUNTIME;
 if(process.platform!=='linux'||process.versions.node.split('.')[0]!=='24'||!root||!rt||!rt.startsWith(root+'/runtime/')||path.resolve(rt)!==rt||process.cwd()!==rt+'/cwd')fail('launch_profile');
 const expectedEnv={PATH:'/usr/bin:/bin',HOME:rt+'/home',TMPDIR:rt+'/tmp',XDG_CONFIG_HOME:rt+'/home',XDG_CACHE_HOME:rt+'/home/cache',LANG:'C.UTF-8',LC_ALL:'C.UTF-8',TZ:'UTC',SPIKE_ROOT:root,SPIKE_RUNTIME:rt};
 if(Object.keys(process.env).length!==Object.keys(expectedEnv).length||Object.entries(expectedEnv).some(([k,v])=>process.env[k]!==v))fail('launch_environment');
 if(process.argv.length!==4||process.argv[1]!==root+'/'+roles.bootstrap||process.argv[2]!==root+'/launch-manifest.json'||!(/^[a-f0-9]{64}$/).test(process.argv[3]))fail('launch_arguments');
 const raw=fs.readFileSync(process.argv[2]);if(raw.length>16*1024*1024||hash(raw)!==process.argv[3])fail('launch_digest');
 const m=JSON.parse(raw);if(m.profile!=='host-sdk-readonly-node24-linux-v1'||JSON.stringify(m.sdk)!==JSON.stringify(SDK))fail('launch_identity');
 const sdk=fs.readlinkSync(root+'/node_modules');
 if(path.resolve(sdk)!==sdk||fs.realpathSync(root+'/node_modules')!==sdk)fail('launch_link');
 const reads=[process.argv[2],root+'/node_modules',sdk,rt,...Object.values(roles).map(x=>root+'/'+x)];
 const args=['--permission','--no-addons','--jitless',...reads.map(x=>'--allow-fs-read='+x),'--allow-fs-write='+rt,'--import',root+'/'+roles.preload];
 if(JSON.stringify(process.execArgv)!==JSON.stringify(args))fail('launch_flags');
 const proof=[];
 for(const [role,relative]of Object.entries(roles)){
  if(m.roles[role]?.root!=='release'||m.roles[role]?.path!==relative)fail('launch_roles');
  const rows=m.files.filter(r=>r.root==='release'&&r.path===relative);if(rows.length!==1)fail('launch_roles');
  const p=root+'/'+relative,b=fs.readFileSync(p);if(fs.realpathSync(p)!==p||b.length!==rows[0].bytes||hash(b)!==rows[0].sha256)fail('launch_source');proof.push(relative);
 }
 for(const relative of ['@earendil-works/pi-coding-agent/package.json','@earendil-works/pi-coding-agent/dist/index.js']){
  const rows=m.files.filter(r=>r.root==='sdk'&&r.path===relative);if(rows.length!==1)fail('launch_sdk');const b=fs.readFileSync(sdk+'/'+relative);if(b.length!==rows[0].bytes||hash(b)!==rows[0].sha256)fail('launch_sdk');
 }
 const meta=JSON.parse(fs.readFileSync(sdk+'/@earendil-works/pi-coding-agent/package.json','utf8'));if(meta.name!==SDK.engine||meta.version!==SDK.version)fail('launch_sdk');
 const state=globalThis[Symbol.for('v4.spike.guard')];if(!state?.installed)fail('launch_guard');
 return Object.freeze({root,rt,proof,manifestDigest:process.argv[3],childRecheck:'DIGEST_FIXED_MODULES_PACKAGE_ENTRY_NOT_FULL_CLOSURE'});
}
export async function launch({model,produce,resolveAuth,observe=()=>{}}){
 const config=verifyLaunch();if(typeof produce!=='function'||typeof resolveAuth!=='function'||typeof observe!=='function')fail('launch_callbacks');
 const policy={cwd:config.rt+'/cwd',agentDir:config.rt+'/home/pi-agent',noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true};
 fs.mkdirSync(policy.agentDir,{recursive:true});observe('launch_verified',config);
 return serve({verifiedSdk:SDK,run:async(start,channel)=>{
  // This fixed relative import happens only after the validated start frame.
  const {runReadonlySession}=await import('./runtime.mjs');
  return runReadonlySession({start,channel,model,produce,resolveAuth,observe,policy,sessionDir:config.rt+'/sessions'});
 }});
}
