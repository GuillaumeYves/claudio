"""claudio run — execute a task plan from claudio-task.json.

Always reads claudio-task.json from the current workspace.
Validates required fields and warns about missing ones.
Prompts for confirmation before executing.

Two execution modes:
    serial (default)  -- one `claude --print` call per task. Simple, isolated,
                         but each task restarts cold (no shared reasoning,
                         no cache reuse between tasks).
    agentic (--agentic) -- one `claude --print` call with Read/Grep/Glob tools.
                         Claude drives the whole plan in a single session, can
                         follow references between tasks, and can pull files
                         it decides it needs mid-flight.

Usage:
    claudio run
    claudio run @extra-context.py @config.yaml
    claudio run --agentic           # single agentic session with tool access
"""

import json
import sys
from pathlib import Path

from claudio import session_files
from claudio.pipeline.process import process
from claudio.commands.run_prompt import execute_with_tracking
from claudio.utils.tokens import estimate_tokens, format_token_info
from claudio.utils.args import (
    parse_command_args,
    resolve_file_attachments,
    format_file_context,
)
from claudio.utils.output import Output

TASK_FILE = "claudio-task.json"

REQUIRED_FIELDS = {"name", "tasks"}
REQUIRED_TASK_FIELDS = {"name", "prompt"}
OPTIONAL_TASK_FIELDS = {"context", "intent", "constraints", "output_format"}

# Tools offered to Claude in agentic mode. Read-only by default — users can
# widen this via config if they want Edit/Write, but we don't enable file
# mutation from a plan runner without an explicit opt-in.
AGENTIC_ALLOWED_TOOLS = ["Read", "Grep", "Glob"]


