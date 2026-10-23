"""Toolbench trial records from a Conjure session.

A Conjure trial keeps its native record under ``<run>/conjure-native/``: the
session journal (spawns, messages, grants, per-agent metadata), one event log
per agent, the session manifest (software and toolkit versions) and, for Codex
agents, Codex's own rollout files. :func:`write_records` reads that record and
writes, beside ``external-run.json``:

``trial.json``
    what the trial ran and what it cost, per agent and in total: engine,
    model, how it was paid for, tokens with the cache, cost and
    API-equivalent cost, toolkits at spawn and every grant, tool-call counts;
    the Toolbench, Conjure and Toolbase versions and the toolkits pinned and
    served; execution status and timings.
``transcript.jsonl.gz``
    every event of every agent in time order: ``tool_call`` rows (the
    Toolbench transcript row: ``name``, ``args``, ``ok``, ``t``, plus the
    agent, the call id and the full result), the agents' replies and inputs,
    messages between agents, spawns, grants and terminations, and, for Codex
    agents, each web search with the results it returned.

Nothing is inferred that the record does not show: an unknown count is null,
never zero. Credentials are never read (Codex homes are searched for rollout
files only).
"""
from __future__ import annotations

import gzip
import json
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 'conjure-trial-1.0'
TRANSCRIPT = 'transcript.jsonl.gz'
TRIAL = 'trial.json'
SUMMARY_CHARS = 1000
TOKEN_KEYS = ('input', 'output', 'cache_read', 'cache_creation')


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def session_dir(run: Path) -> Path:
    """The trial's one native session directory."""
    folders = [p for p in (run / 'conjure-native' / 'sessions').glob('*') if p.is_dir() and not p.is_symlink()]
    if len(folders) != 1:
        raise ValueError(f'Expected one native Conjure session in {run}, found {len(folders)}')
    return folders[0]


def _git(path: Path) -> dict:
    def git(*args):
        try:
            r = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    rev = git('rev-parse', 'HEAD')
    return {'revision': rev, 'dirty': bool(git('status', '--porcelain')) if rev else None}


def toolbench_identity() -> dict:
    import toolbench
    root = Path(toolbench.__file__).resolve().parent
    return {'version': getattr(toolbench, '__version__', None), **_git(root)}


# ------------------------------------------------------------------ agents


def _agents(journal: list[dict]) -> dict[str, dict]:
    """Every agent the journal saw, with its spawn, grants, metadata and end."""
    agents: dict[str, dict] = {}
    for e in journal:
        kind, p, ts = e.get('kind'), e.get('payload') or {}, e.get('ts')
        if kind == 'spawn':
            addr, spec = p.get('addr') or {}, p.get('spec') or {}
            agents[addr['id']] = {
                'id': addr['id'], 'label': addr.get('label') or spec.get('label'),
                'parent': (p.get('parent') or {}).get('id'), 'spawned_at': ts,
                'internal': bool(spec.get('internal')),
                'spec': {k: spec.get(k) for k in ('engine', 'llm', 'model', 'tools', 'capabilities',
                                                  'toolkits', 'toolbase_loadout', 'sandbox_dir', 'budget')},
                'grants': [], 'meta': {}, 'terminated_at': None, 'terminated_by': None,
            }
        elif kind == 'toolkits':
            a = agents.get((p.get('addr') or {}).get('id'))
            if a is not None:
                a['grants'].append({'ts': ts, 'granted_by': (p.get('granted_by') or {}).get('id'),
                                    'toolkits': p.get('toolkits')})
        elif kind == 'agent_meta':
            a = agents.get(p.get('addr'))
            if a is not None:
                a['meta'].update(p.get('meta') or {})
        elif kind == 'terminate':
            a = agents.get((p.get('addr') or {}).get('id'))
            if a is not None and a['terminated_at'] is None:
                a['terminated_at'], a['terminated_by'] = ts, p.get('requested_by')
    depth = {}
    def d(aid):
        if aid not in depth:
            parent = agents[aid]['parent']
            depth[aid] = 0 if parent is None or parent not in agents else d(parent) + 1
        return depth[aid]
    for aid in agents:
        agents[aid]['depth'] = d(aid)
    return agents


def _access(meta: dict, engine: str | None) -> dict:
    """How the agent's model was paid for. Journaled by Conjure; for a
    session recorded before it was, inferred where the engine settles it
    (orchestral calls a provider's API, Codex runs on the ChatGPT
    subscription), else unknown."""
    if meta.get('access'):
        return {'access': meta['access'], 'access_source': 'journaled'}
    inferred = {'orchestral': 'api', 'codex': 'subscription'}.get(engine or '')
    return {'access': inferred, 'access_source': 'inferred' if inferred else None}


