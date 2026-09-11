"""Read-only, privacy-filtered operator endpoints used only by dashboard BFF."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import IdempotencyKey, PromptVersion, Team, TeamBudget, UsageRecord
from app.security.admin_auth import require_admin

router = APIRouter(prefix="/admin/v1", tags=["admin"], dependencies=[Depends(require_admin)])


def money(value: Decimal | None) -> str:
    return format(value or Decimal("0"), ".6f")


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict:
    today = date.today()
    active_budgets = db.execute(
        select(TeamBudget.allocated_amount, TeamBudget.remaining_amount).where(
            TeamBudget.period_start <= today, TeamBudget.period_end > today
        )
    ).all()
    successful, failed = db.execute(
        select(
            func.count().filter(UsageRecord.status == "succeeded"),
            func.count().filter(UsageRecord.status == "failed"),
        )
    ).one()
    return {
        "active_teams": db.scalar(select(func.count()).select_from(Team).where(Team.is_active.is_(True))) or 0,
        "current_budget_allocated_usd": money(sum((row[0] for row in active_budgets), Decimal("0"))),
        "current_budget_remaining_usd": money(sum((row[1] for row in active_budgets), Decimal("0"))),
        "usage_succeeded": successful or 0,
        "usage_failed": failed or 0,
    }


@router.get("/teams")
def teams(limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db)) -> dict:
    today = date.today()
    budgets = (
        select(TeamBudget)
        .where(TeamBudget.period_start <= today, TeamBudget.period_end > today)
        .subquery()
    )
    rows = db.execute(
        select(Team.name, Team.is_active, Team.created_at, budgets.c.allocated_amount, budgets.c.remaining_amount)
        .outerjoin(budgets, Team.id == budgets.c.team_id)
        .order_by(Team.name.asc()).limit(limit)
    ).all()
    return {"items": [
        {"name": name, "active": active, "created_at": created_at.isoformat() if created_at else None,
         "budget_allocated_usd": money(allocated), "budget_remaining_usd": money(remaining)}
        for name, active, created_at, allocated, remaining in rows
    ]}


@router.get("/usage")
def usage(limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(Team.name, UsageRecord.final_provider, UsageRecord.final_model, UsageRecord.status,
               UsageRecord.actual_cost, UsageRecord.prompt_tokens, UsageRecord.completion_tokens,
               UsageRecord.created_at)
        .join(IdempotencyKey, IdempotencyKey.id == UsageRecord.idempotency_key_id)
        .join(Team, Team.id == IdempotencyKey.team_id)
        .order_by(UsageRecord.created_at.desc()).limit(limit)
    ).all()
    return {"items": [
        {"team_name": team_name, "provider": provider, "model": model, "status": status.value,
         "actual_cost_usd": money(cost), "prompt_tokens": prompt_tokens,
         "completion_tokens": completion_tokens, "created_at": created_at.isoformat() if created_at else None}
        for team_name, provider, model, status, cost, prompt_tokens, completion_tokens, created_at in rows
    ]}


@router.get("/prompt-versions")
def prompt_versions(limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(PromptVersion.name, PromptVersion.version, PromptVersion.status,
               PromptVersion.created_at, PromptVersion.approved_at, PromptVersion.retired_at)
        .order_by(PromptVersion.name.asc(), PromptVersion.version.desc()).limit(limit)
    ).all()
    return {"items": [
        {"name": name, "version": version, "status": status, "created_at": created.isoformat() if created else None,
         "approved_at": approved.isoformat() if approved else None, "retired_at": retired.isoformat() if retired else None}
        for name, version, status, created, approved, retired in rows
    ]}


@router.get("/provider-status")
def provider_status() -> dict:
    # Circuit-breaker state is deliberately omitted: it is process-local, so a
    # multi-instance dashboard value would be misleading. Metrics/traces are
    # the authoritative multi-instance observability path.
    return {"rate_limiting": "redis", "circuit_breaker": "per_process", "provider": "openai"}
