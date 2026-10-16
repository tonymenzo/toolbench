from pathlib import Path
import json
import pytest
from toolbench.external import prepare,evaluate,read,write

@pytest.fixture
def benchmark(tmp_path):
    b=tmp_path/'benchmark';v=b/'variants/standard';v.mkdir(parents=True)
    (b/'benchmark.yaml').write_text('''name: external-test
version: '1'
default_variant: standard
checks: checks.py
rubric:
  type: stagewise
  stages:
    - id: answer
      weight: 1
      gating: false
      checks:
        - answer: {}
    - id: certified
      weight: 0
      checks:
        - certified: {}
''')
    (b/'checks.py').write_text("def answer(root,params):\n return (root/'answer.txt').read_text()=='42', 'answer'\ndef certified(root,params):\n return (root/'certified').exists(), 'certification'\nCHECKS={'answer':answer,'certified':certified}\n")
    (v/'variant.yaml').write_text('name: standard\nuser_prompt_file: user.md\nsystem_prompt_file: system.md\nsandbox:\n  template_dir: seed\n')
    (v/'user.md').write_text('Write answer.txt');(v/'system.md').write_text('Test role')
    (v/'seed').mkdir();(v/'seed/input.txt').write_text('frozen')
    return b

def test_external_no_telemetry(benchmark,tmp_path):
    run=tmp_path/'run';prepare(benchmark,run)
    assert not (run/'workspace/checks.py').exists()
    (run/'workspace/answer.txt').write_text('42')
    result=evaluate(run,'completed')
    assert result['grade']['score']==1 and not result['accepted']
    (run/'workspace/certified').touch()
    result=evaluate(run);assert result['accepted']
    assert len(list((run/'evaluations').glob('*.json')))==2
    assert read(run/'external-run.json')['execution']['cost_usd'] is None
    assert not (run/'transcript.jsonl.gz').exists()

@pytest.mark.parametrize('mutation',['seed','prompt','reference','symlink'])
def test_integrity(benchmark,tmp_path,mutation):
    run=tmp_path/'run';prepare(benchmark,run);(run/'workspace/answer.txt').write_text('42')
    if mutation=='seed':(run/'workspace/input.txt').write_text('modified')
    if mutation=='prompt':(run/'prompts.json').write_text('{}')
    if mutation=='reference':(benchmark/'checks.py').write_text('CHECKS={}')
    if mutation=='symlink':(run/'workspace/linked').symlink_to(benchmark/'checks.py')
    with pytest.raises(ValueError):evaluate(run,'completed')

def test_failed_execution_cannot_pass(benchmark,tmp_path):
    run=tmp_path/'run';prepare(benchmark,run)
    (run/'workspace/answer.txt').write_text('42');(run/'workspace/certified').touch()
    assert not evaluate(run,'failed')['accepted']
    with pytest.raises(ValueError):evaluate(run,'completed')

def test_prepare_exclusive_and_status(benchmark,tmp_path):
    run=tmp_path/'run';prepare(benchmark,run)
    with pytest.raises(FileExistsError):prepare(benchmark,run)
    with pytest.raises(ValueError):prepare(benchmark,benchmark/'runs')
    with pytest.raises(ValueError):evaluate(run)

def test_cli_registration():
    from click.testing import CliRunner
    from toolbench.cli import cli
    result=CliRunner().invoke(cli,['external','--help'])
    assert result.exit_code==0 and 'conjure' in result.output and 'evaluate' in result.output

@pytest.mark.parametrize('behavior',['success','error','timeout'])
def test_real_conjure_runtime(benchmark,tmp_path,behavior):
    pytest.importorskip('conjure')
    from conjure import Runtime,AgentSpec
    from toolbench.external_conjure import execute
    run=tmp_path/'run';prepare(benchmark,run)
    config=tmp_path/'config.yaml';config.write_text('llms: {}\nroot:\n  engine: codex\n  role_prompt: ignored\n')
    class Engine:
        def step(self,prompt):
            assert 'Write answer.txt' in prompt
            if behavior=='error':raise RuntimeError('test error')
            if behavior=='timeout':
                import time;time.sleep(.2)
            (run/'workspace/answer.txt').write_text('42')
            (run/'workspace/certified').touch()
            return 'done'
    def builder(cfg,**kw):
        assert cfg.root.role_prompt=='Test role' and cfg.root.sandbox_dir==str(run/'workspace')
        assert cfg.runtime.auto_approve is True
        rt=Runtime(store_dir=Path(cfg.runtime.store_dir),engine_factory=lambda record,runtime:Engine(),spawn_listener=kw['spawn_listener'])
        root=rt.root(AgentSpec(role_prompt=cfg.root.role_prompt))
        return rt,root
    manifest=execute(run,config,timeout=.03 if behavior=='timeout' else 3,builder=builder)
    assert manifest['execution']['status']=={'success':'completed','error':'failed','timeout':'timed_out'}[behavior]
    assert evaluate(run)['accepted']==(behavior=='success')
    with pytest.raises(ValueError):execute(run,config,builder=builder)

