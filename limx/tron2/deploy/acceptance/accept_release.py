#!/usr/bin/env python3
"""Run the exact PR Bot image for acceptance, with a retained rollback container."""
import argparse,ast,base64,contextlib,fcntl,hashlib,io,json,os,sys,time,types
from pathlib import Path
ROOT=Path(__file__).resolve().parent
FOLDER=Path('/opt/phanthy-motus/projects/tron2/deployment/pr464-release-b369922')
JOURNAL=FOLDER/'journal.json'
def digest(data):return hashlib.sha256(data).hexdigest()
MANIFEST_SHA256='96f82087e5bd4ed25936e8fb28af51f4e0452d76fbdfd30c59381103fa02e701'
raw=(ROOT/'accept_manifest.json').read_bytes()
if digest(raw)!=MANIFEST_SHA256:raise RuntimeError('Release manifest checksum mismatch')
M=json.loads(raw);M['pr_source_commit']=M['source_commit'];support=(ROOT/'support.py').read_bytes()
if digest(support)!=M['support_sha256']:raise RuntimeError('Deployment support checksum mismatch')
b=types.ModuleType('release_support');b.__file__=str(ROOT/'support.py')
exec(compile(support.decode().replace('adfee05','accept-b369922'),b.__file__,'exec'),b.__dict__)
b.FOLDER,b.JOURNAL=FOLDER,JOURNAL
need=b.require
old_idle=b.idle;old_clone=b.clone_payload;old_validate=b.validate_config
CONTEXT=None
SNAPSHOT=r'''
import ast,hashlib,json,os,yaml
from pathlib import Path
p=Path('/work');config=Path(os.environ.get('CONFIG_PATH','/work/config.yaml'));home=Path(os.environ.get('TRON2_HOME_FILE','/config/tron2/home.json'))
files={str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in list(p.glob('*.py'))+list((p/'head_follow').glob('*.py')) if f.is_file()}
common=p/'common/teaching/service.py';stopsha=None;commonsha=None
if common.is_file():
 tree=ast.parse(common.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TeachingPlugin');stop=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='stop')
 def portable(x):
  if isinstance(x,ast.AST):return [type(x).__name__,{k:portable(v) for k,v in ast.iter_fields(x) if not(k=='type_params' and not v)}]
  if isinstance(x,list):return [portable(v) for v in x]
  return x
 stopsha=hashlib.sha256(json.dumps(portable(stop),sort_keys=True,separators=(',',':')).encode()).hexdigest();commonsha=hashlib.sha256(common.read_bytes()).hexdigest()
print(json.dumps({'files':files,'config':yaml.safe_load(config.read_text()),'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),'home_sha256':hashlib.sha256(home.read_bytes()).hexdigest(),'common_stop_ast_sha256':stopsha,'common_teaching_module_sha256':commonsha}))
'''
def state():return json.loads(b.docker('exec',b.NAME,'python3','-c',SNAPSHOT))
def assert_source(s,j,patched):
    need(s['files']==M['target_sources' if patched else 'old_sources'],'Driver source differs from the verified version')
    need(s['home_sha256']==j['source']['home_sha256'],'Original home changed')
    need(s['config_sha256']==(j['target_config_sha256'] if patched else j['source']['config_sha256']),'Runtime config changed')
    if not patched:need(s['common_teaching_module_sha256']==j['source']['common_teaching_module_sha256'],'Prior common teaching source changed')
def idle(version):
    if version==M['original_version']:return old_idle(version)
    b.core_idle();end=time.monotonic()+8;positions=[];window=[];last=None
    while time.monotonic()<end:
        h=b.request();q,dq,stamp=b.no_motion(h,version);now=time.monotonic();positions.append(q)
        need(last is None or stamp>=last,'Feedback timestamp moved backwards')
        need(max(max(v[i] for v in positions)-min(v[i] for v in positions) for i in range(16))<=.0006,'Robot position changed')
        if max(map(abs,dq))>.01:window=[]
        elif stamp!=last:window.append((now,stamp))
        last=stamp
        if len(window)>=3 and now-window[0][0]>=.8:return h
        time.sleep(.2)
    raise b.Rejected('No stationary advancing-feedback window')
def fresh(m,j,patched=False):
    b.core_idle();assert_source(state(),j,patched);h=idle(m['version'] if patched else m['original_version'])
    need(h['samples']['joint_state'].get('accid')==j['robot_accid'],'Robot identity changed');return h
