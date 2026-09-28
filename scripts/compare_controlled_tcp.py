#!/usr/bin/env python3
"""Serial ABBA comparison of the explicit-executor TCP fixture; no clock changes."""
import argparse,json,os,re,signal,subprocess,sys
from pathlib import Path
from compare_builds import digest,execute,rows
from collect_environment import collect_environment

def validate(row,log,strategy,payload,cpus):
    if row['transport']!='tcp' or row['strategy']!=strategy or int(row['payload_size'])!=payload:
        raise ValueError('wrong benchmark condition')
    expected=dict(zip(['bench-main','bench-sender','bench-client','bench-server'],cpus))
    roles=re.findall(r'^ROLE role=(\S+) tid=(\d+) cpu=(\d+) verified=1',log,re.MULTILINE)
    if len(roles)!=4 or len({tid for role,tid,cpu in roles})!=4 or {role:int(cpu) for role,tid,cpu in roles}!=expected:
        raise ValueError('missing or incorrect role affinity evidence')
    callbacks=re.findall(r'^CALLBACK_ROLE role=(\S+) tid=(\d+) cpu=(\d+) verified=1',log,re.MULTILINE)
    executor_roles={(role,tid,cpu) for role,tid,cpu in roles if role in ['bench-client','bench-server']}
    if len(callbacks)!=2 or set(callbacks)!=executor_roles:raise ValueError('callback executed outside verified role')
    if [int(row[k+'_cpu']) for k in ['main','sender','client','server']]!=cpus:
        raise ValueError('CSV CPU configuration mismatch')
    if int(row['accepted_messages'])<=0 or int(row['accepted_messages'])*payload!=int(row['accepted_bytes']) or row['accepted_bytes']!=row['received_bytes']:
        raise ValueError('accepted/delivery mismatch')
    if int(row['client_queued_bytes_final']) or int(row['client_pending_bytes_final']):
        raise ValueError('queued work remains')
    if row['accounting_supported']=='1' and int(row['outstanding_requests_final']):
        raise ValueError('ledger outstanding remains')
    if row['accounting_supported'] not in ['0','1']:
        raise ValueError('invalid accounting availability')
    if int(row['elapsed_ns'])<int(row['duration_ms'])*1000000:
        raise ValueError('measurement shorter than requested duration')

def fingerprint(binary):
    ldd=subprocess.check_output(['ldd',str(binary)],text=True)
    paths={binary.resolve()}
    for token in ldd.split():
        if token.startswith('/') and Path(token).is_file(): paths.add(Path(token).resolve())
    return {'binary':str(binary),'ldd':ldd,'sha256':{str(p):digest(p) for p in sorted(paths)}}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-build',type=Path,required=True);p.add_argument('--candidate-build',type=Path,required=True)
    for role in ['main','sender','client','server']:p.add_argument('--'+role+'-cpu',type=int,required=True)
    p.add_argument('--payloads',type=int,nargs='+',default=[1024]);p.add_argument('--strategies',choices=['reliable','besteffort'],nargs='+',default=['reliable','besteffort'])
    p.add_argument('--rounds',type=int,default=3);p.add_argument('--duration-ms',type=int,default=3000);p.add_argument('--warmup-messages',type=int,default=512)
    p.add_argument('--port',type=int,default=19290);p.add_argument('--timeout',type=int,default=60);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();cpus=[a.main_cpu,a.sender_cpu,a.client_cpu,a.server_cpu]
    if min(cpus)<0 or min(a.payloads+[a.rounds,a.duration_ms,a.warmup_messages,a.timeout])<=0:p.error('invalid nonpositive argument')
    if len(set(a.payloads))!=len(a.payloads) or len(set(a.strategies))!=len(a.strategies):p.error('duplicate conditions')
    builds={v:(getattr(a,v+'_build').resolve()/'bin/bench_tcp_controlled') for v in ['baseline','candidate']}
    for b in builds.values():
        if not os.access(b,os.X_OK):p.error('missing executable: '+str(b))
    a.output.mkdir(parents=True,exist_ok=False)
    def interrupted(signum,frame):raise KeyboardInterrupt('signal '+str(signum))
    for sig in [signal.SIGTERM,signal.SIGHUP]:signal.signal(sig,interrupted)
    completed=[]
    try:
        meta={'options':{k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()},'builds':{v:fingerprint(b) for v,b in builds.items()},'environment':collect_environment()}
        (a.output/'metadata.json').write_text(json.dumps(meta,indent=2))
        for payload in a.payloads:
            for strategy in a.strategies:
                for i,version in enumerate(['baseline','candidate','candidate','baseline']*a.rounds):
                    name=f'{strategy}-{payload}-{i}-{version}';prefix=a.output/name
                    env=dict(os.environ)
                    for k in ['LD_PRELOAD','LD_LIBRARY_PATH','WIRESTEAD_REQUEST_TRACE']:env.pop(k,None)
                    cmd=[str(builds[version]),'--strategy',strategy,'--payload-size',str(payload),'--duration-ms',str(a.duration_ms),'--warmup-messages',str(a.warmup_messages),'--port',str(a.port),'--csv-output',str(prefix.with_suffix('.csv'))]
                    for role,cpu in zip(['main','sender','client','server'],cpus):cmd+=['--'+role+'-cpu',str(cpu)]
                    execute(cmd,prefix,a.timeout,env)
                    data=rows(prefix.with_suffix('.csv'))
                    if len(data)!=1:raise ValueError('expected one isolated strategy result')
                    validate(data[0],prefix.with_suffix('.log').read_text(),strategy,payload,cpus)
                    if int(data[0]['duration_ms'])!=a.duration_ms or int(data[0]['warmup_messages'])!=a.warmup_messages:raise ValueError('window/warmup mismatch')
                    completed.append({'name':name,'version':version,'strategy':strategy,'payload':payload,'row':data[0]})
                    print(name+' PASS',flush=True)
        for build in meta['builds'].values():
            for path,sha in build['sha256'].items():
                if digest(Path(path))!=sha:raise ValueError('binary/library changed during comparison')
        status={'status':'success','runs':completed,'sha256':{x.name:digest(x) for x in a.output.iterdir() if x.is_file()}}
    except BaseException as e:
        (a.output/'completed.json').write_text(json.dumps({'status':'failed','runs':completed,'error':repr(e)},indent=2));raise
    (a.output/'completed.json').write_text(json.dumps(status,indent=2))
if __name__=='__main__':main()
