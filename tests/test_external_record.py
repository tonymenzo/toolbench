"""Trial records from a Conjure session: trial.json and transcript.jsonl.gz."""
from pathlib import Path
import gzip
import json
import pytest
from toolbench.external import prepare,evaluate,read,write
from toolbench.external_record import build,write_records,relocate_tool_output,_costs
from tests.test_external import benchmark  # noqa: F401  (fixture)


def jsonl(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))


def session(run,*,ids=True,codex=False,access='api'):
    """A small native session: a root and one worker, as Conjure writes it."""
    s=run/'conjure-native/sessions/s1'
    worker_engine='codex' if codex else 'orchestral'
    jsonl(s/'journal.jsonl',[
        {'kind':'spawn','ts':100.0,'payload':{'addr':{'id':'ag-root','label':'root'},'parent':None,
            'spec':{'engine':'orchestral','llm':'opus','toolkits':{'heptapod':{'bundles':['nda']}},'toolbase_loadout':'kit'}}},
        {'kind':'send','ts':100.5,'payload':{'envelope':{'msg_id':'m1','from':{'id':'@user'},'to':{'id':'ag-root'},'body':'go'}}},
        {'kind':'spawn','ts':101.0,'payload':{'addr':{'id':'ag-w','label':'worker'},'parent':{'id':'ag-root'},
            'spec':{'engine':worker_engine,'llm':'gpt','toolkits':None}}},
        {'kind':'toolkits','ts':101.5,'payload':{'addr':{'id':'ag-w'},'granted_by':{'id':'ag-root'},
            'toolkits':{'heptapod':{'bundles':['pdg']}}}},
        {'kind':'agent_meta','ts':109.0,'payload':{'addr':'ag-root','meta':{'engine':'orchestral','model':'opus-5','model_source':'observed',
            'cost_usd':2.5,'tokens':130,**({'access':access,'token_usage':{'input':120,'output':10,'cache_read':100,'cache_creation':0}} if access else {})}}},
        {'kind':'agent_meta','ts':109.0,'payload':{'addr':'ag-w','meta':{'engine':worker_engine,'model':'gpt-5',
            'cost_usd':0.0 if codex else 0.5,**({'access':'subscription' if codex else access,
            'token_usage':{'input':50,'output':5,'cache_read':0,'cache_creation':0}} if access else {})}}},
        {'kind':'terminate','ts':110.0,'payload':{'addr':{'id':'ag-w'},'requested_by':'ag-root'}},
    ])
    cid=lambda i:{'id':i} if ids else {}
    tid=lambda i:{'tool_call_id':i} if ids else {}
    jsonl(s/'agents/ag-root.jsonl',[
        {'kind':'turn_start','ts':100.6},
        {'kind':'chunk','ts':100.7,'text':'Reading '},{'kind':'chunk','ts':100.8,'text':'the scope.'},
        {'kind':'stream_end','ts':101.0,'tool_calls':[{**cid('c1'),'name':'Bash','args':{'command':'cat scope.json'}},
                                                     {**cid('c2'),'name':'heptapod__EstimateDecayWidthNDA','args':{'process':'Z > e+ e-'}}]},
        {'kind':'usage','ts':101.0,'tokens_in':60,'tokens_out':4},
        # results out of call order: ids pair them; without ids, order does
        {'kind':'tool','ts':102.0,**tid('c2' if ids else None),'text':'{"width_gev": 0.08}','failed':False},
        {'kind':'tool','ts':103.0,**tid('c1' if ids else None),'text':'x'*50,'failed':True},
        {'kind':'tool_output_truncated','ts':103.0,**tid('c1'),'tool':'Bash','chars':5000,'kept':50,'truncated':4950,
         'path':str(run/'workspace/.conjure/tool-output/c1.txt')},
        {'kind':'usage','ts':104.0,'tokens_in':120,'tokens_out':10},
        {'kind':'chunk','ts':105.0,'text':'Done.'},{'kind':'stream_end','ts':105.1,'tool_calls':[]},
        {'kind':'turn_end','ts':106.0,'errored':False},
    ])
    if codex:
        jsonl(s/'agents/ag-w.jsonl',[
            {'kind':'turn_start','ts':102.0},
            {'kind':'stream_end','ts':102.1,'tool_calls':[{'id':'i1','name':'web_search','args':{'query':'Z width'}}]},
            {'kind':'tool','ts':102.2,'tool_call_id':'i1','text':'completed','failed':False},
            {'kind':'turn_end','ts':103.0,'errored':False}])
        home=run/'conjure-native/codex_homes/ag-w'
        (home).mkdir(parents=True);(home/'auth.json').write_text('{"secret": "never read"}')
        jsonl(home/'sessions/2026/10/22/rollout-x.jsonl',[
            {'type':'event_msg','payload':{'type':'web_search_end','call_id':'ws1','query':'Z width',
             'action':{'type':'search','query':'Z width'},
             'results':[{'url':'https://pdg.lbl.gov/z','title':'Z boson','snippet':'2.4955 GeV'}]}}])
    else:
        jsonl(s/'agents/ag-w.jsonl',[{'kind':'system_prompt','ts':101.1,'text':'You are the worker.'}])
    return s