def _tokens(meta: dict, stats: dict, engine: str | None) -> dict:
    """The agent's tokens: Conjure's journaled breakdown; for an orchestral
    session recorded before it was, input and output summed from the
    agent's usage events, cache counts unknown; else unknown."""
    if meta.get('token_usage'):
        return {'tokens': meta['token_usage'], 'tokens_source': 'journaled'}
    if not stats['turns']:
        return {'tokens': dict.fromkeys(TOKEN_KEYS, 0), 'tokens_source': 'no turns'}
    if engine == 'orchestral' and stats['usage_events']:
        return {'tokens': {'input': stats['usage_in'], 'output': stats['usage_out'],
                           'cache_read': None, 'cache_creation': None}, 'tokens_source': 'usage events'}
    return {'tokens': None, 'tokens_source': None}


def _llm_models(config: dict) -> dict[str, str]:
    return {name: (llm or {}).get('model') for name, llm in ((config or {}).get('llms') or {}).items()}


# --------------------------------------------------------------- transcript


def _codex_searches(run: Path, agent: str) -> list[dict]:
    """Web searches a Codex agent made, with their results, from Codex's rollout."""
    home = run / 'conjure-native' / 'codex_homes' / agent / 'sessions'
    out = []
    for path in sorted(home.rglob('*.jsonl')) if home.is_dir() else []:
        for rec in _jsonl(path):
            msg = rec.get('payload') if isinstance(rec.get('payload'), dict) else rec
            if not isinstance(msg, dict) or msg.get('type') != 'web_search_end':
                continue
            out.append({'call_id': msg.get('call_id'), 'query': msg.get('query'),
                        'action': msg.get('action'), 'results': msg.get('results'),
                        'rollout': path.relative_to(run).as_posix()})
    return out


def _agent_rows(run: Path, folder: Path, agent: dict, origin: float) -> tuple[list[dict], dict]:
    """One agent's events as transcript rows, and its tool counts."""
    events = _jsonl(folder / 'agents' / f"{agent['id']}.jsonl")
    rows: list[dict] = []
    pending: list[dict] = []     # calls awaiting a result, in call order
    by_id: dict[str, dict] = {}
    calls: list[dict] = []
    reply: list[str] = []
    reply_ts = None
    searches = _codex_searches(run, agent['id']) if agent['spec'].get('engine') == 'codex' else []
    who = {'agent': agent['id'], 'agent_label': agent['label']}

    def flush(ts):
        nonlocal reply, reply_ts
        if reply:
            rows.append({'type': 'assistant', **who, 'ts': reply_ts, 't': _t(reply_ts, origin),
                         'content': ''.join(reply)})
        reply, reply_ts = [], None

    for ev in events:
        kind, ts = ev.get('kind'), ev.get('ts')
        if kind == 'chunk':
            if reply_ts is None:
                reply_ts = ts
            reply.append(ev.get('text') or '')
        elif kind in ('response', 'assistant'):
            flush(ts)
            if ev.get('text'):
                rows.append({'type': 'assistant', **who, 'ts': ts, 't': _t(ts, origin), 'content': ev['text']})
            for tc in ev.get('tool_calls') or []:
                _open_call(tc, ts, who, origin, rows, pending, by_id, calls)
        elif kind == 'stream_end':
            flush(ts)
            for tc in ev.get('tool_calls') or []:
                _open_call(tc, ts, who, origin, rows, pending, by_id, calls)
        elif kind == 'tool':
            call = by_id.pop(ev.get('tool_call_id'), None) if ev.get('tool_call_id') else None
            if call is not None:
                pending.remove(call)
            elif pending:
                call = pending.pop(0)
                by_id.pop(call.get('call_id'), None)
            text, failed = ev.get('text') or '', bool(ev.get('failed'))
            if call is None:   # a result with no call on record
                call = {'type': 'tool_call', **who, 'name': None, 'args': {}, 'call_id': ev.get('tool_call_id'),
                        'ts': ts, 't': _t(ts, origin)}
                rows.append(call)
                calls.append(call)
            call.update(ok=not failed, result=text, result_summary=text[:SUMMARY_CHARS],
                        returned_at=ts, duration_s=_dt(call.get('ts'), ts))
            if call['name'] == 'web_search' and searches:
                found = searches.pop(0)
                call['web_search'] = found
        elif kind == 'tool_output_truncated':
            target = next((c for c in reversed(calls) if ev.get('tool_call_id') and c.get('call_id') == ev['tool_call_id']), None)
            target = target or (calls[-1] if calls else None)
            if target is not None:
                target['result_truncated'] = {k: ev.get(k) for k in ('chars', 'kept', 'truncated')} \
                    | {'path': overflow_path(run, ev.get('path'))}
        elif kind in ('user', 'system_prompt'):
            flush(ts)
            rows.append({'type': 'input' if kind == 'user' else 'system_prompt', **who, 'ts': ts,
                         't': _t(ts, origin), 'content': ev.get('text') or ''})
        elif kind in ('turn_start', 'turn_end', 'retry', 'error'):
            flush(ts)
            row = {'type': kind, **who, 'ts': ts, 't': _t(ts, origin)}
            row.update({k: v for k, v in ev.items() if k not in ('kind', 'ts')})
            rows.append(row)
    flush(None)
    for call in pending:   # called, never answered (the run stopped)
        call.setdefault('ok', None)
        call.setdefault('result', None)
    for left in searches:  # searches with no call row on record
        rows.append({'type': 'web_search', **who, 'ts': None, 't': None, **left})
    usage_in = usage_out = usage_events = 0
    turn_in = turn_out = 0
    for ev in events:   # usage is cumulative within a turn: count each turn's last
        if ev.get('kind') == 'turn_start':
            usage_in, usage_out, turn_in, turn_out = usage_in + turn_in, usage_out + turn_out, 0, 0
        elif ev.get('kind') == 'usage':
            usage_events += 1
            turn_in = max(turn_in, int(ev.get('tokens_in') or 0))
            turn_out = max(turn_out, int(ev.get('tokens_out') or 0))
    usage_in, usage_out = usage_in + turn_in, usage_out + turn_out
    by_name: dict[str, int] = {}
    for c in calls:
        by_name[c['name']] = by_name.get(c['name'], 0) + 1
    stats = {'usage_in': usage_in, 'usage_out': usage_out, 'usage_events': usage_events,'n_tool_calls': len(calls), 'n_tool_errors': sum(1 for c in calls if c.get('ok') is False),
             'tool_calls_by_name': dict(sorted(by_name.items(), key=lambda kv: (-kv[1], str(kv[0])))),
             'turns': sum(1 for e in events if e.get('kind') == 'turn_start'),
             'errored_turns': sum(1 for e in events if e.get('kind') == 'turn_end' and e.get('errored')),
             'final_response': next((r['content'] for r in reversed(rows) if r['type'] == 'assistant'), '')}
    return rows, stats


