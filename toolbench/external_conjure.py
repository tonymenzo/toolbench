"""Optional Conjure adapter; imports Conjure only when explicitly executed.

A benchmark can declare what a Conjure trial of it needs in a ``conjure:``
block of its benchmark.yaml (paths relative to that yaml):

    conjure:
      runtime: conjure-runtime.yaml        # mapping layered over the operator config
      toolbase:
        loadout: heptapod-sm-enumeration   # served to the root, pinned per trial
        profile: toolbase-loadout.yaml     # the loadout file (with its versions: pins)
      preflight:
        module: preflight.py               # check(config: dict, run: Path, params: dict) -> dict
        params: {...}
      deliverable: results/catalogue.json  # optional: one reminder if the root stops before it exists

Before any model call the adapter layers the runtime overlay, writes a
trial-local Toolbase project (run/.toolbase: the profile, serve.yaml making it
the active loadout) and runs from the run directory, so Toolbase serves exactly
the pinned versions -- Toolbase resolves version pins from the project it finds
above the working directory, not from the project Conjure is given. It then
runs the benchmark's preflight and writes run/preflight.json. A failed
preflight ends the trial before the model is called; its execution is recorded
as failed with ``invalid_trial: true`` (a provisioning problem, not an agent
result).
"""
from __future__ import annotations
from pathlib import Path
import importlib.util
import os
import signal
import subprocess
import sys
import time
import math
import yaml
from toolbench.external import read,write,digest,verify,now


def execute(run_dir,config_path,*,timeout=3600.,builder=None,preflight_only=False):
    lock=Path(run_dir).resolve()/'.execution-lock'
    try:lock.mkdir()
    except FileExistsError as exc:raise ValueError('An execution already holds this run') from exc
    cwd=os.getcwd()
    try:return _execute(run_dir,config_path,timeout=timeout,builder=builder,preflight_only=preflight_only)
    finally:
        os.chdir(cwd);lock.rmdir()


def _cwds():
    """pid -> working directory for this user's processes (Linux /proc, else lsof)."""
    out={}
    if Path('/proc/self/cwd').exists():
        for d in Path('/proc').iterdir():
            if d.name.isdigit():
                try:out[int(d.name)]=os.readlink(d/'cwd')
                except OSError:pass
        return out
    r=subprocess.run(['lsof','-a','-d','cwd','-u',str(os.getuid()),'-Fpn'],capture_output=True,text=True,timeout=30)
    pid=None
    for line in r.stdout.splitlines():
        if line[:1]=='p':pid=int(line[1:])
        elif line[:1]=='n' and pid is not None:out[pid]=line[1:]
    return out


def reap(folder,grace=5.):
    """Stop processes the agents left running inside folder (e.g. backgrounded
    jobs), so they cannot mutate the submission while it is graded."""
    root=os.path.realpath(folder);spared={os.getpid(),os.getppid()}
    def stray():
        return [pid for pid,cwd in _cwds().items() if pid not in spared
                and (cwd==root or cwd.startswith(root+os.sep))]
    found=stray()
    for sig in (signal.SIGTERM,signal.SIGKILL):
        for pid in stray():
            try:os.kill(pid,sig)
            except OSError:pass
        end=time.monotonic()+grace
        while stray() and time.monotonic()<end:time.sleep(.2)
    return sorted(found)


def _overlay(data,path):
    """Layer the benchmark's runtime mapping over the operator config, section by section."""
    overlay=yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(overlay,dict):raise ValueError('Benchmark conjure runtime overlay must be a mapping')
    for key,value in overlay.items():
        if isinstance(value,dict) and isinstance(data.get(key,{}),dict):data.setdefault(key,{}).update(value)
        else:data[key]=value
    return digest(Path(path))


