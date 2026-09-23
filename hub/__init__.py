"""Survey hub: the central, multi-user service behind the Emergence Review Kit.

What a single laptop can't do on its own:

* **locks**   one reviewer per recording at a time, and QA only by a different person
* **jobs**    a queue so prep and detection run on a server, not on survey laptops
* **anchors** a copy of each survey folder's audit-chain head, somewhere the reviewer
              can't edit, so a rewritten or truncated audit trail is detected
* **one record** of every survey and recording's state, per tenant (subsidiary)

PostgreSQL in production (``HUB_DATABASE_URL=postgresql://...``); SQLite for tests and
single-machine trials (``sqlite:///path/hub.db``). Sign-in is Microsoft Entra ID only:
every action is taken by a named person with an app role.

    uvicorn hub.api:app                 # API
    python -m hub.worker                # job runner(s), as many as needed
    python -m hub.backup create|restore|verify
"""

__version__ = "0.1.0"