def overflow_path(run: Path, path: str | None) -> str | None:
    """Where a tool's full output now is, relative to the run: Conjure writes
    it under the agent's sandbox (``.conjure/tool-output/``), and the adapter
    moves it out of the graded workspace to ``conjure-native/tool-output/``."""
    if not path:
        return None
    p, workspace = Path(path), run / 'workspace'
    try:
        rel = p.relative_to(workspace)
    except ValueError:
        try:
            return p.relative_to(run).as_posix()
        except ValueError:
            return path
    moved = run / 'conjure-native' / 'tool-output' / rel
    return moved.relative_to(run).as_posix() if moved.exists() else ('workspace/' + rel.as_posix())


def relocate_tool_output(run: Path) -> list[str]:
    """Move every ``.conjure/tool-output`` directory out of the workspace to
    ``conjure-native/tool-output/``, keeping its path, so the graded
    artifacts are the agents' work alone. Returns the moved directories."""
    import shutil
    run = Path(run).resolve()
    workspace, moved = run / 'workspace', []
    for d in sorted(workspace.rglob('.conjure/tool-output')):
        if d.is_symlink() or not d.is_dir():
            continue
        rel = d.relative_to(workspace)
        target = run / 'conjure-native' / 'tool-output' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(target))
        moved.append(rel.as_posix())
        try:
            d.parent.rmdir()   # .conjure, when nothing else is in it
        except OSError:
            pass
    return moved


def _open_call(tc, ts, who, origin, rows, pending, by_id, calls):
    call = {'type': 'tool_call', **who, 'name': tc.get('name'), 'args': tc.get('args') or {},
            'call_id': tc.get('id'), 'ts': ts, 't': _t(ts, origin)}
    rows.append(call)
    pending.append(call)
    calls.append(call)
    if call['call_id']:
        by_id[call['call_id']] = call


def _t(ts, origin):
    return round(ts - origin, 4) if isinstance(ts, (int, float)) and origin is not None else None


def _dt(a, b):
    return round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None


