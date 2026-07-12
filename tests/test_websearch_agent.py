"""The websearch roster agent — the one-shot web-lookup leaf that tutor.md's
KB-discipline step 5 delegates to via ``ask_agent("websearch", ...)``.

Until this agent existed, that prompt step named a target that wasn't in the
roster; these tests pin the contract so the prompt and the roster can't drift
apart again.
"""

from __future__ import annotations

from pathlib import Path

from salient_tutor.daemon import _PROVIDER_ENV, _build_agent_configs


def test_websearch_in_default_roster():
    configs = _build_agent_configs()
    ws = configs["websearch"]
    assert ws["system_prompt_file"] == "websearch.md"
    assert ws["builtin_tools"] == ["WebSearch", "WebFetch"]
    assert ws["bus_tools"] is False  # leaf agent — never delegates back
    assert ws["max_turns"] == 8


def test_websearch_prompt_file_matches_roster_contract():
    prompt = Path(__file__).resolve().parent.parent / "prompts" / "websearch.md"
    text = prompt.read_text()
    # The output shape the tutor consumes verbatim.
    assert "## Answer" in text
    assert "## Sources" in text
    assert "## Caveats" in text


def test_tutor_prompt_delegation_target_is_real():
    # tutor.md tells the tutor to ask_agent("websearch", ...) — the named
    # target must exist in the default roster.
    tutor_prompt = Path(__file__).resolve().parent.parent / "prompts" / "tutor.md"
    assert 'ask_agent("websearch"' in tutor_prompt.read_text()
    assert "websearch" in _build_agent_configs()


def test_websearch_env_provider_routable():
    # Startup env can route websearch to another provider like any agent.
    assert _PROVIDER_ENV["websearch"] == "TUTOR_WEBSEARCH_PROVIDER"
