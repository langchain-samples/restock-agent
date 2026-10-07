"""Optional native Slack channel. Include with scripts/setup.py --slack."""

from managed_deepagents import channels

channel = channels.slack(
    name="Restock",
    description="Find office supplies, review your budget, and approve payment with Link.",
    trigger_on_all_messages=False,
    allow_bot_triggers=False,
)
