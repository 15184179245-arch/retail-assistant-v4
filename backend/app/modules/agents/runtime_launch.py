"""Host-owned launch assembly, not a CLI, release signer or untrusted plugin API.

Expected digest and roots must come from independent trusted Host configuration.
The release and SDK tree must not be writable by untrusted subjects. Hashing does
not remove TOCTOU. The old supervisor remains an internal trusted lower-level API.
"""
from backend.app.modules.agents.pi_runtime_contract import contract_for_profile, profiles_for_platform
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path, PurePosixPath
from hashlib import sha256, file_digest
import json,os,stat,platform
from backend.app.modules.agents import runtime_supervisor as supervisor
from backend.app.workers.agent_executor import _sdk_exception_detail

PROFILE='host-sdk-readonly-node24-linux-v1'
RELEASE_ROLES={'preload':'guard/preload.mjs','bootstrap':'fixtures/launch_bootstrap.mjs','launch':'candidate/apps/agent-runtime/src/launch.mjs','main':'candidate/apps/agent-runtime/src/main.mjs','protocol':'candidate/apps/agent-runtime/src/protocol.mjs','runtime':'candidate/apps/agent-runtime/src/runtime.mjs','package':'package.json'}
SDK_ROLES={'sdk_package':'@earendil-works/pi-coding-agent/package.json','sdk_entry':'@earendil-works/pi-coding-agent/dist/index.js'}
class LaunchError(ValueError):pass
def reject(code):raise LaunchError(code)
def pairs(items):
 d={}
 for k,v in items:
  if k in d:reject('manifest_duplicate_key')
  d[k]=v
 return d
def strict_json(raw):
 try:return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:reject('manifest_number'))
 except (UnicodeError,json.JSONDecodeError):reject('manifest_json')
def exact(value,keys):
 if type(value)is not dict or set(value)!=set(keys):reject('manifest_fields')
def relative(p):
 if type(p)is not str or not p or '\\'in p or '\0'in p or p.startswith('/')or any(x in ('','..','.')for x in p.split('/')):reject('manifest_path')
 return p
def regular(root,name):
 p=root
 for part in relative(name).split('/'):
  p=p/part
  if p.is_symlink():reject('unapproved_symlink')
 if not stat.S_ISREG(p.stat().st_mode):reject('not_regular_file')
 return p
def digest_ok(value):return type(value)is str and len(value)==64 and all(c in '0123456789abcdef'for c in value)
@dataclass(frozen=True)
class HostConfig:
 release_root:Path
 sdk_root:Path
 node_binary:Path
 expected_manifest_sha256:str
 manifest_name:str='launch-manifest.json'
@dataclass(frozen=True)
class PreparedRelease:
 config:HostConfig
 verified_sdk:supervisor.VerifiedSdk
 manifest_sha256:str
 files_verified:int
def _config_shape(config):
 """Pure lexical/type admission. No stat, resolve, read, mkdir or DB work."""
 if type(config)is not HostConfig or set(vars(config))!={'release_root','sdk_root','node_binary','expected_manifest_sha256','manifest_name'}:reject('config_shape')
 if not digest_ok(config.expected_manifest_sha256)or type(config.manifest_name)is not str or config.manifest_name!='launch-manifest.json':reject('config_shape')
 for value in (config.release_root,config.sdk_root,config.node_binary):
  if type(value)not in (str,type(Path())):reject('config_path_shape')
  raw=str(value);p=Path(raw)
  if not p.is_absolute()or raw!=str(p)or raw.startswith('//')or '..'in p.parts or '\\'in raw or '\0'in raw:reject('config_path_shape')
 if Path(config.node_binary).name!='node':reject('config_path_shape')
