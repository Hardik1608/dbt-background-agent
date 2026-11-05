"""Tool definitions shown to the model (OpenAI function-calling JSON schema).

Descriptions are short and say *when* to use each tool; error messages from the toolbox
say what to do next. ``finish`` is handled by the harness itself (pre-completion
checklist), every other tool runs inside the sandbox.
"""

from __future__ import annotations

from typing import Any

MUTATING_TOOLS = {"write_file", "edit_file", "delete_file", "apply_patch"}

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": "List project files (build and package directories are hidden). Use "
        "it first to find models, YAML and tests.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the project root. Default: the root.",
                },
                "pattern": {"type": "string", "description": "Optional glob, e.g. '*.yml'."},
            },
        },
    },
    "read_file": {
        "description": "Read a text file. Each line is shown as <line number><TAB><text>; "
        "the number and the tab are not part of the file. Long files are paged: use "
        "start_line to continue.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "num_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["path"],
        },
    },
    "search_files": {
        "description": "Search project files for a text (like grep). Returns file:line: text. "
        "Use it to find where a model, column, macro or ref is used.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search. Default: all."},
                "glob": {"type": "string", "description": "File filter, e.g. '*.sql'."},
                "regex": {"type": "boolean"},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
    },
    "write_file": {
        "description": "Create a file or replace its whole content. Prefer edit_file for small "
        "changes to existing files.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    "edit_file": {
        "description": "Replace an exact snippet in a file. old_string must match the current "
        "text exactly (copy it from read_file without the line number and the tab) and be "
        "unique unless replace_all is true.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    "delete_file": {
        "description": "Delete a project file (for example when you move or rename a model: "
        "write the new file, then delete the old one).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "apply_patch": {
        "description": "Apply a unified diff (git format, paths a/... b/...) to the project.",
        "parameters": {
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
        },
    },
    "run_dbt": {
        "description": "Run dbt in the project. command: build (models+tests), run, test, "
        "compile, parse, ls, seed, snapshot or show. select uses dbt syntax, "
        "e.g. 'my_model', 'my_model+' (with children), '+my_model', "
        "'state:modified+' (everything you changed and its children).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "build",
                        "run",
                        "test",
                        "compile",
                        "parse",
                        "ls",
                        "seed",
                        "snapshot",
                        "show",
                    ],
                },
                "select": {"type": "string"},
                "exclude": {"type": "string"},
                "full_refresh": {
                    "type": "boolean",
                    "description": "Rebuild incremental models and seeds from scratch.",
                },
            },
            "required": ["command"],
        },
    },
    "query_duckdb": {
        "description": "Run ONE read-only SQL query (SELECT, DESCRIBE, SHOW, SUMMARIZE, "
        "EXPLAIN) on the project's DuckDB warehouse. Use schema-qualified "
        "names, e.g. select * from gold.fct_orders limit 5. 'show all tables' "
        "lists everything.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["sql"],
        },
    },
    "git_diff": {
        "description": "Show your changes so far compared with the starting commit.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "stat_only": {"type": "boolean"},
            },
        },
    },
    "finish": {
        "description": "Declare the task complete (or impossible). The harness then runs a "
        "checklist (your changes build and pass tests; no tests were removed). "
        "summary must explain the root cause or requirement, what you changed, "
        "and the test evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "tests_changed_reason": {
                    "type": "string",
                    "description": "Only if the "
                    "ticket explicitly required removing or weakening "
                    "a test: why.",
                },
            },
            "required": ["summary"],
        },
    },
}


def tool_schemas(enabled: list[str]) -> list[dict[str, Any]]:
    names = [n for n in enabled if n in TOOL_SPECS and n != "finish"] + ["finish"]
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": TOOL_SPECS[n]["description"],
                "parameters": TOOL_SPECS[n]["parameters"],
            },
        }
        for n in names
    ]
