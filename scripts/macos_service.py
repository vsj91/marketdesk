#!/usr/bin/env python3
"""Manage the per-user MarketDesk login service. No admin rights or credentials."""
import argparse,os,plistlib,subprocess,sys,shutil,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path.home()/'Library'/'Application Support'/'MarketDeskLocal'
LABEL='com.vinayjain.marketdesk-local'
DOMAIN=f'gui/{os.getuid()}'
TARGET=f'{DOMAIN}/{LABEL}'
PLIST=Path.home()/'Library'/'LaunchAgents'/(LABEL+'.plist')

def run(*args,check=True):
    result=subprocess.run(['/bin/launchctl',*args],capture_output=True,text=True)
    if check and result.returncode:raise RuntimeError(result.stderr.strip() or 'launchctl failed')
    return result

def config():
    return {'Label':LABEL,'ProgramArguments':[str(RUNTIME/'.venv/bin/python'),'-u',str(RUNTIME/'app.py'),'serve','--live'],
            'WorkingDirectory':str(RUNTIME),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'Umask':0o077,
            'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','PATH':'/usr/bin:/bin:/usr/sbin:/sbin'},
            'StandardOutPath':str(RUNTIME/'data/logs/server.out.log'),'StandardErrorPath':str(RUNTIME/'data/logs/server.err.log')}

def deploy():
    """Keep only executable runtime outside Documents; preserve a single data store."""
    RUNTIME.mkdir(parents=True,exist_ok=True);RUNTIME.chmod(0o700)
    owner=RUNTIME/'source-project.txt'
    if owner.exists() and owner.read_text().strip()!=str(ROOT):raise RuntimeError('Runtime belongs to another project; refusing to overwrite it.')
    owner.write_text(str(ROOT)+'\n')
    for name in ('data','.env'):
        source=ROOT/name;target=RUNTIME/name
        if source.is_symlink():
            if source.resolve()!=target.resolve():raise RuntimeError('Unexpected existing link: '+name)
        elif source.exists():
            if target.exists():raise RuntimeError('Both project and runtime contain '+name+'; reconcile before deployment.')
            shutil.move(str(source),str(target));source.symlink_to(target,target_is_directory=name=='data')
        elif name=='data':
            target.mkdir(exist_ok=True);source.symlink_to(target,target_is_directory=True)
        elif target.exists():source.symlink_to(target)
        else:
            target.touch(mode=0o600);source.symlink_to(target)
    # scripts/: the research baskets run the backtest scripts' own rule code (marketdesk/research_live.py).
    for name in ('marketdesk','web','scripts'):
        shutil.copytree(ROOT/name,RUNTIME/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    if not (RUNTIME/'.venv').exists():
        shutil.copytree(ROOT/'.venv',RUNTIME/'.venv',symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(ROOT/'app.py',RUNTIME/'app.py')
    (RUNTIME/'.env').chmod(0o600)

def install():
    if not (ROOT/'.venv/bin/python').exists():raise RuntimeError('Project Python environment missing. Run start.command first.')
    if run('print',TARGET,check=False).returncode==0:run('bootout',TARGET)
    deploy()
    PLIST.parent.mkdir(parents=True,exist_ok=True)
    logs=ROOT/'data/logs';logs.mkdir(parents=True,exist_ok=True)
    for name in ('server.out.log','server.err.log'):
        file=logs/name;file.touch(exist_ok=True);file.chmod(0o600)
    if PLIST.exists():
        backup=ROOT/'data/backups/previous-login-service.plist';backup.parent.mkdir(parents=True,exist_ok=True);backup.write_bytes(PLIST.read_bytes());backup.chmod(0o600)
    temp=PLIST.with_suffix('.plist.tmp');temp.write_bytes(plistlib.dumps(config()));temp.chmod(0o600);os.replace(temp,PLIST)
    subprocess.run(['/usr/bin/plutil','-lint',str(PLIST)],check=True)
    run('enable',TARGET)
    # bootout may return while launchd is still releasing the old service.
    for attempt in range(5):
        result=run('bootstrap',DOMAIN,str(PLIST),check=False)
        if result.returncode==0:break
        if attempt==4:raise RuntimeError(result.stderr.strip() or 'launchctl bootstrap failed')
        if run('print',TARGET,check=False).returncode==0:
            raise RuntimeError('Service is already registered; inspect status before restarting.')
        time.sleep(2)
    print('Installed login service. MarketDesk starts at login and restarts if it exits.')
    print('Local paper dashboard: http://127.0.0.1:8765/')

def remove():
    if run('print',TARGET,check=False).returncode==0:run('bootout',TARGET)
    if PLIST.exists():
        backup=ROOT/'data/backups/disabled-login-service.plist';backup.parent.mkdir(parents=True,exist_ok=True)
        os.replace(PLIST,backup)
    print('Login service disabled and stopped. Project, saved settings and paper ledger retained.')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['install','status','restart','remove']);args=parser.parse_args()
    if args.action=='install':install()
    elif args.action=='remove':remove()
    elif args.action=='restart':run('kickstart','-k',TARGET);print('Restart requested.')
    else:
        result=run('print',TARGET,check=False)
        print(result.stdout if result.returncode==0 else 'MarketDesk login service is not loaded.')
        sys.exit(result.returncode)
if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,subprocess.CalledProcessError) as error:print(str(error),file=sys.stderr);sys.exit(1)
