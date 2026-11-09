"""JW3.ai trading tools — non-custodial wrappers over the ``jw3`` CLI.

Every tool shells out to the installed ``jw3`` CLI (``@jw3/cli``), which owns
the PAT (0600 credentials file) and enforces every server-side gate (scope,
YOLO/kill-switch, fee-aware stops, idempotency, caps). This plugin never
reads, prints, or transmits credentials, never signs anything, and never
synthesizes market data: tool results are the CLI's own ``--json`` output,
pass-through, with errors reported as-is.

Safety contract (mirrors the CLI):
- Reads are free; fund-moving commands (execute/close/exits/cancel) print a
  preview ticket and send ZERO bytes unless ``confirm=true``.
- ``confirm=true`` raises a Hermes human-approval prompt showing the exact
  ticket; the live CLI call (``--yes``) runs only after a human approves.
  With no human present (cron, ``-q``, other unattended runs) Hermes applies
  ``approvals.cron_mode`` / ``single_query_mode`` / ``unattended_mode``
  (default ``deny``); YOLO or ``approvals.mode: off`` auto-approves by the
  user's own choice. Silence is never consent (timeouts deny).
- A CLI ``confirm=true`` never overrides a server no (kill switch,
  autonomy level, scopes, fee gates). Server refusals surface as errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import unicodedata
from decimal import Decimal, InvalidOperation

TOOLSET = "jw3"
_TIMEOUT_S = 180

_EXEC_DESC_SUFFIX = (
    " Fund-moving: without confirm=true this runs the CLI dry run only — "
    "it prints the full ticket and sends zero bytes. With confirm=true Hermes "
    "raises a human-approval prompt showing the exact ticket (unattended runs "
    "are refused per the user approval mode) and the live call runs only after "
    "a human approves. The server re-checks scope, kill-switch/YOLO, and fee "
    "gates; a server no overrides confirm=true."
)

#: Tools whose live path moves real funds. Only these can trigger --yes,
#: and only via the approval hook + one-shot token below.
FUND_MOVING = ("jw3_execute", "jw3_close", "jw3_exits", "jw3_cancel")

#: Max length of any single string field sent to the CLI (venue pairs, order
#: ids and sides are all far shorter; anything longer is a paste error).
_MAX_FIELD_LEN = 64

#: Lifetime of a hook-recorded approval token. Covers the default Hermes
#: approval wait (approvals.timeout, default 300s) with headroom; if a custom
#: timeout outlasts it the handler fails closed (refuses) rather than firing.
_APPROVAL_TTL_S = 900


def _true(value) -> bool:
    """Strict truth: only bool True, or the string 'true' (any case/space)."""
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


#: Fund-moving fields the CLI parses as numbers. Their argv text is the
#: canonical decimal (0.10 -> 0.1, 5.0 -> 5, 1e-5 -> 0.00001) so the hook,
#: which sees the model's raw args, and the handler, which sees them after
#: Hermes' schema coercion, build the identical argv the token is bound to.
_NUMERIC = frozenset({"amount", "risk_usd", "price", "lev", "stop", "tp", "size", "sl", "quantity"})


def _arg_text(value, field: str):
    """Validated argv text for one fund-moving field.

    Returns ``(text, None)``, ``(None, None)`` when absent, or ``(None, error)``.
    Rejects (never strips) control/format characters, over-long values and a
    leading ``-``: the CLI switches to live mode on any ``--yes`` token, so a
    value such as ``margin_mode="--yes"`` must never reach argv.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None, f"{field} must be a string or number — refused, nothing was sent."
    text = value if isinstance(value, str) else str(value)
    if len(text) > _MAX_FIELD_LEN:
        return None, f"{field} over {_MAX_FIELD_LEN} characters — refused, nothing was sent."
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf"):
            return None, f"{field} contains a control/format character (U+{ord(ch):04X}) — refused, nothing was sent."
    if field in _NUMERIC:
        try:
            num = Decimal(text)
        except InvalidOperation:
            return None, f"{field} must be a number — refused, nothing was sent."
        if not num.is_finite() or abs(num.adjusted()) > 18:
            return None, f"{field} is not a usable number — refused, nothing was sent."
        text = format(num.normalize(), "f")
    if text.startswith("-"):
        return None, f"{field} may not start with '-' — refused, nothing was sent."
    return text, None


