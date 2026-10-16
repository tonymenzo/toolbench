"""Optional Conjure adapter; imports Conjure only when explicitly executed."""
from __future__ import annotations
from pathlib import Path
import time
import math
import yaml
from toolbench.external import read,write,digest,verify,now


def execute(run_dir,config_path,*,timeout=3600.,builder=None):
    lock=Path(run_dir).resolve()/'.execution-lock'
    try:lock.mkdir()
    except FileExistsError as exc:raise ValueError('An execution already holds this run') from exc
    try:return _execute(run_dir,config_path,timeout=timeout,builder=builder)
    finally:lock.rmdir()


def _execute(run_dir,config_path,*,timeout=3600.,builder=None):
    if not math.isfinite(timeout) or timeout<=0:raise ValueError('Timeout must be finite and positive')
    from conjure.config import load_config_from_mapping
    from conjure.runner import build_runtime
    from conjure.event_log import EventLog
    import conjure
    run=Path(run_dir).resolve();manifest=read(run/'external-run.json');verify(run,manifest)
    if manifest['execution']['status']!='prepared':raise ValueError('Run has already started; prepare a new run for retries')
    data=yaml.safe_load(Path(config_path).read_text())
    if not isinstance(data,dict):raise ValueError('Conjure configuration must be a mapping')
    prompts=read(run/'prompts.json')
    root=data.setdefault('root',{})
    if root.get('engine','auto')=='auto':raise ValueError('Pin the Conjure root engine explicitly')
    # Benchmark prompts are authoritative. Workflow instructions belong in the
    # benchmark variant, not an unrecorded replacement prompt in adapter config.
    root['role_prompt']=prompts['system'];root['sandbox_dir']=str(run/'workspace')
    runtime_cfg=data.setdefault('runtime',{});runtime_cfg['store_dir']=str(run/'conjure-native')
    runtime_cfg.setdefault('auto_approve',True)
    data['mode']='one-shot';data['initial_task']=prompts['user']
    cfg=load_config_from_mapping(data)
    # Snapshot effective configuration; API keys belong in environment variables.
    write(run/'conjure-config.json',cfg.model_dump())
    manifest['execution'].update(status='running',engine='conjure',models={'root':cfg.root.model,'llm':cfg.root.llm},
        config_sha256=digest(run/'conjure-config.json'),started_at=now(),conjure_version=conjure.__version__)
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
        while time.monotonic()<deadline:
            record=runtime.record_for(addr)
            if record.status in {'error','terminated'}:break
            if runtime.wait_for_idle(addr,seq,timeout_s=min(.25,max(.001,deadline-time.monotonic()))):
                if runtime.record_for(addr).status=='idle':status='completed'
                break
        else:status='timed_out'
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
        if runtime is not None:
            manifest['execution']['observed_root']=dict(runtime.record_for(addr).meta) if 'addr' in locals() else None
        manifest['execution'].update(status=status,ended_at=now(),error=error,native_traces='conjure-native',status_source='conjure_adapter')
        write(run/'external-run.json',manifest)
    return manifest
