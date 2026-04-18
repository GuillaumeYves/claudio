<p align="center">
  <img src="https://raw.githubusercontent.com/GuillaumeYves/claudio/main/assets/images/claudio.png" alt="claudio logo" width="60%"/>
</p>

<h1 align="center">Claudio - Claude Intelligence Optimizer</h1>

<p align="center">
  <a href="https://github.com/GuillaumeYves/claudio/releases"><img src="https://img.shields.io/github/v/release/GuillaumeYves/claudio" alt="Release"></a>
  <a href="https://pypi.org/project/claudio-cli/"><img src="https://img.shields.io/pypi/v/claudio-cli" alt="PyPI"></a>
  <img src="https://img.shields.io/pypi/pyversions/claudio-cli" alt="Python">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GuillaumeYves/claudio" alt="License"></a>
</p>

**A CLI that sits between you and Claude to reduce token waste, structure your inputs, and make every request cheaper and faster.**

Claudio is not a chatbot. It is not a new model. It is a deterministic preprocessing layer that compresses context, filters noise, and builds structured prompts before anything reaches Claude. The result: you pay less, get more relevant output, and iterate faster.

The premise is simple: **Claude doesn't need to change. Your inputs do.**

---

## Install

### Option 1: pip (recommended)

```bash
pip install claudio-cli
```

### Option 2: Source

```bash
git clone https://github.com/GuillaumeYves/claudio.git
cd claudio
pip install -e .
```

Both options install a single command: **`claudio`**.

- Run bare `claudio` to drop *inside* the tool — interactive session with tab-completion, history, and slash commands (same feel as `claude`).
- Run `claudio <subcommand> …` for one-shot use in scripts, pipes, or CI (`claudio build -r @file "…"`, `claudio ask -q "…"`).

> **Usage pattern — same as `claude`:** `cd` into your project first, *then* launch `claudio`. `@file` completion, `claudio-task.json`, and the `.claudio/cache/` folder all resolve against the current working directory. To switch projects mid-session without exiting, use `/cwd <path>`.

Requires **Python 3.10+**. Everything is bundled by default — no extras to enable.

### Post-install setup

After installing, run:

```bash
claudio setup
```

This will:
- **Detect if `claudio` is on your PATH** and offer to add it automatically (recommended for speed -- type `claudio` from any directory instead of `python -m claudio`)
- **Verify Claude CLI** is installed
- **Create config directory** at `~/.config/claudio/`
- **Install shell completions** (Bash, Zsh, or PowerShell) for tab-completing commands, modes, flags, and `@file` paths

If you decline any step, it shows you the exact command to do it manually.

Claudio calls the [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) under the hood. Install it separately if you haven't already. You can use `--dry-run` on any command to see the optimized prompt without sending it.

---

## Commands

Claudio has **3 core commands** + `stats` and `setup`. Each core command takes a **mode**, optional **file attachments**, and a **description**.

```
claudio <command> <mode> [@file [-lines]] ... [description]
```

**Argument order is strictly enforced**: mode first, then files, then description. This creates a logical parsing flow that lets Claude process the request with zero ambiguity.

---

### `claudio build`

Create or modify code.

| Mode | Short | Purpose |
|------|-------|---------|
| `-refactor` | `-r` | Refactor existing code (preserve behavior, improve structure) |
| `-generate` | `-g` | Generate new code from a description |

**Examples:**

```bash
# Refactor a specific function (lines 40-80)
claudio build -refactor @src/auth.py -40-80 "extract the validation logic into its own function"

# Refactor with multiple context files
claudio build -refactor @src/handler.py @src/models.py "consolidate duplicate error handling"

# Generate new code using existing files as reference
claudio build -generate @src/models/user.py "create a REST endpoint for user CRUD operations"

# Generate from scratch
claudio build -generate "python script that watches a directory for CSV changes and loads them into SQLite"
```

**Refactor output:** unified diff with one-line explanation per change.
**Generate output:** complete, runnable code block.

---

### `claudio ask`

Ask Claude a question.

| Mode | Short | Purpose |
|------|-------|---------|
| `-review` | `-rv` | Code review (security, quality, bugs) |
| `-question` | `-q` | General question (explain, how-to, architecture) |
| `-debug` | `-d` | Debug an issue (root cause, fix, explanation) |

**Examples:**

