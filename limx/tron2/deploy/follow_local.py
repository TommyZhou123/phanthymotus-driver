#!/usr/bin/env python3
"""Field deployment for PR464's VOP follow, retaining existing teaching/grasp fixes.
One TRON2 container only; no controller commands, platform file edits or cleanup.
"""
import argparse,ast,base64,copy,fcntl,hashlib,http.client,io,json,math,os,secrets,socket,sqlite3,subprocess,sys,tarfile,time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
import urllib.error,urllib.request
COMPAT_SHA256='c6115d4d6986324a08b81fec3d9779498500c441c65cec4e3c9dea945adca203'
NAME='embodied-limx-tron2'
ROOT=Path(__file__).resolve().parent
FOLDER=Path('/opt/phanthy-motus/projects/tron2/deployment/vop-follow-adfee05')
JOURNAL=FOLDER/'journal.json'
class Rejected(RuntimeError):pass
def require(value,text):
    if not value:raise Rejected(text)
base=SimpleNamespace(Rejected=Rejected)
def sha(b):return hashlib.sha256(b).hexdigest()
def save(path,data):
    tmp=path.with_name(path.name+'.tmp-'+secrets.token_hex(3))
    fd=os.open(str(tmp),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(data,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
def run(args,timeout=40):
    p=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
    if p.returncode:
        log=FOLDER/('command-error-'+secrets.token_hex(3)+'.log')
        fd=os.open(str(log),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(p.stdout+'\n'+p.stderr)
        raise Rejected('Command failed: '+args[0]+' '+args[1]+'; private log '+str(log))
    return p.stdout
def docker(*args,timeout=40):return run(['docker',*args],timeout)
def inspect(ident):return json.loads(docker('inspect',ident))[0]
def inventory():
    ids=docker('ps','-aq').split()
    return json.loads(docker('inspect',*ids)) if ids else []
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**kw):raise Rejected('Unexpected driver redirect')
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def request(body=None):
    req=urllib.request.Request('http://127.0.0.1:15791'+('/mcp' if body else '/health'),
        data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json'})
    with OPENER.open(req,timeout=8) as r:return json.load(r)
def rpc(method,params):
    result=request({'jsonrpc':'2.0','id':1,'method':method,'params':params})
    require('error' not in result and not result.get('result',{}).get('isError'),'Read-only MCP query rejected')
    return result['result']
def info(name,**args):
    data=rpc('tools/call',{'name':name,'arguments':{'action':'info',**args}})
    return json.loads(next(x['text'] for x in data['content'] if x['type']=='text'))
def core_idle():
    cs=[c for c in inventory() if c['Name']=='/phanthy-motus-agent-core-1' and c['State']['Running']]
    require(len(cs)==1,'Expected existing main Core')
    c=cs[0];import re
    pids={x.strip() for x in docker('top',c['Id'],'-eo','pid').splitlines()[1:]}
    owners=[set(re.findall(r'pid=(\d+)',line)) for line in run(['ss','-H','-ltnp']).splitlines()
            if len(line.split())>3 and line.split()[3].rsplit(':',1)[-1]=='15678']
    require(owners and all(v and v<=pids for v in owners),'Actual Core listener owner changed')
    env=dict(x.split('=',1) for x in c['Config'].get('Env',[]) if '=' in x)
    target=Path(env.get('DB_PATH','/work/resource/data.db'));matches=[]
    for mount in c.get('Mounts',[]):
        try:relative=target.relative_to(mount['Destination'])
        except ValueError:continue
        matches.append((len(mount['Destination']),Path(mount['Source'])/relative))
    require(matches and max(matches,key=lambda v:v[0])[1].resolve()==Path('/opt/phanthy-motus/data/data.db'),'Core database mount mapping changed')
    with sqlite3.connect(Path('/opt/phanthy-motus/data/data.db').as_uri()+'?mode=ro',uri=True,timeout=3) as db:
        row=db.execute("SELECT value FROM config WHERE key='core'").fetchone()
    require(row is not None,'Core status unavailable')
    data=json.loads(row[0]);require(data.get('project_running') is False and data.get('auto_start') is False,
        'Project and boot auto-start must already be off; do not stop teaching implicitly')
def terminal(m):
    return m is None or m.get('status')=='completed' and m.get('arrival_verified') is True and m.get('callback_delivered') is True and m.get('motion_may_continue') is False and not m.get('recovering')
def no_motion(h,version):
    require(h.get('version')==version and h.get('ready') is True and h.get('robot_connected') is True,'Driver version/readiness changed')
    s=h.get('samples',{}).get('joint_state') or {};d=s.get('data') or {};q,dq=d.get('q'),d.get('dq')
    finite=lambda x:type(x) in (int,float) and math.isfinite(x)
    require(s.get('available') is True and finite(s.get('reception_age_seconds')) and 0<=s['reception_age_seconds']<=1,
        'Fresh joint feedback required')
    require(all(isinstance(v,list) and len(v)==16 and all(finite(x) for x in v) for v in (q,dq)) and finite(d.get('robot_timestamp')),'Complete finite feedback required')
    require(all(terminal(h.get(k)) for k in ('arm_motion','head_motion','pose_motion')),'Active/failed/unknown robot task')
    gs=h.get('grippers') or {};require(set(gs)=={'gripper_left','gripper_right'},'Gripper owner state missing')
    require(all(g.get('state')=='ready' and g.get('can_accept_motion') is True and not g.get('recovering') and not g.get('waiting_action') for g in gs.values()),'Gripper task not idle')
    f=h.get('head_visual_follow')
    require(f is None or f.get('active') is False and not f.get('send_result_unknown') and f.get('head_motion') is None,'Follow still active or uncertain')
    return q,dq,d['robot_timestamp']
def idle(version):
    core_idle();t=info('tron2_teaching');states=[t]+[t[k] for k in ('adapter','runtime','status','device') if isinstance(t.get(k),dict)]
    require(any(x.get('busy') is False and x.get('replay_running') is False and x.get('recovering') is False and (x.get('recording') or {}).get('recording') is False for x in states),'Teaching/recording/replay idle state not established')
    export=t.get('training_export');require(not isinstance(export,dict) or export.get('state') not in ('running','pending','queued'),'Teaching export is busy')
    end=time.monotonic()+8;allq=[];window=[];last=None
    while time.monotonic()<end:
        h=request();q,dq,stamp=no_motion(h,version);now=time.monotonic();allq.append(q)
        require(last is None or stamp>=last,'Feedback timestamp moved backwards')
        require(max(max(v[i] for v in allq)-min(v[i] for v in allq) for i in range(16))<=.0006,'Robot position changed')
        if max(map(abs,dq))>.01:window=[]
        elif stamp!=last:window.append((now,stamp))
        last=stamp
        if len(window)>=3 and now-window[0][0]>=.8:return h
        time.sleep(.2)
    raise Rejected('No stationary advancing-feedback window; no service switch sent')
def near(a,b):require(max(abs(x-y) for x,y in zip(a,b))<=.0006,'Robot position changed across service switch')
def q_of(h):return h['samples']['joint_state']['data']['q']
def listener(c):
    lines=run(['ss','-H','-ltnp']).splitlines();pids={x.strip() for x in docker('top',c['Id'],'-eo','pid').splitlines()[1:]}
    import re
    owners=[set(re.findall(r'pid=(\d+)',x)) for x in lines if len(x.split())>3 and x.split()[3].rsplit(':',1)[-1]=='15791']
    require(owners and all(x and x<=pids for x in owners),'15791 listener owner changed')
def stopped(c):
    require(c['State'].get('Running') is False and not c['State'].get('Restarting') and c['State'].get('Status') in ('created','exited'),'Container has not stopped cleanly')
def port_free():
    require(not any(len(x.split())>3 and x.split()[3].rsplit(':',1)[-1]=='15791' for x in run(['ss','-H','-ltnp']).splitlines()),'15791 still occupied')
class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout);self.sock.connect('/var/run/docker.sock')
class Engine:
    def __init__(self):
        self.version='';v=self.request('GET','/version');version=v.get('ApiVersion','')
        require(version and all(x.isdigit() for x in version.split('.')),'Docker API version unavailable')
        self.version='/v'+version
    def request(self,method,path,data=None,ok=(200,201,204)):
        c=UnixHTTP('localhost',timeout=40);raw=None if data is None else json.dumps(data).encode()
        try:
            c.request(method,self.version+path,body=raw,headers={'Content-Type':'application/json'})
            r=c.getresponse();body=r.read(8*1024*1024+1)
            require(len(body)<=8*1024*1024,'Docker API response too large')
            if r.status not in ok:
                # Never print the request body: it contains the private runtime Env.
                raise base.Rejected('Docker API '+method+' '+path.split('?')[0]+' returned HTTP'+str(r.status)+'; inspect journal before retry')
            return json.loads(body) if body else None
        finally:c.close()
    def inspect(self,ident):return self.request('GET','/containers/'+quote(ident,safe='')+'/json')
    def create(self,name,payload):return self.request('POST','/containers/create?name='+quote(name,safe=''),payload)
    def update_restart(self,ident,policy):return self.request('POST','/containers/'+ident+'/update',{'RestartPolicy':policy})
    def stop(self,ident):return self.request('POST','/containers/'+ident+'/stop?t=30',ok=(204,304))
    def rename(self,ident,name):return self.request('POST','/containers/'+ident+'/rename?name='+quote(name,safe=''))
    def start(self,ident):return self.request('POST','/containers/'+ident+'/start',ok=(204,304))

def mount_signature(c):
    return sorted((m['Type'],m.get('Name') if m['Type']=='volume' else m.get('Source',''),m['Destination'],m.get('RW'),m.get('Propagation','')) for m in c.get('Mounts',[]))
def clone_payload(original,image_id,update_id):
    config=copy.deepcopy(original['Config']);hc=copy.deepcopy(original['HostConfig'])
    require(hc.get('ReadonlyRootfs') is True,'Never weaken the reviewed read-only rootfs')
    require(hc.get('NetworkMode')=='host','Network mode needs review before replacement')
    require(hc.get('AutoRemove') is False and not hc.get('ContainerIDFile'),'Auto-remove/container-ID-file prevents reviewed rollback')
    require(not hc.get('Links'),'Legacy Docker links require separate review')
    config['Image']=image_id;config.setdefault('Labels',{})
    if config['Labels'] is None:config['Labels']={}
    config['Labels']['io.codex.tron2.vop-update']=update_id
    if 'com.docker.compose.image' in config['Labels']:config['Labels']['com.docker.compose.image']=image_id
    # A newly created candidate must never auto-start while the old driver runs.
    hc['RestartPolicy']={'Name':'no','MaximumRetryCount':0}
    destinations=set()
    for b in hc.get('Binds') or []:
        parts=b.split(':');require(len(parts) in (2,3),'Unsupported bind syntax')
        destinations.add(parts[1])
    for m in hc.get('Mounts') or []:destinations.add(m['Target'])
    for p in (hc.get('Tmpfs') or {}):destinations.add(p)
    pinned=[]
    for m in original.get('Mounts',[]):
        require(m['Type'] in ('bind','volume','tmpfs'),'Unsupported mount type')
        if m['Type']=='volume' and m['Destination'] not in destinations:
            require(m.get('Name'),'Existing volume name absent')
            hc.setdefault('Mounts',[])
            if hc['Mounts'] is None:hc['Mounts']=[]
            hc['Mounts'].append({'Type':'volume','Source':m['Name'],'Target':m['Destination'],'ReadOnly':not m['RW'],'VolumeOptions':{'NoCopy':True}})
            pinned.append(m['Destination'])
    return {**config,'HostConfig':hc},pinned
# Docker can serialize optional empty collections as null or empty. Only
# these documented collection fields are normalized; all other fields compare
# exactly and every differing key is reported without values/secrets.
EMPTY_LISTS={'Binds','CapAdd','CapDrop','Dns','DnsOptions','DnsSearch','ExtraHosts',
 'GroupAdd','Links','SecurityOpt','VolumesFrom','Devices','DeviceCgroupRules',
 'DeviceRequests','Ulimits','Mounts','MaskedPaths','ReadonlyPaths','BlkioWeightDevice',
 'BlkioDeviceReadBps','BlkioDeviceWriteBps','BlkioDeviceReadIOps','BlkioDeviceWriteIOps'}
EMPTY_DICTS={'PortBindings','Tmpfs','Sysctls','StorageOpt','Annotations'}
def normalized_host(value):
    value=copy.deepcopy(value)
    for k in EMPTY_LISTS:
        if k in value and value[k] is None:value[k]=[]
    for k in EMPTY_DICTS:
        if k in value and value[k] is None:value[k]={}
    # Moby adaptContainerSettings defaults nil OomKillDisable to false;
    # verifyPlatformContainerResources can discard unsupported false to nil.
    # Neither disables the kernel OOM killer. Never coerce true or other types.
    oom=value.get('OomKillDisable')
    require(oom is None or type(oom) is bool,'OomKillDisable must be null or boolean')
    value['OomKillDisable']=False if oom is None else oom
    return value
def same_config(actual,expected,label):
    keys=sorted(k for k in set(actual)|set(expected) if actual.get(k)!=expected.get(k))
    require(not keys,label+' differs in keys: '+','.join(keys))
def validate_config(candidate,payload,original):
    same_config(candidate['Config'],{k:v for k,v in payload.items() if k!='HostConfig'},'Container Config')
    same_config(normalized_host(candidate['HostConfig']),normalized_host(payload['HostConfig']),'Container HostConfig')
    require(candidate['Image']==payload['Image'],'Container resolved image changed')
    require(candidate['HostConfig'].get('ReadonlyRootfs') is True,'Rootfs protection changed')
    require(mount_signature(candidate)==mount_signature(original),'Data/certificate/device mounts differ')
def validate_candidate(candidate,payload,original):
    require(candidate['State']['Status']=='created' and candidate['State'].get('Running') is False,'Candidate unexpectedly started')
    validate_config(candidate,payload,original)

STATE_CODE="from pathlib import Path\nimport os,json,hashlib,base64,ast\nroot=Path('/work');config=Path(os.environ.get('CONFIG_PATH','/work/config.yaml'));home=Path(os.environ.get('TRON2_HOME_FILE','/config/tron2/home.json'))\ncommon=Path('/work/common/teaching/service.py');tree=ast.parse(common.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TeachingPlugin');stop=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='stop')\ndef portable(x):\n if isinstance(x,ast.AST):return [type(x).__name__,{k:portable(v) for k,v in ast.iter_fields(x) if not(k=='type_params' and not v)}]\n if isinstance(x,list):return [portable(v) for v in x]\n return x\nstopsha=hashlib.sha256(json.dumps(portable(stop),sort_keys=True,separators=(',',':')).encode()).hexdigest()\nprint(json.dumps({'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py') if p.is_file()},'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),'home_sha256':hashlib.sha256(home.read_bytes()).hexdigest(),'common_stop_ast_sha256':stopsha,'common_teaching_module_sha256':hashlib.sha256(common.read_bytes()).hexdigest(),'follow_files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'head_follow').glob('*.py') if p.is_file()}}))"
def state():return json.loads(docker('exec',NAME,'python3','-c',STATE_CODE))
def check_source(s,m,patched=False):
    expected=dict(m['expected_root_sources'])
    if patched:
        expected.update(m['patched_root_sources'])
        expected.update({n:v for n,v in m['extract_sha256'].items() if '/' not in n})
        require(s['follow_files']=={n:v for n,v in m['extract_sha256'].items() if '/' in n},'Follow module source mismatch')
    else:require(not s['follow_files'],'Unexpected existing in-driver follow module')
    require(s['files']==expected,'Current source differs from the reviewed field version; preserve colleague changes')
    require(s['common_stop_ast_sha256']==m['common_stop_ast_sha256'],'Teaching shutdown behavior changed')
def preserved_source(s,before):
    for k in ('config_sha256','home_sha256','common_stop_ast_sha256','common_teaching_module_sha256'):
        require(s[k]==before[k],'Protected source/config changed: '+k)
def protected_files(c):
    paths=['/opt/phanthy-motus/dds-local.xml','/opt/phanthy-motus/data/certs/cert.pem','/opt/phanthy-motus/docker-compose.yml']
    labels=c['Config'].get('Labels') or {}
    paths+=(labels.get('com.docker.compose.project.config_files') or '').split(',')
    return {str(p):sha(p.read_bytes()) for x in paths if x and (p:=Path(x)).is_file()}
def shared(items,excluded):
    out={}
    for c in items:
        if c['Id'] in excluded:continue
        value={k:c[k] for k in ('Id','Name','Image','Config','HostConfig')}
        value['Mounts']=sorted(c['Mounts'],key=lambda m:m['Destination'])
        out[c['Id']]=sha(json.dumps(value,sort_keys=True).encode())
    return out
def shared_check(j,items):
    require(shared(items,{j['old']['Id'],j.get('new_id')})==j['shared'],'Other container configuration/identity changed')
    require(protected_files(j['old'])==j['protected_files'],'Protected compose/DDS/certificate changed')
def step(j,phase,operation):
    j['phase']=phase;j['pending_operation']=True;save(JOURNAL,j)
    answer=operation()
    j['pending_operation']=False;save(JOURNAL,j)
    return answer
def original(j):
    c=inspect(j['old']['Id']);o=j['old']
    require(c['Id']==o['Id'] and c['Image']==o['Image'] and c['Name']=='/'+NAME and c['State']['Running'] is True,'Original driver identity changed')
    require(c['State']['StartedAt']==o['State']['StartedAt'],'Original driver restarted during staging')
    require(c['Config']==o['Config'] and normalized_host(c['HostConfig'])==normalized_host(o['HostConfig']) and mount_signature(c)==mount_signature(o),'Original runtime configuration changed')
    return c
def fresh(m,j,patched=False):
    core_idle();s=state();check_source(s,m,patched);preserved_source(s,j['source'])
    h=idle(m['version'] if patched else m['original_version'])
    require(h['samples']['joint_state'].get('accid')==j['robot_accid'],'Robot identity changed')
    return h
def download(url,expected,path):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=20) as r:
        b=r.read(1024*1024+1)
    require(len(b)<=1024*1024 and sha(b)==expected,'Downloaded file checksum mismatch: '+path.name)
    path.write_bytes(b)
def apply_edits(text,edits):
    for old,new in edits:
        require(old in text,'Compatibility patch anchor changed')
        text=text.replace(old,new,1)
    return text
AUDIT_CODE="""import ast,json,hashlib;from pathlib import Path
import main,device,head_follow.runtime
p=Path('/work')
for f in list(p.glob('*.py'))+list((p/'head_follow').glob('*.py')):compile(f.read_bytes(),str(f),'exec')
print(json.dumps({'version':device.VERSION,'files':{x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in p.glob('*.py')},'follow_files':{str(x.relative_to(p)):hashlib.sha256(x.read_bytes()).hexdigest() for x in (p/'head_follow').glob('*.py')}}))"""
def isolated(image,code=None,tests=None):
    args=['run','--rm','--name','tron2-vop-isolated-'+secrets.token_hex(5),
        '--label','io.codex.tron2.vop-audit=adfee05','--network','none','--read-only','--no-healthcheck',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--cap-drop','ALL','--security-opt','no-new-privileges=true',
        '--env','PYTHONDONTWRITEBYTECODE=1','--env','PYTHONPATH=/tmp/tests:/work','--env','ROS_LOG_DIR=/tmp/ros-log']
    if tests:args+=['--mount','type=bind,source='+str(tests)+',target=/tmp/tests,readonly']
    args+=['--entrypoint','/bin/bash',image,'-c',
        'source /opt/ros/humble/setup.bash && exec python3 '+('-c "$1"' if code else '-m unittest test_head_follow test_follow_runtime.RuntimeTests test_follow_e2e -v 2>&1'),'isolated']
    if code:args.append(code)
    return docker(*args,timeout=120)
def stage(m):
    require(not JOURNAL.exists(),'Deployment journal exists; use --status, do not repeat staging')
    items=inventory();cs=[c for c in items if c['Name']=='/'+NAME]
    require(len(cs)==1 and cs[0]['State']['Running'] is True,'Expected one running TRON2 driver')
    c=cs[0];require(c['HostConfig'].get('ReadonlyRootfs') is True,'Read-only rootfs required')
    require(not any(x['Name'] in ('/tron2-head-visual-follow-local','/tron2-yolo-card-local') and x['State']['Running'] for x in items),'Old standalone visual service is still running')
    for mount in c.get('Mounts',[]):
        for n in list(m['edits'])+list(m['extract_sha256']):
            path='/work/'+n;require(not(path==mount['Destination'] or path.startswith(mount['Destination'].rstrip('/')+'/')),'Changed source is now mounted; preserve it')
    s=state();check_source(s,m);h=idle(m['original_version']);listener(c)
    accid=h['samples']['joint_state'].get('accid');require(isinstance(accid,str) and accid,'Robot identity unavailable')
    parent=json.loads(docker('image','inspect',c['Image']))[0]
    require(parent.get('Os')=='linux' and parent.get('Architecture')=='arm64' and not parent['Config'].get('OnBuild'),'Expected Linux ARM64 base without ONBUILD')
    oldtools=rpc('tools/list',{})['tools'];names=sorted(t['name'] for t in oldtools)
    require('tron2_teaching' in names and 'gripper' in names and 'head_visual_follow' not in names,'Unexpected field tool list')
    j={'phase':'preparing','pending_operation':False,'robot_accid':accid,'old':c,'source':s,'shared':shared(items,{c['Id']}),
       'protected_files':protected_files(c),'old_tools':oldtools,'q_before':q_of(h),'motion_sent':False,
       'service_starts_sent':0,'applied':False,'rollback_attempted':False,'production_modified':False}
    save(JOURNAL,j)
    (FOLDER/'driver-update.py').write_bytes(Path(__file__).read_bytes())
    (FOLDER/'follow_compat.json').write_bytes((ROOT/'follow_compat.json').read_bytes())
    context=FOLDER/'context';context.mkdir(mode=0o700);(context/'head_follow').mkdir(mode=0o700)
    for n,edits in m['edits'].items():
        b=docker('exec',c['Id'],'python3','-c',"from pathlib import Path;import sys;sys.stdout.write(Path('/work/'+sys.argv[1]).read_text())",n)
        require(sha(b.encode())==m['expected_root_sources'][n],'Source changed before patch')
        text=apply_edits(b,edits);require(sha(text.encode())==m['patched_root_sources'][n],'Compatibility result mismatch')
        compile(text,n,'exec');(context/n).write_text(text)
    step(j,'pull_pr_image',lambda:docker('pull',m['image'],timeout=300))
    meta=json.loads(docker('image','inspect',m['image']))[0];require(meta['Architecture']=='arm64','PR image must be ARM64')
    audit='tron2-vop-extract-'+secrets.token_hex(5)
    extraction=step(j,'create_source_container',lambda:docker('create','--name',audit,'--label','io.codex.tron2.vop-audit=adfee05','--network','none','--read-only','--entrypoint','/bin/true',meta['Id'])).strip()
    j['extraction_container']=extraction;save(JOURNAL,j)
    for n,digest in m['extract_sha256'].items():
        if n in m.get('source_overrides',[]):
            download('https://raw.githubusercontent.com/TommyZhou123/phanthymotus-driver/'+m['pr_source_commit']+'/limx/tron2/'+n,digest,context/n)
        else:
            docker('cp',extraction+':/work/'+n,str(context/n))
        require(sha((context/n).read_bytes())==digest,'PR source mismatch: '+n)
    step(j,'remove_source_container',lambda:docker('rm',extraction))
    (context/'Dockerfile').write_text('FROM '+c['Image']+'\nCOPY main.py device.py vendor.py head.py ros2.py /work/\nCOPY head_follow/ /work/head_follow/\n')
    tag='local/tron2-vop-follow:adfee05-'+c['Image'].split(':')[-1][:12]
    step(j,'build_compatibility_image',lambda:docker('build','--pull=false','--network=none','--quiet','--tag',tag,str(context),timeout=180))
    newmeta=json.loads(docker('image','inspect',tag))[0];image=newmeta['Id'];j['new_image']=image;save(JOURNAL,j)
    require(newmeta['RootFS']['Layers'][:len(parent['RootFS']['Layers'])]==parent['RootFS']['Layers'],'Original base layers not retained')
    for key in ('Entrypoint','Cmd','Env','WorkingDir','User','ExposedPorts','Volumes','Healthcheck','StopSignal','StopTimeout','Shell','Labels'):
        require(newmeta['Config'].get(key)==parent['Config'].get(key),'Image runtime default changed: '+key)
    audit=json.loads(isolated(image,code=AUDIT_CODE));require(audit['version']==m['version'],'Compatibility image version mismatch')
    expected={**m['expected_root_sources'],**m['patched_root_sources'],**{n:v for n,v in m['extract_sha256'].items() if '/' not in n}}
    require(audit['files']==expected and audit['follow_files']=={n:v for n,v in m['extract_sha256'].items() if '/' in n},'Isolated image source audit failed')
    tests=FOLDER/'tests';tests.mkdir(mode=0o700)
    for n,digest in m['tests_sha256'].items():download('https://raw.githubusercontent.com/TommyZhou123/phanthymotus-driver/'+m['pr_source_commit']+'/limx/tron2/tests/'+n,digest,tests/n)
    result=isolated(image,tests=tests);(FOLDER/'isolated-tests.log').write_text(result)
    original(j);shared_check(j,inventory());fresh(m,j)
    j['phase']='staged';j['isolated_tests_passed']=True;save(JOURNAL,j)
    print(json.dumps({'result':'VOP_COMPAT_STAGE_PASS_NO_MOTION','version':m['version'],
        'source_commit':m['pr_source_commit'],'teaching_and_grasp_preserved':True,'motion_sent':False,'production_modified':False}))
    return j
def ready(version):
    end=time.monotonic()+40
    while time.monotonic()<end:
        try:
            h=request();no_motion(h,version);return h
        except (Rejected,OSError,ValueError,KeyError):time.sleep(.4)
    raise Rejected('Driver readiness not confirmed; preserve journal, do not retry start')
def tool_check(j,m):
    tools=rpc('tools/list',{})['tools'];byname={t['name']:t for t in tools}
    old={t['name']:t for t in j['old_tools']}
    require(set(byname)==set(old)|{'head_visual_follow'},'Unexpected tool list change')
    for name in old:require(byname[name]==old[name],'Original tool schema changed: '+name)
    require(list(byname['head_visual_follow']['inputSchema']['x-action-params'])==['follow'],'Unexpected visible follow actions')
    require(byname['gripper']['inputSchema']['properties'].get('grasp_mode',{}).get('type')=='boolean','Original local grasp fix missing')
    h=request();f=h.get('head_visual_follow') or {}
    require(f.get('active') is False and f.get('head_motion') is None and f.get('head_commands_sent')==0,'New follow unexpectedly active')
def apply(m,j):
    require(j.get('phase')=='staged' and j.get('isolated_tests_passed') is True and not j.get('pending_operation'),'Only a verified staged image can be applied')
    old=original(j);shared_check(j,inventory());h=fresh(m,j);listener(old)
    j['q_before']=q_of(h);j['candidate_name']='tron2-vop-candidate-adfee05';j['backup_name']='tron2-vop-backup-adfee05'
    payload,pinned=clone_payload(old,j['new_image'],'vop-follow-adfee05');j['payload']=payload;j['pinned_volumes']=pinned
    api=Engine();save(JOURNAL,j)
    created=step(j,'create_candidate',lambda:api.create(j['candidate_name'],payload))
    j['new_id']=created['Id'];save(JOURNAL,j);validate_candidate(api.inspect(j['new_id']),payload,old)
    original(j);shared_check(j,inventory());h=fresh(m,j);near(j['q_before'],q_of(h));listener(old)
    require(rpc('tools/list',{})['tools']==j['old_tools'],'Original tool list changed before switch')
    validate_candidate(api.inspect(j['new_id']),payload,old)
    j['production_modified']=True;j['switch_precheck_passed']=True;save(JOURNAL,j)
    step(j,'disable_original_restart',lambda:api.update_restart(old['Id'],{'Name':'no','MaximumRetryCount':0}))
    step(j,'stop_original',lambda:api.stop(old['Id']))
    stopped(api.inspect(old['Id']))
    step(j,'rename_original',lambda:api.rename(old['Id'],j['backup_name']))
    step(j,'name_candidate',lambda:api.rename(j['new_id'],NAME));port_free()
    j['service_starts_sent']=1;save(JOURNAL,j)
    step(j,'start_candidate',lambda:api.start(j['new_id']))
    ready(m['version']);fresh(m,j,True);tool_check(j,m);h=idle(m['version']);near(j['q_before'],q_of(h))
    validate_config(api.inspect(j['new_id']),payload,old);listener(api.inspect(j['new_id']));shared_check(j,inventory())
    step(j,'restore_restart_policy',lambda:api.update_restart(j['new_id'],old['HostConfig']['RestartPolicy']))
    wanted=copy.deepcopy(payload);wanted['HostConfig']['RestartPolicy']=old['HostConfig']['RestartPolicy']
    validate_config(api.inspect(j['new_id']),wanted,old)
    j.update(phase='complete',applied=True);save(JOURNAL,j)
    print(json.dumps({'result':'TRON2_VOP_FOLLOW_DEPLOY_PASS_IDLE','version':m['version'],'source_commit':m['pr_source_commit'],
        'container':NAME,'backup_container':j['backup_name'],'teaching_and_grasp_preserved':True,'follow_active':False,
        'motion_sent':False,'other_service_operations':[],'local_runtime_override_only':True,
        'journal':str(JOURNAL),'rollback':'sudo python3 '+str(FOLDER/'driver-update.py')+' --rollback --confirm-empty-stationary'},indent=2))
def status():
    if not JOURNAL.exists():print(json.dumps({'journal_exists':False,'motion_sent':False}));return
    j=json.loads(JOURNAL.read_text());out={k:j.get(k) for k in ('phase','pending_operation','applied','production_modified','service_starts_sent','rollback_attempted')}
    out['motion_sent']=False
    for key,ident in [('original',j['old']['Id']),('replacement',j.get('new_id'))]:
        if ident:
            c=inspect(ident);out[key]={k:c.get(k) for k in ('Id','Name','Image')};out[key]['state']=c['State']['Status']
    print(json.dumps(out,indent=2))
def rollback(m,j):
    require(not j.get('pending_operation') and not j.get('rollback_attempted'),'Uncertain operation or rollback already attempted; inspect status, do not repeat')
    api=Engine();old=api.inspect(j['old']['Id']);snapshot=j['old'];core_idle();shared_check(j,inventory())
    require(old['Image']==snapshot['Image'] and old['Config']==snapshot['Config'] and mount_signature(old)==mount_signature(snapshot),'Original backup identity/config changed')
    expected=copy.deepcopy(snapshot['HostConfig']);expected['RestartPolicy']=old['HostConfig']['RestartPolicy']
    require(normalized_host(old['HostConfig'])==normalized_host(expected),'Backup HostConfig changed')
    require(old['HostConfig']['RestartPolicy'] in (snapshot['HostConfig']['RestartPolicy'],{'Name':'no','MaximumRetryCount':0}),'Backup restart policy changed')
    if old['State']['Running']:
        original_name=old['Name'];require(original_name=='/'+NAME,'Running original has unexpected name')
        s=state();check_source(s,m);preserved_source(s,j['source']);idle(m['original_version']);listener(old)
        j['rollback_attempted']=True;save(JOURNAL,j)
        step(j,'restore_original_restart',lambda:api.update_restart(old['Id'],snapshot['HostConfig']['RestartPolicy']))
        j['phase']='rolled_back_original_running';save(JOURNAL,j);status();return
    stopped(old)
    require(j.get('switch_precheck_passed') is True,'No verified stop checkpoint; inspect status')
    new=api.inspect(j['new_id']);require(new['Image']==j['new_image'],'Replacement image changed')
    expected=copy.deepcopy(j['payload']);expected['HostConfig']['RestartPolicy']=new['HostConfig']['RestartPolicy']
    require(new['HostConfig']['RestartPolicy'] in (snapshot['HostConfig']['RestartPolicy'],{'Name':'no','MaximumRetryCount':0}),'Replacement restart policy changed')
    validate_config(new,expected,snapshot)
    q=None
    if new['State']['Running']:
        require(new['Name']=='/'+NAME,'Replacement name changed');h=fresh(m,j,True);listener(new);q=q_of(h)
    else:stopped(new)
    require(all(c['Id'] in {old['Id'],new['Id']} for c in inventory() if c['Name']=='/'+NAME),'Production name belongs to another container')
    j['rollback_attempted']=True;save(JOURNAL,j)
    step(j,'rollback_disable_restart',lambda:api.update_restart(new['Id'],{'Name':'no','MaximumRetryCount':0}))
    if new['State']['Running']:step(j,'rollback_stop_candidate',lambda:api.stop(new['Id']))
    stopped(api.inspect(new['Id']))
    if api.inspect(new['Id'])['Name']=='/'+NAME:step(j,'rollback_rename_candidate',lambda:api.rename(new['Id'],j['candidate_name']))
    if api.inspect(old['Id'])['Name']!='/'+NAME:step(j,'rollback_name_original',lambda:api.rename(old['Id'],NAME))
    port_free();step(j,'rollback_start_original',lambda:api.start(old['Id']))
    ready(m['original_version']);h=fresh(m,j)
    if q is not None:near(q,q_of(h))
    listener(api.inspect(old['Id']));shared_check(j,inventory())
    step(j,'rollback_restore_restart',lambda:api.update_restart(old['Id'],snapshot['HostConfig']['RestartPolicy']))
    require(api.inspect(old['Id'])['Config']==snapshot['Config'] and normalized_host(api.inspect(old['Id'])['HostConfig'])==normalized_host(snapshot['HostConfig']),'Restored container configuration differs')
    j.update(phase='rolled_back',applied=False);save(JOURNAL,j)
    print(json.dumps({'result':'TRON2_VOP_ROLLBACK_PASS','version':m['original_version'],'motion_sent':False,'restored_original_container':old['Id']}))
def main():
    ap=argparse.ArgumentParser(description=__doc__);g=ap.add_mutually_exclusive_group(required=True)
    for flag in ('stage','apply','status','rollback'):g.add_argument('--'+flag,action='store_true')
    ap.add_argument('--confirm-empty-stationary',action='store_true');a=ap.parse_args()
    require(os.geteuid()==0,'Run with sudo')
    FOLDER.mkdir(parents=True,exist_ok=True,mode=0o700)
    require(not FOLDER.is_symlink() and FOLDER.stat().st_uid==0 and FOLDER.stat().st_mode&0o077==0,'Deployment folder must be private and root owned')
    lock=os.open(str(FOLDER/'lock'),os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if a.status:status();return
    raw=(ROOT/'follow_compat.json').read_bytes();require(sha(raw)==COMPAT_SHA256,'Compatibility manifest checksum mismatch');m=json.loads(raw)
    if a.stage:stage(m)
    else:
        require(a.confirm_empty_stationary,'Check stationary robot and empty jaws, then supply --confirm-empty-stationary')
        require(JOURNAL.exists(),'Run --stage first');j=json.loads(JOURNAL.read_text())
        if a.apply:apply(m,j)
        else:rollback(m,j)
if __name__=='__main__':
    try:main()
    except Exception as exc:
        out={'result':'TRON2_VOP_DEPLOY_STOPPED_PRESERVE_STATE','error':str(exc)[:250],
             'motion_sent':False,'note':'Preserve journal and containers. Use --status; never repeat an uncertain mutation.'}
        if JOURNAL.exists():
            j=json.loads(JOURNAL.read_text());out.update({k:j.get(k) for k in ('phase','pending_operation','production_modified','service_starts_sent')})
        print(json.dumps(out,ensure_ascii=False,indent=2));raise SystemExit(2)
