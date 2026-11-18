"""
Muse assistant — a chat front end over the data Muse already has.

The user talks to one bot; the bot answers by calling CONNECTORS, small
read-mostly adapters over things this PC can already reach:

    cases   the user's IPS cases and what Muse has done with them
    mail    the user's own Outlook (COM, this PC only)
    echo    the Echo knowledge base (Intel-internal answers)
    tasks   "at 2 pm do X" - instructions run later, by the same bot

    registry.py     Connector / Ctx, the registry and run_connector
    agent.py        AssistantAgent: the tool-calling loop
    prompts.py      the system prompt
    connectors/     one module per connector family

What the bot may do: look things up, ask Echo, start a Muse analysis of a
case (which queues a draft for review). What it may never do: write to IPS or
send mail - that stays behind the reviewer's Approve on the Muse pages.

Everything runs on the user's own PC, under their own IPS sign-in and their
own mailbox; the routes (blueprints/assistant) answer this machine only.
"""

DOMAIN = "assistant"          # history / job / Gather partition key
