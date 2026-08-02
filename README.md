# alir

[日本語版 README](README_ja.md)

An asynchronous decision service for autonomous Claude Code loops.
The name is short for **A**gent **L**oop for **I**ssue **R**esolution.

When Claude Code needs a human decision during autonomous work, alir lets it register a question and move on without blocking the loop.
Questions are stored in an [iceql](https://github.com/denzow/iceql) database (CSV + YAML), and humans answer them asynchronously via CLI or Web UI.

See [plan.md](plan.md) for the overall design (Japanese).

## Usage

```console
# Register issues to work on (the title is fetched via gh)
$ alir issues add https://github.com/owner/repo/issues/12 --workdir ~/work/repo
# Optionally pick the work mode (auto / implement / refine) and add a note for the prompt
$ alir issues add https://github.com/owner/repo/issues/13 --workdir ~/work/repo \
    --mode refine --note "limit the change to the API layer"
$ alir issues                       # list
$ alir issues import --label agent-ready --workdir ~/work/repo   # bulk import by label (helper)
$ alir issues targets add --label agent-ready --workdir ~/work/repo  # auto-import issues with this label

# Direct-push mode: skip PRs for this repository and keep pushing to develop
$ alir push-branch set --workdir ~/work/repo --branch develop

# Toggle claude -p --resume when resuming answered parked issues (enabled by default)
$ alir resume disable

# Switch the model used by sessions (falls back to claude's default when unset)
$ alir model set sonnet

# Configure Pushover notifications (new questions, retry limit reached) and send a test
$ alir pushover set --token <api-token> --user <user-key>
$ alir pushover test

# Start everything in one process: loop driver + Web UI + MCP (HTTP)
$ alir serve --port 8710 --session-budget 4000000 --weekly-budget 20000000

# List unanswered questions
$ alir questions

# Answer a question (option number or free text)
$ alir answer 1 2 "Go with B, but write the tests first"
```

Repositories registered with `alir push-branch set` run in direct-push mode.
Sessions skip PRs and work in a shared worktree that checks out the push branch, pushing to it directly.
Only one session runs at a time per such repository.
Pick a branch that is not checked out in the workdir (git refuses to create a worktree for a checked-out branch).

`alir serve` is the recommended way to run alir.
Claude sessions started by the driver connect to the MCP endpoint at `http://127.0.0.1:PORT/mcp` instead of spawning a subprocess per session.
Each piece is also available standalone: `alir run` (driver only), `alir web` (Web UI only), and `alir mcp` (stdio MCP server for manual Claude Code sessions).

The data directory is specified with the `ALIR_DATA_DIR` environment variable.
If unset, `$XDG_DATA_HOME/alir` (default `~/.local/share/alir`) is used.

## Security note

The Web UI has no authentication. Keep it inside your LAN and never expose it to the internet:
questions can contain internal details of the repositories being worked on.