```bash
# Code review
claudio ask -review @src/auth.py "check for security issues"

# Review specific lines
claudio ask -review @src/api/handler.py -120-180 "is this input validation sufficient"

# Ask a question with file context
claudio ask -question @src/pipeline/process.py "how does the compression stage work"

# Ask without files
claudio ask -question "what is the difference between asyncio.gather and asyncio.wait"

# Debug with error context
claudio ask -debug @logs/error.log -500-520 "why is the connection pool exhausting"

# Debug specific code
claudio ask -debug @src/db.py -30-45 "this query returns duplicates when it shouldn't"
```

**Review output:** issues ranked by severity with fixes.
**Question output:** concise, direct answer.
**Debug output:** root cause, fix (as diff), brief explanation.

---

### `claudio run`

Execute a multi-step task plan from `claudio-task.json`.

```bash
# Execute the plan (prompts for confirmation)
claudio run

# Execute with additional file context
claudio run @src/config.py @docs/api-spec.md

# Preview all prompts without executing
claudio run --dry-run

# Run as a single agentic session with tool access (Read/Grep/Glob)
claudio run --agentic
```

`claudio run` always reads from `claudio-task.json` in the current directory. It validates the file, warns about missing fields, and asks for confirmation before executing.

**Execution modes:**

| Mode | How it runs | When to use |
|------|-------------|-------------|
| serial (default) | One `claude --print` per task | Tasks are independent; you want clean per-task output |
| `--agentic` | One session for the whole plan, with `Read`/`Grep`/`Glob` tools | Tasks share reasoning, or Claude should discover files itself |

Agentic mode saves tokens on multi-task plans (no per-task prompt re-ingest) and lets Claude carry insight from task 1 into task 2.

**Task file format:**

```json
{
  "name": "Audit authentication module",
  "tasks": [
    {
      "name": "Review auth middleware",
      "prompt": "Review this middleware for security vulnerabilities",
      "context": "This handles JWT validation for all API routes",
      "intent": "review",
      "constraints": ["Focus on token expiry handling", "Check for injection vectors"],
      "output_format": "Severity-ranked list with fix suggestions"
    },
    {
      "name": "Generate test cases",
      "prompt": "Generate unit tests for the auth middleware edge cases",
      "intent": "generate"
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes (plan + each task) | Human-readable identifier |
| `tasks` | Yes | Array of task objects |
| `prompt` | Yes (per task) | What Claude should do |
| `context` | No | Additional input/context |
| `intent` | No | `general`, `debug`, `refactor`, `generate`, `review` |
| `constraints` | No | Array of requirements for the output |
| `output_format` | No | Expected output structure |

A template is provided at `claudio-task.template.json`.

---

### `claudio setup`

Post-install configuration.

```bash
claudio setup
```

Checks PATH, offers automatic setup, installs shell completions, verifies Claude CLI.

---

## Interactive Mode (`claudio`)

Running `claudio` drops you **inside** the tool — same feel as `claude`. Type commands directly at the prompt. No prefix required for anything.

```bash
claudio
```

```
 ▄████▄   ██▓    ▄▄▄      █    ██ ▓█████▄  ██▓ ▒█████
...
  Claude Intelligence Optimizer  v1.2.0
  Type  /help  for commands   ·   @  to reference files   ·   Ctrl-D to exit

