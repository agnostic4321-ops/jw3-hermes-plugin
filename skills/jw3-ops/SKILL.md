---
name: jw3-ops
description: Trade crypto through JW3.ai safely from Hermes.
version: 1.0.0
author: agnostic4321-ops, Hermes Agent
license: MIT
platforms: [linux, macos]
---

# JW3 trading ops

Operate JW3.ai (non-custodial multi-venue trading) from Hermes through the
`jw3` tools this plugin registers. Thin wrappers over the `jw3` CLI — the CLI
owns the PAT and enforces every server-side gate.

## When to Use

Load this skill when the user asks to check markets, positions, balances,
orders, fills, PnL, risk state, JEV analysis, Diary entries, scans — or to
propose, execute, close, or manage exits on a trade. Do not hand-roll
`terminal` calls to `jw3` when a `jw3_*` tool covers the operation.

## Prerequisites

- `jw3` CLI on PATH (`~/.local/bin/jw3`, package `@jw3/cli`).
  Verify with `jw3 auth status` (prints no secrets).
- Authenticated via `jw3 auth login --browser` (default scope
  `mcp:read trade:propose`; fund-moving needs `trade:execute` + kill
  switch OFF + autonomy `full`/`ask_above_threshold`).
- Ground truth lives in `~/Desktop/jw3-mcp-cli/`: CLI source
  `cli/src/commands/`, MCP contract `edge-functions/mcp-server/tools.ts`
  (26 tools), shared gates `edge-functions/_shared/`.

## How to Run

Use the `jw3_*` tools (toolset `jw3`), or `/jw3-status` for a quick
positions + risk readout. Reads first, always: `jw3_risk` before anything
fund-moving, `jw3_orders` before every new entry.

## Quick Reference

| Goal | Tool |
|---|---|
| Touch price / movers | `jw3_market`, `jw3_screener` |
| Open positions / balances / orders | `jw3_positions`, `jw3_balances`, `jw3_orders` |
| Fills / closed-PnL ledger | `jw3_fills`, `jw3_pnl` |
| Candles | `jw3_klines` |
| JEV verdict (trend / prediction) | `jw3_analyze` |
| May-I-act state (risk + connections + heat) | `jw3_risk` |
| Fade/exhaustion screen | `jw3_scan` |
| Diary read | `jw3_diary` |
| Diary setup card / note (never executes) | `jw3_propose` |
| Open / close / exits / cancel (confirm-gated) | `jw3_execute`, `jw3_close`, `jw3_exits`, `jw3_cancel` |

## Safety

- Reads freely. Fund-moving tools (`jw3_execute`, `jw3_close`,
  `jw3_exits`, `jw3_cancel`) run the CLI dry run and send ZERO bytes
  unless `confirm=true` — pass it only with explicit per-trade user
  approval. A CLI confirm never overrides a server no.
- Never print or paste PATs; never commit them. Quote figures only from
  `jw3_pnl` (the venue ledger is the book of record; live reads carry
  uPnL and mid-session drift).
- Unknown-origin open position = stand down and report: another session,
  bot, or manual trade holds the margin pool. Never fire blind into a
  locked pool.
- `uncertain`/`mixed` JEV verdicts mean no trade, not a smaller trade.
  One confirm covers one scalp window (~15-min TTL).
- KuCoin futures tape names BTC as XBT (`XBT/USDT`); canonical pair keys
  use BTC. Amounts are BASE units (one contract = multiplier base units).
  Fee math runs on leveraged NOTIONAL at taker rates until a fill proves
  maker; small accounts go maker-only on entries and TPs.
- Short SL legs via exits can misfire — TP-only plus a manual kill watch
  on shorts until proven otherwise.

## Pitfalls

- The old `packages/jw3-cli` and `supabase/functions/mcp-server` paths in
  early notes are stale: real paths are `~/Desktop/jw3-mcp-cli/cli/src`
  and `~/Desktop/jw3-mcp-cli/edge-functions/mcp-server`.
- Combined SL+TP attach rejects on isolated margin — fall back to
  `jw3_exits` TP-only plus a kill watch, and re-check on `jw3_positions`.
- First `analyze` after a deploy can cold-start and time out — retry once
  warm before treating it as failure.
- Public Polygon RPCs 403 non-browser user agents; Polygon-specific
  onchain reads may fail while venue paths stay green.

## Verification

After acting, re-read the venue: `jw3_positions` + `jw3_orders` after any
fire, `jw3_pnl` before quoting any figure. A watchdog/monitor one-shot
exiting 0 is the healthy case, not a failure.
