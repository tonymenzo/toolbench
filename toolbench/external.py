"""Artifact-only evaluation for external orchestration systems.

Control records stay outside the worker workspace. Benchmark Python checks are
trusted code, as in ordinary Toolbench runs. No worker trajectory is fabricated.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import click
from toolbench.core.benchmark import YamlBenchmark
from toolbench.core.checks import load_benchmark_checks, merged_registry
from toolbench.core.judge import RuleJudge
from toolbench.core.trajectory import Trajectory


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def inventory(root,*,allow_internal_links=False):
    root=Path(root);result={}
    if root.is_symlink() or not root.is_dir():raise ValueError(f'Expected a real directory: {root}')
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            # Links inside the workspace are recorded, never followed. A
            # dangling one is accepted when its path stays inside (build tools
            # such as MG5 link optional libraries that were never built); a
            # link leaving the workspace is rejected, since a check reading it
            # could be fed files from outside. Seeded inputs are compared to
            # their hashes separately, so replacing one with any link fails.
            base=root.resolve();target=p.resolve()
            lexical=Path(os.path.normpath(p.parent.resolve()/os.readlink(p)))
            contained=target.is_relative_to(base) and lexical.is_relative_to(base)
            if not allow_internal_links or not contained:
                raise ValueError(f'Unsafe or unsupported symlink: {p}')
            entry={'symlink':str(p.readlink())}
            if not target.exists():entry['broken']=True
            result[p.relative_to(root).as_posix()]=entry
            continue
        if p.is_file() and not any(x in {'.git','__pycache__','.pytest_cache'} for x in p.relative_to(root).parts):
            result[p.relative_to(root).as_posix()]=digest(p)
    return result


def write(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+'\n')
    os.replace(tmp,path)


def read(path):return json.loads(Path(path).read_text())
def now():return time.time()


def benchmark_identity(b):
    return {'name':b.name,'version':b.version,'directory':str(b.BENCHMARK_DIR),
            'files':{str(p):inventory(p) for p in b.search_dirs}}


def prepare(benchmark,run_dir,variant=None,dependency_roots=()):
    b=YamlBenchmark(benchmark);v=b.get_variant(variant or b.default_variant)
    run=Path(run_dir).resolve()
    if any(run.is_relative_to(p) for p in b.search_dirs):raise ValueError('Run must be outside benchmark sources')
    dependencies={str(Path(p).resolve()):inventory(Path(p).resolve()) for p in dependency_roots}
    if any(run.is_relative_to(Path(p)) for p in dependencies):raise ValueError('Run must be outside dependency roots')
    run.mkdir(parents=True,exist_ok=False)
    workspace=run/'workspace';v.setup_workspace(workspace)
    prompts={'system':v.read_system_prompt(),'user':v.read_user_prompt()}
    write(run/'prompts.json',prompts)
    manifest={'schema_version':'1.0','mode':'external','run_id':str(uuid.uuid4()),
              'benchmark':benchmark_identity(b),'dependencies':dependencies,'variant':v.name,'inputs':inventory(workspace),
              'prompts_sha256':digest(run/'prompts.json'),'created_at':now(),
              'execution':{'status':'prepared','engine':None,'models':None,'config_sha256':None,
                           'started_at':None,'ended_at':None,'tokens':None,'cost_usd':None},
              'trajectory_available':False}
    write(run/'external-run.json',manifest)
    return manifest


def verify(run,manifest):
    b=YamlBenchmark(manifest['benchmark']['directory'])
    if benchmark_identity(b)!=manifest['benchmark']:raise ValueError('Benchmark/reference drift; prepare a new run')
    if digest(run/'prompts.json')!=manifest['prompts_sha256']:raise ValueError('Prompt drift')
    for path,expected in manifest.get('dependencies',{}).items():
        if inventory(Path(path))!=expected:raise ValueError('Benchmark dependency drift')
    files=inventory(run/'workspace',allow_internal_links=True)
    if any(files.get(p)!=h for p,h in manifest['inputs'].items()):raise ValueError('Seeded input drift')
    return b,files


def evaluate(run_dir,execution_status=None):
    run=Path(run_dir).resolve()
    if (run/'.execution-lock').exists():raise ValueError('Execution is still locked; cannot evaluate active workers')
    manifest=read(run/'external-run.json')
    b,files=verify(run,manifest)
    if execution_status:
        if manifest['execution']['status']!='prepared':raise ValueError('Cannot overwrite recorded execution status')
        if execution_status not in {'completed','failed','timed_out'}:raise ValueError('Invalid external status')
        manifest['execution'].update(status=execution_status,ended_at=now(),status_source='operator_declared')
        write(run/'external-run.json',manifest)
    if manifest['execution']['status'] not in {'completed','failed','timed_out'}:raise ValueError('Execution is not finished; declare its status or use an adapter')
    judge=RuleJudge(registry=merged_registry(load_benchmark_checks(b.checks_module_path())),benchmark_dir=b.search_dirs)
    grade=judge.grade(Trajectory(),b.rubric,str(run/'workspace')).to_dict()
    # Absence of a trajectory is not evidence that a tool was unused.
    for stage in grade['stage_grades']:
        stage['evidence']=stage['evidence'].replace('tool unused:', 'tool unobserved (external execution):')
    _,after=verify(run,manifest)
    if after!=files:raise ValueError('Artifacts changed during evaluation')
    result={'schema_version':'1.0','run_id':manifest['run_id'],'evaluated_at':now(),
            'execution_status':manifest['execution']['status'],'grade':grade,
            'accepted':manifest['execution']['status']=='completed' and bool(grade['stages']) and all(grade['stages'].values()),
            'artifacts':files,'trajectory_available':False}
    # Append-only evaluations preserve prior grades rather than rewriting history.
    folder=run/'evaluations';folder.mkdir(exist_ok=True)
    write(folder/(str(uuid.uuid4())+'.json'),result)
    write(run/'external-result.json',result)
    return result


@click.group('external')
def external():
    """Prepare and grade external workflows without agent telemetry."""

@external.command('prepare')
@click.argument('benchmark',type=click.Path(exists=True,file_okay=False))
@click.argument('run_dir',type=click.Path())
@click.option('--variant')
@click.option('--dependency-root',multiple=True,type=click.Path(exists=True,file_okay=False))
def prepare_command(benchmark,run_dir,variant,dependency_root):
    try:result=prepare(benchmark,run_dir,variant,dependency_root)
    except (ValueError,OSError) as exc:raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({'run_id':result['run_id'],'workspace':str(Path(run_dir).resolve()/'workspace'),'status':'prepared'},indent=2))

@external.command('evaluate')
@click.argument('run_dir',type=click.Path(exists=True,file_okay=False))
@click.option('--execution-status',type=click.Choice(['completed','failed','timed_out']))
def evaluate_command(run_dir,execution_status):
    try:result=evaluate(run_dir,execution_status)
    except (ValueError,OSError) as exc:raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({k:result[k] for k in ['run_id','execution_status','accepted','grade']},indent=2))
    return 0 if result['accepted'] else 1

@external.command('conjure')
@click.argument('run_dir',type=click.Path(exists=True,file_okay=False))
@click.option('--config',required=True,type=click.Path(exists=True,dir_okay=False))
@click.option('--timeout',type=click.FloatRange(min=0,min_open=True),default=3600.,show_default=True)
@click.option('--preflight-only',is_flag=True,help="Provision and run the benchmark's preflight; no model calls, no grading.")
def conjure_command(run_dir,config,timeout,preflight_only):
    """Execute one Conjure root workflow, then grade its artifacts."""
    from toolbench.external_conjure import execute
    if preflight_only:
        try:click.echo(json.dumps(execute(run_dir,config,timeout=timeout,preflight_only=True),indent=2))
        except (ValueError,OSError,ImportError) as exc:raise click.ClickException(str(exc)) from exc
        return 0
    try:
        execute(run_dir,config,timeout=timeout)
        result=evaluate(run_dir)
    except (ValueError,OSError,ImportError) as exc:raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({k:result[k] for k in ['run_id','execution_status','accepted','grade']},indent=2))
    return 0 if result['accepted'] else 1
