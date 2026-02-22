"""Project management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from bugalizer.auth import require_api_key
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
