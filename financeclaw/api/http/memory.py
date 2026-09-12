"""Authenticated memory management and independent candidate decisions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from financeclaw.api.http.auth import principal_dependency
from financeclaw.shared.infrastructure.asyncio import run_sync


class MemorySettingsRequest(BaseModel):
    """Change owner policy under the revision displayed in settings."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    read_enabled: bool | None = None
    auto_extract: bool | None = None


class MemoryWriteRequest(BaseModel):
    """An authenticated user's explicit content, without caller-selected ownership."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["profile", "task"]
    field: str | None = Field(default=None, max_length=64)
    content: str = Field(min_length=1, max_length=2000)
    scope_type: Literal["user", "agent", "conversation"] = "user"
    scope_id: str = Field(default="", max_length=128)
    valid_until: AwareDatetime | None = None


class MemoryEditRequest(MemoryWriteRequest):
    """Replace exactly the version that the user reviewed."""

    expected_revision: int = Field(ge=1)


class MemoryDeleteRequest(BaseModel):
    """Forget a specific reviewed memory, keeping only its replay barrier."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class MemoryDecisionRequest(BaseModel):
    """Bind confirmation to immutable candidate content and revision."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    expected_revision: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def memory_router(service, authenticator):
    """Expose governed SQL facts without native Store or Turn coordinates."""
    router = APIRouter(prefix="/v1")
    dependency = principal_dependency(authenticator)
    Principal = Annotated[object, Depends(dependency)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)]

    @router.get("/memory/settings")
    async def settings(user: Principal):
        """Read the effective owner policy and its revision."""
        return await run_sync(service.settings, user)

    @router.patch("/memory/settings")
    async def update_settings(request: MemorySettingsRequest, user: Principal, key: Key):
        """Persist policy and revoke pending derivation when automation is disabled."""
        return await run_sync(service.update_settings, user, request, key=key)

    @router.get("/memories")
    async def memories(
        user: Principal,
        kind: Literal["profile", "task"] | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=128),
        status: Literal[
            "active", "proposed", "approved", "rejected", "expired", "superseded"
        ] = "active",
        scope_type: Literal["user", "agent", "conversation"] | None = None,
        scope_id: str | None = Query(default=None, max_length=128),
    ):
        """List current owned facts using bounded keyset pagination."""
        return await run_sync(
            service.list,
            user,
            kind=kind,
            limit=limit,
            after=after,
            status=status,
            scope_type=scope_type,
            scope_id=scope_id,
        )

    @router.get("/memories/{memory_id}")
    async def memory(memory_id: str, user: Principal):
        """Read one owned current memory and its evidence references."""
        return await run_sync(service.get, user, memory_id)

    @router.post("/memories", status_code=201)
    async def create(request: MemoryWriteRequest, user: Principal, key: Key):
        """Save explicit user content with a permanent idempotency receipt."""
        return await run_sync(service.write, user, request, key=key)

    @router.patch("/memories/{memory_id}")
    async def edit(memory_id: str, request: MemoryEditRequest, user: Principal, key: Key):
        """Correct a memory without silently overwriting a concurrent update."""
        return await run_sync(service.write, user, request, key=key, memory_id=memory_id)

    @router.delete("/memories/{memory_id}")
    async def forget(memory_id: str, request: MemoryDeleteRequest, user: Principal, key: Key):
        """Make forgetting effective before asynchronous index cleanup."""
        return await run_sync(service.forget, user, memory_id, request, key=key)

    @router.get("/memory/candidates")
    async def candidates(
        user: Principal,
        limit: int = Query(default=50, ge=1, le=100),
        after: str | None = Query(default=None, max_length=128),
    ):
        """Read proposals independently of the originating conversation status."""
        return await run_sync(service.list, user, candidates=True, limit=limit, after=after)

    @router.post("/memory/candidates/{candidate_id}/decision")
    async def decide(candidate_id: str, request: MemoryDecisionRequest, user: Principal, key: Key):
        """Commit a reviewed candidate without creating a native resume command."""
        return await run_sync(service.decide, user, candidate_id, request, key=key)

    return router
