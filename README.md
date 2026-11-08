# jw3 Hermes plugin

![JW3](assets/jw3-icon.svg)

JW3.ai trading inside Hermes Agent: 17 non-custodial tools, the `jw3-ops`
skill, and a `/jw3-status` slash command — all thin wrappers over the `jw3`
CLI (`@jw3/cli`), which owns the PAT and enforces every server-side gate
(scope, YOLO/kill-switch, fee-aware stops, idempotency, caps).

Third-party-product plugin: standalone repo, never merged into Hermes core.

## Layout

```
jw3-hermes-plugin/
├── plugin.yaml            # manifest (tools, requires_hermes >=0.22.5)
├── __init__.py            # register(ctx): tools + skill + /jw3-status
├── tools.py               # subprocess wrappers over `jw3 ... --json`
├── skills/jw3-ops/SKILL.md
└── README.md
```

No Python dependencies (stdlib only). No secrets anywhere — the plugin
never reads credentials; the CLI holds the PAT 0600.

## Install

```bash
# from a checkout of this repo:
git clone <this-repo-url> ~/.hermes/plugins/jw3
hermes plugins adopt jw3
hermes plugins enable jw3
hermes plugins doctor jw3
```

Restart the gateway/CLI session so discovery picks the tools up, then
`/jw3-status` should report positions + risk.

## Safety

Reads are free. `jw3_execute`, `jw3_close`, `jw3_exits`, `jw3_cancel` run
the CLI dry run (zero bytes) unless `confirm=true`, which maps to the
CLI's `--yes` and must only be passed with explicit per-trade user
approval. The server re-checks everything; a server no overrides any
confirm.

## Verify

```bash
hermes plugins validate /path/to/jw3-hermes-plugin
hermes plugins doctor jw3
hermes plugins list | grep jw3
```

Exercise without spending: `/jw3-status`, `jw3_risk`, `jw3_propose`
(kind=trade writes a Diary card only — in-app approve-and-sign, never
executes).

## Catalog path (left for later)

Public repo + `hermes plugins validate --install-deps` green, then a PR
adding `plugin-catalog/jw3.yaml` (40-char SHA pin) to
`NousResearch/hermes-agent` for human review.
