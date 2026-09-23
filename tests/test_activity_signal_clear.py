"""Activity-signal lifecycle: the dot must clear PROMPTLY at turn end.

The sender (this plugin) owns the activity-signal lifecycle. While the agent
works, Hermes core's ``_keep_typing`` loop drives ``send_typing`` every ~2s
(the heartbeat) and each beat carries SIGNAL_TTL_MS. When the turn ends core
calls ``stop_typing`` from ``_keep_typing``'s ``finally`` on every exit path.
Because CV's signal endpoint is stateless with no delete verb, ``stop_typing``
must emit one final beat re-asserting the current phase with the MIN ttl
(SIGNAL_CLEAR_TTL_MS) so the dot disappears within ~1s instead of waiting out
the full ~4s lease — otherwise it stays stuck "thinking…" after the reply.

These tests exercise the real adapter methods against a fake API. ``adapter.py``
imports ``gateway.*`` (only present inside an installed Hermes), so — like the
CI smoke test — we stub those modules, load the package as ``carbonvoice``, and
build the adapter via ``object.__new__`` (no gateway wiring needed for the
signal path).
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root (package dir)


def _bootstrap():
    # --- Stub gateway.* so adapter.py imports without an installed Hermes. ---
    class _Named:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class SendResult:
        def __init__(self, success=False, message_id=None, error=None, **kw):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.__dict__.update(kw)

    class BasePlatformAdapter:  # minimal base; adapter is built via __new__
        pass

    gw = types.ModuleType("gateway")
    gw.__path__ = []  # mark as package
    gw_config = types.ModuleType("gateway.config")
    gw_config.Platform = _Named
    gw_config.PlatformConfig = _Named
    gw_platforms = types.ModuleType("gateway.platforms")
    gw_platforms.__path__ = []
    gw_base = types.ModuleType("gateway.platforms.base")
    gw_base.BasePlatformAdapter = BasePlatformAdapter
    gw_base.MessageEvent = _Named
    gw_base.MessageType = types.SimpleNamespace(TEXT="text", VOICE="voice")
    gw_base.SendResult = SendResult
    gw_base.IMAGE_CACHE_DIR = str(ROOT)
    gw_session = types.ModuleType("gateway.session")
    gw_session.SessionSource = _Named
    gw_session.build_session_key = lambda *a, **k: "sk"
    for name, mod in {
        "gateway": gw,
        "gateway.config": gw_config,
        "gateway.platforms": gw_platforms,
        "gateway.platforms.base": gw_base,
        "gateway.session": gw_session,
    }.items():
        sys.modules.setdefault(name, mod)

    # --- Load the plugin package the way CI does (as ``carbonvoice``). ---
    pkg_name = "carbonvoice"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg
    for name in [
        "constants", "parse", "dedupe", "state", "api", "transport",
        "permits", "audit", "reactions", "channels", "gate", "conversations",
        "adapter",
    ]:
        mod_name = f"{pkg_name}.{name}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, ROOT / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    return sys.modules["carbonvoice.adapter"], sys.modules["carbonvoice.constants"]


adapter_mod, constants_mod = _bootstrap()
CarbonVoiceAdapter = adapter_mod.CarbonVoiceAdapter
SIGNAL_TTL_MS = constants_mod.SIGNAL_TTL_MS
SIGNAL_CLEAR_TTL_MS = constants_mod.SIGNAL_CLEAR_TTL_MS


class FakeAPI:
    """Records every send_signal call; can be made to raise."""

    def __init__(self, raises=False):
        self.calls = []  # list of (chat_id, signal_type, ttl_ms)
        self.raises = raises

    async def send_signal(self, chat_id, signal_type, *, body=None,
                          ttl_ms=None, message_id=None):
        self.calls.append((chat_id, signal_type, ttl_ms))
        if self.raises:
            raise RuntimeError("502 from gateway")


def _make_adapter(api):
    a = object.__new__(CarbonVoiceAdapter)
    a._api = api
    a._signal_skip_until = {}
    a._signal_last_type = {}
    return a


CHAT = "conv-1"


def test_stop_typing_emits_min_ttl_clear_for_current_phase():
    """After thinking beats, stop_typing fires ONE 500ms clear as 'thinking'."""
    api = FakeAPI()
    a = _make_adapter(api)

    asyncio.run(a.send_typing(CHAT))
    asyncio.run(a.send_typing(CHAT))
    assert [c[2] for c in api.calls] == [SIGNAL_TTL_MS, SIGNAL_TTL_MS]

    asyncio.run(a.stop_typing(CHAT))

    # Exactly one extra beat, the clear: same phase, min (500ms) ttl.
    assert len(api.calls) == 3
    chat, sig_type, ttl = api.calls[-1]
    assert chat == CHAT
    assert sig_type == "thinking"
    assert ttl == SIGNAL_CLEAR_TTL_MS == 500
    assert ttl < SIGNAL_TTL_MS  # strictly shortens the lease -> dot clears fast


def test_stop_typing_clears_the_last_phase_not_a_fixed_one():
    """The clear re-asserts whatever phase was last shown (supersede, no flip)."""
    api = FakeAPI()
    a = _make_adapter(api)

    asyncio.run(a.send_typing(CHAT))                       # thinking
    asyncio.run(a.send_or_update_status(CHAT, "tool", ""))  # -> tool_call
    assert api.calls[-1][1] == "tool_call"

    asyncio.run(a.stop_typing(CHAT))

    chat, sig_type, ttl = api.calls[-1]
    assert sig_type == "tool_call"  # not "thinking" — matches the live dot
    assert ttl == SIGNAL_CLEAR_TTL_MS


def test_stop_typing_is_idempotent():
    """Core may stop more than once; only the first emits a clear."""
    api = FakeAPI()
    a = _make_adapter(api)

    asyncio.run(a.send_typing(CHAT))
    asyncio.run(a.stop_typing(CHAT))
    n_after_first_stop = len(api.calls)

    asyncio.run(a.stop_typing(CHAT))  # no phase left -> no-op
    assert len(api.calls) == n_after_first_stop


def test_stop_typing_without_any_signal_is_a_noop():
    """No dot was ever shown -> nothing to clear, no spurious beat."""
    api = FakeAPI()
    a = _make_adapter(api)

    asyncio.run(a.stop_typing(CHAT))
    assert api.calls == []


def test_stop_typing_never_raises_when_the_clear_fails():
    """A failed clear POST must not crash turn teardown (best-effort)."""
    api = FakeAPI()
    a = _make_adapter(api)
    asyncio.run(a.send_typing(CHAT))

    api.raises = True
    asyncio.run(a.stop_typing(CHAT))  # must not propagate the RuntimeError

    # It attempted the clear and armed the error cooldown for the conversation.
    assert api.calls[-1][2] == SIGNAL_CLEAR_TTL_MS
    assert CHAT in a._signal_skip_until


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