def test_records_pair_results_with_their_calls(tmp_path):
    run=tmp_path/'run';session(run)
    trial,rows=build(run)
    calls=[r for r in rows if r['type']=='tool_call']
    nda=next(c for c in calls if c['name']=='heptapod__EstimateDecayWidthNDA')
    bash=next(c for c in calls if c['name']=='Bash')
    assert nda['ok'] is True and nda['result']=='{"width_gev": 0.08}' and nda['call_id']=='c2'
    assert bash['ok'] is False and bash['duration_s']==2.0 and bash['agent_label']=='root'
    assert bash['result_truncated']['chars']==5000
    assert [r['content'] for r in rows if r['type']=='assistant']==['Reading the scope.','Done.']
    assert {r['type'] for r in rows}>={'message','spawn','grant','terminate','system_prompt','turn_start'}
    root,worker=trial['agents']
    assert root['toolkits_at_spawn']=={'heptapod':{'bundles':['nda']}} and root['toolbase_loadout']=='kit'
    assert worker['grants']==[{'ts':101.5,'granted_by':'ag-root','toolkits':{'heptapod':{'bundles':['pdg']}}}]
    assert worker['depth']==1 and worker['terminated_by']=='ag-root' and worker['final_response'] is None
    assert root['final_response']=='Done.' and root['n_tool_errors']==1
    t=trial['totals']
    assert t['tokens']=={'input':170,'output':15,'cache_read':100,'cache_creation':0}
    assert t['cost_usd']==3.0 and t['api_equivalent_cost_usd']==3.0 and t['billing']=='api'
    assert trial['trajectory']['resolved_model']=='opus-5' and trial['trajectory']['n_tool_calls']==2


def test_old_logs_pair_by_order_and_infer_what_the_engine_settles(tmp_path):
    run=tmp_path/'run';session(run,ids=False,access=None)
    trial,rows=build(run)
    calls=[r for r in rows if r['type']=='tool_call']
    # Without ids the first result answers the first call.
    assert calls[0]['name']=='Bash' and calls[0]['result']=='{"width_gev": 0.08}'
    root=trial['agents'][0]
    assert root['access']=='api' and root['access_source']=='inferred'
    # Tokens from the usage events (cumulative within a turn), cache unknown.
    assert root['tokens']=={'input':120,'output':10,'cache_read':None,'cache_creation':None}
    assert root['tokens_source']=='usage events'
    assert trial['totals']['tokens']['cache_read'] is None


def test_codex_web_searches_keep_their_results(tmp_path):
    run=tmp_path/'run';session(run,codex=True)
    trial,rows=build(run)
    search=next(r for r in rows if r.get('name')=='web_search')
    assert search['web_search']['results'][0]['url']=='https://pdg.lbl.gov/z'
    assert search['web_search']['rollout'].startswith('conjure-native/codex_homes/ag-w/sessions/')
    assert 'never read' not in json.dumps(rows)+json.dumps(trial)
    worker=trial['agents'][1]
    assert worker['access']=='subscription'
    # The subscription spends nothing per token; its API-equivalent cost is unknown.
    assert trial['totals']['cost_usd']==2.5 and trial['totals']['api_equivalent_cost_usd'] is None
    assert trial['totals']['billing']=='mixed'


