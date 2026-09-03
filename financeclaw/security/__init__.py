"""Security primitives shared by trusted infrastructure adapters."""

from .egress import EgressDenied, EgressPolicy

__all__ = ["EgressDenied", "EgressPolicy"]
