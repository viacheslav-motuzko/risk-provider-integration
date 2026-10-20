import json
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.betting.services import place_bet
from apps.wallets import services as wallets


class Command(BaseCommand):
    help = (
        "STUB demo: fund a wallet and place bets on the stub monolith; each bet gets a PENDING_SEND ticket for the "
        "integration process to send. A payout above the stub limit is rejected at acceptance."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--count", type=int, default=1)
        parser.add_argument("--stake", type=Decimal, default=Decimal("10.00"))
        parser.add_argument("--odds", type=Decimal, default=Decimal("1.85"))
        parser.add_argument("--currency", default="EUR")
        parser.add_argument("--customer", default="demo-customer")
        parser.add_argument("--selection", default="demo-selection")

    def handle(self, *args: Any, **options: Any) -> None:
        stake: Decimal = options["stake"]
        wallets.fund(customer_ref=options["customer"], currency=options["currency"], amount=stake * options["count"])
        for _ in range(options["count"]):
            placed = place_bet(
                customer_ref=options["customer"],
                selection_id=options["selection"],
                odds=options["odds"],
                stake=stake,
                currency=options["currency"],
            )
            self.stdout.write(json.dumps({"bet_id": placed.bet_id, "ticket_id": str(placed.ticket_id)}))