claudio> build -r @src/auth.py -40-80 "extract validation"
claudio> ask -q "what is dependency injection"
claudio> /model haiku
claudio> run --dry-run
```

Every command you'd run as `claudio build …` works as just `build …` inside the session. `@` triggers live tab-completion from the current directory (`.git`, `node_modules`, `__pycache__`, and other noise are filtered out).

**Slash commands:**

| Command | Purpose |
|---------|---------|
| `/help` | Show available commands |
| `/model NAME` | Pin a model for the session (`haiku`, `sonnet`, `opus`). `/model auto` resets. |
| `/cwd [PATH]` | Show or change the working directory |
| `/clear` | Clear the screen |
| `/stats` | Shortcut for `claudio stats` inside the REPL |
| `/exit` \| `/quit` | Exit (Ctrl-D also works) |

History is stored at `~/.claudio/repl_history` (or `$CLAUDIO_HOME/repl_history`). Errors in one command never kill the session — you stay inside until you exit.

The REPL requires a TTY. When stdin is piped or redirected, bare `claudio` exits with a hint pointing you at the one-shot form (`claudio <subcommand> …`).

---

## Shell Completions

`claudio setup` installs completions automatically. To install manually:

**Bash** (add to `~/.bashrc`):
```bash
eval "$(claudio --completions bash)"
```

**Zsh** (add to `~/.zshrc`):
```bash
eval "$(claudio --completions zsh)"
```

**PowerShell** (add to `$PROFILE`):
```powershell
claudio --completions powershell | Invoke-Expression
```

What you get:
```
claudio <TAB>              -> build  ask  run  stats  setup
claudio build -<TAB>       -> -refactor  -r  -generate  -g
claudio ask -<TAB>         -> -review  -rv  -question  -q  -debug  -d
claudio ask -d @src/<TAB>  -> @src/main.py  @src/auth.py  ...
```

---

## Response Cache

Claudio caches responses locally. Same prompt = instant result, zero tokens spent.

- **Location:** `.claudio/cache/` in your workspace (auto-gitignored)
- **Key:** SHA-256 hash of the final optimized prompt
- **TTL:** 1 hour (expired entries are auto-cleaned)
- **Scope:** Per-workspace, because the same `@file.py` in different projects contains different code

Cache hits show a `[cache hit]` indicator:
```
[claudio] [cache hit] Returning cached response
```

**Bypass cache** for a single request:
```bash
claudio ask -question --no-cache @src/main.py "explain this"
```

**Clear all cached responses:**
```bash
claudio stats --reset
```

The cache is deterministic: if the file hasn't changed and your prompt is the same, you get the same answer. If you edit the file and run again, the prompt changes (different file contents) so you get a fresh response automatically.

---

## Cost Tracking

Every request is logged with token estimates and cost. View your usage with:

```bash
claudio stats
```

```
Claudio Usage Stats

  Period        Requests   Tokens In       Cost  Cache Hits
  ------------ --------- ----------- ---------- -----------
  Today                8       3,200    $0.0340           2
  This week           23      12,500    $0.1520           7
  All time            91      48,000    $0.5800          19

  By Command:
  Command                 Requests   Tokens In       Cost
  ---------------------- --------- ----------- ----------
  ask -review                   12       8,000    $0.1200
  build -refactor               15       6,200    $0.0900
  ask -debug                     8       4,800    $0.0700
  ...

  Cache hit rate: 21% (19 of 91 requests)
```

**JSON output** for scripts/dashboards:
```bash
claudio stats --json
```

**Reset all data** (also clears cache):
```bash
claudio stats --reset
```

Usage data is stored at `~/.config/claudio/usage.json` (global, persists across workspaces).

---

## File Attachments

Attach up to **10 files** from your workspace using `@path`:

```bash
claudio build -refactor @src/main.py "simplify error handling"
```

Add a **line range** immediately after any `@file`:

```bash
@src/main.py -10-25          # lines 10 through 25
@src/main.py -42             # line 42 only
@logs/error.log -500-520     # lines 500 through 520
```

**Multiple files:**

```bash
claudio ask -review @src/auth.py -30-60 @src/middleware.py @tests/test_auth.py "is the auth flow correct"
```

Each file gets its own line range. Claude receives only the lines that matter -- not entire files.

### PowerShell note

PowerShell treats `@name` as the splatting operator and silently drops the token when `$name` isn't a defined variable — so `claudio build -r @src/main.py "fix"` becomes `claudio build -r "fix"` with no warning. Two safe alternatives on PowerShell:

```powershell
claudio build -r '@src/main.py' "fix"          # quote the @-token
claudio build -r -f src/main.py "fix"          # use -f / --file instead
claudio build -r --file src/main.py -10-25 "fix"
```

`-f` / `--file` is rewritten to `@<path>` internally, so line ranges and all other features work identically. Bash, zsh, cmd, and the `claudio` REPL are unaffected.

---

## Argument Order

Arguments **must** follow this order:

```
claudio <command> <mode> [@file [-lines]] ... [description]
     1          2          3                    4
