"""Per-agent provider/model config + subject-aware routing.

Generalizes the librarian-only provider switch to every agent (tutor, librarian,
judge). Each can run on anthropic (inherited env) or be rerouted at a non-
Anthropic endpoint (deepseek/minimax/local) with its own model + effort. The
provider policy (auth style, thinking support, tool disabling) lives in
salient_tutor.providers — the single source of truth shared by daemon + UI.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from salient_tutor import web
from salient_tutor.daemon import TutorDaemon


class _DaemonShell:
    """Bare daemon with the per-agent config methods bound + a minimal roster."""

    agent_config = TutorDaemon.agent_config
    all_agent_configs = TutorDaemon.all_agent_configs
    set_agent_config = TutorDaemon.set_agent_config
    _agent_endpoint_for = TutorDaemon._agent_endpoint_for
    _runtime_for = TutorDaemon._runtime_for
    _make_options = TutorDaemon._make_options
    _rebuild_runner = TutorDaemon._rebuild_runner
    _persist_agent_runtime = TutorDaemon._persist_agent_runtime
    _seed_providers_from_env = TutorDaemon._seed_providers_from_env

    def __init__(self, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.setenv("SALIENT_TUTOR_WORK_ROOT", str(tmp_path))
        self.work_root = Path(tmp_path)
        self._librarian_config_path = self.work_root / "librarian_config.json"
        self._agent_config_path = self.work_root / "agent_configs.json"
        self._agent_runtime: dict = {}
        self._tasks: list = []
        self.agent_configs = {
            "tutor": {
                "system_prompt_file": "tutor.md",
                "model": "claude-opus-4-8[1m]",
                "builtin_tools": ["WebSearch", "WebFetch"],
                "max_turns": 30,
            },
            "librarian": {
                "system_prompt_file": "librarian.md",
                "model": "claude-sonnet-5[1m]",
                "builtin_tools": ["Read"],
                "bus_tools": False,
                "max_turns": 20,
            },
        }
        self.runners: dict = {}


# ── Provider registry ──────────────────────────────────────────────────────
class TestProviderRegistry:
    def test_all_five_providers_present(self):
        from salient_tutor.providers import PROVIDERS

        assert set(PROVIDERS) == {"anthropic", "deepseek", "minimax", "local", "codex"}

    def test_only_endpoint_providers_need_endpoints(self):
        from salient_tutor.providers import PROVIDERS

        assert PROVIDERS["anthropic"].needs_endpoint is False
        assert PROVIDERS["codex"].needs_endpoint is False
        assert all(PROVIDERS[p].needs_endpoint for p in ("deepseek", "minimax", "local"))

    def test_provider_kinds(self):
        from salient_tutor.providers import PROVIDERS

        assert PROVIDERS["anthropic"].kind == "sdk"
        assert PROVIDERS["codex"].kind == "backend"
        assert all(PROVIDERS[p].kind == "endpoint" for p in ("deepseek", "minimax", "local"))

    def test_codex_model_tier_mapping(self):
        from salient_tutor.providers import CODEX_DEFAULT_MODEL, codex_model_for

        # An explicit config model always wins.
        assert codex_model_for(" gpt-5.5 ", "claude-opus-4-8[1m]") == "gpt-5.5"
        # Roster Claude tier maps to its codex counterpart.
        assert codex_model_for("", "claude-opus-4-8[1m]") == "gpt-5.5"
        assert codex_model_for("", "claude-fable-5[1m]") == "gpt-5.5"
        assert codex_model_for("", "claude-sonnet-5[1m]") == "gpt-5.4"
        assert codex_model_for("", "claude-haiku-4-5") == "gpt-5.3-codex-spark"
        # Unknown roster model → the default.
        assert codex_model_for("", "mystery-model") == CODEX_DEFAULT_MODEL
        assert codex_model_for("", "") == CODEX_DEFAULT_MODEL

    def test_codex_effort_mapping(self):
        from salient_tutor.providers import codex_effort

        assert codex_effort("low") == "low"
        assert codex_effort("med") == "medium"
        assert codex_effort("high") == "high"
        assert codex_effort("HIGH ") == "high"
        assert codex_effort("xhigh") is None
        assert codex_effort("") is None
        assert codex_effort(None) is None

    def test_minimax_uses_bearer_deepseek_local_use_api_key(self):
        from salient_tutor.providers import PROVIDERS

        assert PROVIDERS["minimax"].auth_style == "bearer"
        assert PROVIDERS["deepseek"].auth_style == "api_key"
        assert PROVIDERS["local"].auth_style == "api_key"

    def test_local_and_deepseek_cannot_think(self):
        from salient_tutor.providers import PROVIDERS

        assert PROVIDERS["local"].supports_thinking is False
        assert PROVIDERS["deepseek"].supports_thinking is False
        assert PROVIDERS["anthropic"].supports_thinking is True

    def test_resolve_thinking_per_provider(self):
        from salient_tutor.providers import resolve_thinking

        # local/deepseek → always disabled regardless of effort
        assert resolve_thinking("local", "high") == {"type": "disabled"}
        assert resolve_thinking("deepseek", "high") == {"type": "disabled"}
        # anthropic low → off; high → enabled with budget
        assert resolve_thinking("anthropic", "low") == {"type": "disabled"}
        high = resolve_thinking("anthropic", "high")
        assert high["type"] == "enabled" and high["budget_tokens"] > 0
        # minimax M3 → adaptive
        assert resolve_thinking("minimax", "med", "MiniMax-M3") == {"type": "adaptive"}

    def test_subject_model_map(self):
        from salient_tutor.providers import suggested_tutor_model

        assert "opus" in suggested_tutor_model("cyber").lower()
        assert "fable" in suggested_tutor_model("biology").lower()
        assert "fable" in suggested_tutor_model("other").lower()


# ── Env-var provider seeding (route any agent without the UI) ───────────────
class TestProviderFromEnv:
    def _shell_with_judge(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.agent_configs["judge"] = {
            "system_prompt_file": "judge.md",
            "model": "claude-opus-4-8[1m]",
            "builtin_tools": [],
            "max_turns": 4,
        }
        return shell

    def test_judge_env_routes_to_deepseek_with_inherited_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-deepseek-inherited")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # else the default key-env fills it
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()

        assert shell._runtime_for("judge")["provider"] == "deepseek"
        ep = shell._agent_endpoint_for("judge")
        assert ep is not None
        base_url, _model, api_key, auth_style, _bare, provider, _effort = ep
        assert provider == "deepseek"
        assert base_url == "https://api.deepseek.com/anthropic"  # registry default
        assert auth_style == "api_key"
        assert api_key == ""  # inherited from process env, not stored in the block

    def test_per_agent_key_and_base_url_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER_KEY", "sk-judge-only")
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER_BASE_URL", "https://proxy.internal/anthropic")
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()

        base_url, _m, api_key, _a, _b, _p, _e = shell._agent_endpoint_for("judge")
        assert base_url == "https://proxy.internal/anthropic"
        assert api_key == "sk-judge-only"

    def test_persisted_endpoint_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._agent_runtime["judge"] = {"provider": "minimax"}  # explicit UI endpoint
        shell._seed_providers_from_env()
        assert shell._runtime_for("judge")["provider"] == "minimax"

    def test_env_upgrades_a_default_anthropic_block(self, tmp_path, monkeypatch):
        # A persisted {provider: anthropic} is only the default (inherited env),
        # so env may upgrade it to an endpoint — preserving the effort setting.
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._agent_runtime["judge"] = {"provider": "anthropic", "effort": "low"}
        shell._seed_providers_from_env()
        cfg = shell._runtime_for("judge")
        assert cfg["provider"] == "deepseek" and cfg["effort"] == "low"

    def test_judge_env_routes_to_codex_without_endpoint_fields(self, tmp_path, monkeypatch):
        # codex is a backend provider — no endpoint reroute, so base_url/key
        # envs are ignored and _agent_endpoint_for stays None.
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "codex")
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER_BASE_URL", "https://ignored.example")
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER_KEY", "sk-ignored")
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        rt = shell._runtime_for("judge")
        assert rt["provider"] == "codex"
        assert "base_url" not in rt and "api_key" not in rt
        assert shell._agent_endpoint_for("judge") is None

    def test_unknown_provider_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "gpt5")
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        assert shell._runtime_for("judge")["provider"] == "anthropic"  # unchanged

    def test_anthropic_and_absent_are_noops(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "anthropic")
        monkeypatch.delenv("TUTOR_PROVIDER", raising=False)
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        assert shell._agent_endpoint_for("judge") is None
        assert shell._agent_endpoint_for("tutor") is None


# ── Per-provider default key env (Claude + DeepSeek + MiniMax coexist) ───────
class TestDefaultKeyEnv:
    """Each provider auto-resolves its OWN conventional key env when no per-agent
    key is set — so setting a DeepSeek/MiniMax key never touches the Claude key."""

    def _shell_with_judge(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.agent_configs["judge"] = {
            "system_prompt_file": "judge.md",
            "model": "claude-opus-4-8[1m]",
            "builtin_tools": [],
            "max_turns": 4,
        }
        return shell

    def test_deepseek_resolves_default_key_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
        monkeypatch.delenv("TUTOR_JUDGE_PROVIDER_KEY", raising=False)
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        _base, _m, api_key, auth_style, _b, provider, _e = shell._agent_endpoint_for("judge")
        assert provider == "deepseek"
        assert api_key == "sk-ds"
        assert auth_style == "api_key"

    def test_minimax_resolves_default_key_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "minimax")
        monkeypatch.setenv("MINIMAX_API_KEY", "mm")
        monkeypatch.delenv("TUTOR_JUDGE_PROVIDER_KEY", raising=False)
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        _base, _m, api_key, auth_style, _b, provider, _e = shell._agent_endpoint_for("judge")
        assert provider == "minimax"
        assert api_key == "mm"
        assert auth_style == "bearer"  # goes to ANTHROPIC_AUTH_TOKEN at spawn

    def test_per_agent_key_wins_over_default_key_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER_KEY", "explicit")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        _base, _m, api_key, _a, _b, _p, _e = shell._agent_endpoint_for("judge")
        assert api_key == "explicit"

    def test_deepseek_without_any_key_stays_inherited(self, tmp_path, monkeypatch):
        # No per-agent key and no DEEPSEEK_API_KEY → api_key stays "" (subprocess
        # inherits ANTHROPIC_API_KEY, the pre-existing fallback). Backward-compat.
        monkeypatch.setenv("TUTOR_JUDGE_PROVIDER", "deepseek")
        monkeypatch.delenv("TUTOR_JUDGE_PROVIDER_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        shell = self._shell_with_judge(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        _base, _m, api_key, _a, _b, _p, _e = shell._agent_endpoint_for("judge")
        assert api_key == ""

    def test_local_needs_no_key(self, tmp_path, monkeypatch):
        # local has no default_key_env and legitimately needs no key.
        monkeypatch.setenv("TUTOR_LIBRARIAN_PROVIDER", "local")
        monkeypatch.delenv("TUTOR_LIBRARIAN_PROVIDER_KEY", raising=False)
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell._seed_providers_from_env()
        _base, _m, api_key, _a, _b, provider, _e = shell._agent_endpoint_for("librarian")
        assert provider == "local"
        assert api_key == ""


# ── Per-agent config round-trip ────────────────────────────────────────────
class TestAgentConfigRoundTrip:
    def test_default_is_anthropic(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        for name in ("tutor", "librarian"):
            cfg = shell.agent_config(name)
            assert cfg["provider"] == "anthropic"
            assert cfg["model"] == "" and cfg["api_key"] is False
            assert shell._agent_endpoint_for(name) is None

    def test_set_minimax_then_clear(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        res = shell.set_agent_config(
            "tutor",
            provider="minimax",
            base_url="https://api.minimax.io/anthropic",
            model="MiniMax-M3",
            api_key="supersecret-token-xyz",
            effort="high",
        )
        assert res["provider"] == "minimax" and res["model"] == "MiniMax-M3"
        assert res["effort"] == "high" and res["api_key"] is True
        assert "supersecret-token-xyz" not in str(res)  # secret never echoed
        assert (tmp_path / "agent_configs.json").exists()

        cleared = shell.set_agent_config("tutor", provider="anthropic")
        assert cleared["provider"] == "anthropic"
        assert shell._agent_endpoint_for("tutor") is None

    def test_set_codex_without_endpoint_fields_then_clear(self, tmp_path, monkeypatch):
        # codex needs no base_url/api_key, and model is optional for a roster
        # agent (the tier map fills it at runner build).
        shell = _DaemonShell(tmp_path, monkeypatch)
        res = shell.set_agent_config("tutor", provider="codex", effort="high")
        assert res["provider"] == "codex" and res["effort"] == "high"
        assert res["model"] == "" and res["base_url"] == "" and res["api_key"] is False
        assert shell._agent_endpoint_for("tutor") is None  # not an endpoint reroute
        # a codex block persists (non-anthropic) and survives a reload
        import json

        saved = json.loads((tmp_path / "agent_configs.json").read_text())
        assert saved["tutor"] == {"provider": "codex", "effort": "high"}

        cleared = shell.set_agent_config("tutor", provider="anthropic")
        assert cleared["provider"] == "anthropic"

    def test_set_codex_with_explicit_model(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        res = shell.set_agent_config("tutor", provider="codex", model="gpt-5.5")
        assert res["provider"] == "codex" and res["model"] == "gpt-5.5"
        assert shell._runtime_for("tutor")["model"] == "gpt-5.5"

    def test_optional_agent_on_codex_requires_model(self, tmp_path, monkeypatch):
        # Roster registration of judge/tutor_alt keys on a configured model, so
        # creating one on codex without a model must be a clear error, and
        # providing one must register it live.
        shell = _DaemonShell(tmp_path, monkeypatch)
        assert "error" in shell.set_agent_config("judge", provider="codex")
        res = shell.set_agent_config("judge", provider="codex", model="gpt-5.5")
        assert res["provider"] == "codex" and res["model"] == "gpt-5.5"
        assert "judge" in shell.agent_configs

    def test_endpoint_provider_requires_base_url_and_model(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        res = shell.set_agent_config("tutor", provider="local", base_url="http://x", model="")
        assert "error" in res

    def test_unknown_provider_and_effort_rejected(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        assert "error" in shell.set_agent_config(
            "tutor", provider="gemini", model="x", base_url="y"
        )

    def test_anthropic_per_agent_model_is_stored_and_persisted(self, tmp_path, monkeypatch):
        # Pick a specific Claude model (opus/sonnet/fable) for one agent without
        # any endpoint reroute — provider stays anthropic (inherited OAuth env).
        shell = _DaemonShell(tmp_path, monkeypatch)
        res = shell.set_agent_config("tutor", provider="anthropic", model="claude-fable-5[1m]")
        assert res["provider"] == "anthropic"
        assert res["model"] == "claude-fable-5[1m]"
        assert shell._agent_endpoint_for("tutor") is None  # no reroute — still Anthropic
        assert shell._runtime_for("tutor")["model"] == "claude-fable-5[1m]"
        # an anthropic block carrying a model must survive a reload
        assert (tmp_path / "agent_configs.json").exists()

    def test_anthropic_without_model_stays_unpersisted(self, tmp_path, monkeypatch):
        # provider=anthropic + default effort + no model → nothing to persist.
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config("tutor", provider="anthropic")
        assert not (tmp_path / "agent_configs.json").exists()

    def test_optional_agent_created_via_set_config_registers_and_persists(
        self, tmp_path, monkeypatch
    ):
        # judge is NOT in the base roster; set_agent_config must be able to
        # create it (the 🤖 Agents tab flow), register it live, and persist it.
        shell = _DaemonShell(tmp_path, monkeypatch)
        assert "judge" not in shell.agent_configs
        res = shell.set_agent_config(
            "judge",
            provider="minimax",
            base_url="https://api.minimax.io/anthropic",
            model="MiniMax-M3",
            api_key="tok",
        )
        assert res["provider"] == "minimax" and res["model"] == "MiniMax-M3"
        assert "judge" in shell.agent_configs  # now live this session
        # persisted, so a fresh session reload will re-register it
        import json

        saved = json.loads((tmp_path / "agent_configs.json").read_text())
        assert saved["judge"]["model"] == "MiniMax-M3"

    def test_optional_agent_created_on_anthropic_with_model(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        res = shell.set_agent_config("tutor_alt", provider="anthropic", model="claude-fable-5[1m]")
        assert res["provider"] == "anthropic" and res["model"] == "claude-fable-5[1m]"
        assert "tutor_alt" in shell.agent_configs
        assert shell.agent_configs["tutor_alt"]["substitute_for"] == "tutor"

    def test_optional_agent_removed_by_bare_anthropic(self, tmp_path, monkeypatch):
        # anthropic + no model on an optional agent = its "remove" gesture.
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config("judge", provider="deepseek", base_url="http://x", model="dc")
        assert "judge" in shell.agent_configs
        res = shell.set_agent_config("judge", provider="anthropic")  # no model
        assert res.get("removed") is True
        assert "judge" not in shell.agent_configs
        assert "judge" not in shell._agent_runtime
        # nothing left to persist → file gone
        assert not (tmp_path / "agent_configs.json").exists()

    def test_unknown_non_optional_agent_still_rejected(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        assert "error" in shell.set_agent_config("wizard", provider="anthropic", model="x")
        assert "error" in shell.set_agent_config("tutor", provider="anthropic", effort="ultra")

    def test_api_key_kept_when_blank_on_resave(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config(
            "tutor", provider="deepseek", base_url="http://x", model="m", api_key="secret"
        )
        # Re-save without re-typing the key → existing key kept.
        res = shell.set_agent_config("tutor", provider="deepseek", base_url="http://x", model="m2")
        assert res["api_key"] is True
        ep = shell._agent_endpoint_for("tutor")
        assert ep[2] == "secret"  # the original key survived


# ── Endpoint override per provider ─────────────────────────────────────────
class TestEndpointOverridePerProvider:
    def _options(self, shell, agent, monkeypatch):
        monkeypatch.setattr("salient_tutor.daemon.make_bus", lambda d, a: (None, "bus", []))
        shell._load_prompt = lambda a: "prompt"
        return shell._make_options(agent)

    def test_minimax_bearer_auth_and_thinking(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config(
            "tutor",
            provider="minimax",
            base_url="http://mm/anthropic",
            model="MiniMax-M3",
            api_key="tok",
            effort="high",
        )
        opts = self._options(shell, "tutor", monkeypatch)
        assert opts.env["ANTHROPIC_BASE_URL"] == "http://mm/anthropic"
        assert opts.env.get("ANTHROPIC_AUTH_TOKEN") == "tok"
        assert "ANTHROPIC_API_KEY" not in opts.env
        assert opts.model == "MiniMax-M3"
        assert opts.thinking == {"type": "adaptive"}  # M3 coupled policy
        assert opts.effort == "high"

    def test_deepseek_api_key_auth_and_tools_disabled(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config(
            "tutor", provider="deepseek", base_url="http://ds", model="deepseek-chat", api_key="k"
        )
        opts = self._options(shell, "tutor", monkeypatch)
        assert opts.env.get("ANTHROPIC_API_KEY") == "k"
        assert opts.thinking == {"type": "disabled"}  # deepseek can't think
        # WebSearch/WebFetch disabled on deepseek (Anthropic-side server tools).
        assert "WebSearch" not in (opts.tools or []) and "WebFetch" not in (opts.tools or [])

    def test_local_bare_and_disabled_thinking(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config(
            "librarian", provider="local", base_url="http://ai.home:1234", model="glm-4.6v-flash"
        )
        opts = self._options(shell, "librarian", monkeypatch)
        assert opts.extra_args.get("bare") is None
        assert opts.thinking == {"type": "disabled"}

    def test_anthropic_agent_has_no_override(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        opts = self._options(shell, "tutor", monkeypatch)
        assert opts.env is None or "ANTHROPIC_BASE_URL" not in (opts.env or {})
        assert opts.model == "claude-opus-4-8[1m]"

    def test_anthropic_effort_drives_thinking_budget(self, tmp_path, monkeypatch):
        # Regression: the effort dial was a no-op for anthropic agents — effort
        # + thinking were only applied inside the non-anthropic endpoint branch.
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config("tutor", provider="anthropic", effort="high")
        opts = self._options(shell, "tutor", monkeypatch)
        assert opts.effort == "high"
        assert opts.thinking == {"type": "enabled", "budget_tokens": 24576}
        # 'low' turns extended thinking off entirely.
        shell.set_agent_config("tutor", provider="anthropic", effort="low")
        opts_low = self._options(shell, "tutor", monkeypatch)
        assert opts_low.effort == "low"
        assert opts_low.thinking == {"type": "disabled"}

    def test_per_agent_isolation(self, tmp_path, monkeypatch):
        # Tutor on minimax, librarian on local — each gets its own endpoint.
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config("tutor", provider="minimax", base_url="http://mm", model="M3")
        shell.set_agent_config("librarian", provider="local", base_url="http://lms", model="glm")
        topts = self._options(shell, "tutor", monkeypatch)
        lopts = self._options(shell, "librarian", monkeypatch)
        assert topts.env["ANTHROPIC_BASE_URL"] == "http://mm" and topts.model == "M3"
        assert lopts.env["ANTHROPIC_BASE_URL"] == "http://lms" and lopts.model == "glm"


class TestAnthropicEffortPersists:
    """The effort dial must survive a save + reload for anthropic agents (the
    tutor's default) — previously the anthropic block was dropped on save and
    filtered out of the persisted file, so the setting silently reverted."""

    def test_effort_persists_and_reloads(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config("tutor", provider="anthropic", effort="high")
        assert shell.agent_config("tutor")["effort"] == "high"
        assert (tmp_path / "agent_configs.json").exists()  # non-default effort persisted

        # Fresh daemon reading the same file recovers the setting.
        reloaded = _DaemonShell(tmp_path, monkeypatch)
        reloaded._load_agent_runtime = TutorDaemon._load_agent_runtime.__get__(reloaded)
        reloaded._agent_runtime = reloaded._load_agent_runtime()
        assert reloaded.agent_config("tutor")["effort"] == "high"

    def test_default_effort_not_persisted(self, tmp_path, monkeypatch):
        # A plain anthropic/med agent leaves no file behind (clean state).
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.set_agent_config("tutor", provider="anthropic", effort="med")
        assert not (tmp_path / "agent_configs.json").exists()


# ── Runner rebuild on change ───────────────────────────────────────────────
class TestRunnerRebuilt:
    def test_runner_dropped_and_task_tracked(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        shell.runners["tutor"] = object()
        shell.set_agent_config("tutor", provider="minimax", base_url="http://mm", model="M3")
        assert "tutor" not in shell.runners  # dropped → next prompt rebuilds

    def test_rebuild_safe_when_no_runner(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        # No runner yet — must not error.
        shell.set_agent_config("librarian", provider="local", base_url="http://x", model="m")
        assert "librarian" not in shell.runners


# ── Routes ─────────────────────────────────────────────────────────────────
class TestAgentConfigRoutes:
    def test_get_returns_registry_and_configs(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        monkeypatch.setattr(web, "daemon", shell)
        r = TestClient(web.app).get("/api/agents/config")
        assert r.status_code == 200
        body = r.json()
        assert set(body["providers"]) == {"anthropic", "deepseek", "minimax", "local", "codex"}
        assert "tutor" in body["agents"] and body["agents"]["tutor"]["provider"] == "anthropic"
        # The routing kind rides along so the UI can shape fields per provider.
        assert body["providers"]["anthropic"]["kind"] == "sdk"
        assert body["providers"]["codex"]["kind"] == "backend"
        assert body["providers"]["local"]["kind"] == "endpoint"

    @staticmethod
    def _isolate_probe_state(monkeypatch):
        # The probe endpoint keeps a module-level TTL cache + in-flight map;
        # give each test a fresh pair so results can't leak across tests.
        monkeypatch.setattr(web, "_probe_cache", {})
        monkeypatch.setattr(web, "_probe_inflight", {})

    @staticmethod
    def _stub_probe_provider(probe_coro):
        from salient_core.codex import CodexProvider

        class _StubProbe(CodexProvider):
            def __init__(self):
                self.probe_calls = 0

            async def probe(self):
                self.probe_calls += 1
                return await probe_coro()

        return _StubProbe()

    def test_codex_probe_endpoint(self, tmp_path, monkeypatch):
        # Stubbed provider registry — the endpoint must relay probe() and stay
        # FAIL-SAFE (never a 500), without touching a real codex runtime.
        from salient_core import ProviderRegistry, reset_provider_registry, set_provider_registry
        from salient_core.providers import ProviderProbe

        async def _unavailable():
            return ProviderProbe(False, "install the optional codex extra")

        self._isolate_probe_state(monkeypatch)
        set_provider_registry(ProviderRegistry([self._stub_probe_provider(_unavailable)]))
        try:
            shell = _DaemonShell(tmp_path, monkeypatch)
            monkeypatch.setattr(web, "daemon", shell)
            client = TestClient(web.app)
            r = client.get("/api/providers/probe", params={"name": "codex"})
            assert r.status_code == 200
            body = r.json()
            assert body["provider"] == "codex"
            assert body["available"] is False
            assert "codex extra" in body["detail"]
            # Endpoint providers aren't probeable backends.
            r2 = client.get("/api/providers/probe", params={"name": "local"})
            assert "error" in r2.json()
        finally:
            reset_provider_registry()

    def test_codex_probe_timeout_is_fail_safe(self, tmp_path, monkeypatch):
        # A wedged codex binary must not pin the request: the endpoint bounds
        # the probe and reports unavailable instead of hanging.
        import asyncio

        from salient_core import ProviderRegistry, reset_provider_registry, set_provider_registry

        async def _wedged():
            await asyncio.sleep(30)

        self._isolate_probe_state(monkeypatch)
        monkeypatch.setattr(web, "_PROBE_TIMEOUT", 0.05)
        set_provider_registry(ProviderRegistry([self._stub_probe_provider(_wedged)]))
        try:
            shell = _DaemonShell(tmp_path, monkeypatch)
            monkeypatch.setattr(web, "daemon", shell)
            r = TestClient(web.app).get("/api/providers/probe", params={"name": "codex"})
            body = r.json()
            assert body["available"] is False
            assert "timed out" in body["detail"]
        finally:
            reset_provider_registry()

    def test_codex_probe_result_is_cached_with_ttl(self, tmp_path, monkeypatch):
        # Every cold probe spawns a codex CLI handshake — repeat requests
        # inside the TTL must be served from cache, and expiry re-probes.
        from salient_core import ProviderRegistry, reset_provider_registry, set_provider_registry
        from salient_core.providers import ProviderProbe

        async def _available():
            return ProviderProbe(True, "codex 1.2.3, authenticated")

        self._isolate_probe_state(monkeypatch)
        provider = self._stub_probe_provider(_available)
        set_provider_registry(ProviderRegistry([provider]))
        try:
            shell = _DaemonShell(tmp_path, monkeypatch)
            monkeypatch.setattr(web, "daemon", shell)
            client = TestClient(web.app)
            for _ in range(3):
                assert client.get("/api/providers/probe").json()["available"] is True
            assert provider.probe_calls == 1
            monkeypatch.setattr(web, "_PROBE_TTL", 0.0)  # force expiry
            assert client.get("/api/providers/probe").json()["available"] is True
            assert provider.probe_calls == 2
        finally:
            reset_provider_registry()

    def test_codex_probe_concurrent_callers_share_one_probe(self, tmp_path, monkeypatch):
        # Single-flight: N concurrent requests (an Agents-tab render across
        # browser tabs) must spawn ONE probe, not stack N subprocesses.
        import asyncio

        from salient_core import ProviderRegistry, reset_provider_registry, set_provider_registry
        from salient_core.providers import ProviderProbe

        async def _slow_available():
            await asyncio.sleep(0.05)
            return ProviderProbe(True, "codex 1.2.3, authenticated")

        self._isolate_probe_state(monkeypatch)
        provider = self._stub_probe_provider(_slow_available)
        set_provider_registry(ProviderRegistry([provider]))
        try:
            shell = _DaemonShell(tmp_path, monkeypatch)
            monkeypatch.setattr(web, "daemon", shell)

            async def scenario():
                return await asyncio.gather(*(web.provider_probe("codex") for _ in range(5)))

            results = asyncio.run(scenario())
            assert all(r["available"] is True for r in results)
            assert provider.probe_calls == 1
        finally:
            reset_provider_registry()

    def test_post_sets_agent(self, tmp_path, monkeypatch):
        shell = _DaemonShell(tmp_path, monkeypatch)
        monkeypatch.setattr(web, "daemon", shell)
        r = TestClient(web.app).post(
            "/api/agents/config",
            json={
                "agent": "tutor",
                "provider": "minimax",
                "base_url": "http://mm",
                "model": "MiniMax-M3",
                "effort": "high",
            },
        )
        assert r.status_code == 200 and r.json()["provider"] == "minimax"

    def test_local_blank_model_autofills_single_loaded(self, tmp_path, monkeypatch):
        # Local + blank model → auto-fill from the single loaded LM Studio chat
        # model instead of erroring 'model required'.
        shell = _DaemonShell(tmp_path, monkeypatch)
        monkeypatch.setattr(web, "daemon", shell)

        async def _fake_loaded(base_url, api_key):
            return ["glm-4.6v-flash"]

        monkeypatch.setattr(web, "_lms_loaded_chat_models", _fake_loaded)
        r = TestClient(web.app).post(
            "/api/agents/config",
            json={"agent": "librarian", "provider": "local", "base_url": "http://lms"},
        )
        body = r.json()
        assert r.status_code == 200 and body.get("provider") == "local"
        assert body["model"] == "glm-4.6v-flash"

    def test_local_blank_model_ambiguous_still_errors(self, tmp_path, monkeypatch):
        # Two loaded models → can't pick; fall through to the 'model required' error.
        shell = _DaemonShell(tmp_path, monkeypatch)
        monkeypatch.setattr(web, "daemon", shell)

        async def _fake_loaded(base_url, api_key):
            return ["a", "b"]

        monkeypatch.setattr(web, "_lms_loaded_chat_models", _fake_loaded)
        r = TestClient(web.app).post(
            "/api/agents/config",
            json={"agent": "librarian", "provider": "local", "base_url": "http://lms"},
        )
        assert "error" in r.json()


# ── Subject routing ────────────────────────────────────────────────────────
class TestSubjectRouting:
    def test_study_create_with_subject(self, tmp_path, monkeypatch):
        from salient_tutor.study import load_study, new_study, save_study

        monkeypatch.setenv("SALIENT_TUTOR_WORK_ROOT", str(tmp_path))

        class _Ctx:
            def __init__(self):
                self.kv = {}

            def meta_get(self, k):
                return self.kv.get(k)

            def meta_set(self, k, v):
                self.kv[k] = v

            def meta_delete(self, k):
                self.kv.pop(k, None)

            def meta_keys(self, p=""):
                return [k for k in self.kv if k.startswith(p)]

        ctx = _Ctx()
        save_study(ctx, new_study("bio", "Cell bio", subject="biology"))
        s = load_study(ctx, "bio")
        assert s["subject"] == "biology"

    def test_invalid_subject_defaults_to_cyber(self, tmp_path, monkeypatch):
        from salient_tutor.study import new_study

        monkeypatch.setenv("SALIENT_TUTOR_WORK_ROOT", str(tmp_path))
        s = new_study("x", "X", subject="nonsense")
        assert s["subject"] == "cyber"
