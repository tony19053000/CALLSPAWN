"""Explicit approval records: the only path to an authorized call."""

from callswarm.approvals.service import (
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalService,
    Decision,
)

__all__ = ["ApprovalNotFound", "ApprovalNotPending", "ApprovalService", "Decision"]
