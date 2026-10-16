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
