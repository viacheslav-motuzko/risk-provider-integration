from django.db import models


class HoldState(models.TextChoices):
    HELD = "HELD"
    CAPTURED = "CAPTURED"
    RELEASED = "RELEASED"


class Wallet(models.Model):
    """STUB: the monolith's wallet, reduced to one balance per customer and currency."""

    customer_ref = models.CharField(max_length=64)
    currency = models.CharField(max_length=3)
    balance = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer_ref", "currency"], name="wallet_customer_currency_unique"),
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="wallet_balance_non_negative"),
        ]


class WalletHold(models.Model):
    """STUB: one per bet, the stake taken off the balance while the bet waits for its decision. No ledger."""

    # No foreign key: wallets does not depend on betting.
    bet_id = models.BigIntegerField(unique=True)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="holds")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    state = models.CharField(max_length=8, choices=HoldState.choices, default=HoldState.HELD)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="wallet_hold_amount_positive"),
            models.CheckConstraint(condition=models.Q(state__in=HoldState.values), name="wallet_hold_state_valid"),
        ]
