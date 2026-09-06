from enum import StrEnum


class UsageStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"