def _provision_toolbase(data,run,spec):
    """A trial-local Toolbase project whose active loadout is the benchmark's pinned profile."""
    name,profile=spec['loadout'],Path(spec['profile'])
    target=run/'.toolbase';(target/'loadouts').mkdir(parents=True,exist_ok=True)
    files={target/'loadouts'/f'{name}.yaml':profile.read_text(),
           target/'serve.yaml':f'default:\n  loadout: {name}\n',
           target/'manifest.yaml':'schema_version: 1\ntoolkits: []\n'}
    for path,text in files.items():
        if path.exists() and path.read_text()!=text:raise ValueError(f'Trial-local Toolbase project changed: {path.name}')
        path.write_text(text)
    tb=data.setdefault('toolbase',{});tb['project_root']=str(run);tb['loadout']=name
    root=data.setdefault('root',{})
    root.pop('toolkits',None)  # never inherit an unrelated broad grant
    root['toolbase_loadout']=name
    return {'loadout':name,'profile_sha256':digest(profile)}


def _preflight(data,run,spec):
    module_path=Path(spec['module'])
    sys.path.insert(0,str(module_path.parent))
    try:
        mod_spec=importlib.util.spec_from_file_location('benchmark_conjure_preflight',module_path)
        module=importlib.util.module_from_spec(mod_spec);mod_spec.loader.exec_module(module)
        report=module.check(data,run,dict(spec.get('params') or {}))
    finally:
        if sys.path and sys.path[0]==str(module_path.parent):sys.path.pop(0)
    if not isinstance(report,dict) or report.get('status')!='passed':raise ValueError('Preflight did not pass')
    report['module_sha256']=digest(module_path)
    return report