def target_config(config,serial):
    import copy
    c=copy.deepcopy(config);plugins=c.get('plugins')
    need(isinstance(plugins,dict),'Prior plugin configuration invalid')
    allowed={'joint_state','arm_control','move_head','pose','gripper','gripper_left','gripper_right','camera','camera_left','camera_right','head_visual_follow'}
    removed=set(plugins)-allowed;need(removed<={'tron2_teaching'},'Unknown local plugin; preserve it for review')
    c['plugins']={k:v for k,v in plugins.items() if k in allowed}
    for name in ('joint_state','camera','move_head','head_visual_follow'):c['plugins'][name]={'enabled':True}
    c.setdefault('robot',{})['serial']=serial
    return c
PROFILE_DEST='/run/tron2-pr464-acceptance.yaml'
def profile_view(original):
    view=b.copy.deepcopy(original)
    view['Mounts'].append({'Type':'bind','Source':str(FOLDER/'target-config.yaml'),'Destination':PROFILE_DEST,'RW':False,'Propagation':'rprivate'})
    return view
def clone_payload(original,image_id,update_id):
    need(CONTEXT is not None,'Release context missing')
    p,pinned=old_clone(original,image_id,update_id);new=CONTEXT['target_meta']['Config'];prior=CONTEXT['old_meta']['Config']
    for k in ('Entrypoint','Cmd','WorkingDir','User','Healthcheck','StopSignal'):
        p[k]=new.get(k)
    oldenv=dict(x.split('=',1) for x in prior.get('Env',[]) if '=' in x)
    current=dict(x.split('=',1) for x in original['Config'].get('Env',[]) if '=' in x)
    env=dict(x.split('=',1) for x in new.get('Env',[]) if '=' in x)
    critical={'TRON2_SERIAL','TRON2_HOME_FILE','TRON2_ROBOT_URL','AGENT_CORE_URL','AGENT_CORE_CA_CERT','ROS_DOMAIN_ID','TRON2_CORE_DOMAIN_ID','FASTRTPS_DEFAULT_PROFILES_FILE','RMW_IMPLEMENTATION','ROS_LOG_DIR','ACCESS_TOKEN'}
    env.update({k:v for k,v in current.items() if k not in oldenv or oldenv[k]!=v or k in critical})
    env.update(CONFIG_PATH=PROFILE_DEST,TRON2_SERIAL=CONTEXT['robot_accid'])
    p['Env']=[k+'='+v for k,v in env.items()]
    p['HostConfig'].setdefault('Binds',[])
    if p['HostConfig']['Binds'] is None:p['HostConfig']['Binds']=[]
    p['HostConfig']['Binds'].append(str(FOLDER/'target-config.yaml')+':'+PROFILE_DEST+':ro')
    p['Labels']['io.codex.tron2.acceptance-release']=M['release']
    return p,pinned
def validate_config(candidate,payload,original):return old_validate(candidate,payload,profile_view(original))
def tool_check(j,m):
    tools=b.rpc('tools/list',{})['tools'];need(tools==j['target_tools'],'Released card schemas differ from the isolated image')
    follow=next(t for t in tools if t['name']=='head_visual_follow')
    need(follow.get('topic_in')==[] and follow.get('topic_out')==[],'Follow must be a standalone card')
    h=b.request();f=h.get('head_visual_follow') or {}
    need(f.get('active') is False and f.get('head_commands_sent')==0 and f.get('head_motion') is None,'Follow unexpectedly active')
def no_teaching_card():
    with b.sqlite3.connect(Path('/opt/phanthy-motus/data/data.db').as_uri()+'?mode=ro',uri=True,timeout=3) as db:
        row=db.execute("SELECT value FROM config WHERE key='canvas_layout'").fetchone()
    layout=json.loads(row[0]) if row else {}
    need(not any(c.get('toolName',c.get('tool_name'))=='tron2_teaching' for c in layout.get('cards',[])),
         'Canvas contains a teaching card; preserve that workflow before exact-image acceptance')
