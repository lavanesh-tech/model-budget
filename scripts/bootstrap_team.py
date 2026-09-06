import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import ApiKey, Team, TeamBudget
from app.security.api_keys import generate_api_key


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a team, its first budget period, and one API key."
        )
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Unique team name",
    )
    parser.add_argument(
        "--budget",
        required=True,
        help="Initial budget, for example 50.00",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=30,
        help="Budget period length in days (default: 30)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    team_name = args.name.strip()

    if not team_name:
        print("Error: --name must not be blank", file=sys.stderr)
        return 1

    try:
        allocated_amount = Decimal(args.budget)
    except InvalidOperation:
        print(
            f"Error: --budget {args.budget!r} is not a valid decimal",
            file=sys.stderr,
        )
        return 1

    if not allocated_amount.is_finite():
        print("Error: --budget must be a finite number", file=sys.stderr)
        return 1

    if allocated_amount < 0:
        print("Error: --budget must not be negative", file=sys.stderr)
        return 1

    if args.period_days <= 0:
        print("Error: --period-days must be positive", file=sys.stderr)
        return 1

    period_start = datetime.now(timezone.utc).date()
    period_end = period_start + timedelta(days=args.period_days)
    generated_key = generate_api_key()

    try:
        with SessionLocal() as session:
            with session.begin():
                team = Team(name=team_name)
                session.add(team)
                session.flush()

                team_id = team.id

                session.add(
                    TeamBudget(
                        team_id=team_id,
                        period_start=period_start,
                        period_end=period_end,
                        allocated_amount=allocated_amount,
                        remaining_amount=allocated_amount,
                    )
                )

                session.add(
                    ApiKey(
                        team_id=team_id,
                        public_key_id=generated_key.public_key_id,
                        name="bootstrap key",
                        key_prefix=generated_key.key_prefix,
                        secret_hash=generated_key.secret_hash,
                    )
                )
    except IntegrityError as exc:
        constraint_name = getattr(
            getattr(exc.orig, "diag", None),
            "constraint_name",
            None,
        )

        if constraint_name == "uq_teams_name":
            print(
                f"Error: team {team_name!r} already exists",
                file=sys.stderr,
            )
        else:
            print(
                "Error: database rejected the bootstrap data",
                file=sys.stderr,
            )

        return 1

    print("Team created successfully.")
    print(f"  team_id:       {team_id}")
    print(f"  name:          {team_name}")
    print(
        f"  budget:        {allocated_amount} "
        f"({period_start} to {period_end}, exclusive)"
    )
    print(f"  public_key_id: {generated_key.public_key_id}")
    print()
    print(
        "API key (shown once—store it securely because it cannot "
        "be retrieved again):"
    )
    print(generated_key.plaintext.get_secret_value())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())