"""Shared fixtures for verbose output tests."""

import io

import pytest
from shepherd_providers import VerboseConfig, VerboseFormatter


@pytest.fixture
def output_stream() -> io.StringIO:
    """StringIO fixture for capturing output."""
    return io.StringIO()


@pytest.fixture
def formatter(output_stream: io.StringIO) -> VerboseFormatter:
    """Create formatter with test output stream, colors/emoji disabled."""
    config = VerboseConfig(
        enabled=True,
        output=output_stream,
        use_color=False,
        use_emoji=False,
    )
    return VerboseFormatter(config)


@pytest.fixture
def formatter_all_enabled(output_stream: io.StringIO) -> VerboseFormatter:
    """Create formatter with all output enabled."""
    config = VerboseConfig(
        enabled=True,
        output=output_stream,
        use_color=False,
        use_emoji=False,
        show_tool_results=True,
        show_prompts=True,
        stream_partial=False,  # Show complete effects
    )
    return VerboseFormatter(config)