```

| Position | What | Examples |
|----------|------|---------|
| 1 | Command | `build`, `ask`, `run` |
| 2 | Mode flag | `-refactor`, `-review`, `-debug` |
| 3 | Files + lines | `@file.py -10-25 @other.py` |
| 4 | Description | `"your prompt text here"` |

**Wrong order = error:**

```bash
claudio build "text" -refactor @file.py      # ERROR: mode after description
claudio build -refactor "text" @file.py      # ERROR: file after description
claudio build @file.py -refactor "text"      # ERROR: mode after file
```

**Why strict order?** It eliminates parsing ambiguity. When Claude receives the structured prompt, every field is in a predictable position. No tokens wasted on disambiguation.

---

## Global Flags

Global flags can go anywhere after the command:

```bash
claudio build -refactor --dry-run @file.py "simplify"
claudio ask -debug --verbose @log.txt "what happened"
claudio run --json
```

| Flag | Description |
|------|-------------|
| `--dry-run` | Print the optimized prompt without calling Claude |
| `--no-cache` | Bypass response cache for this request |
| `--verbose` | Show token count, compression ratio, model, and metadata |
| `--json` | Output results as structured JSON |
| `--model NAME` | Override model (`haiku` / `sonnet` / `opus` or full ID) |
| `--session-id UUID` | Start a session with a fixed ID (reusable via `--resume`) |
| `--resume UUID` | Resume an existing Claude session (warm prompt cache) |
| `--feedback` | Let Claude request missing context; auto-retry once with expanded range |
| `--agentic` | (`claudio run` only) Execute the plan in one agentic session with tool access |
| `-v`, `--version` | Print version |
| `-h`, `--help` | Show help |

---

## How It Works

Every input goes through a four-stage pipeline:

```
Input --> Filter --> Compress --> Prompt --> Claude
```

### 1. Filter (intent-aware)

Removes content that wastes tokens:

**Always:**
- Trailing whitespace from every line (~2-5% savings)
- License/copyright headers (legal boilerplate, not code)
- Shebang lines
- Consecutive blank lines collapsed to one
- Log deduplication (normalizes timestamps/UUIDs, shows repeat counts)
- Low-signal log lines (health checks, separators)

**When intent is refactor/review/debug (behavior-focused):**
- Strips comments (full-line and inline)
- Strips docstrings (triple-quote blocks)
- Result: Claude sees only the code logic, not the documentation about it

This is safe because refactor/review/debug tasks are about what the code *does*, not what the comments *say*. For question-mode, comments and docs are preserved since they may be what the user is asking about.

### 2. Compress

Reduces large inputs to structured summaries:

- **Code > 50 lines**: structural map (classes, functions with line numbers) + import summary. No raw code dump.
- **Code < 50 lines**: imports collapsed into a one-line summary, code preserved.
- **Logs > 150 lines**: errors + warnings (capped) + info count + last 15 lines for recency.

### 3. Prompt (XML-tagged, cache-aligned, zero duplication)

Builds a minimal prompt using XML tags instead of markdown, with the **stable sections first and the variable `<task>` last**:

```xml
<rules>
- Preserve behavior
- Output unified diff
- One-line reason per change
</rules>
<format>diff with explanation</format>
<context>
<file path="auth.py" lines="40-80">
def validate_token(token):
    ...
</file>
</context>
<task>Refactor: extract validation logic</task>
```

**Why this order?** Anthropic's prompt cache keys on the prefix. Rules, format, and file context rarely change between two back-to-back calls on the same file; only the `<task>` really varies. Putting the task last means a follow-up call can hit the 5-minute prompt cache instead of paying full ingest cost. Combine with `--session-id` / `--resume` for iterative workflows.

**Why XML over markdown?** Claude parses XML tags natively (it's the same format used for tool use). XML tags cost ~2 tokens each vs ~4-6 for `## Header` + newlines. Across a session of 50 requests, this saves ~200 tokens of pure formatting overhead.

**Zero duplication:** The user's description appears exactly once in `<task>`. File contents appear exactly once in `<context>`.

**No default padding:** Question-mode (`claudio ask -question`) produces just `<task>your question</task>` -- no constraints, no format instructions, no boilerplate. Claude doesn't need to be told "be concise" on a simple question.

### 4. Execute

Sends to Claude CLI via `claude --print`. In `--dry-run` mode, prints the prompt instead.

While the call is in flight, a stderr spinner (`| asking sonnet (2.3s)`) shows progress with an elapsed-time counter. It stays silent when stderr is not a TTY (piped / CI / scripts) so captured output is never polluted, and it degrades to ASCII on legacy Windows consoles that can't render Unicode.

Flags plumbed to the Claude CLI when set:

- `--model` → `claude --model` (auto-routed by intent + input size if unset; see below)
- `--session-id` / `--resume` → session continuity across calls (warm prompt cache)
- `--agentic` (claudio run) → adds `--allowedTools Read,Grep,Glob` for agentic execution

---

## Model Routing

When `--model` is not set, Claudio picks the cheapest model that fits the task:

| Intent | Input size | Model |
|--------|------------|-------|
| question / general | < 2k tokens | `haiku` |
| review / refactor / debug | > 8k tokens | `opus` |
| any | > 20k tokens | `opus` |
| everything else | — | `sonnet` |

Override with `--model haiku|sonnet|opus` (or a full model ID) on any command. `--verbose` prints the resolved model.

---

## Feedback Channel (`--feedback`)