def _journal_rows(journal: list[dict], agents: dict, origin: float) -> list[dict]:
    rows = []
    for e in journal:
        kind, p, ts = e.get('kind'), e.get('payload') or {}, e.get('ts')
        if kind == 'send':
            env = p.get('envelope') or {}
            rows.append({'type': 'message', 'ts': ts, 't': _t(ts, origin),
                         'from': (env.get('from') or {}).get('id'), 'to': (env.get('to') or {}).get('id'),
                         'msg_id': env.get('msg_id'), 'in_reply_to': env.get('in_reply_to'), 'body': env.get('body')})
        elif kind == 'spawn':
            aid = (p.get('addr') or {}).get('id')
            a = agents.get(aid, {})
            rows.append({'type': 'spawn', 'ts': ts, 't': _t(ts, origin), 'agent': aid, 'agent_label': a.get('label'),
                         'parent': a.get('parent'), 'spec': a.get('spec')})
        elif kind == 'toolkits':
            rows.append({'type': 'grant', 'ts': ts, 't': _t(ts, origin), 'agent': (p.get('addr') or {}).get('id'),
                         'granted_by': (p.get('granted_by') or {}).get('id'), 'toolkits': p.get('toolkits')})
        elif kind == 'terminate':
            rows.append({'type': 'terminate', 'ts': ts, 't': _t(ts, origin),
                         'agent': (p.get('addr') or {}).get('id'), 'requested_by': p.get('requested_by')})
    return rows


# ------------------------------------------------------------------- totals


def _sum_tokens(parts: list[dict | None]) -> dict | None:
    """Summed per key; a key any agent lacks is null."""
    if not parts or any(p is None for p in parts):
        return None
    return {k: None if any(p.get(k) is None for p in parts) else sum(int(p[k]) for p in parts)
            for k in TOKEN_KEYS}


def _costs(agents: list[dict]) -> tuple[float | None, float | None, str]:
    """(actual spend, API-equivalent cost, billing) over the agents.

    Spend counts API-billed agents only; a subscription spends nothing per
    token. The API-equivalent cost needs every agent's figure, so it is null
    when any agent's is unknown (a Codex agent, or an unpriced model)."""
    access = {a['access'] for a in agents}
    billing = access.pop() if len(access) == 1 else ('mixed' if access else 'unknown')
    if billing is None:
        billing = 'unknown'
    api = [a for a in agents if a['access'] == 'api']
    unknown = any(a['access'] is None for a in agents)
    spend = None if unknown or any(a['cost_usd'] is None or a['unpriced_tokens'] for a in api) \
        else round(sum(a['cost_usd'] for a in api), 6)
    equivalent = []
    for a in agents:
        if a['access'] == 'api':
            equivalent.append(None if a['unpriced_tokens'] else a['cost_usd'])
        else:
            equivalent.append(a['api_equivalent_cost_usd'])
    equivalent_total = None if any(x is None for x in equivalent) else round(sum(equivalent), 6)
    return spend, equivalent_total, billing


# --------------------------------------------------------------------- main