def _execute(run_dir,config_path,*,timeout=3600.,builder=None,preflight_only=False):
    if not math.isfinite(timeout) or timeout<=0:raise ValueError('Timeout must be finite and positive')
    run=Path(run_dir).resolve();manifest=read(run/'external-run.json');benchmark,_=verify(run,manifest)
    if manifest['execution']['status']!='prepared':raise ValueError('Run has already started; prepare a new run for retries')
    data=yaml.safe_load(Path(config_path).read_text())
    if not isinstance(data,dict):raise ValueError('Conjure configuration must be a mapping')
    spec=benchmark.cfg.get('conjure') or {}
    applied={}
    if spec.get('runtime'):applied['runtime_sha256']=_overlay(data,spec['runtime'])
    if spec.get('toolbase'):applied['toolbase']=_provision_toolbase(data,run,spec['toolbase'])
    # Toolbase resolves version pins from the project above the working
    # directory; run from the trial so it finds the one provisioned above.
    os.chdir(run)
    if spec.get('preflight'):
        try:
            report=_preflight(data,run,spec['preflight'])
            write(run/'preflight.json',report);applied['preflight']='passed'
        except Exception as exc:
            write(run/'preflight.json',{'status':'failed','error_type':type(exc).__name__,'error':str(exc)})
            if preflight_only:raise
            manifest['execution'].update(status='failed',engine='conjure',invalid_trial=True,benchmark_conjure=applied,
                error=f'Preflight failed: {type(exc).__name__}: {exc}',started_at=now(),ended_at=now(),status_source='conjure_adapter')
            write(run/'external-run.json',manifest)
            return manifest
    if preflight_only:return read(run/'preflight.json') if (run/'preflight.json').exists() else {'status':'no_preflight'}
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime
    from conjure.event_log import EventLog
    import conjure
    prompts=read(run/'prompts.json')
    root=data.setdefault('root',{})
    if root.get('engine','auto')=='auto':raise ValueError('Pin the Conjure root engine explicitly')
    # Benchmark prompts are authoritative. Workflow instructions belong in the
    # benchmark variant, not an unrecorded replacement prompt in adapter config.
    root['role_prompt']=prompts['system'];root['sandbox_dir']=str(run/'workspace')
    runtime_cfg=data.setdefault('runtime',{});runtime_cfg['store_dir']=str(run/'conjure-native')
    runtime_cfg.setdefault('auto_approve',True)
    data['mode']='one-shot';data['initial_task']=prompts['user']
    # Let agents see the trial's wall clock: Conjure clips blocking waits to it
    # (conjure > 0.2.7), leaving a margin to deliver before the harness stops.
    from conjure.config import RuntimeConfig
    if 'wall_clock_s' in RuntimeConfig.model_fields:
        runtime_cfg.setdefault('wall_clock_s',max(timeout-min(300.,.1*timeout),.5*timeout))
    cfg=load_config_from_mapping(data)
    # Snapshot effective configuration; API keys belong in environment variables.
    write(run/'conjure-config.json',cfg.model_dump())
    manifest['execution'].update(status='running',engine='conjure',models={'root':cfg.root.model,'llm':cfg.root.llm},
        config_sha256=digest(run/'conjure-config.json'),started_at=now(),conjure_version=conjure.__version__,
        benchmark_conjure=applied)
    write(run/'external-run.json',manifest)
    runtime=None;logs=[];status='failed';error=None
    def listen(record):
        folder=run/'conjure-native'/'events';folder.mkdir(parents=True,exist_ok=True)
        record.event_log=EventLog(folder/(record.addr.id+'.jsonl'));logs.append(record.event_log)
    try:
        runtime,addr=(builder or build_runtime)(cfg,session_id=manifest['run_id'],spawn_listener=listen,
            event_log_router=lambda record:record.event_log,stream=True)
        runtime.send_external(to=addr,body=prompts['user'])
        seq=runtime.record_for(addr).inbox.latest_seq();deadline=time.monotonic()+timeout
        # A root that ends its turn before the benchmark's declared deliverable
        # exists (e.g. it announced a step and stopped) gets one reminder.
        deliverable=spec.get('deliverable');nudges=[]
        while time.monotonic()<deadline:
            record=runtime.record_for(addr)
            if record.status in {'error','terminated'}:break
            if runtime.wait_for_idle(addr,seq,timeout_s=min(.25,max(.001,deadline-time.monotonic()))):
                if runtime.record_for(addr).status!='idle':break
                if deliverable and not nudges and not (run/'workspace'/deliverable).exists():
                    nudges.append(now())
                    runtime.send_external(to=addr,body=f'Your turn ended, but {deliverable} does not exist yet. '
                                          'Continue the task, or report why it cannot be completed.')
                    seq=runtime.record_for(addr).inbox.latest_seq();continue
                status='completed';break
        else:status='timed_out'
        if nudges:manifest['execution']['completion_nudges']=nudges
        # The coordinator must join all children before returning. Do not grade
        # while background workers can still mutate the shared result package.
        def descendants(a):
            r=runtime.record_for(a)
            for child in r.children:
                yield runtime.record_for(child)
                yield from descendants(child)
        children=list(descendants(addr))
        if status=='completed' and any(r.status not in {'idle','terminated'} for r in children):
            status='failed';error='Coordinator returned before descendants finished successfully'
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}'
    finally:
        try:
            if runtime is not None:runtime.shutdown()
            for log in logs:log.close()
        except Exception as exc:
            status='failed';error=f'Shutdown failed: {exc}'
        try:
            stray=reap(run/'workspace')
            if stray:manifest['execution']['reaped_pids']=stray
        except Exception as exc:
            status='failed';error=f'Could not stop leftover processes: {exc}'
        if runtime is not None:
            manifest['execution']['observed_root']=dict(runtime.record_for(addr).meta) if 'addr' in locals() else None
            if hasattr(runtime,'costs_by_agent'):
                try:
                    manifest['execution']['cost_usd']=runtime.total_cost()
                    manifest['execution']['costs_by_agent']={a.id:c for a,c in runtime.costs_by_agent()}
                except Exception as exc:manifest['execution']['cost_error']=f'{type(exc).__name__}: {exc}'
        manifest['execution'].update(status=status,ended_at=now(),error=error,native_traces='conjure-native',status_source='conjure_adapter')
        write(run/'external-run.json',manifest)
    return manifest
