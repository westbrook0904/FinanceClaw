"""Optional engine dependency failures explain how to repair the runtime environment."""

from importlib import metadata

import pytest

from financeclaw.agent_server.domains.ziwei.adapters.x_iztro import XIztroEngine


@pytest.mark.parametrize("missing", ["x-iztro", "tzdata"])
def test_missing_engine_dependency_explains_required_extra(monkeypatch, missing):
    """A missing distribution must name the extra and the affected runtime image."""

    def version(package):
        """Simulate either missing distribution independently of the developer environment."""
        if package == missing:
            raise metadata.PackageNotFoundError(package)
        return {"x-iztro": "0.4.0", "tzdata": "2026.3"}[package]

    monkeypatch.setattr(metadata, "version", version)
    with pytest.raises(RuntimeError, match="uv sync --extra ziwei") as error:
        XIztroEngine()
    assert missing in str(error.value)
    assert "Agent Server image" in str(error.value)
