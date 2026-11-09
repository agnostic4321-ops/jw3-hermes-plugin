"""Approval-gate tests for the jw3 Hermes plugin.

Stdlib only (unittest, no pytest, no deps). The plugin is pointed at a
stub, never the real CLI: ``tools._jw3_bin`` resolves to a sentinel path
and ``subprocess.run`` is replaced by a fake that records argv + env.
"""

import importlib.util
import json
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_pkg = types.ModuleType("jw3testpkg")
_pkg.__path__ = [REPO]
sys.modules["jw3testpkg"] = _pkg


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, filename))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tools = _load("jw3testpkg.tools", "tools.py")
plugin_init = _load("jw3testpkg.__init__", "__init__.py")

HANDLERS = {schema["name"]: handler for schema, handler in tools.TOOLS}

LIVE = {
    "jw3_execute": {"exchange": "kucoin", "pair": "SOL/USDT", "side": "buy",
                    "amount": 0.5, "market": True, "lev": 3, "stop": 85,
                    "tp": 95, "confirm": True},
    "jw3_close": {"exchange": "kucoin", "pair": "SOL/USDT", "side": "long",
                  "confirm": True},
    "jw3_exits": {"exchange": "kucoin", "pair": "SOL/USDT", "sl": 80,
                  "tp": 95, "confirm": True},
    "jw3_cancel": {"exchange": "kucoin", "order_id": "abc123", "confirm": True},
}
DRY = {
    "jw3_execute": {"exchange": "kucoin", "pair": "SOL/USDT", "side": "buy",
                    "amount": 0.5},
    "jw3_close": {"exchange": "kucoin", "pair": "SOL/USDT", "side": "long"},
    "jw3_exits": {"exchange": "kucoin", "pair": "SOL/USDT", "tp": 95},
    "jw3_cancel": {"exchange": "kucoin", "order_id": "abc123"},
}
VERBS = {"jw3_execute": "ORDER", "jw3_close": "CLOSE",
         "jw3_exits": "EXITS", "jw3_cancel": "CANCEL"}
NONLIVE = [False, None, "false", "1", "yes", 1, 0]


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.hooks = {}
        self.skills = []
        self.commands = []

    def register_tool(self, name, **kw):
        self.tools.append(name)

    def register_hook(self, hook_name, callback, **kw):
        self.hooks[hook_name] = callback

    def register_skill(self, name, *a, **kw):
        self.skills.append(name)

    def register_command(self, name, *a, **kw):
        self.commands.append(name)


class FakeProc:
    returncode = 0
    stdout = '{"ok": true, "stub": true}'
    stderr = ""


