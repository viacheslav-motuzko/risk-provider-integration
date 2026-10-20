from django.db import models
from django.db.models.functions import Now


class BetState(models.TextChoices):
    PENDING_RISK = "PENDING_RISK"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    VOIDED = "VOIDED"


class Bet(models.Model):
    """STUB: the monolith's bet, reduced to a single bet on one selection."""

    customer_ref = models.CharField(max_length=64)
    selection_id = models.CharField(max_length=64)
    odds = models.DecimalField(max_digits=12, decimal_places=4)
    stake = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.CharField(max_length=3)
    state = models.CharField(max_length=16, choices=BetState.choices, default=BetState.PENDING_RISK)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(stake__gt=0), name="bet_stake_positive"),
            models.CheckConstraint(condition=models.Q(odds__gt=1), name="bet_odds_above_one"),
            models.CheckConstraint(condition=models.Q(state__in=BetState.values), name="bet_state_valid"),
        ]