AUDIT=r'''
import asyncio,hashlib,json
from pathlib import Path
import main,device
async def run():
 p=Path('/work');settings=main.load_settings();adapter=device.Adapter(lambda *a:None,settings)
 for f in list(p.glob('*.py'))+list((p/'head_follow').glob('*.py')):compile(f.read_bytes(),str(f),'exec')
 print(json.dumps({'version':device.VERSION,'files':{str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in list(p.glob('*.py'))+list((p/'head_follow').glob('*.py')) if f.is_file()},'tools':adapter.tools()}))
asyncio.run(run())
'''
def isolated(image,code=None,tests=None):
    args=['run','--rm','--name','tron2-release-audit-'+b.secrets.token_hex(5),'--network','none','--read-only','--no-healthcheck','--cap-drop','ALL','--security-opt','no-new-privileges=true','--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--env','PYTHONDONTWRITEBYTECODE=1','--env','PYTHONPATH=/tmp/tests:/work','--env','ROS_LOG_DIR=/tmp/ros-log','--env','CONFIG_PATH='+PROFILE_DEST,'--mount','type=bind,source='+str(FOLDER/'target-config.yaml')+',target='+PROFILE_DEST+',readonly']
    if tests:args+=['--mount','type=bind,source='+str(tests)+',target=/tmp/tests,readonly']
    args+=['--entrypoint','/bin/bash',image,'-c','source /opt/ros/humble/setup.bash && exec python3 '+('-c "$1"' if code else '-m unittest test_head_follow test_follow_runtime.RuntimeTests test_follow_e2e -v 2>&1'),'audit']
    if code:args.append(code)
    return b.docker(*args,timeout=120)