class StubRun:
    """Fake subprocess.run: records argv + env, never executes anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, capture_output=False, text=False, timeout=None, env=None):
        self.calls.append((list(argv), dict(env) if env is not None else None))
        return FakeProc()


class GateTest(unittest.TestCase):
    def setUp(self):
        self._real_run = tools.subprocess.run
        self._real_bin = tools._jw3_bin
        self.stub = StubRun()
        tools.subprocess.run = self.stub
        tools._jw3_bin = lambda: "/tmp/jw3-stub-bin/jw3"
        tools._clear_approvals()
        ctx = FakeCtx()
        plugin_init.register(ctx)
        self.hook = ctx.hooks["pre_tool_call"]
        self.ctx = ctx

    def tearDown(self):
        tools.subprocess.run = self._real_run
        tools._jw3_bin = self._real_bin
        tools._clear_approvals()

    # ── hook gating ──

    def test_hook_ignores_read_tools(self):
        self.assertIsNone(self.hook("jw3_market", {"exchange": "kucoin", "pair": "SOL/USDT", "confirm": True}))
        self.assertIsNone(self.hook("jw3_risk", {"exchange": "kucoin", "confirm": True}))
        self.assertIsNone(self.hook("jw3_balances", {"exchange": "kucoin"}))
        self.assertIsNone(self.hook("jw3_propose", {"kind": "trade", "pair": "SOL/USDT"}))

    def test_hook_ignores_nonlive_confirm(self):
        for name in tools.FUND_MOVING:
            for val in NONLIVE:
                args = dict(LIVE[name])
                args["confirm"] = val
                self.assertIsNone(self.hook(name, args), f"{name} confirm={val!r}")

    def test_hook_approves_all_four(self):
        for name in tools.FUND_MOVING:
            for val in (True, "true", " True "):
                if val == " True " and name != "jw3_execute":
                    continue
                args = dict(LIVE[name])
                args["confirm"] = val
                res = self.hook(name, args)
                self.assertIsInstance(res, dict, name)
                self.assertEqual(res.get("action"), "approve", name)
                self.assertIn("JW3 LIVE", res.get("message", ""), name)
                self.assertIn(VERBS[name], res.get("message", ""), name)
                self.assertIn("--yes", res.get("message", ""), name)
                self.assertTrue(res.get("rule_key", "").startswith(name + ":"), name)
                tools._clear_approvals()

    def test_true_string_case_variants(self):
        for val in ("TRUE", "True", "  true\n"):
            args = dict(LIVE["jw3_close"])
            args["confirm"] = val
            res = self.hook("jw3_close", args)
            self.assertIsNotNone(res, val)
            tools._clear_approvals()

    # ── handler fail-closed ──

    def test_live_without_hook_refused(self):
        for name in tools.FUND_MOVING:
            with self.subTest(tool=name):
                out = json.loads(HANDLERS[name](dict(LIVE[name])))
                self.assertIn("refused", out.get("error", ""), name)
        self.assertEqual(self.stub.calls, [])

    def test_hook_then_handler_runs_once_with_yes(self):
        args = dict(LIVE["jw3_execute"])
        res = self.hook("jw3_execute", args)
        self.assertEqual(res["action"], "approve")
        out = json.loads(HANDLERS["jw3_execute"](dict(args)))
        self.assertEqual(out, {"result": {"ok": True, "stub": True}})
        self.assertEqual(len(self.stub.calls), 1)
        argv, _env = self.stub.calls[0]
        self.assertIn("--yes", argv)
        self.assertIn("execute", argv)

    def test_market_flag_true_adds_market(self):
        self.hook("jw3_execute", dict(LIVE["jw3_execute"]))
        HANDLERS["jw3_execute"](dict(LIVE["jw3_execute"]))
        argv, _ = self.stub.calls[0]
        self.assertIn("--market", argv)

    def test_repeat_handler_call_refused(self):
        self.hook("jw3_close", dict(LIVE["jw3_close"]))
        HANDLERS["jw3_close"](dict(LIVE["jw3_close"]))
        out = json.loads(HANDLERS["jw3_close"](dict(LIVE["jw3_close"])))
        self.assertIn("refused", out.get("error", ""))
        self.assertEqual(len(self.stub.calls), 1)

    def test_changed_args_refused(self):
        self.hook("jw3_execute", dict(LIVE["jw3_execute"]))
        tampered = dict(LIVE["jw3_execute"])
        tampered["amount"] = 5.0
        out = json.loads(HANDLERS["jw3_execute"](tampered))
        self.assertIn("refused", out.get("error", ""))
        self.assertEqual(self.stub.calls, [])

    def test_expired_token_refused(self):
        self.hook("jw3_exits", dict(LIVE["jw3_exits"]))
        with tools._approvals_lock:
            for key in tools._approvals:
                tools._approvals[key] = [0.0]
        out = json.loads(HANDLERS["jw3_exits"](dict(LIVE["jw3_exits"])))
        self.assertIn("refused", out.get("error", ""))
        self.assertEqual(self.stub.calls, [])

    def test_two_identical_approvals_allow_two_runs(self):
        self.hook("jw3_cancel", dict(LIVE["jw3_cancel"]))
        self.hook("jw3_cancel", dict(LIVE["jw3_cancel"]))
        HANDLERS["jw3_cancel"](dict(LIVE["jw3_cancel"]))
        HANDLERS["jw3_cancel"](dict(LIVE["jw3_cancel"]))
        self.assertEqual(len(self.stub.calls), 2)
        out = json.loads(HANDLERS["jw3_cancel"](dict(LIVE["jw3_cancel"])))
        self.assertIn("refused", out.get("error", ""))

    # ── builder validation ──

    def test_control_chars_rejected(self):
        bad = ["SOL/USDT\n", "SOL\u202e/USDT", "abc\x00123", "x\x7f"]
        for name, field in (("jw3_execute", "pair"), ("jw3_cancel", "order_id")):
            for val in bad:
                args = dict(LIVE[name])
                args[field] = val
                args["confirm"] = True
                self.assertIsNone(self.hook(name, args), f"{name} {val!r}")
                out = json.loads(HANDLERS[name](dict(args)))
                self.assertIn("control/format", out.get("error", ""), f"{name} {val!r}")
        self.assertEqual(self.stub.calls, [])

    def test_overlong_values_rejected(self):
        args = dict(LIVE["jw3_close"])
        args["pair"] = "A" * 65
        self.assertIsNone(self.hook("jw3_close", args))
        out = json.loads(HANDLERS["jw3_close"](dict(args)))
        self.assertIn("64", out.get("error", ""))
        self.assertEqual(self.stub.calls, [])

    def test_side_enums_enforced(self):
        bad_exec = dict(LIVE["jw3_execute"])
        bad_exec["side"] = "long"
        out = json.loads(HANDLERS["jw3_execute"](bad_exec))
        self.assertIn("buy or sell", out.get("error", ""))
        bad_close = dict(LIVE["jw3_close"])
        bad_close["side"] = "buy"
        out = json.loads(HANDLERS["jw3_close"](bad_close))
        self.assertIn("long or short", out.get("error", ""))
        self.assertEqual(self.stub.calls, [])

    def test_required_fields_still_required(self):
        out = json.loads(HANDLERS["jw3_execute"]({"side": "buy", "confirm": True}))
        self.assertIn("pair", out.get("error", ""))
        out = json.loads(HANDLERS["jw3_exits"]({"confirm": True}))
        self.assertIn("pair", out.get("error", ""))
        out = json.loads(HANDLERS["jw3_cancel"]({"confirm": True}))
        self.assertIn("order_id", out.get("error", ""))
        self.assertEqual(self.stub.calls, [])

    def test_cancel_all_pairs_string_false_ok(self):
        args = {"exchange": "kucoin", "pair": "SOL/USDT", "all_for_pair": True,
                "all_pairs": "false"}
        out = HANDLERS["jw3_cancel"](dict(args))  # dry run
        self.assertNotIn("error", json.loads(out))
        argv, _ = self.stub.calls[0]
        self.assertIn("cancel-all", argv)
        self.assertNotIn("--yes", argv)

    # ── option injection, coercion parity, approval grain ──

    def test_option_injection_rejected_even_in_dry_run(self):
        # The CLI goes live on any --yes token, so a dash-led value must never
        # reach argv — not even on the dry-run path, which has no hook.
        cases = (
            ("jw3_execute", "margin_mode", "--yes"),
            ("jw3_execute", "pair", "-y"),
            ("jw3_close", "pair", "--yes"),
            ("jw3_exits", "quantity", "-1"),
            ("jw3_cancel", "order_id", "--yes"),
        )
        for name, field, val in cases:
            for confirm in (False, True):
                with self.subTest(tool=name, field=field, confirm=confirm):
                    args = dict(DRY[name])
                    args[field] = val
                    args["confirm"] = confirm
                    self.assertIsNone(self.hook(name, args))
                    out = json.loads(HANDLERS[name](dict(args)))
                    self.assertIn("refused", out.get("error", ""))
        self.assertEqual(self.stub.calls, [])

    def test_hook_raw_strings_match_handler_coerced_numbers(self):
        # The agent loop fires the hook on the model's raw args, then Hermes
        # coerces strings to schema types before the handler runs.
        raw = {"exchange": "kucoin", "pair": "SOL/USDT", "side": "buy",
               "amount": "0.10", "lev": "3", "stop": "85.0", "market": "true",
               "confirm": "true"}
        coerced = {"exchange": "kucoin", "pair": "SOL/USDT", "side": "buy",
                   "amount": 0.1, "lev": 3, "stop": 85, "market": True,
                   "confirm": True}
        res = self.hook("jw3_execute", raw)
        self.assertEqual(res["action"], "approve")
        out = json.loads(HANDLERS["jw3_execute"](coerced))
        self.assertEqual(out, {"result": {"ok": True, "stub": True}})
        argv, _ = self.stub.calls[0]
        self.assertEqual(argv[argv.index("--amount") + 1], "0.1")
        self.assertEqual(argv[argv.index("--stop") + 1], "85")
        self.assertIn("--yes", argv)

    def test_rule_key_is_per_ticket(self):
        a = self.hook("jw3_execute", dict(LIVE["jw3_execute"]))
        b = self.hook("jw3_execute", dict(LIVE["jw3_execute"]))
        bigger = dict(LIVE["jw3_execute"])
        bigger["amount"] = 50
        c = self.hook("jw3_execute", bigger)
        self.assertTrue(a["rule_key"].startswith("jw3_execute:"))
        self.assertEqual(a["rule_key"], b["rule_key"])
        self.assertNotEqual(a["rule_key"], c["rule_key"])

    def test_ticket_describes_exact_argv(self):
        msg = self.hook("jw3_execute", dict(LIVE["jw3_execute"]))["message"]
        for part in ("JW3 LIVE ORDER", "open BUY 0.5 SOL/USDT on kucoin",
                     "market", "3x", "SL 85", "TP 95",
                     "command: jw3 execute --exchange kucoin --pair SOL/USDT"):
            self.assertIn(part, msg)
        no_venue = dict(LIVE["jw3_close"])
        del no_venue["exchange"]
        msg = self.hook("jw3_close", no_venue)["message"]
        self.assertIn("the CLI default venue", msg)
        self.assertNotIn("--exchange", msg)

    def test_non_scalar_values_rejected(self):
        for field, val in (("amount", [1]), ("pair", {"a": 1}), ("lev", True)):
            args = dict(LIVE["jw3_execute"])
            args[field] = val
            self.assertIsNone(self.hook("jw3_execute", args), field)
            out = json.loads(HANDLERS["jw3_execute"](dict(args)))
            self.assertIn("refused", out.get("error", ""), field)
        self.assertEqual(self.stub.calls, [])

    def test_jw3_bin_fallback_requires_executable(self):
        import tempfile
        real_which = tools.shutil.which
        real_home = os.environ.get("HOME")
        tools.shutil.which = lambda name: None
        try:
            with tempfile.TemporaryDirectory() as home:
                os.environ["HOME"] = home
                os.makedirs(os.path.join(home, ".local", "bin"))
                exe = os.path.join(home, ".local", "bin", "jw3")
                with open(exe, "w") as fh:
                    fh.write("#!/bin/sh\n")
                os.chmod(exe, 0o644)
                self.assertIsNone(self._real_bin())
                os.chmod(exe, 0o755)
                self.assertEqual(self._real_bin(), exe)
        finally:
            tools.shutil.which = real_which
            if real_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = real_home

    # ── dry run / market flag ──

    def test_market_string_false_adds_no_market(self):
        args = dict(DRY["jw3_execute"])
        args["market"] = "false"
        HANDLERS["jw3_execute"](args)
        argv, _ = self.stub.calls[0]
        self.assertNotIn("--market", argv)
        self.assertNotIn("--yes", argv)

    def test_dry_run_never_yes(self):
        for name in tools.FUND_MOVING:
            with self.subTest(tool=name):
                before = len(self.stub.calls)
                out = json.loads(HANDLERS[name](dict(DRY[name])))
                self.assertNotIn("error", out, name)
                argv, _ = self.stub.calls[before]
                self.assertNotIn("--yes", argv, name)
        payload = json.loads(HANDLERS["jw3_execute"](dict(DRY["jw3_execute"])))
        self.assertTrue(payload.get("preview_only"))

    # ── child env allowlist ──

    def test_env_allowlist(self):
        os.environ["OPENROUTER_API_KEY"] = "fake-provider-key-for-test"
        os.environ["ANTHROPIC_API_KEY"] = "fake-provider-key-for-test"
        os.environ["JW3_TEST_ALLOW"] = "keepme"
        try:
            HANDLERS["jw3_market"]({"exchange": "kucoin", "pair": "SOL/USDT"})
        finally:
            del os.environ["OPENROUTER_API_KEY"]
            del os.environ["ANTHROPIC_API_KEY"]
            del os.environ["JW3_TEST_ALLOW"]
        self.assertEqual(len(self.stub.calls), 1)
        _argv, env = self.stub.calls[0]
        self.assertIsNotNone(env)
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env.get("JW3_TEST_ALLOW"), "keepme")
        self.assertIn("PATH", env)
        self.assertIn("HOME", env)
        allowed = {"PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR",
                   "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA",
                   "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"}
        for key in env:
            self.assertTrue(key in allowed or key.startswith("JW3_"), key)

    # ── registration wiring ──

    def test_register_wires_hook_and_tools(self):
        self.assertEqual(self.hook, self.ctx.hooks.get("pre_tool_call"))
        self.assertEqual(len(self.ctx.tools), 17)
        for name in tools.FUND_MOVING:
            self.assertIn(name, self.ctx.tools)
        self.assertIn("jw3-ops", self.ctx.skills)
        self.assertIn("jw3-status", self.ctx.commands)


if __name__ == "__main__":
    unittest.main()
