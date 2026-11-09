# jw3 Hermes plugin

[![skills.sh](https://skills.sh/b/agnostic4321-ops/jw3-hermes-plugin)](https://skills.sh/agnostic4321-ops/jw3-hermes-plugin)

![JW3](assets/jw3-icon.svg)

JW3.ai trading inside Hermes Agent: 17 non-custodial tools, the `jw3-ops`
skill, and a `/jw3-status` slash command — all thin wrappers over the `jw3`
CLI (`@jw3/cli`), which owns the PAT and enforces every server-side gate
(scope, YOLO/kill-switch, fee-aware stops, idempotency, caps).

Third-party-product plugin: standalone repo, never merged into Hermes core.

## Layout

```
jw3-hermes-plugin/
├── plugin.yaml            # manifest (tools, pre_tool_call hook, requires_hermes >=0.22.5)
├── __init__.py            # register(ctx): tools + approval hook + skill + /jw3-status
├── tools.py               # subprocess wrappers over `jw3 ... --json`
├── skills/jw3-ops/SKILL.md
├── tests/                 # stdlib unittest suite (stubbed CLI, no network)
├── assets/                # icons
├── .claude-plugin/        # Claude Code marketplace wrapper (skill only)
└── README.md
```

No Python dependencies (stdlib only). No secrets anywhere — the plugin
never reads credentials; the CLI holds the PAT 0600.

## Install

Prerequisite: the `jw3` CLI on `PATH`, authenticated with
`jw3 auth login --browser` — source and install steps at
https://github.com/agnostic4321-ops/jw3-mcp-cli.

```bash
git clone https://github.com/agnostic4321-ops/jw3-hermes-plugin ~/.hermes/plugins/jw3
hermes plugins adopt jw3
hermes plugins enable jw3
hermes plugins doctor jw3
```

Restart the gateway/CLI session so discovery picks the tools up, then
`/jw3-status` should report positions + risk.

## Safety

Reads are free. `jw3_execute`, `jw3_close`, `jw3_exits`, `jw3_cancel` run
the CLI dry run (zero bytes) unless `confirm=true`, which first raises a
Hermes human-approval prompt (see Approvals below) and maps to the CLI's
`--yes` only after a human approves. Pass it only with explicit
per-trade user approval. The server re-checks everything; a server no
overrides any confirm.

## Approvals

`jw3_execute`, `jw3_close`, `jw3_exits` and `jw3_cancel` are dry-run by
default. A strictly-true `confirm` (boolean `true` or the string `"true"`;
`"false"`, `1`, `0` and friends stay dry) triggers a `pre_tool_call`
hook that returns `{"action": "approve", "message": ..., "rule_key": ...}`.
The message is rendered from the exact argv that will run — action verb
(ORDER / CLOSE / EXITS / CANCEL), side, size, pair, venue, and the full
`jw3 ... --yes` command.

What Hermes does with it (`tools/approval.py`, `request_tool_approval`):

- Interactive CLI, desktop, or a messaging gateway: the human gets the
  prompt; a timeout denies (silence is never consent).
- No human present: `approvals.cron_mode`, `approvals.single_query_mode`
  (`hermes chat -q`) and `approvals.unattended_mode` decide — all default
  to `deny`; any other context without an approval bridge is blocked.
- YOLO (`--yolo`, `/yolo`) or `approvals.mode: off` auto-approve by the
  user's own choice; the JW3 server gates (kill switch, autonomy level,
  scopes, fee gates) still apply.
- `rule_key` is per exact ticket, so a "session" or "always" answer only
  re-approves that identical call. Hermes checks saved approvals before its
  unattended deny, so a tool-wide grain would let one "always" pre-approve
  every future trade, cron and `-q` included.

Fail-closed token: the hook records a one-shot approval token for the
exact argv (sha256, 900 s TTL, longer than the default 300 s
`approvals.timeout`); the handler consumes it before appending `--yes`.
Hermes treats hook exceptions as non-blocking, so if the hook did not run
for this exact call — hooks disabled, a hook error, args changed after
approval, or a call outside Hermes dispatch — the handler refuses and
nothing is sent.

Argument hygiene on the fund-moving tools: values with control/format
characters, over 64 characters, or starting with `-` are rejected
outright (the CLI goes live on any `--yes` token, so `margin_mode="--yes"`
must never reach argv, not even on a dry run). Numeric fields are sent in
canonical decimal form (`0.10` → `0.1`), so the hook, which sees the
model's raw args, and the handler, which sees them after Hermes' schema
coercion, bind to the same argv.

The CLI child runs with a minimal allowlist environment (`PATH`, `HOME`,
`XDG_CONFIG_HOME`, `TMPDIR`, `JW3_*`, plus Windows-only system vars) —
provider keys never reach it.

## Verify

```bash
hermes plugins validate /path/to/jw3-hermes-plugin --install-deps
hermes plugins doctor jw3
python3 -m unittest discover -s tests -v
```

Exercise without spending: `/jw3-status`, `jw3_risk`, `jw3_propose`
(kind=trade writes a Diary card only — in-app approve-and-sign, never
executes), and any fund-moving tool without `confirm` (dry run).