Claudio's compressor trades raw code for a structural map above 50 lines. That's usually enough — but sometimes Claude genuinely needs specific lines back.

`--feedback` opens a two-way channel: the prompt tells Claude to respond with only

```
<need-context file="PATH" lines="START-END" reason="..."/>
```

when the compressed context is insufficient. Claudio parses that signal, expands the requested file range, and re-runs the prompt once with the richer context (no loops — max one retry per call).

```bash
claudio ask -review --feedback @src/auth.py "is the token flow correct?"
# -> Claude replies: <need-context file="auth.py" lines="120-180" reason="need validate_refresh body"/>
# -> Claudio re-runs with those lines included
```

Useful for large files where you don't know upfront which lines matter. No effect when no `@file` is attached.

---

## Sessions (`--session-id` / `--resume`)

For iterative work, reuse a Claude session so the prompt cache stays warm and context carries across calls:

```bash
ID=$(uuidgen)
claudio ask -question --session-id $ID @src/pipeline.py "walk me through this"
claudio ask -question --resume $ID "now explain the compression stage"
claudio ask -question --resume $ID "why XML over JSON in the prompt?"
```

`--resume` bypasses the local response cache (the point is to get a fresh Claude turn, not a stored echo).

---

## Token Savings

Real measurements from the Claudio codebase itself:

| Command | Input | After pipeline | Saved |
|---------|-------|---------------|-------|
| `claudio build -r @filter.py "simplify"` | 2,844 tokens | 128 tokens | **96%** |
| `claudio ask -rv @executor.py "security"` | 650 tokens | 80 tokens | **94%** |
| `claudio ask -q @process.py "how it works"` | 1,161 tokens | 92 tokens | **91%** |
| `claudio ask -d @files.py -28-45 "crash"` | 209 tokens | 170 tokens | **21%** |
| `claudio ask -q "prompt caching"` | 1 token | 9 tokens | n/a |

Small inputs (under 50 lines, no compression needed) see modest savings from comment/whitespace stripping. Large files see 90%+ savings from structural compression. Questions without files add near-zero overhead.

Use `--verbose` to see estimates on any command:

```
[claudio] ~128 tokens (est. $0.0079)
[claudio] Saved ~2,716 tokens via compression
```

---

## Configuration

Claudio looks for config at `~/.config/claudio/config.json`:

```json
{
  "claude_binary": "claude",
  "default_model": "sonnet",
  "max_input_tokens": 32000,
  "compression_threshold": 4000,
  "output_format": "text",
  "verbose": false
}
```

All fields are optional. Defaults are used for anything not specified.

---

## Piping and Composability

Results go to stdout, info/warnings to stderr:

```bash
# JSON output piped to jq
claudio --json ask -question @api.py "list the public functions" | jq '.result'

# Use in scripts
if claudio --dry-run --verbose build -refactor @src/ 2>&1 | grep -q "WARNING"; then
  echo "Input too large, consider narrowing scope"
fi
```

---

## Project Structure

```
claudio/
  cli.py                 Command router (subcommand dispatch)
  repl.py                Unified `claudio` entry point (REPL + subcommand dispatch)
  cache.py               Response cache (SHA-256 keyed, TTL-based)
  usage.py               Cost and usage tracking
  config.py              Configuration management
  executor.py            Claude CLI integration (with progress spinner)
  commands/
    build.py             claudio build (-refactor, -generate)
    ask.py               claudio ask (-review, -question, -debug)
    run.py               claudio run (claudio-task.json)
    run_prompt.py         Shared execution with cache + tracking
    stats.py             claudio stats (usage dashboard)
    setup.py             claudio setup (PATH, completions, verification)
  completions/
    bash.py              Bash completion generator
    zsh.py               Zsh completion generator
    powershell.py        PowerShell completion generator
  pipeline/
    filter.py            Intent-aware noise filtering
    compress.py          Structural compression
    prompt.py            XML-tagged prompt construction
    process.py           Pipeline orchestrator
  utils/
    args.py              @file parser with strict order enforcement
    tokens.py            Token estimation
    output.py            Output formatting
    files.py             File reading and ingestion
    spinner.py           TTY-only stderr progress spinner
tests/                   pytest suite (60 tests: args, repl, spinner, integration)
```

---

## Releases

Claudio uses tag-based releases. Each GitHub release auto-publishes to PyPI.

```bash
# Update to latest
pip install --upgrade claudio-cli

# Install a specific version
pip install claudio-cli==0.2.0
```

---

## License

[MIT](LICENSE)