def test_dependency_drift(benchmark,tmp_path):
    dependency=tmp_path/'shared';dependency.mkdir();(dependency/'check.py').write_text('# original')
    run=tmp_path/'run';prepare(benchmark,run,dependency_roots=[dependency])
    (dependency/'check.py').write_text('# changed')
    with pytest.raises(ValueError,match='dependency drift'):evaluate(run,'completed')

def test_workspace_symlink_rejected(benchmark,tmp_path):
    import shutil
    run=tmp_path/'run';prepare(benchmark,run);shutil.rmtree(run/'workspace')
    (run/'workspace').symlink_to(benchmark,target_is_directory=True)
    with pytest.raises(ValueError):evaluate(run,'completed')

def test_conjure_execution_lock(tmp_path):
    from toolbench.external_conjure import execute
    (tmp_path/'.execution-lock').mkdir()
    with pytest.raises(ValueError,match='already holds'):execute(tmp_path,tmp_path/'missing.yaml')

@pytest.mark.parametrize('auto_setting',[None,False])
def test_actual_conjure_builder(benchmark,tmp_path,monkeypatch,auto_setting):
    pytest.importorskip('conjure')
    import conjure.runner
    from toolbench.external_conjure import execute
    run=tmp_path/'run';prepare(benchmark,run)
    config=tmp_path/'config.yaml';config.write_text('llms: {}\nroot:\n  engine: codex\n  role_prompt: ignored\n  tools: []\n')
    if auto_setting is False:
        config.write_text(config.read_text()+'runtime:\n  auto_approve: false\n')
    class Engine:
        def step(self,prompt):
            (run/'workspace/answer.txt').write_text('42');(run/'workspace/certified').touch()
            return 'done'
    # Exercise actual config -> build_runtime -> engine dispatch -> driver ->
    # completion. Only the model-facing engine is replaced; no provider call.
    monkeypatch.setattr(conjure.runner,'_build_codex_engine',lambda **kwargs:Engine())
    result=execute(run,config,timeout=3)
    assert read(run/'conjure-config.json')['runtime']['auto_approve'] is (auto_setting is not False)
    assert result['execution']['status']=='completed',result['execution']
    assert evaluate(run)['accepted']


def test_internal_build_symlinks(benchmark,tmp_path):
    run=tmp_path/'run';prepare(benchmark,run)
    work=run/'workspace';(work/'answer.txt').write_text('42');(work/'certified').touch()
    (work/'scratch').mkdir();(work/'scratch/build.inc').write_text('build data')
    (work/'scratch/coupl.inc').symlink_to('build.inc')
    result=evaluate(run,'completed')
    assert result['accepted']
    assert result['artifacts']['scratch/coupl.inc']=={'symlink':'build.inc'}
    # A seeded input cannot be substituted with even a contained link.
    (work/'input.txt').unlink();(work/'input.txt').symlink_to('scratch/build.inc')
    with pytest.raises(ValueError,match='Seeded input drift'):evaluate(run)


def conjure_benchmark(b,fail=False):
    """Give the fixture benchmark a `conjure:` block (overlay, pinned profile, preflight)."""
    (b/'runtime.yaml').write_text('runtime:\n  max_depth: 1\nroot:\n  tools: [filesystem]\n')
    (b/'profile.yaml').write_text('versions:\n  kit: 1.2.3\ntoolkits:\n  kit:\n    bundles: [core]\n')
    (b/'preflight.py').write_text(
        "import os\nfrom pathlib import Path\n"
        "def check(config,run,params):\n"
        f"    if {fail!r}: raise RuntimeError('kit 1.2.3 not installed')\n"
        "    assert Path(os.getcwd()).resolve()==Path(run).resolve(), 'not run from the trial'\n"
        "    assert (Path(run)/'.toolbase/loadouts/kit-profile.yaml').is_file()\n"
        "    assert config['toolbase']['project_root']==str(run) and config['runtime']['max_depth']==1\n"
        "    return {'status':'passed','params':params}\n")
    y=(b/'benchmark.yaml').read_text()
    (b/'benchmark.yaml').write_text(y+'conjure:\n  runtime: runtime.yaml\n  toolbase:\n    loadout: kit-profile\n    profile: profile.yaml\n'
        '  preflight:\n    module: preflight.py\n    params: {required: [a, b]}\n')


