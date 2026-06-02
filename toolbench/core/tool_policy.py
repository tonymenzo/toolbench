"""
Core (primitive) tool factory.

`_build_core_tool` instantiates an Orchestral primitive tool by class name
(`RunCommandTool`, `WriteFileTool`, ...), scoped to a trial's sandbox
`base_directory`. A harness's `core: {tools: [...]}` block names which
primitives it supplies; the resolver (eval/core/tool_resolver.py) calls
this factory for each. These are framework-level primitives, distinct from
the benchmark-specific tools a loadout brings in via its sources.
"""


def _build_core_tool(name: str, base_directory: str):
    """Instantiate an Orchestral primitive tool by class name.

    The factory table here is deliberately exhaustive of Orchestral's
    stock tools used in this harness; adding a new core tool means
    extending this dict. `base_directory` scopes the tool to a single
    trial's sandbox.
    """
    from orchestral.tools import (
        RunCommandTool, WriteFileTool, ReadFileTool, EditFileTool,
        FindFilesTool, FileSearchTool, RunPythonTool, WebSearchTool,
        TodoRead, TodoWrite,
    )
    factories = {
        "RunCommandTool": lambda: RunCommandTool(base_directory=base_directory),
        "WriteFileTool":  lambda: WriteFileTool(base_directory=base_directory),
        "ReadFileTool":   lambda: ReadFileTool(base_directory=base_directory,
                                               show_line_numbers=True),
        "EditFileTool":   lambda: EditFileTool(base_directory=base_directory),
        "FindFilesTool":  lambda: FindFilesTool(base_directory=base_directory),
        "FileSearchTool": lambda: FileSearchTool(base_directory=base_directory),
        "RunPythonTool":  lambda: RunPythonTool(base_directory=base_directory,
                                                timeout=1000),
        "WebSearchTool":  lambda: WebSearchTool(),
        "TodoRead":       lambda: TodoRead(),
        "TodoWrite":      lambda: TodoWrite(base_directory=base_directory),
    }
    if name not in factories:
        raise ValueError(
            f"Unknown core tool: {name!r}. Known: {sorted(factories)}"
        )
    return factories[name]()