def _prepared_shape(prepared):
 """Invalid descriptors cannot acquire failure-disposition ownership."""
 if type(prepared)is not PreparedRelease or set(vars(prepared))!={'config','verified_sdk','manifest_sha256','files_verified'}:reject('unprepared_release')
 _config_shape(prepared.config)
 if not digest_ok(prepared.manifest_sha256)or prepared.manifest_sha256!=prepared.config.expected_manifest_sha256:reject('prepared_digest_shape')
 if type(prepared.files_verified)is not int or prepared.files_verified<=0:reject('prepared_count_shape')
 sdk=prepared.verified_sdk
 if type(sdk)is not supervisor.VerifiedSdk or set(vars(sdk))!={'package','entry','package_sha256','entry_sha256'}:reject('prepared_sdk_shape')
 if any(type(x)is not str for x in (sdk.package,sdk.entry))or not digest_ok(sdk.package_sha256)or not digest_ok(sdk.entry_sha256):reject('prepared_sdk_shape')
 root=Path(prepared.config.sdk_root)
 if sdk.package!=str(root/SDK_ROLES['sdk_package'])or sdk.entry!=str(root/SDK_ROLES['sdk_entry']):reject('prepared_sdk_shape')
def _private_shape(prepared,private_tree):
 """Lexical scope only: existence, links and access are operational checks."""
 if type(private_tree)not in (str,type(Path())):reject('private_tree_shape')
 raw=str(private_tree);p=Path(raw)
 if not p.is_absolute()or raw!=str(p)or raw.startswith('//')or '..'in p.parts or '\\'in raw or '\0'in raw:reject('private_tree_shape')
 if Path(prepared.config.release_root)/'runtime'not in p.parents:reject('private_tree_scope')
def prepare_release(config):
 _config_shape(config)
 roots={'release':Path(config.release_root),'sdk':Path(config.sdk_root),'node':Path(config.node_binary).parent}
 for root in roots.values():
  if not root.is_absolute()or root!=root.resolve()or not root.is_dir():reject('trusted_root')
 node=Path(config.node_binary)
 if node.name!='node'or node!=node.resolve():reject('node_profile')
 manifest=regular(roots['release'],config.manifest_name);raw=manifest.read_bytes()
 if len(raw)>16*1024*1024 or sha256(raw).hexdigest()!=config.expected_manifest_sha256:reject('manifest_digest')
 data=strict_json(raw);exact(data,('profile','sdk','roles','files','links'))
 if data['profile']!=PROFILE or data['sdk']!=supervisor.wire.SDK:reject('manifest_identity')
 roles={k:{'root':'release','path':v}for k,v in RELEASE_ROLES.items()}
 roles.update({k:{'root':'sdk','path':v}for k,v in SDK_ROLES.items()});roles['node']={'root':'node','path':'node'}
 if data['roles']!=roles:reject('manifest_roles')
 if data['links']!=[{'root':'release','path':'node_modules','target_root':'sdk'}]:reject('manifest_links')
 link=roots['release']/'node_modules'
 if not link.is_symlink()or os.readlink(link)!=str(roots['sdk'])or link.resolve()!=roots['sdk']:reject('manifest_links')
 if type(data['files'])is not list:reject('manifest_files')
 seen={};sdk_names=set()
 for row in data['files']:
  exact(row,('root','path','bytes','sha256'))
  if row['root']not in roots or type(row['bytes'])is not int or row['bytes']<0 or not digest_ok(row['sha256']):reject('manifest_file')
  key=(row['root'],relative(row['path']))
  if key in seen:reject('manifest_duplicate_file')
  if key[0]=='release'and key[1]not in RELEASE_ROLES.values():reject('manifest_release_file')
  if key[0]=='node'and key[1]!='node':reject('manifest_node_file')
  p=regular(roots[key[0]],key[1]);raw=p.read_bytes()
  if len(raw)!=row['bytes']or sha256(raw).hexdigest()!=row['sha256']:reject('manifest_file_digest')
  seen[key]=row
  if key[0]=='sdk':sdk_names.add(key[1])
 actual=set()
 for parent,dirs,files in os.walk(roots['sdk'],followlinks=False):
  for name in dirs+files:
   p=Path(parent)/name
   if p.is_symlink():reject('unapproved_symlink')
  actual.update(str((Path(parent)/name).relative_to(roots['sdk']))for name in files)
 if actual!=sdk_names:reject('sdk_closure')
 for role in roles.values():
  if (role['root'],role['path'])not in seen:reject('manifest_missing_role')
 meta=seen[('sdk',SDK_ROLES['sdk_package'])];entry=seen[('sdk',SDK_ROLES['sdk_entry'])]
 verified=supervisor.verify_sdk(package=roots['sdk']/SDK_ROLES['sdk_package'],entry=roots['sdk']/SDK_ROLES['sdk_entry'],package_sha256=meta['sha256'],entry_sha256=entry['sha256'])
 return PreparedRelease(config,verified,config.expected_manifest_sha256,len(seen))