def scripted_builder(run,seen):
    from conjure import Runtime,AgentSpec
    import os
    class Engine:
        def step(self,prompt):
            (run/'workspace/answer.txt').write_text('42');return 'done'
    def builder(cfg,**kw):
        seen.update(cwd=os.getcwd(),max_depth=cfg.runtime.max_depth,project_root=cfg.toolbase.project_root,
                    loadout=cfg.toolbase.loadout,root_loadout=cfg.root.toolbase_loadout,tools=cfg.root.tools)
        rt=Runtime(store_dir=Path(cfg.runtime.store_dir),engine_factory=lambda record,runtime:Engine(),spawn_listener=kw['spawn_listener'])
        return rt,rt.root(AgentSpec(role_prompt=cfg.root.role_prompt))
    return builder


def test_benchmark_conjure_block_is_honoured(benchmark,tmp_path):
    pytest.importorskip('conjure')
    import os
    from toolbench.external_conjure import execute
    conjure_benchmark(benchmark)
    run=tmp_path/'run';prepare(benchmark,run)
    config=tmp_path/'config.yaml';config.write_text('llms: {}\nroot:\n  engine: codex\n  role_prompt: x\n  toolkits: [broad]\n')
    before=os.getcwd();seen={}
    manifest=execute(run,config,timeout=3,builder=scripted_builder(run,seen))
    assert os.getcwd()==before
    assert manifest['execution']['status']=='completed'
    assert Path(seen['cwd']).resolve()==run.resolve() and seen['max_depth']==1 and seen['tools']==['filesystem']
    assert seen['project_root']==str(run.resolve()) and seen['loadout']==seen['root_loadout']=='kit-profile'
    assert (run/'.toolbase/serve.yaml').read_text()=='default:\n  loadout: kit-profile\n'
    report=read(run/'preflight.json');assert report['status']=='passed' and report['params']=={'required':['a','b']}
    applied=manifest['execution']['benchmark_conjure']
    assert applied['preflight']=='passed' and applied['toolbase']['loadout']=='kit-profile' and applied['runtime_sha256']
    assert 'toolkits' not in read(run/'conjure-config.json')['root'] or not read(run/'conjure-config.json')['root']['toolkits']


def test_failed_preflight_is_an_invalid_trial(benchmark,tmp_path):
    pytest.importorskip('conjure')
    from toolbench.external_conjure import execute
    conjure_benchmark(benchmark,fail=True)
    run=tmp_path/'run';prepare(benchmark,run)
    config=tmp_path/'config.yaml';config.write_text('llms: {}\nroot:\n  engine: codex\n  role_prompt: x\n')
    seen={}
    manifest=execute(run,config,timeout=3,builder=scripted_builder(run,seen))
    assert not seen, 'the model must not be called after a failed preflight'
    e=manifest['execution'];assert e['status']=='failed' and e['invalid_trial'] and 'kit 1.2.3' in e['error']
    assert read(run/'preflight.json')['status']=='failed'
    evaluate(run)  # still gradeable; nothing passes


def test_preflight_only_leaves_the_run_ready(benchmark,tmp_path):
    pytest.importorskip('conjure')
    from toolbench.external_conjure import execute
    conjure_benchmark(benchmark)
    run=tmp_path/'run';prepare(benchmark,run)
    config=tmp_path/'config.yaml';config.write_text('llms: {}\nroot:\n  engine: codex\n  role_prompt: x\n')
    report=execute(run,config,preflight_only=True)
    assert report['status']=='passed' and read(run/'external-run.json')['execution']['status']=='prepared'
    seen={};assert execute(run,config,timeout=3,builder=scripted_builder(run,seen))['execution']['status']=='completed'


def test_inherited_conjure_paths_anchor_at_the_parent(benchmark,tmp_path):
    from toolbench.core.benchmark import YamlBenchmark
    conjure_benchmark(benchmark)
    child=tmp_path/'child';(child/'variants/standard').mkdir(parents=True)
    (child/'benchmark.yaml').write_text('name: child\nextends: ../benchmark\n')
    for f in ['variant.yaml','user.md','system.md']:(child/'variants/standard'/f).write_text((benchmark/'variants/standard'/f).read_text())
    (child/'variants/standard/seed').mkdir();(child/'variants/standard/seed/input.txt').write_text('frozen')
    cj=YamlBenchmark(child).cfg['conjure']
    assert cj['runtime']==str((benchmark/'runtime.yaml').resolve()) and cj['preflight']['module']==str((benchmark/'preflight.py').resolve())
    assert cj['toolbase']['profile']==str((benchmark/'profile.yaml').resolve())
