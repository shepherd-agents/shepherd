"""Tests for Tier 2: ATIF export/import."""

from __future__ import annotations

import json

import pytest
from shepherd_core.effects import (
    AgentMessage,
    AgentThinking,
    PromptSent,
    TaskCompleted,
    TaskFailed,
    TaskStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from shepherd_core.scope.stream import Stream

try:
    from harbor.models.trajectories import Trajectory
    from shepherd_export.atif import from_atif, from_atif_json, to_atif, to_atif_json

    HAS_HARBOR = True
except ImportError:
    HAS_HARBOR = False

pytestmark = pytest.mark.skipif(not HAS_HARBOR, reason="Harbor not installed")


def _sample_stream() -> Stream:
    stream = Stream()
    stream = stream.append(TaskStarted(task_name="FixBug"))
    stream = stream.append(PromptSent(user_prompt="Fix the auth bug"))
    stream = stream.append(AgentThinking(content="Let me check the auth module"))
    stream = stream.append(AgentMessage(content="I'll fix the authentication issue."))
    stream = stream.append(ToolCallStarted(tool_call_id="tc1", tool_name="read_file", params={"path": "auth.py"}))
    stream = stream.append(ToolCallCompleted(tool_call_id="tc1", tool_name="read_file", output="def login(): ..."))
    stream = stream.append(AgentMessage(content="Found the bug. Fixing now."))
    stream = stream.append(
        ToolCallStarted(tool_call_id="tc2", tool_name="write_file", params={"path": "auth.py", "content": "..."})
    )
    stream = stream.append(ToolCallCompleted(tool_call_id="tc2", tool_name="write_file", output="File written"))
    return stream.append(TaskCompleted(task_name="FixBug", duration_ms=2000.0))


class TestToAtif:
    def test_produces_valid_trajectory(self):
        stream = _sample_stream()
        atif_dict = to_atif(stream, session_id="test-123")

        trajectory = Trajectory.model_validate(atif_dict)
        assert trajectory.session_id == "test-123"
        assert len(trajectory.steps) > 0

    def test_step_sources(self):
        stream = _sample_stream()
        atif_dict = to_atif(stream)

        sources = [step["source"] for step in atif_dict["steps"]]
        assert "system" in sources
        assert "user" in sources
        assert "agent" in sources

    def test_tool_calls_grouped_with_agent(self):
        stream = _sample_stream()
        atif_dict = to_atif(stream)

        agent_steps = [step for step in atif_dict["steps"] if step["source"] == "agent"]
        assert any("tool_calls" in step for step in agent_steps)

    def test_reasoning_content_included(self):
        stream = _sample_stream()
        atif_dict = to_atif(stream)

        agent_steps = [step for step in atif_dict["steps"] if step["source"] == "agent"]
        assert any(step.get("reasoning_content") for step in agent_steps)

    def test_observations_linked(self):
        stream = _sample_stream()
        atif_dict = to_atif(stream)

        agent_steps = [step for step in atif_dict["steps"] if step["source"] == "agent"]
        for step in agent_steps:
            if "tool_calls" in step and "observation" in step:
                tool_call_ids = {tool_call["tool_call_id"] for tool_call in step["tool_calls"]}
                observation_ids = {result["source_call_id"] for result in step["observation"]["results"]}
                assert observation_ids.issubset(tool_call_ids)

    def test_empty_stream_produces_single_step(self):
        atif_dict = to_atif(Stream())
        assert len(atif_dict["steps"]) == 1
        assert atif_dict["steps"][0]["source"] == "system"

    def test_agent_metadata(self):
        stream = _sample_stream()
        atif_dict = to_atif(stream, agent_name="my-agent", agent_version="1.0", model_name="claude")

        assert atif_dict["agent"]["name"] == "my-agent"
        assert atif_dict["agent"]["version"] == "1.0"
        assert atif_dict["agent"]["model_name"] == "claude"

    def test_task_failed_mapped(self):
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="Broken"))
        stream = stream.append(TaskFailed(task_name="Broken", error="segfault"))

        atif_dict = to_atif(stream)
        system_steps = [step for step in atif_dict["steps"] if step["source"] == "system"]
        messages = [step["message"] for step in system_steps]
        assert any("failed" in message.lower() for message in messages)


class TestToAtifJson:
    def test_produces_valid_json(self):
        stream = _sample_stream()
        json_str = to_atif_json(stream)
        parsed = json.loads(json_str)
        assert "steps" in parsed
        assert "agent" in parsed


class TestFromAtif:
    def test_user_step_to_prompt(self):
        imported = from_atif(to_atif(_sample_stream()))

        prompts = [layer for layer in imported if layer.effect.effect_type == "prompt_sent"]
        assert len(prompts) >= 1

    def test_agent_step_to_message(self):
        imported = from_atif(to_atif(_sample_stream()))

        messages = [layer for layer in imported if layer.effect.effect_type == "agent_message"]
        assert len(messages) >= 1

    def test_tool_calls_reconstructed(self):
        imported = from_atif(to_atif(_sample_stream()))

        started = [layer for layer in imported if layer.effect.effect_type == "tool_call_started"]
        completed = [layer for layer in imported if layer.effect.effect_type == "tool_call_completed"]
        assert len(started) >= 1
        assert len(completed) >= 1
        assert len(started) == len(completed)

    def test_reasoning_reconstructed(self):
        imported = from_atif(to_atif(_sample_stream()))

        thinking = [layer for layer in imported if layer.effect.effect_type == "agent_thinking"]
        assert len(thinking) >= 1

    def test_system_step_heuristic(self):
        imported = from_atif(to_atif(_sample_stream()))

        started = [layer for layer in imported if layer.effect.effect_type == "task_started"]
        completed = [layer for layer in imported if layer.effect.effect_type == "task_completed"]
        assert len(started) >= 1
        assert len(completed) >= 1


class TestFromAtifJson:
    def test_from_json_string(self):
        imported = from_atif_json(to_atif_json(_sample_stream()))
        assert len(imported) > 0


class TestRoundTrip:
    def test_effect_types_preserved(self):
        original = _sample_stream()
        imported = from_atif(to_atif(original))

        original_types = {layer.effect.effect_type for layer in original}
        imported_types = {layer.effect.effect_type for layer in imported}

        for expected in ["prompt_sent", "agent_message", "tool_call_started", "tool_call_completed"]:
            if expected in original_types:
                assert expected in imported_types, f"{expected} lost in round-trip"