def preflight_sdk_private_tree(prepared,private_tree):
 """Check a candidate under a verified legacy SDK release without creating it."""
 root=Path(prepared.config.release_root)
 private=Path(private_tree)
 if not private.is_absolute()or root/'runtime'not in private.parents or private.exists()or private.is_symlink():reject('private_tree_reused_or_foreign')
 relative(str(private.relative_to(root)))
 if private.parent.resolve()!=private.parent or not private.parent.is_dir():reject('private_tree_parent')
 return private
def prepare_invocation(prepared,private_tree):
 _prepared_shape(prepared)
 _private_shape(prepared,private_tree)
 # Repeat before spawn; this is still an assumption of Host-owned immutability,
 # not an atomic filesystem snapshot or a new persistent per-Run source pin.
 prepared=prepare_release(prepared.config);config=prepared.config;root=Path(config.release_root)
 private=preflight_sdk_private_tree(prepared,private_tree)
 private.mkdir(mode=0o700)
 for name in ('home','tmp','cwd','sessions'):(private/name).mkdir(mode=0o700)
 reads=[root/config.manifest_name,root/'node_modules',Path(config.sdk_root),private,*[root/v for v in RELEASE_ROLES.values()]]
 argv=[str(config.node_binary),'--permission','--no-addons','--jitless',*['--allow-fs-read='+str(x)for x in reads],'--allow-fs-write='+str(private),'--import',str(root/RELEASE_ROLES['preload']),str(root/RELEASE_ROLES['bootstrap']),str(root/config.manifest_name),prepared.manifest_sha256]
 env={'PATH':'/usr/bin:/bin','HOME':str(private/'home'),'TMPDIR':str(private/'tmp'),'XDG_CONFIG_HOME':str(private/'home'),'XDG_CACHE_HOME':str(private/'home/cache'),'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TZ':'UTC','SPIKE_ROOT':str(root),'SPIKE_RUNTIME':str(private)}
 return prepared,argv,str(private/'cwd'),env
def run_prepared(*,prepared,private_tree,bridge,prompt,publish=lambda report:None,observe=lambda kind,data:None):
 _prepared_shape(prepared)
 _private_shape(prepared,private_tree)
 supervisor.admit_bridge(bridge) # Invalid/foreign input cannot consume a binding.
 try:
  ready,argv,cwd,env=prepare_invocation(prepared,private_tree)
 except BaseException as primary:
  # Structural fact: supervisor has not been called, spawn has not been tried.
  bridge.closed=True;status='unresolved';recovery_error=None
  if bridge.close_attempts:status=bridge.close_attempts[0]['status']
  else:
   try:status=bridge.abandon_unknown()
   except BaseException as error:recovery_error=_sdk_exception_detail(error)
  report={'stage':'factory_prepare','spawn_started':False,'terminated':True,'disposition':status,'primary':_sdk_exception_detail(primary),'recovery_error':recovery_error,'close_attempts':bridge.close_attempts}
  try:publish(report)
  except BaseException as error:
   wrapped=LaunchError('launch_publication_failed');wrapped.primary_failure=report['primary'];wrapped.publication_failure=_sdk_exception_detail(error);wrapped.close_attempts=bridge.close_attempts
   raise wrapped from primary
  raise
 # Deliberately outside factory recovery catch. Supervisor owns the entire call.
 return supervisor.run_session(bridge=bridge,argv=argv,cwd=cwd,env=env,verified_sdk=ready.verified_sdk,prompt=prompt,publish=publish,observe=observe)



