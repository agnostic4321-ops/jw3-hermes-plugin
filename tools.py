"""JW3.ai trading tools — non-custodial wrappers over the ``jw3`` CLI.

Every tool shells out to the installed ``jw3`` CLI (``@jw3/cli``), which owns
the PAT (0600 credentials file) and enforces every server-side gate (scope,
YOLO/kill-switch, fee-aware stops, idempotency, caps). This plugin never
reads, prints, or transmits credentials, never signs anything, and never
synthesizes market data: tool results are the CLI's own ``--json`` output,
pass-through, with errors reported as-is.

Safety contract (mirrors the CLI):
- Reads are free; fund-moving commands (execute/close/exits/cancel) print a
  preview ticket and send ZERO bytes unless ``confirm=true`` (maps to the
  CLI's ``--yes`` flag).
- A CLI ``confirm=true`` never overrides a server no (kill switch,
  autonomy level, scopes, fee gates). Server refusals surface as errors.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

TOOLSET = "jw3"
_TIMEOUT_S = 180

_EXEC_DESC_SUFFIX = (
    " Fund-moving: without confirm=true this runs the CLI dry run only — "
    "it prints the full ticket and sends zero bytes. Pass confirm=true only "
    "with explicit per-trade user approval. The server re-checks scope, "
    "kill-switch/YOLO, and fee gates; a server no overrides confirm=true."
)


def _jw3_bin() -> str | None:
    found = shutil.which("jw3")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/jw3")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def check_jw3_available() -> bool:
    """Gate: tools appear only when the ``jw3`` CLI is installed."""
    return _jw3_bin() is not None


def run_jw3(argv: list[str], timeout: int = _TIMEOUT_S) -> str:
    """Run ``jw3 <argv> --json``; return a JSON string (result or error)."""
    exe = _jw3_bin()
    if not exe:
        return json.dumps({
            "error": "jw3 CLI not found. Install @jw3/cli (npm link in the "
                     "jw3-mcp-cli repo) and ensure `jw3` is on PATH.",
        })
    try:
        proc = subprocess.run(
            [exe, *argv, "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"jw3 {' '.join(argv)} timed out after {timeout}s. "
                     "First calls after a deploy can cold-start — retry once "
                     "warm before treating this as a failure.",
        })
    except OSError as exc:
        return json.dumps({"error": f"failed to launch jw3: {exc}"})
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or out or f"jw3 exited {proc.returncode}"
        return json.dumps({"error": err[:2000]})
    if not out:
        return json.dumps({"result": {}})
    try:
        return json.dumps({"result": json.loads(out)})
    except ValueError:
        return json.dumps({"result": out[:8000]})


def _opt(argv: list[str], flag: str, value) -> list[str]:
    if value is not None and value != "":
        argv += [flag, str(value)]
    return argv


def _schema(name: str, desc: str, props: dict, required: tuple = ()) -> dict:
    return {
        "name": name,
        "description": desc,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(required),
        },
    }


_PAIR = {
    "type": "string",
    "description": "Venue pair, e.g. SOL/USDT. KuCoin futures tape names BTC as XBT (XBT/USDT).",
}
_EXCHANGE = {
    "type": "string",
    "description": "Venue key, e.g. kucoin.",
    "default": "kucoin",
}
_CONFIRM = {
    "type": "boolean",
    "description": "Set true only with explicit per-trade user approval. False runs the CLI preview (zero bytes sent).",
    "default": False,
}


# ── read-only handlers ────────────────────────────────────────────────

def _h_market(args: dict, **kw) -> str:
    argv = _opt(_opt(["market"], "--exchange", args.get("exchange")), "--pair", args.get("pair"))
    return run_jw3(argv)


def _h_screener(args: dict, **kw) -> str:
    return run_jw3(_opt(["screener"], "--exchange", args.get("exchange")))


def _h_positions(args: dict, **kw) -> str:
    argv = _opt(_opt(["positions"], "--exchange", args.get("exchange")), "--pair", args.get("pair"))
    return run_jw3(argv)


def _h_balances(args: dict, **kw) -> str:
    return run_jw3(_opt(["balances"], "--exchange", args.get("exchange")))


def _h_orders(args: dict, **kw) -> str:
    argv = _opt(_opt(["orders"], "--exchange", args.get("exchange")), "--pair", args.get("pair"))
    return run_jw3(argv)


def _h_fills(args: dict, **kw) -> str:
    argv = ["fills"]
    for k, f in (("exchange", "--exchange"), ("pair", "--pair"), ("days", "--days"), ("limit", "--limit")):
        argv = _opt(argv, f, args.get(k))
    return run_jw3(argv)


def _h_pnl(args: dict, **kw) -> str:
    argv = ["pnl"]
    for k, f in (("exchange", "--exchange"), ("pair", "--pair"), ("days", "--days"), ("limit", "--limit")):
        argv = _opt(argv, f, args.get(k))
    return run_jw3(argv)


def _h_klines(args: dict, **kw) -> str:
    argv = _opt(_opt(["klines"], "--pair", args.get("pair")), "--tf", args.get("tf"))
    argv = _opt(argv, "--limit", args.get("limit"))
    return run_jw3(argv)


def _h_analyze(args: dict, **kw) -> str:
    kind = (args.get("kind") or "trend").lower()
    if kind == "prediction":
        slug = args.get("slug_or_pair") or ""
        if not slug:
            return json.dumps({"error": "kind=prediction requires slug_or_pair (market slug, conditionId, or id)."})
        return run_jw3(["analyze", "prediction", slug])
    argv = _opt(["analyze", "trend"], "--exchange", args.get("exchange"))
    argv = _opt(argv, "--pair", args.get("slug_or_pair"))
    return run_jw3(argv)


def _h_risk(args: dict, **kw) -> str:
    """Merge risk limits + connections + heat so one call answers 'may I act?'."""
    merged: dict = {}
    for argv in (["risk"], ["connections"], _opt(["heat"], "--exchange", args.get("exchange"))):
        try:
            payload = json.loads(run_jw3(argv))
        except ValueError:
            payload = {"error": "unparseable CLI output"}
        merged[" ".join(argv)] = payload
    return json.dumps(merged)


def _h_scan(args: dict, **kw) -> str:
    argv = _opt(["scan", "fade"], "--exchange", args.get("exchange"))
    argv = _opt(_opt(argv, "--min-chg", args.get("min_chg")), "--limit", args.get("limit"))
    return run_jw3(argv)


def _h_diary(args: dict, **kw) -> str:
    argv = ["diary"]
    for k, f in (("day", "--day"), ("start_day", "--start-day"), ("end_day", "--end-day")):
        argv = _opt(argv, f, args.get(k))
    return run_jw3(argv)


# ── propose (Diary cards — never executes) ────────────────────────────

def _h_propose(args: dict, **kw) -> str:
    kind = (args.get("kind") or "trade").lower()
    if kind == "note":
        body = args.get("note_body") or ""
        if not body:
            return json.dumps({"error": "kind=note requires note_body."})
        argv = _opt(["diary-note", "--body", body], "--day", args.get("day"))
        return run_jw3(_opt(argv, "--mode", args.get("note_mode")))
    argv = ["propose"]
    for k, f in (
        ("pair", "--pair"), ("side", "--side"), ("entry", "--entry"),
        ("stop", "--stop"), ("size_usd", "--size-usd"), ("thesis", "--thesis"),
        ("timeframe", "--timeframe"), ("rationale", "--rationale"),
    ):
        argv = _opt(argv, f, args.get(k))
    return run_jw3(argv)


# ── fund-moving (confirm-gated) ───────────────────────────────────────

def _maybe_yes(argv: list[str], args: dict) -> tuple[list[str], bool]:
    live = bool(args.get("confirm"))
    if live:
        argv = [*argv, "--yes"]
    return argv, live


def _h_execute(args: dict, **kw) -> str:
    for req in ("pair", "side"):
        if not args.get(req):
            return json.dumps({"error": f"execute requires {req}."})
    if not args.get("amount") and not args.get("risk_usd"):
        return json.dumps({"error": "execute requires amount (base units) or risk_usd."})
    argv = ["execute"]
    for k, f in (
        ("exchange", "--exchange"), ("pair", "--pair"), ("side", "--side"),
        ("amount", "--amount"), ("risk_usd", "--risk-usd"), ("price", "--price"),
        ("lev", "--lev"), ("margin_mode", "--margin-mode"),
        ("stop", "--stop"), ("tp", "--tp"),
    ):
        argv = _opt(argv, f, args.get(k))
    if args.get("market"):
        argv.append("--market")
    argv, live = _maybe_yes(argv, args)
    out = run_jw3(argv)
    if not live:
        try:
            payload = json.loads(out)
            payload["preview_only"] = True
            payload["note"] = "Dry run — zero bytes sent. Re-call with confirm=true (explicit per-trade approval) to fire."
            return json.dumps(payload)
        except ValueError:
            pass
    return out


def _h_close(args: dict, **kw) -> str:
    for req in ("pair", "side"):
        if not args.get(req):
            return json.dumps({"error": f"close requires {req}."})
    argv = _opt(_opt(_opt(["close"], "--exchange", args.get("exchange")), "--pair", args.get("pair")), "--side", args.get("side"))
    argv = _opt(argv, "--size", args.get("size"))
    argv, live = _maybe_yes(argv, args)
    return run_jw3(argv)


def _h_exits(args: dict, **kw) -> str:
    if not args.get("pair"):
        return json.dumps({"error": "exits requires pair."})
    argv = _opt(_opt(["exits"], "--exchange", args.get("exchange")), "--pair", args.get("pair"))
    for k, f in (("sl", "--sl"), ("tp", "--tp"), ("quantity", "--quantity")):
        argv = _opt(argv, f, args.get(k))
    if args.get("market"):
        argv.append("--market")
    argv, live = _maybe_yes(argv, args)
    return run_jw3(argv)


def _h_cancel(args: dict, **kw) -> str:
    if args.get("all_pairs"):
        return json.dumps({"error": "cancel-all is scoped per --pair only; book-wide cancels are refused by the CLI."})
    order_id = args.get("order_id")
    if order_id:
        argv = _opt(_opt(["cancel"], "--exchange", args.get("exchange")), "--order-id", order_id)
    elif args.get("pair") and args.get("all_for_pair"):
        argv = _opt(_opt(["cancel-all"], "--exchange", args.get("exchange")), "--pair", args.get("pair"))
    else:
        return json.dumps({"error": "cancel requires order_id, or pair + all_for_pair=true."})
    argv, live = _maybe_yes(argv, args)
    return run_jw3(argv)


TOOLS = (
    (_schema("jw3_market", "Spot/futures touch price for a pair via the JW3 read path.",
             {"exchange": _EXCHANGE, "pair": _PAIR}, ("pair",)), _h_market),
    (_schema("jw3_screener", "Top movers by volume on an exchange (read-only).",
             {"exchange": _EXCHANGE}, ()), _h_screener),
    (_schema("jw3_positions", "Open positions, optionally filtered by pair (read-only).",
             {"exchange": _EXCHANGE, "pair": dict(_PAIR, description="Optional pair filter.")}, ()), _h_positions),
    (_schema("jw3_balances", "Per-account balances; read the futures pocket before sizing (read-only).",
             {"exchange": _EXCHANGE}, ()), _h_balances),
    (_schema("jw3_orders", "Open/resting orders, optionally filtered by pair (read-only). Always check before a new entry — a stale TP/SL keeps working after its trade closes.",
             {"exchange": _EXCHANGE, "pair": dict(_PAIR, description="Optional pair filter.")}, ()), _h_orders),
    (_schema("jw3_fills", "Recent fills on an exchange/pair (read-only).",
             {"exchange": _EXCHANGE, "pair": _PAIR, "days": {"type": "integer"}, "limit": {"type": "integer"}}, ()), _h_fills),
    (_schema("jw3_pnl", "Closed PnL ledger — the book of record for quoted figures (read-only).",
             {"exchange": _EXCHANGE, "pair": _PAIR, "days": {"type": "integer"}, "limit": {"type": "integer"}}, ()), _h_pnl),
    (_schema("jw3_klines", "Candles for a pair/timeframe — the same tape scalp plans derive from (read-only).",
             {"pair": _PAIR, "tf": {"type": "string", "description": "5m, 15m, 1h, or 1d.", "default": "1h"},
              "limit": {"type": "integer", "default": 50}}, ("pair",)), _h_klines),
    (_schema("jw3_analyze", "JEV verdict: trend read on a CEX pair, or prediction-market read on a slug (read-only, fail-closed — uncertain/mixed means no trade, not a smaller trade).",
             {"kind": {"type": "string", "enum": ["trend", "prediction"], "default": "trend"},
              "slug_or_pair": {"type": "string", "description": "Pair for trend (SOL/USDT) or slug/conditionId for prediction."},
              "exchange": _EXCHANGE}, ("slug_or_pair",)), _h_analyze),
    (_schema("jw3_risk", "Risk limits + connections + heat in one call: kill-switch/autonomy state, where you may act, and free margin (read-only). Check before anything fund-moving.",
             {"exchange": _EXCHANGE}, ()), _h_risk),
    (_schema("jw3_scan", "Fade/exhaustion screen across the liquid universe (read-only signal — a trigger is a watch until two-model confluence confirms).",
             {"exchange": _EXCHANGE, "min_chg": {"type": "number"}, "limit": {"type": "integer"}}, ()), _h_scan),
    (_schema("jw3_diary", "Read Diary entries for a day or range (read-only).",
             {"day": {"type": "string"}, "start_day": {"type": "string"}, "end_day": {"type": "string"}}, ()), _h_diary),
    (_schema("jw3_propose", "Write a Diary setup card (trade) or note for in-app approve-and-sign. Proposes only — never executes, never touches keys.",
             {"kind": {"type": "string", "enum": ["trade", "note"], "default": "trade"},
              "pair": _PAIR, "side": {"type": "string", "enum": ["long", "short"]},
              "entry": {"type": "number"}, "stop": {"type": "number"},
              "size_usd": {"type": "number"}, "thesis": {"type": "string"},
              "timeframe": {"type": "string"}, "rationale": {"type": "string"},
              "note_body": {"type": "string"}, "day": {"type": "string"},
              "note_mode": {"type": "string", "enum": ["append", "replace"], "default": "append"}}, ()), _h_propose),
    (_schema("jw3_execute", "Open a position via execute_trade." + _EXEC_DESC_SUFFIX + " Size in base units (one KuCoin futures contract = multiplier base units — check before firing).",
             {"exchange": _EXCHANGE, "pair": _PAIR, "side": {"type": "string", "enum": ["buy", "sell"], "description": "buy opens a long (or closes a short); sell mirrors."},
              "amount": {"type": "number", "description": "Size in BASE units."},
              "risk_usd": {"type": "number"}, "price": {"type": "number"},
              "market": {"type": "boolean", "default": False},
              "lev": {"type": "integer"}, "margin_mode": {"type": "string"},
              "stop": {"type": "number"}, "tp": {"type": "number"},
              "confirm": _CONFIRM}, ("pair", "side")), _h_execute),
    (_schema("jw3_close", "Close a position via close_position." + _EXEC_DESC_SUFFIX,
             {"exchange": _EXCHANGE, "pair": _PAIR, "side": {"type": "string", "enum": ["long", "short"]},
              "size": {"type": "number", "description": "Explicit open qty — sizeless closes on sub-lot remainders fail venue-side."},
              "confirm": _CONFIRM}, ("pair", "side")), _h_close),
    (_schema("jw3_exits", "Arm venue-native TP/SL legs via set_exits (TP-only on shorts is the proven path; short SL legs can misfire)." + _EXEC_DESC_SUFFIX,
             {"exchange": _EXCHANGE, "pair": _PAIR, "sl": {"type": "number"}, "tp": {"type": "number"},
              "market": {"type": "boolean", "default": False}, "quantity": {"type": "number"},
              "confirm": _CONFIRM}, ("pair",)), _h_exits),
    (_schema("jw3_cancel", "Cancel one resting order (order_id) or all resting orders for ONE pair. Book-wide cancels are refused." + _EXEC_DESC_SUFFIX,
             {"exchange": _EXCHANGE, "order_id": {"type": "string"},
              "pair": _PAIR, "all_for_pair": {"type": "boolean", "default": False},
              "all_pairs": {"type": "boolean", "description": "Always refused.", "default": False},
              "confirm": _CONFIRM}, ()), _h_cancel),
)


def register_tools(ctx) -> None:
    """Register all JW3 tools in the ``jw3`` toolset via the plugin API."""
    for schema, handler in TOOLS:
        ctx.register_tool(
            name=schema["name"], toolset=TOOLSET, schema=schema,
            handler=handler, check_fn=check_jw3_available, emoji="📈",
        )