def _opt_checked(argv: list[str], flag: str, value, field: str):
    """Append ``flag text`` for a validated fund-moving field; else an error."""
    text, err = _arg_text(value, field)
    if err:
        return argv, err
    return (argv if text is None else [*argv, flag, text]), None


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


def _child_env() -> dict[str, str]:
    """Minimal allowlist environment for the CLI child process.

    Passes only what the CLI needs: PATH/HOME/XDG_CONFIG_HOME/TMPDIR for
    process launch and config-dir resolution, plus ``JW3_*`` (credential dir,
    PAT fallback, endpoint override, fee-veto inputs). Provider keys
    (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, …) never reach the child.
    """
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    # JW3_CONFIG_HOME (credential dir), JW3_MCP_PAT (PAT fallback),
    # JW3_SUPABASE_URL (endpoint override), JW3_ACCOUNT_USD / JW3_FEE_TIER_OK
    # (fee-veto inputs), and any future JW3_-namespaced CLI setting.
    for key, val in os.environ.items():
        if key.startswith("JW3_"):
            env[key] = val
    if os.name == "nt":  # process creation / node needs these on Windows only
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA",
                    "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"):
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
    return env


def run_jw3(argv: list[str], timeout: int = _TIMEOUT_S) -> str:
    """Run ``jw3 <argv> --json``; return a JSON string (result or error)."""
    exe = _jw3_bin()
    if not exe:
        return json.dumps({
            "error": "jw3 CLI not found. Install from "
                     "https://github.com/agnostic4321-ops/jw3-mcp-cli (@jw3/cli) "
                     "and ensure `jw3` is on PATH.",
        })
    try:
        proc = subprocess.run(
            [exe, *argv, "--json"],
            capture_output=True, text=True, timeout=timeout, env=_child_env(),
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
    "description": "Set true only with explicit per-trade user approval. True raises a Hermes human-approval prompt showing the exact ticket — approve to fire, deny to stop; unattended runs are refused per the user approval mode. False (default) runs the CLI preview (zero bytes sent).",
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


# ── fund-moving argv builders (pure: used by the hook AND the handler) ──
# Each returns the exact CLI argv (no --yes, no --json), or an error string.

def _build_execute(args: dict):
    for req in ("pair", "side"):
        if not args.get(req):
            return f"execute requires {req}."
    if not args.get("amount") and not args.get("risk_usd"):
        return "execute requires amount (base units) or risk_usd."
    side = args.get("side")
    side_text = side if isinstance(side, str) else str(side)
    if side_text.lower() not in ("buy", "sell"):
        return "execute side must be buy or sell."
    argv = ["execute"]
    fields = (
        ("exchange", "--exchange"), ("pair", "--pair"), ("side", "--side"),
        ("amount", "--amount"), ("risk_usd", "--risk-usd"), ("price", "--price"),
        ("lev", "--lev"), ("margin_mode", "--margin-mode"),
        ("stop", "--stop"), ("tp", "--tp"),
    )
    for key, flag in fields:
        val = side_text.lower() if key == "side" else args.get(key)
        argv, err = _opt_checked(argv, flag, val, key)
        if err:
            return err
    if _true(args.get("market")):
        argv.append("--market")
    return argv


def _build_close(args: dict):
    for req in ("pair", "side"):
        if not args.get(req):
            return f"close requires {req}."
    side = args.get("side")
    side_text = side if isinstance(side, str) else str(side)
    if side_text.lower() not in ("long", "short"):
        return "close side must be long or short."
    argv = ["close"]
    for key, flag in (("exchange", "--exchange"), ("pair", "--pair"), ("side", "--side")):
        val = side_text.lower() if key == "side" else args.get(key)
        argv, err = _opt_checked(argv, flag, val, key)
        if err:
            return err
    argv, err = _opt_checked(argv, "--size", args.get("size"), "size")
    if err:
        return err
    return argv


def _build_exits(args: dict):
    if not args.get("pair"):
        return "exits requires pair."
    argv = ["exits"]
    for key, flag in (("exchange", "--exchange"), ("pair", "--pair")):
        argv, err = _opt_checked(argv, flag, args.get(key), key)
        if err:
            return err
    for key, flag in (("sl", "--sl"), ("tp", "--tp"), ("quantity", "--quantity")):
        argv, err = _opt_checked(argv, flag, args.get(key), key)
        if err:
            return err
    if _true(args.get("market")):
        argv.append("--market")
    return argv


def _build_cancel(args: dict):
    if _true(args.get("all_pairs")):
        return "cancel-all is scoped per --pair only; book-wide cancels are refused by the CLI."
    order_id = args.get("order_id")
    if order_id:
        argv = ["cancel"]
        for key, flag in (("exchange", "--exchange"), ("order_id", "--order-id")):
            argv, err = _opt_checked(argv, flag, args.get(key), key)
            if err:
                return err
        return argv
    if args.get("pair") and _true(args.get("all_for_pair")):
        argv = ["cancel-all"]
        for key, flag in (("exchange", "--exchange"), ("pair", "--pair")):
            argv, err = _opt_checked(argv, flag, args.get(key), key)
            if err:
                return err
        return argv
    return "cancel requires order_id, or pair + all_for_pair=true."


_BUILDERS = {
    "jw3_execute": _build_execute,
    "jw3_close": _build_close,
    "jw3_exits": _build_exits,
    "jw3_cancel": _build_cancel,
}


# ── approval tokens (one-shot, exact-argv, fail-closed) ───────────────
# The pre_tool_call hook records one token per live call for the exact argv;
# the handler consumes it before appending --yes. A missing/expired token
# means the gate did not run for this exact call → refuse, never execute.

_approvals: dict[str, list[float]] = {}
_approvals_lock = threading.Lock()


def _token_key(argv: list[str]) -> str:
    return hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest()


def _record_approval(argv: list[str]) -> None:
    key = _token_key(argv)
    with _approvals_lock:
        _approvals.setdefault(key, []).append(time.monotonic() + _APPROVAL_TTL_S)
        if len(_approvals) > 512:  # opportunistic sweep against unbounded growth
            now = time.monotonic()
            for k in [k for k, v in _approvals.items() if not [e for e in v if e > now]]:
                del _approvals[k]


def _consume_approval(argv: list[str]) -> bool:
    now = time.monotonic()
    with _approvals_lock:
        live = [e for e in _approvals.get(_token_key(argv), []) if e > now]
        if not live:
            _approvals.pop(_token_key(argv), None)
            return False
        live.pop()  # one-shot: each recorded approval allows exactly one run
        if live:
            _approvals[_token_key(argv)] = live
        else:
            _approvals.pop(_token_key(argv), None)
        return True


def _clear_approvals() -> None:
    """Test helper: drop all recorded approval tokens."""
    with _approvals_lock:
        _approvals.clear()


_REFUSAL = (
    "Live JW3 action refused - the jw3 approval gate did not run for this exact call "
    "(plugin hooks disabled, args changed after approval, or the tool was invoked outside "
    "Hermes dispatch). Nothing was sent."
)


# ── ticket summaries (what the human approves) ───────────────────────

def _ticket(tool_name: str, argv: list[str]) -> str:
    """Human summary of the exact argv the approval binds to.

    Rendered from the validated argv, never from raw args, so the summary and
    the command line always describe the call that will run. Values cannot
    start with ``-`` (see ``_arg_text``), so flag/value pairs parse
    unambiguously.
    """
    flags: dict[str, str] = {}
    i = 1
    while i < len(argv):
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            flags[argv[i]] = argv[i + 1]
            i += 2
        else:
            flags[argv[i]] = ""
            i += 1
    venue = flags.get("--exchange") or "the CLI default venue"
    pair = flags.get("--pair", "")

    def extras(*spec: tuple[str, str]) -> list[str]:
        return [label.format(flags[flag]) for flag, label in spec if flag in flags]

    if tool_name == "jw3_execute":
        size = flags.get("--amount") or f"risk ${flags.get('--risk-usd')}"
        price = "market" if "--market" in flags else (
            f"limit {flags['--price']}" if "--price" in flags else "no price set (CLI default)")
        bits = [f"open {flags.get('--side', '').upper()} {size} {pair} on {venue}", price]
        bits += extras(("--lev", "{}x"), ("--margin-mode", "margin {}"), ("--stop", "SL {}"), ("--tp", "TP {}"))
        verb, what = "ORDER (real funds)", ", ".join(bits)
    elif tool_name == "jw3_close":
        bits = [f"close {flags.get('--side', '').upper()} {pair} on {venue}"] + extras(("--size", "size {}"))
        verb, what = "CLOSE (real funds)", ", ".join(bits)
    elif tool_name == "jw3_exits":
        bits = [f"arm exits on {pair} on {venue}"]
        bits += extras(("--sl", "SL {}"), ("--tp", "TP {}"), ("--quantity", "qty {}"), ("--market", "market"))
        verb, what = "EXITS (real funds)", ", ".join(bits)
    elif "--order-id" in flags:
        verb, what = "CANCEL (live orders)", f"cancel order {flags['--order-id']} on {venue}"
    else:
        verb, what = "CANCEL (live orders)", f"cancel all resting orders for {pair} on {venue}"
    cmd = "jw3 " + shlex.join([*argv, "--yes"])
    return (f"JW3 LIVE {verb}: {what} | command: {cmd} | "
            "the JW3 server still re-checks scope, kill switch and fee gates.")


# ── pre_tool_call approval hook ──────────────────────────────────────

def pre_tool_call_hook(tool_name: str, args: dict | None = None, **kwargs) -> dict | None:
    """Escalate live fund-moving calls to the Hermes human-approval gate.

    Returns ``None`` (no opinion) unless this is a fund-moving tool with a
    strictly-true ``confirm``. Otherwise records a one-shot approval token
    for the exact argv and returns ``{"action": "approve", ...}`` so Hermes
    prompts the human with the ticket and refuses unattended runs per the
    user approval mode. Builder errors return ``None`` — the handler then
    reports the error without calling the CLI.

    ``rule_key`` scopes Hermes' session/[a]lways answers to this exact
    ticket: Hermes checks saved approvals before its unattended deny, so a
    tool-wide grain would let one "always" pre-approve every future trade,
    cron and ``-q`` runs included.
    """
    if tool_name not in _BUILDERS or not isinstance(args, dict):
        return None
    if not _true(args.get("confirm")):
        return None
    built = _BUILDERS[tool_name](args)
    if isinstance(built, str):
        return None
    _record_approval(built)
    return {
        "action": "approve",
        "message": _ticket(tool_name, built),
        "rule_key": f"{tool_name}:{_token_key(built)[:16]}",
    }


# ── fund-moving handlers (hook-gated live path) ──────────────────────

def _live_or_refuse(argv: list[str]) -> str | None:
    """Consume the approval token; return an error JSON, or None to proceed."""
    if not _consume_approval(argv):
        return json.dumps({"error": _REFUSAL})
    return None


def _h_execute(args: dict, **kw) -> str:
    built = _build_execute(args)
    if isinstance(built, str):
        return json.dumps({"error": built})
    if _true(args.get("confirm")):
        refused = _live_or_refuse(built)
        if refused is not None:
            return refused
        return run_jw3([*built, "--yes"])
    out = run_jw3(built)
    try:
        payload = json.loads(out)
        payload["preview_only"] = True
        payload["note"] = "Dry run — zero bytes sent. Re-call with confirm=true (explicit per-trade approval) to fire."
        return json.dumps(payload)
    except ValueError:
        pass
    return out


def _h_close(args: dict, **kw) -> str:
    built = _build_close(args)
    if isinstance(built, str):
        return json.dumps({"error": built})
    if _true(args.get("confirm")):
        refused = _live_or_refuse(built)
        if refused is not None:
            return refused
        return run_jw3([*built, "--yes"])
    return run_jw3(built)


def _h_exits(args: dict, **kw) -> str:
    built = _build_exits(args)
    if isinstance(built, str):
        return json.dumps({"error": built})
    if _true(args.get("confirm")):
        refused = _live_or_refuse(built)
        if refused is not None:
            return refused
        return run_jw3([*built, "--yes"])
    return run_jw3(built)


def _h_cancel(args: dict, **kw) -> str:
    built = _build_cancel(args)
    if isinstance(built, str):
        return json.dumps({"error": built})
    if _true(args.get("confirm")):
        refused = _live_or_refuse(built)
        if refused is not None:
            return refused
        return run_jw3([*built, "--yes"])
    return run_jw3(built)


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