PI_PROFILE = "host-pi-readonly-node24-linux-v1"
PI_DARWIN_PROFILE = "host-pi-readonly-node24-darwin-v1"


def host_pi_profile():
    system, machine = platform.system(), platform.machine()
    if machine not in ("x86_64", "AMD64"):
        reject("pi_host_platform")
    if system == "Linux":
        return PI_PROFILE
    if system == "Darwin" and machine == "x86_64":
        return PI_DARWIN_PROFILE
    reject("pi_host_platform")


def _pi_node_platform(path, profile):
    with path.open("rb") as stream:
        header = stream.read(20)
    if profile == PI_PROFILE:
        valid = (header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00")
    elif profile == PI_DARWIN_PROFILE:
        valid = header[:8] == b"\xcf\xfa\xed\xfe\x07\x00\x00\x01"
    else:
        valid = False
    if not valid:
        reject("pi_node_platform")
PI_RELEASE_ROLES = {
    "preload": "dist/src/permission-guard.js", "main": "dist/src/main.js",
    "protocol": "dist/src/protocol.js", "runtime": "dist/src/runtime.js",
    "provider": "dist/src/provider.js", "tool_bridge": "dist/src/tool-bridge.js",
    "package": "package.json", "lock": "package-lock.json",
}
def pi_release_roles(profile):
    return {**PI_RELEASE_ROLES, **({'capabilities':'dist/src/capabilities.js'} if profile.startswith(('host-pi-discovery-', 'host-pi-source-', 'host-pi-workbench-')) else {})}


PI_SYNTHETIC_CASES = frozenset(("tasks", "knowledge", "catalog", "memory", "memory-save", "memory-correct", "memory-withdraw"))
PI_SOURCE_SYNTHETIC_CASES = frozenset(('source-prices', 'source-stock'))


@dataclass(frozen=True)
class PiHostConfig:
    release_root: Path
    sdk_root: Path
    node_binary: Path
    expected_manifest_sha256: str
    manifest_name: str = "pi-release.json"


@dataclass(frozen=True)
class PreparedPiRelease:
    config: PiHostConfig
    verified_sdk: supervisor.VerifiedSdk
    manifest_sha256: str
    files_verified: int
    launch_profile: str

    def runtime_record(self):
        contract = contract_for_profile(self.launch_profile)
        return {"contract_version": contract.version,
                "wire_protocol": contract.wire_protocol,
                "launch_profile": self.launch_profile, "source_manifest_sha256": self.manifest_sha256}


def _pi_config_shape(config):
    if (type(config) is not PiHostConfig
            or set(vars(config)) != {"release_root", "sdk_root", "node_binary", "expected_manifest_sha256", "manifest_name"}
            or config.manifest_name != "pi-release.json" or not digest_ok(config.expected_manifest_sha256)):
        reject("pi_config_shape")
    for value in (config.release_root, config.sdk_root, config.node_binary):
        if type(value) not in (str, type(Path())):
            reject("pi_config_path")
        raw = str(value)
        path = Path(raw)
        if (not path.is_absolute() or raw != str(path) or raw.startswith("//")
                or ".." in path.parts or "\\" in raw or "\0" in raw):
            reject("pi_config_path")
    if (Path(config.sdk_root) != Path(config.release_root)/"node_modules"
            or Path(config.node_binary).name != "node"):
        reject("pi_config_path")


def _pi_file_fingerprint(root, name):
    """Fresh byte-for-byte verification; no path, stat or digest cache.

    String paths avoid constructing Path objects for every ancestor of every
    SDK file. Keep the same trusted-root/non-atomic-snapshot contract, and
    additionally refuse a leaf replaced by a link or special file at open.
    """
    path = os.fspath(root)
    for part in relative(name).split('/'):
        path = os.path.join(path, part)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            reject('unapproved_symlink')
    if not stat.S_ISREG(info.st_mode):
        reject('not_regular_file')
    return _pi_open_fingerprint(path, info)


def _pi_open_fingerprint(path, info, *, dir_fd=None):
    """Read every byte through the admitted directory; never trust stat alone."""
    if not stat.S_ISREG(info.st_mode):
        reject('not_regular_file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            reject('not_regular_file')
        fingerprint = file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode')
    if any(getattr(before, key) != getattr(after, key) or
           getattr(info, key) != getattr(before, key) for key in fields):
        reject('pi_manifest_file_changed')
    return before.st_size, fingerprint, stat.S_IMODE(before.st_mode)


def _pi_verify_sdk_files(root, expected, links):
    """One fresh descriptor walk, with full per-file hashes and exact closure.

    fwalk opens each directory without following symlinks. Relative leaf opens
    stay bound to that directory while bytes are read, avoiding a separate
    ancestor lstat chain for every leaf. No descriptor or digest is retained
    between verifications; this remains a non-atomic consistency check.
    """
    root = os.fspath(root)
    prefix = root + os.sep
    actual, actual_links = set(), set()
    def unreadable(error):
        raise error
    with closing(os.fwalk(root, follow_symlinks=False, onerror=unreadable)) as walk:
        for parent, dirs, files, directory in walk:
            before = os.fstat(directory)
            for name in dirs:
                if stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                    reject('pi_sdk_directory_link')
            relative_parent = '' if parent == root else parent[len(prefix):] + '/'
            for name in files:
                key = relative_parent + name
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    if key not in links or os.readlink(name, dir_fd=directory) != links[key]:
                        reject('pi_manifest_link')
                    actual_links.add(key)
                    continue
                row = expected.get(key)
                if row is None:
                    reject('pi_sdk_closure')
                size, fingerprint, mode = _pi_open_fingerprint(name, info, dir_fd=directory)
                if (size != row['bytes'] or fingerprint != row['sha256'] or mode != row['mode']):
                    reject('pi_manifest_file_digest')
                actual.add(key)
            after = os.fstat(directory)
            current = os.stat(parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')
            if any(getattr(before, key) != getattr(after, key) or
                   getattr(after, key) != getattr(current, key) for key in fields):
                reject('pi_sdk_directory_changed')
    if actual != set(expected) or actual_links != set(links):
        reject('pi_sdk_closure')


def prepare_pi_release(config):
    """Verify a formal built package and exact installed closure, never a fixture.

    The expected digest is trusted Host deployment input. This is consistency
    verification, not a signature or atomic filesystem snapshot. No downloads,
    npm/install, build, subprocess or credentials are used by this function.
    """
    _pi_config_shape(config)
    profile = host_pi_profile()
    roots = {"release": Path(config.release_root), "sdk": Path(config.sdk_root),
             "node": Path(config.node_binary).parent}
    for root in roots.values():
        if not root.is_dir() or root.resolve() != root:
            reject("pi_trusted_root")
    raw = regular(roots["release"], config.manifest_name).read_bytes()
    if len(raw) > 16*1024*1024 or sha256(raw).hexdigest() != config.expected_manifest_sha256:
        reject("pi_manifest_digest")
    manifest = strict_json(raw)
    exact(manifest, ("profile", "wire_protocol", "sdk", "provider", "roles", "files", "links"))
    try:
        contract = contract_for_profile(manifest["profile"])
    except ValueError:
        reject("pi_manifest_identity")
    if manifest["profile"] not in profiles_for_platform(profile):
        reject("pi_manifest_identity")
    family = contract.family
    memory, discovery, source = family == "memory", family == "discovery", family == "source"
    streaming, online = contract.streaming, contract.online
    profile = contract.launch_profile
    if (manifest["wire_protocol"] != contract.wire_protocol
            or manifest["sdk"] != supervisor.wire.SDK):
        reject("pi_manifest_identity")
    if online:
        if manifest["provider"] != {"kind": "host-proxy", "provider": "opencode-go", "model": "deepseek-v4.1-flash"}:
            reject("pi_provider_disabled")
    else:
        exact(manifest["provider"], ("kind", "case"))
        if (manifest["provider"]["kind"] != "synthetic"
                or type(manifest["provider"]["case"]) is not str
                or manifest["provider"]["case"] not in (PI_SYNTHETIC_CASES | PI_SOURCE_SYNTHETIC_CASES if source else PI_SYNTHETIC_CASES)):
            reject("pi_provider_disabled")
    release_roles = pi_release_roles(profile)
    roles = {key: {"root": "release", "path": path} for key, path in release_roles.items()}
    roles.update({key: {"root": "sdk", "path": path} for key, path in SDK_ROLES.items()})
    roles["node"] = {"root": "node", "path": "node"}
    if manifest["roles"] != roles or type(manifest["files"]) is not list or type(manifest["links"]) is not list:
        reject("pi_manifest_roles")
    seen = {}
    for row in manifest["files"]:
        exact(row, ("root", "path", "bytes", "sha256", "mode"))
        if (row["root"] not in roots or type(row["bytes"]) is not int or row["bytes"] < 0
                or not digest_ok(row["sha256"]) or type(row["mode"]) is not int):
            reject("pi_manifest_file")
        key = row["root"], relative(row["path"])
        if key in seen or key[0] == "release" and key[1] not in release_roles.values():
            reject("pi_manifest_file")
        if key[0] == "node" and key[1] != "node":
            reject("pi_manifest_file")
        if key[0] != 'sdk':
            size, fingerprint, mode = _pi_file_fingerprint(roots[key[0]], key[1])
            if (size != row["bytes"] or fingerprint != row["sha256"] or mode != row["mode"]):
                reject("pi_manifest_file_digest")
        seen[key] = row
    for role in roles.values():
        if (role["root"], role["path"]) not in seen:
            reject("pi_manifest_missing_role")
    links = {}
    for item in manifest["links"]:
        exact(item, ("path", "target"))
        name = relative(item["path"])
        if name in links or not name.startswith(".bin/") or type(item["target"]) is not str:
            reject("pi_manifest_link")
        file = roots["sdk"]/name
        if not file.is_symlink() or os.readlink(file) != item["target"]:
            reject("pi_manifest_link")
        target = file.resolve(strict=True)
        if not target.is_relative_to(roots["sdk"]) or ("sdk", str(target.relative_to(roots["sdk"]))) not in seen:
            reject("pi_manifest_link")
        links[name] = item["target"]
    _pi_verify_sdk_files(roots['sdk'], {path: row for (root, path), row in seen.items() if root == 'sdk'}, links)
    package = strict_json(regular(roots["release"], PI_RELEASE_ROLES["package"]).read_bytes())
    lock = strict_json(regular(roots["release"], PI_RELEASE_ROLES["lock"]).read_bytes())
    if (package.get("name") != "@retail/agent-runtime" or package.get("version") != "0.1.0"
            or package.get("private") is not True or package.get("type") != "module"
            or package.get("dependencies", {}).get("@earendil-works/pi-coding-agent") != "1.4.0"
            or package.get("dependencies", {}).get("@earendil-works/pi-ai") != "1.4.0"
            or lock.get("lockfileVersion") != 3
            or lock.get("packages", {}).get("", {}).get("dependencies") != package["dependencies"]):
        reject("pi_package_identity")
    _pi_node_platform(Path(config.node_binary), host_pi_profile())
    meta, entry = seen[("sdk", SDK_ROLES["sdk_package"])], seen[("sdk", SDK_ROLES["sdk_entry"])]
    verified = supervisor.verify_sdk(package=roots["sdk"]/SDK_ROLES["sdk_package"],
        entry=roots["sdk"]/SDK_ROLES["sdk_entry"], package_sha256=meta["sha256"], entry_sha256=entry["sha256"])
    return PreparedPiRelease(config, verified, config.expected_manifest_sha256, len(seen), profile)


def prepare_pi_invocation(prepared, private_tree):
    if type(prepared) is not PreparedPiRelease or prepared.manifest_sha256 != prepared.config.expected_manifest_sha256:
        reject("pi_unprepared_release")
    _pi_config_shape(prepared.config)
    _private_shape(prepared, private_tree)
    ready = prepare_pi_release(prepared.config)
    root, private = Path(ready.config.release_root), Path(private_tree)
    if private.exists() or private.is_symlink() or private.parent.resolve() != private.parent or not private.parent.is_dir():
        reject("pi_private_tree_reused_or_foreign")
    private.mkdir(mode=0o700)
    for name in ("home", "tmp", "cwd", "sessions", "agent"):
        (private/name).mkdir(mode=0o700)
    reads = [root/ready.config.manifest_name, root/"node_modules", private,
             *[root/path for path in pi_release_roles(ready.launch_profile).values()]]
    argv = [str(ready.config.node_binary), "--permission", "--no-addons", "--jitless",
        *["--allow-fs-read="+str(path) for path in reads], "--allow-fs-write="+str(private),
        "--import", str(root/PI_RELEASE_ROLES["preload"]), str(root/PI_RELEASE_ROLES["main"]),
        str(root/ready.config.manifest_name), ready.manifest_sha256]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(private/"home"), "TMPDIR": str(private/"tmp"),
        "XDG_CONFIG_HOME": str(private/"home"), "XDG_CACHE_HOME": str(private/"home/cache"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "PI_PRIVATE_ROOT": str(private)}
    if ready.launch_profile.replace('memory', 'readonly').replace('discovery', 'readonly').replace('source', 'readonly').replace('workbench', 'readonly') in (PI_DARWIN_PROFILE, PI_DARWIN_PROFILE.removesuffix('-v1') + '-online-v1', PI_DARWIN_PROFILE.removesuffix('-v1') + '-online-stream-v1', PI_DARWIN_PROFILE.removesuffix('-v1') + '-online-stream-result-v1'):
        env["__CF_USER_TEXT_ENCODING"] = f"0x{os.getuid():X}:0x0:0x0"
    return ready, argv, str(private/"cwd"), env


def run_pi_prepared(*, prepared, private_tree, bridge, prompt,
                    publish=lambda report: None, observe=lambda kind, data: None):
    if type(prepared) is not PreparedPiRelease:
        reject("pi_unprepared_release")
    _pi_config_shape(prepared.config)
    _private_shape(prepared, private_tree)
    supervisor.admit_pi_bridge(bridge)
    if bridge.runtime_record != prepared.runtime_record():
        reject("pi_release_binding")
    try:
        ready, argv, cwd, env = prepare_pi_invocation(prepared, private_tree)
        env.update(PI_PROVIDER_KEY=bridge.model_identity["provider"], PI_MODEL_ID=bridge.model_identity["model"])
    except BaseException as primary:
        bridge.closed = True
        status = bridge.abandon_unknown()
        report = {"stage": "formal_factory_prepare", "spawn_started": False, "terminated": True,
                  "disposition": status, "primary": _sdk_exception_detail(primary), "close_attempts": bridge.close_attempts}
        try:
            publish(report)
        except BaseException as publication:
            failure = LaunchError("pi_launch_publication_failed")
            failure.primary_failure = report["primary"]
            failure.publication_failure = _sdk_exception_detail(publication)
            raise failure from primary
        raise
    return supervisor.run_pi_session(bridge=bridge, argv=argv, cwd=cwd, env=env,
        verified_sdk=ready.verified_sdk, prompt=prompt, publish=publish, observe=observe,
        **({"execution_control": True} if prepared.runtime_record()["contract_version"] == 3 else {}))