def stage():
    need(not JOURNAL.exists(),'Acceptance journal exists; use --status, never repeat --stage')
    b.core_idle();no_teaching_card();items=b.inventory();cs=[c for c in items if c['Name']=='/'+b.NAME]
    need(len(cs)==1 and cs[0]['State']['Running'],'Expected existing running TRON2')
    c=cs[0];s=state();need(s['files']==M['old_sources'],'Current source changed since the prior deployment')
    need(s['common_stop_ast_sha256']==M['old_common_stop_ast'],'Prior teaching shutdown behavior changed')
    for mount in c.get('Mounts',[]):
        dest=mount['Destination'];need(not(dest=='/work' or dest.startswith('/work/') and dest!='/work/config.yaml'),'Live code mounts prevent exact-image acceptance')
    h=idle(M['original_version']);b.listener(c);serial=h['samples']['joint_state'].get('accid');need(isinstance(serial,str) and serial,'Robot identity missing')
    cfg=target_config(s['config'],serial)
    target_bytes=(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode() # JSON is a YAML subset.
    (FOLDER/'target-config.yaml').write_bytes(target_bytes);os.chmod(FOLDER/'target-config.yaml',0o600)
    j={'phase':'preparing','pending_operation':False,'old':c,'source':s,'robot_accid':serial,'shared':b.shared(items,{c['Id']}),'protected_files':b.protected_files(c),'q_before':b.q_of(h),'old_tools':b.rpc('tools/list',{})['tools'],'target_config_sha256':digest(target_bytes),'production_modified':False,'motion_sent':False,'service_starts_sent':0,'rollback_attempted':False,'applied':False}
    b.save(JOURNAL,j)
    for n in ('accept_release.py','support.py','accept_manifest.json'):(FOLDER/('driver-update.py' if n=='accept_release.py' else n)).write_bytes((ROOT/n).read_bytes())
    b.step(j,'pull_release_image',lambda:b.docker('pull',M['image'],timeout=300))
    meta=json.loads(b.docker('image','inspect',M['image']))[0]
    need(meta['Os']=='linux' and meta['Architecture']=='arm64','Expected Linux ARM64 release image')
    j.update(new_image=meta['Id'],target_meta=meta,old_meta=json.loads(b.docker('image','inspect',c['Image']))[0]);b.save(JOURNAL,j)
    audit=json.loads(isolated(meta['Id'],code=AUDIT));need(audit['version']==M['version'] and audit['files']==M['target_sources'],'Bot image source/version differs from PR464')
    j['target_tools']=audit['tools'];need('head_visual_follow' in {t['name'] for t in audit['tools']},'Follow card missing')
    tests=FOLDER/'tests';tests.mkdir(mode=0o700)
    for n,v in M['tests_sha256'].items():b.download('https://raw.githubusercontent.com/TommyZhou123/phanthymotus-driver/'+M['source_commit']+'/limx/tron2/tests/'+n,v,tests/n)
    (FOLDER/'isolated-tests.log').write_text(isolated(meta['Id'],tests=tests))
    b.original(j);b.shared_check(j,b.inventory());fresh(M,j);j.update(phase='staged',isolated_tests_passed=True);b.save(JOURNAL,j)
    print(json.dumps({'result':'PR464_OFFICIAL_IMAGE_STAGE_PASS','image':M['image'],'image_id':meta['Id'],'source_sha256_verified':True,'production_modified':False,'motion_sent':False}))
CORE_READ=r'''
import http.client,json,os,pathlib,socket,ssl
key=os.environ.get('ACCESS_TOKEN','')
if not key:
 for p in (pathlib.Path('/work/.env'),pathlib.Path('/opt/phanthy-motus/.env')):
  if p.is_file():
   for line in p.read_text().splitlines():
    if line.strip().startswith('ACCESS_TOKEN='):key=line.strip().partition('=')[2].strip().strip("\\\"'")
ctx=ssl.create_default_context(cafile='/work/resource/certs/cert.pem')
c=http.client.HTTPSConnection('phanthy-motus',15678,context=ctx,timeout=5)
c._create_connection=lambda address,timeout=None,source_address=None:socket.create_connection(('127.0.0.1',15678),timeout)
c.request('GET','/api/mcp',headers={'Authorization':'Bearer '+key} if key else {})
r=c.getresponse();data=json.loads(r.read());c.close()
if r.status!=200 or data.get('code')!=200:raise RuntimeError('Core registry read failed')
found=[x for x in data['data'] if x.get('url')=='http://127.0.0.1:15791/mcp']
print(json.dumps([{'id':x.get('id'),'tools':[t.get('name') for t in x.get('tools',[])]} for x in found]))
'''
def verify_motus():
    end=time.monotonic()+35
    while time.monotonic()<end:
        try:
            rows=json.loads(b.docker('exec','phanthy-motus-agent-core-1','python3','-c',CORE_READ,timeout=10))
            mid=b.request().get('registered_mcp_id')
            found=[r for r in rows if 'head_visual_follow' in r['tools'] and r['id']==mid]
            if len(found)==1:return found[0]
        except (b.Rejected,ValueError,KeyError):pass
        time.sleep(2)
    raise b.Rejected('Image is running but Motus card registration is not yet verified; use --status, do not redeploy')
b.state=state;b.idle=idle;b.fresh=fresh;b.clone_payload=clone_payload;b.validate_config=validate_config;b.tool_check=tool_check
b.check_source=lambda s,m:need(s['files']==m['old_sources'],'Prior source differs')
b.preserved_source=lambda s,old:need(s['config_sha256']==old['config_sha256'] and s['home_sha256']==old['home_sha256'] and s['common_teaching_module_sha256']==old['common_teaching_module_sha256'],'Prior source/config/home changed')
def main():
    global CONTEXT
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group(required=True)
    for flag in ('stage','apply','status','rollback'):g.add_argument('--'+flag,action='store_true')
    ap.add_argument('--confirm-empty-stationary',action='store_true');a=ap.parse_args()
    need(os.geteuid()==0,'Run with sudo');FOLDER.mkdir(parents=True,exist_ok=True,mode=0o700)
    need(not FOLDER.is_symlink() and FOLDER.stat().st_uid==0 and FOLDER.stat().st_mode&0o077==0,'Private root-owned deployment directory required')
    fd=os.open(str(FOLDER/'lock'),os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if a.status:b.status();return
    if a.stage:stage();return
    need(a.confirm_empty_stationary,'Stationary robot and empty jaws must be checked')
    need(JOURNAL.exists(),'Run --stage first');CONTEXT=json.loads(JOURNAL.read_text())
    if a.rollback:b.rollback(M,CONTEXT);return
    with contextlib.redirect_stdout(io.StringIO()):b.apply(M,CONTEXT)
    registered=verify_motus();tool_check(CONTEXT,M);b.core_idle();b.shared_check(CONTEXT,b.inventory())
    CONTEXT['motus_card_registered']=registered;b.save(JOURNAL,CONTEXT)
    print(json.dumps({'result':'PR464_OFFICIAL_IMAGE_DEPLOY_PASS_IDLE','pr':464,'release':M['release'],'image':M['image'],
     'resolved_image_id':CONTEXT['new_image'],'official_image_unmodified':True,'source_file_hashes_verified':True,
     'driver_version':M['version'],'motus_card_registered':True,'mcp_id':registered['id'],'card':'head_visual_follow',
     'follow_active':False,'motion_sent':False,'backup_container':CONTEXT['backup_name'],
     'local_extensions_retained_in_backup':True,'original_config_unchanged':True,
     'rollback':'sudo python3 '+str(FOLDER/'driver-update.py')+' --rollback --confirm-empty-stationary'},indent=2))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        out={'result':'RELEASE_ACCEPTANCE_STOPPED_PRESERVE_STATE','error':str(exc)[:250],'motion_sent':False,'note':'Keep journal/containers. Use --status; never repeat an uncertain mutation.'}
        if JOURNAL.exists():
            j=json.loads(JOURNAL.read_text());out.update({k:j.get(k) for k in ('phase','pending_operation','production_modified','service_starts_sent')})
        print(json.dumps(out,ensure_ascii=False,indent=2));raise SystemExit(2)
