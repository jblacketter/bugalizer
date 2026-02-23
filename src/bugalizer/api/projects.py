"""Project management endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from bugalizer.auth import require_api_key
from bugalizer.config import settings
from bugalizer.db import (
    project_create,
    project_delete,
    project_get,
    project_list,
    project_update,
)
from bugalizer.models import (
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
    RepoMapResponse,
)

router = APIRouter(tags=["projects"])


def _row_to_response(row: dict) -> ProjectResponse:
    return ProjectResponse(
        id=row["id"],
        name=row["name"],
        repo_url=row["repo_url"],
        repo_path=row.get("repo_path"),
        default_branch=row.get("default_branch", "main"),
        llm_provider=row.get("llm_provider", "ollama"),
        llm_model=row.get("llm_model", "qwen2.5-coder:7b"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("/projects", status_code=201)
def create_project(
    body: ProjectCreate,
    _key: str = Depends(require_api_key),
) -> ProjectResponse:
    """Register a new project for bug analysis."""
    row = project_create(
        name=body.name,
        repo_url=body.repo_url,
        default_branch=body.default_branch,
        llm_provider=body.llm_provider,
        llm_model=body.llm_model,
    )
    return _row_to_response(row)


@router.get("/projects")
def list_projects(
    _key: str = Depends(require_api_key),
) -> ProjectListResponse:
    """List all registered projects."""
    rows = project_list()
    return ProjectListResponse(
        projects=[_row_to_response(r) for r in rows],
        total=len(rows),
    )


@router.get("/projects/{project_id}")
def get_project(
    project_id: str,
    _key: str = Depends(require_api_key),
) -> ProjectResponse:
    """Get a single project by ID."""
    row = project_get(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return _row_to_response(row)


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    body: ProjectUpdate,
    _key: str = Depends(require_api_key),
) -> ProjectResponse:
    """Update project settings."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    row = project_update(project_id, **updates)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return _row_to_response(row)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    _key: str = Depends(require_api_key),
) -> None:
    """Remove a project. Returns 409 if bug reports still reference it."""
    result = project_delete(project_id)
    if result == "has_reports":
        raise HTTPException(
            status_code=409,
            detail="Cannot delete project with existing bug reports. Delete the reports first.",
        )
    if not result:
        raise HTTPException(status_code=404, detail="Project not found")


@router.post("/projects/{project_id}/clone")
async def clone_project_repo(
    project_id: str,
    _key: str = Depends(require_api_key),
) -> ProjectResponse:
    """Clone or update the project's git repository."""
    import os
    from bugalizer.git_ops.repo import clone_repo, get_head_sha

    project = project_get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    repo_url = project["repo_url"]
    branch = project.get("default_branch", "main")
    target_dir = os.path.join(settings.repos_dir, project_id)

    try:
        repo_path = await asyncio.to_thread(clone_repo, repo_url, target_dir, branch)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get HEAD SHA and update project with repo_path + head_sha
    head_sha = await asyncio.to_thread(get_head_sha, repo_path)
    updated = project_update(project_id, repo_path=repo_path, head_sha=head_sha)
    return _row_to_response(updated)


@router.post("/projects/{project_id}/refresh-map")
async def refresh_repo_map(
    project_id: str,
    _key: str = Depends(require_api_key),
) -> RepoMapResponse:
    """Force rebuild the project's repo map (pulls latest + re-parses)."""
    from bugalizer.git_ops.repo import pull_repo
    from bugalizer.pipeline.repo_map import build_repo_map, get_repo_map_cache

    project = project_get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.get("repo_path"):
        raise HTTPException(status_code=400, detail="Project repo not cloned. Run POST /clone first.")

    repo_path = project["repo_path"]
    branch = project.get("default_branch", "main")

    # Pull latest changes
    try:
        await asyncio.to_thread(pull_repo, repo_path)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"git pull failed: {e}")

    # Rebuild map (bypasses cache)
    repo_map = await asyncio.to_thread(build_repo_map, project_id, branch, repo_path)

    # Update project head_sha to reflect pulled state
    project_update(project_id, head_sha=repo_map.sha)

    # Store in cache
    cache = get_repo_map_cache()
    cache.put(repo_map)

    return RepoMapResponse(
        project_id=project_id,
        branch=branch,
        sha=repo_map.sha,
        file_count=len(repo_map.files),
        text=repo_map.text,
    )


@router.get("/projects/{project_id}/repo-map")
async def get_repo_map(
    project_id: str,
    _key: str = Depends(require_api_key),
) -> RepoMapResponse:
    """Get the current repo map for a project."""
    from bugalizer.pipeline.repo_map import get_repo_map_cache

    project = project_get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.get("repo_path"):
        raise HTTPException(status_code=400, detail="Project repo not cloned. Run POST /clone first.")

    repo_path = project["repo_path"]
    branch = project.get("default_branch", "main")

    cache = get_repo_map_cache()
    repo_map = await asyncio.to_thread(cache.get_or_build, project_id, branch, repo_path)

    return RepoMapResponse(
        project_id=project_id,
        branch=branch,
        sha=repo_map.sha,
        file_count=len(repo_map.files),
        text=repo_map.text,
    )