def execute(raw_args: list[str], ctx: dict) -> int:
    out = Output(json_mode=ctx["json_output"], verbose=ctx["verbose"])

    # Parse @file attachments from args (run has no mode flags)
    parsed = parse_command_args(raw_args, {})

    for err in parsed.errors:
        out.warn(err)

    # Prompt text is ignored for run — warn if provided
    if parsed.prompt.strip():
        out.warn(f"claudio run ignores inline text: \"{parsed.prompt}\". Use claudio-task.json for task definitions.")

    # Load claudio-task.json
    task_path = Path(TASK_FILE)
    if not task_path.exists():
        out.error(
            f"{TASK_FILE} not found in current directory.\n"
            f"Create one with: claudio run --init\n"
            f"Or create it manually — see the template at claudio-task.template.json"
        )
        return 1

    try:
        plan = json.loads(task_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        out.error(f"Failed to parse {TASK_FILE}: {e}")
        return 1

    # Validate plan structure
    warnings = _validate_plan(plan)
    if warnings:
        out.warn(f"Issues in {TASK_FILE}:")
        for w in warnings:
            out.warn(f"  - {w}")

        # Prompt to continue
        if not ctx["json_output"] and sys.stdin.isatty():
            try:
                answer = input("\nContinue anyway? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    out.info("Aborted.")
                    return 0
            except (EOFError, KeyboardInterrupt):
                print()
                return 130

    tasks = plan.get("tasks", [])
    if not tasks:
        out.error(f"{TASK_FILE} contains no tasks")
        return 1

    # Resolve additional @file attachments
    extra_context = ""
    if parsed.files:
        _, file_errors = resolve_file_attachments(parsed.files)
        for err in file_errors:
            out.error(err)
            return 1
        # In a serial plan with auto-chain, the same @file appears in every
        # task's context. Mark unchanged ones so format_file_context emits
        # the compact marker from task 2 onwards.
        session_id = ctx.get("session_id") or ctx.get("resume")
        if session_id:
            unchanged = session_files.mark_files_seen(session_id, parsed.files)
            for fa in parsed.files:
                if (fa.path, fa.lines) in unchanged:
                    fa.unchanged = True
        extra_context = format_file_context(parsed.files)

    # Show plan summary and confirm
    plan_name = plan.get("name", TASK_FILE)
    mode_label = "agentic" if ctx.get("agentic") else "serial"
    out.info(f"Plan: {plan_name} ({len(tasks)} tasks, {mode_label})")
    for i, task in enumerate(tasks, 1):
        out.info(f"  [{i}] {task.get('name', f'Task {i}')}")

    if not ctx["dry_run"] and not ctx["json_output"] and sys.stdin.isatty():
        try:
            answer = input("\nExecute this plan? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                out.info("Aborted.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print()
            return 130

    if ctx.get("agentic"):
        return _run_agentic(plan, tasks, extra_context, ctx, out)
    return _run_serial(tasks, extra_context, ctx, out)


def _run_serial(tasks: list[dict], extra_context: str, ctx: dict, out: Output) -> int:
    """Original per-task execution: one `claude --print` per task."""
    results = []
    for i, task in enumerate(tasks, 1):
        task_name = task.get("name", f"Task {i}")
        task_prompt = task.get("prompt", "")
        task_context = task.get("context", "")
        task_intent = task.get("intent", "general")
        task_constraints = task.get("constraints")
        task_format = task.get("output_format")

        # Merge extra @file context
        if extra_context:
            task_context = f"{task_context}\n\n{extra_context}" if task_context else extra_context

        out.info(f"[{i}/{len(tasks)}] {task_name}")

        result = process(
            raw_input=task_context,
            task=task_prompt,
            intent=task_intent,
            constraints=task_constraints,
            output_format=task_format,
        )

        if ctx["verbose"]:
            out.info(format_token_info(result.compressed_tokens))

        if ctx["dry_run"]:
            results.append({"task": task_name, "prompt": result.prompt})
            continue

        # Delegate to the shared executor so run shares cache + model routing
        # with ask/build. We suppress inline result printing here and format
        # the plan output ourselves below.
        response = execute_with_tracking(
            prompt=result.prompt,
            ctx={**ctx, "json_output": ctx["json_output"]},
            out=_SilentOutput(out),
            cmd="run",
            mode=task_intent,
            intent=task_intent,
            metadata=result.metadata,
        )

        results.append({"task": task_name, "response": response or ""})

        if not ctx["json_output"]:
            print(f"\n--- {task_name} ---")
            print(response or "")
            print()

    if ctx["dry_run"]:
        out.result(
            json.dumps(results, indent=2)
            if ctx["json_output"]
            else _format_dry_run(results)
        )
    elif ctx["json_output"]:
        out.result(json.dumps(results, indent=2))

    return 0


def _run_agentic(plan: dict, tasks: list[dict], extra_context: str, ctx: dict, out: Output) -> int:
    """Single agentic session: build one prompt, let Claude use tools across all tasks.

    Why: serial mode pays a full prompt-ingest cost per task and forbids
    Claude from carrying insight from task 1 into task 2. Agentic mode sends
    the whole plan once with Read/Grep/Glob access, so shared reasoning,
    file discovery, and cross-task consistency all become possible.
    """
    plan_name = plan.get("name", "Plan")
    prompt = _build_agentic_prompt(plan_name, tasks, extra_context)

    if ctx["dry_run"]:
        out.result(prompt)
        return 0

    if ctx["verbose"]:
        tokens = estimate_tokens(prompt)
        out.info(format_token_info(tokens))
        out.info(f"[claudio] agentic tools: {', '.join(AGENTIC_ALLOWED_TOOLS)}")

    # Pick the heaviest intent in the plan for model routing — if any task
    # is a review/refactor, the whole session deserves the stronger model.
    intent = _plan_intent(tasks)

    execute_with_tracking(
        prompt=prompt,
        ctx=ctx,
        out=out,
        cmd="run",
        mode="agentic",
        intent=intent,
        allowed_tools=AGENTIC_ALLOWED_TOOLS,
    )

    return 0


def _build_agentic_prompt(plan_name: str, tasks: list[dict], extra_context: str) -> str:
    """Assemble the single prompt sent in agentic mode.

    Structure mirrors prompt.py's cache-aligned order: rules first, variable
    task list last.
    """
    parts = [
        "<rules>",
        "- Execute each <task> in order",
        "- Use Read/Grep/Glob to pull in any files referenced in task contexts",
        "- Share findings across tasks when relevant (don't re-derive)",
        "- Output each task's result under a `### <task name>` heading",
        "</rules>",
    ]

    if extra_context:
        parts.append(f"<context>\n{extra_context}\n</context>")

    task_blocks = [f'<plan name="{_xml_escape(plan_name)}">']
    for i, t in enumerate(tasks, 1):
        name = _xml_escape(t.get("name", f"Task {i}"))
        intent = t.get("intent", "general")
        task_blocks.append(f'  <task name="{name}" intent="{intent}">')
        task_blocks.append(f'    <prompt>{_xml_escape(t.get("prompt", ""))}</prompt>')
        if t.get("context"):
            task_blocks.append(f'    <hint>{_xml_escape(t["context"])}</hint>')
        if t.get("constraints"):
            rules = "\n".join(f"- {_xml_escape(c)}" for c in t["constraints"])
            task_blocks.append(f'    <constraints>\n{rules}\n    </constraints>')
        if t.get("output_format"):
            task_blocks.append(f'    <format>{_xml_escape(t["output_format"])}</format>')
        task_blocks.append("  </task>")
    task_blocks.append("</plan>")

    parts.append("\n".join(task_blocks))
    return "\n".join(parts)


def _plan_intent(tasks: list[dict]) -> str:
    """Pick the most demanding intent across tasks — drives model routing."""
    priority = ["refactor", "review", "debug", "generate", "general"]
    intents = {t.get("intent", "general") for t in tasks}
    for p in priority:
        if p in intents:
            return p
    return "general"


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


class _SilentOutput:
    """Wraps an Output so execute_with_tracking doesn't double-print results.

    run.py formats each task's response itself for the dashed section headers,
    so we need the shared executor to stay quiet on `out.result`.
    """

    def __init__(self, inner):
        self._inner = inner

    def info(self, *a, **kw):
        self._inner.info(*a, **kw)

    def warn(self, *a, **kw):
        self._inner.warn(*a, **kw)

    def error(self, *a, **kw):
        self._inner.error(*a, **kw)

    def result(self, *a, **kw):
        pass


def _validate_plan(plan: dict) -> list[str]:
    """Validate plan structure and return warnings for issues."""
    warnings = []

    if not isinstance(plan, dict):
        warnings.append("Plan must be a JSON object")
        return warnings

    # Check top-level fields
    missing_top = REQUIRED_FIELDS - set(plan.keys())
    for field in missing_top:
        warnings.append(f"Missing required field: '{field}'")

    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        warnings.append("'tasks' must be an array")
        return warnings

    if not tasks:
        warnings.append("'tasks' array is empty")
        return warnings

    # Check each task
    for i, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            warnings.append(f"Task {i}: must be an object")
            continue

        missing = REQUIRED_TASK_FIELDS - set(task.keys())
        for field in missing:
            warnings.append(f"Task {i}: missing required field '{field}'")

    return warnings


def _format_dry_run(results: list[dict]) -> str:
    parts = []
    for r in results:
        parts.append(f"=== {r['task']} ===")
        parts.append(r["prompt"])
        parts.append("")
    return "\n".join(parts)
