"""Custom tools for {{AGENT_CLASS}}.

Add Tool subclasses here and register them on {{AGENT_CLASS}}.tools.
The framework's BUILTIN_TOOLS (cat, head, tail, ls, grep, find, read_lines,
fetch_url, search_vault, search_web, update_scratch, add_learning) are
available by default -- opt out via {{AGENT_CLASS}}.disabled_builtins.

Example (commented out below):

    from agent_core.tools.base import Tool

    class MyTool(Tool):
        name = "my_tool"
        description = "Does a thing"
        parameters = {"type": "object", "properties": {}, "required": []}
        requires = ()  # framework manager attrs needed (e.g. ("retrieval",))

        async def run(self, args, ctx):
            return "did the thing"
"""
