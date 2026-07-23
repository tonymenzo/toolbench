# Custom tools & providers

You have three ways to give an agent domain tools, and a hook for adding a model provider.
For real, versioned, isolated toolkits, author them in
[toolbase](../guides/toolbase.md) and reference a profile. For quick, benchmark-local
tools, write a Python module and point a `python:` loadout source at it, the no-toolbase
escape hatch.

## A benchmark-local tool module

Tools are `@define_tool` functions. A module exposes them in a `TOOLS` list:

```python
# examples/<name>/tools/extras.py
from orchestral.tools import define_tool

@define_tool
def midpoint(p1: list[float], p2: list[float]) -> dict:
    """Component-wise midpoint of two points."""
    return {"result": [(a + b) / 2.0 for a, b in zip(p1, p2)]}

TOOLS = [midpoint]
```

Type hints become the agent-visible input schema and the docstring is what the agent reads,
so write both for the model, not just for yourself. Point a loadout at the module by a path
relative to the benchmark directory:

```yaml
tools:
  sources:
    - python: tools/extras.py
```

A `python:` source accepts either this **filesystem path** (a `.py` file or package
directory, resolved against the benchmark dir) or a **dotted module path** for a tool that
ships in an installed package, so a tool module can live anywhere.

## Bundles and `select:`

Group tools into named bundles so a loadout can take a subset:

```python
TOOLS = [add, subtract, multiply, divide, power]
BUNDLES = {
    "additive": [add, subtract],
    "multiplicative": [multiply, divide],
}
```

```yaml
tools:
  sources:
    - python: my.tools.module
      select: [additive]        # only add + subtract (bundle name or bare tool names)
```

## Tools that need the sandbox or config

When a tool must know the trial's working directory or take external config at construction,
expose a factory instead of a static list:

```python
def make_tools(base_directory, select=None, config=None):
    """Return ready tool instances. The factory owns `select` semantics."""
    ...
    return [ ... ]
```

The runner calls `make_tools(sandbox_dir, select=..., config=...)` per trial. (For simple
`@define_tool` functions you don't need this, since toolbench best-effort scopes any tool
that carries a `base_directory` to the sandbox automatically.)

## Adding a model provider

The CLI's `--models` are resolved through provider factories. toolbench ships
`anthropic`, `openai`, `google`, `groq`, `litellm`, and `stub` (the dry-run stub), plus
`subscription` for the CLI runtimes. To add your own, register a factory before the run:

```python
from toolbench.core.llm_factory import register_provider

def my_provider(model=None, **kw):
    return MyOrchestralLLM(model=model, **kw)

register_provider("myprovider", my_provider)
```

Then a harness whose `provider.name` is `myprovider` will use it. API keys come from the
environment (or a `.env` at the repo root), loaded before anything reads `os.environ`.

The same registry also routes LLM **judges**: an LLM-as-judge is built through the provider
registry exactly like the agent's model, so a custom provider registered here can serve as a
judge backend too (see [Choosing a judge](overview.md#choosing-a-judge)).
