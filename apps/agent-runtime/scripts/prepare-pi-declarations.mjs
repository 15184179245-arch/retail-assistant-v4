// Pi 1.4.0 runtime JS already uses JSON import attributes. Its emitted declarations
// omit them. Repair only the 42 pinned declaration bytes; never alter runtime JS.
import {readFileSync, writeFileSync, readdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const base = new URL('../node_modules/@earendil-works/pi-ai/', import.meta.url);
assert.equal(JSON.parse(readFileSync(new URL('package.json',base),'utf8')).version,'1.4.0');
const manifest = JSON.parse(readFileSync(new URL('./pi-ai-1.4.0-declarations.json',import.meta.url),'utf8'));
const directory = new URL('dist/providers/',base);
assert.equal(Object.keys(manifest).length,42);
assert.deepEqual(readdirSync(directory).filter(n=>n.endsWith('.models.d.ts')).sort(),Object.keys(manifest).sort());
const hash = bytes=>createHash('sha256').update(bytes).digest('hex');
// Validate the complete set before writing anything, including runtime imports.
const changes=[];
for(const [name,pins] of Object.entries(manifest)) {
 const target=new URL(name,directory), original=readFileSync(target);
 const alreadyPatched=hash(original)===pins.patched;
 if(!alreadyPatched) assert.equal(hash(original),pins.original,`Unexpected declaration bytes: ${name}`);
 const first=original.toString('utf8').split('\n')[0];
 if(!alreadyPatched) assert.match(first,/^import values from "\.\/data\/[^"\n]+\.json";$/);
 const replacement=alreadyPatched ? first : first.slice(0,-1)+' with { type: "json" };';
 assert(readFileSync(new URL(name.replace('.d.ts','.js'),directory),'utf8').split('\n').includes(replacement),`Runtime import differs: ${name}`);
 if(alreadyPatched) continue;
 const patched=original.toString('utf8').replace(first,replacement);
 assert.equal(hash(patched),pins.patched);
 changes.push([target,patched]);
}
for(const [target,patched] of changes) writeFileSync(target,patched);
console.log(`Pi 1.4.0 declarations: ${changes.length} repaired; 42 pinned files verified`);