def test_costs():
    a=lambda **k:{'access':'api','cost_usd':1.0,'unpriced_tokens':0,'api_equivalent_cost_usd':None}|k
    assert _costs([a(),a(cost_usd=2.0)])==(3.0,3.0,'api')
    assert _costs([a(unpriced_tokens=5)])==(None,None,'api')                    # unpriced: unknown, never 0
    assert _costs([a(access='subscription',cost_usd=0.0,api_equivalent_cost_usd=4.0)])==(0.0,4.0,'subscription')
    assert _costs([a(access=None)])==(None,None,'unknown')


def test_tool_output_leaves_the_workspace(tmp_path):
    run=tmp_path/'run';ws=run/'workspace'
    for d in [ws/'.conjure/tool-output',ws/'work/unit/.conjure/tool-output']:
        d.mkdir(parents=True);(d/'out.txt').write_text('long')
    (ws/'answer.txt').write_text('42')
    assert relocate_tool_output(run)==['.conjure/tool-output','work/unit/.conjure/tool-output']
    assert not (ws/'.conjure').exists() and not (ws/'work/unit/.conjure').exists()
    assert (run/'conjure-native/tool-output/work/unit/.conjure/tool-output/out.txt').read_text()=='long'
    session(run)
    trial,rows=build(run)
    bash=next(r for r in rows if r.get('name')=='Bash')
    assert bash['result_truncated']['path']=='workspace/.conjure/tool-output/c1.txt'  # this one was never written
    (run/'conjure-native/tool-output/.conjure/tool-output/c1.txt').write_text('long')
    trial,rows=build(run)
    bash=next(r for r in rows if r.get('name')=='Bash')
    assert bash['result_truncated']['path']=='conjure-native/tool-output/.conjure/tool-output/c1.txt'


def test_record_command_rebuilds_the_records(tmp_path):
    from click.testing import CliRunner
    from toolbench.cli import cli
    run=tmp_path/'run';session(run)
    write(run/'external-run.json',{'run_id':'r1','benchmark':{'name':'b'},'variant':'standard','execution':{'status':'completed'}})
    result=CliRunner().invoke(cli,['external','record',str(run)])
    assert result.exit_code==0,result.output
    assert json.loads(result.output)['n_tool_calls']==2
    trial=read(run/'trial.json');assert trial['trial_id']=='r1' and trial['schema_version']=='conjure-trial-1.0'
    with gzip.open(run/'transcript.jsonl.gz','rt') as fh:
        assert sum(1 for l in fh if json.loads(l)['type']=='tool_call')==2


def test_adapter_writes_session_layout_and_records(benchmark,tmp_path):
    pytest.importorskip('conjure')
    import conjure.runner
    from toolbench.external_conjure import execute
    run=tmp_path/'run';prepare(benchmark,run)
    config=tmp_path/'config.yaml';config.write_text('llms: {}\nroot:\n  engine: codex\n  role_prompt: ignored\n  tools: []\n')
    class Engine:
        def step(self,prompt):
            ws=run/'workspace';(ws/'answer.txt').write_text('42');(ws/'certified').touch()
            (ws/'.conjure/tool-output').mkdir(parents=True);(ws/'.conjure/tool-output/big.txt').write_text('x')
            return 'done'
    import pytest as _pytest
    mp=_pytest.MonkeyPatch()
    try:
        mp.setattr(conjure.runner,'_build_codex_engine',lambda **kwargs:Engine())
        manifest=execute(run,config,timeout=3)
    finally:
        mp.undo()
    e=manifest['execution']
    assert e['status']=='completed' and 'records_error' not in e,e
    assert e['toolbench']['version'] and e['tool_output_moved']==['.conjure/tool-output']
    folder=run/'conjure-native/sessions'/manifest['run_id']
    assert (folder/'journal.jsonl').is_file() and (folder/'manifest.json').is_file()
    assert list((folder/'agents').glob('*.jsonl')) and not (run/'conjure-native/events').exists()
    trial=read(run/'trial.json')
    assert trial['trial_id']==manifest['run_id'] and trial['execution']['status']=='completed'
    assert trial['agents'][0]['parent'] is None and trial['harness']['conjure_version']
    assert (run/'transcript.jsonl.gz').is_file()
    assert evaluate(run)['accepted']
    assert '.conjure/tool-output/big.txt' not in read(run/'external-result.json')['artifacts']