def build(run: Path) -> tuple[dict, list[dict]]:
    """The trial record and transcript rows for one Conjure run."""
    run = Path(run).resolve()
    folder = session_dir(run)
    manifest = _json(run / 'external-run.json') or {}
    execution = manifest.get('execution') or {}
    session = _json(folder / 'manifest.json') or {}
    config = _json(run / 'conjure-config.json') or session.get('config') or {}
    journal = _jsonl(folder / 'journal.jsonl')
    agents = _agents(journal)
    origin = execution.get('started_at') or (journal[0].get('ts') if journal else None)
    llm_models = _llm_models(config)

    rows = _journal_rows(journal, agents, origin)
    records = []
    for aid, a in agents.items():
        agent_rows, stats = _agent_rows(run, folder, a, origin)
        rows.extend(agent_rows)
        meta, spec = a['meta'], a['spec']
        records.append({
            'id': aid, 'label': a['label'], 'parent': a['parent'], 'depth': a['depth'], 'internal': a['internal'],
            'engine': meta.get('engine') or spec.get('engine'),
            'llm': spec.get('llm'),
            'model': meta.get('model') or spec.get('model') or llm_models.get(spec.get('llm')),
            'model_source': meta.get('model_source') or ('configured' if spec.get('model') or spec.get('llm') else None),
            **_access(meta, meta.get('engine') or spec.get('engine')),
            **_tokens(meta, stats, meta.get('engine') or spec.get('engine')),
            'tokens_total': meta.get('tokens'),
            'unpriced_tokens': meta.get('unpriced_tokens'),
            # An agent that never took a turn made no model call.
            'cost_usd': meta.get('cost_usd') if meta.get('cost_usd') is not None or stats['turns'] else 0.0,
            'api_equivalent_cost_usd': meta.get('api_equivalent_cost_usd'),
            'toolkits_at_spawn': spec.get('toolkits'),
            'toolbase_loadout': spec.get('toolbase_loadout'),
            'tools': spec.get('tools'),
            'grants': a['grants'],
            'spawned_at': a['spawned_at'], 'terminated_at': a['terminated_at'], 'terminated_by': a['terminated_by'],
            **{k: stats[k] for k in ('n_tool_calls', 'n_tool_errors', 'tool_calls_by_name', 'turns', 'errored_turns')},
            'final_response': stats['final_response'] if a['parent'] is None else None,
        })
    rows.sort(key=lambda r: (r.get('ts') is None, r.get('ts') or 0))

    spend, equivalent, billing = _costs(records)
    roots = [r for r in records if r['parent'] is None]
    root = roots[0] if len(roots) == 1 else None
    by_name: dict[str, int] = {}
    for r in records:
        for name, n in r['tool_calls_by_name'].items():
            by_name[name] = by_name.get(name, 0) + n
    applied = execution.get('benchmark_conjure') or {}
    preflight = _json(run / 'preflight.json')
    pinned = None
    profile = run / '.toolbase' / 'loadouts' / f"{(applied.get('toolbase') or {}).get('loadout')}.yaml"
    if profile.is_file():
        import yaml
        pinned = (yaml.safe_load(profile.read_text()) or {}).get('versions')
    environment = session.get('environment') or {}
    trial = {
        'schema_version': SCHEMA_VERSION,
        'trial_id': manifest.get('run_id'),
        'benchmark': {k: (manifest.get('benchmark') or {}).get(k) for k in ('name', 'version', 'directory')}
                     | {'variant': manifest.get('variant')},
        'harness': {
            'id': 'conjure', 'conjure_version': execution.get('conjure_version')
                or (environment.get('packages') or {}).get('conjure-agents'),
            'packages': environment.get('packages'), 'clis': environment.get('clis'),
            'python': environment.get('python'), 'platform': environment.get('platform'),
            'toolbench': execution.get('toolbench') or toolbench_identity(),
            'config_sha256': execution.get('config_sha256'),
        },
        'toolbase': {
            'loadout': (applied.get('toolbase') or {}).get('loadout') or (config.get('toolbase') or {}).get('loadout'),
            'profile_sha256': (applied.get('toolbase') or {}).get('profile_sha256'),
            'pinned': pinned,
            # What Conjure's Toolbase served the session, from its manifest.
            'served': (session.get('toolbase') or {}).get('toolkits'),
            'authority': (session.get('toolbase') or {}).get('authority'),
            'preflight': preflight,
        },
        'execution': {k: execution.get(k) for k in ('status', 'error', 'invalid_trial', 'started_at', 'ended_at',
                                                    'completion_nudges', 'reaped_pids', 'status_source')}
                     | {'wall_clock_s': _dt(execution.get('started_at'), execution.get('ended_at'))},
        'agents': records,
        'totals': {
            'agents': len(records),
            'tokens': _sum_tokens([r['tokens'] for r in records]),
            'cost_usd': spend,
            'api_equivalent_cost_usd': equivalent,
            'billing': billing,
            'n_tool_calls': sum(r['n_tool_calls'] for r in records),
            'n_tool_errors': sum(r['n_tool_errors'] for r in records),
            'tool_calls_by_name': dict(sorted(by_name.items(), key=lambda kv: (-kv[1], str(kv[0])))),
        },
        # The fields of a native Toolbench trial's trajectory summary, for the team.
        'trajectory': {
            'n_tool_calls': sum(r['n_tool_calls'] for r in records),
            'n_tool_errors': sum(r['n_tool_errors'] for r in records),
            'final_response': root['final_response'] if root else '',
            'tokens': _sum_tokens([r['tokens'] for r in records]),
            'cost_usd': spend,
            'resolved_model': root['model'] if root else None,
        },
        'artifacts': {'transcript': TRANSCRIPT, 'native': folder.relative_to(run).as_posix(),
                      'tool_output': 'conjure-native/tool-output'
                      if (run / 'conjure-native' / 'tool-output').is_dir() else None,
            'tool_output_moved': execution.get('tool_output_moved')},
    }
    return trial, rows


def write_records(run: Path) -> dict:
    """Write trial.json and transcript.jsonl.gz for a Conjure run; return the trial."""
    run = Path(run).resolve()
    trial, rows = build(run)
    tmp = run / (TRANSCRIPT + '.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + '\n')
    tmp.replace(run / TRANSCRIPT)
    from toolbench.external import write
    write(run / TRIAL, json.loads(json.dumps(trial, default=str)))
    return trial
