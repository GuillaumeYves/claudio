"""cld run — execute a task plan from claudio-task.json.

Always reads claudio-task.json from the current workspace.
Validates required fields and warns about missing ones.
Prompts for confirmation before executing.

Usage:
    cld run
    cld run @extra-context.py @config.yaml
"""

import json
import sys
from pathlib import Path

from claudio.pipeline.process import process
from claudio.executor import execute_prompt
from claudio.cache import cache_get, cache_put
from claudio.usage import log_request
from claudio.utils.tokens import estimate_tokens
from claudio.utils.args import (
    parse_command_args,
    resolve_file_attachments,
    format_file_context,
)
from claudio.utils.output import Output
from claudio.utils.tokens import format_token_info

TASK_FILE = "claudio-task.json"

REQUIRED_FIELDS = {"name", "tasks"}
REQUIRED_TASK_FIELDS = {"name", "prompt"}
OPTIONAL_TASK_FIELDS = {"context", "intent", "constraints", "output_format"}


def execute(raw_args: list[str], ctx: dict) -> int:
    out = Output(json_mode=ctx["json_output"], verbose=ctx["verbose"])

    # Parse @file attachments from args (run has no mode flags)
    parsed = parse_command_args(raw_args, {})

    for err in parsed.errors:
        out.warn(err)

    # Prompt text is ignored for run — warn if provided
    if parsed.prompt.strip():
        out.warn(f"cld run ignores inline text: \"{parsed.prompt}\". Use claudio-task.json for task definitions.")

    # Load claudio-task.json
    task_path = Path(TASK_FILE)
    if not task_path.exists():
        out.error(
            f"{TASK_FILE} not found in current directory.\n"
            f"Create one with: cld run --init\n"
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
        extra_context = format_file_context(parsed.files)

    # Show plan summary and confirm
    plan_name = plan.get("name", TASK_FILE)
    out.info(f"Plan: {plan_name} ({len(tasks)} tasks)")
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

    # Execute tasks sequentially
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

        # Cache check
        cached = None
        if not ctx.get("no_cache"):
            cached = cache_get(result.prompt)

        if cached is not None:
            out.info(f"  [cache hit]")
            response = cached
            log_request("run", task_intent, estimate_tokens(result.prompt), cached=True)
        else:
            response = execute_prompt(result.prompt, json_output=ctx["json_output"])
            if not ctx.get("no_cache"):
                cache_put(result.prompt, response, estimate_tokens(result.prompt))
            log_request("run", task_intent, estimate_tokens(result.prompt),
                        output_tokens=estimate_tokens(response), cached=False)

        results.append({"task": task_name, "response": response})

        if not ctx["json_output"]:
            print(f"\n--- {task_name} ---")
            print(response)
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
