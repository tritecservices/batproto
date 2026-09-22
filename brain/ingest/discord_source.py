"""Discord ingestion via an authorised bot (official API, discord.py).

Compliance notes, deliberately enforced in code:
  * Bot token only. Self-bots / user tokens are a ToS breach -- not supported here.
  * The bot must be invited by a server admin and given Read Message History.
  * The `message_content` privileged intent must be enabled in the Developer Portal,
    otherwise Message.content comes back empty.
  * `ALLOWED_GUILDS` acts as a whitelist so you cannot accidentally index a server
    you were not given permission to archive.
  * Discord's Developer Policy forbids using API data to train models. This stores
    text for retrieval (RAG) only; do not pipe it into fine-tuning.

Threads (forum posts) are archived as ONE document per thread, which is what you
actually want: the question plus the whole troubleshooting trail in one retrieval unit.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import discord

from ..sensitivity import classify_sensitivity
from ..store import Document, Store

MAX_DOC_CHARS = 60_000


def _allowed_guilds() -> set[int]:
    raw = os.environ.get("ALLOWED_GUILDS", "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x}


def _fmt(msg: discord.Message) -> str:
    who = msg.author.display_name or str(msg.author)
    body = (msg.content or "").strip()
    for a in msg.attachments:
        body += f"\n[attachment: {a.filename} {a.url}]"
    for e in msg.embeds:
        bits = [e.title or "", e.description or ""]
        body += "\n[embed: " + " | ".join(b for b in bits if b) + "]"
    ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
    return f"{who} ({ts}): {body}".strip()


class Archiver(discord.Client):
    def __init__(self, store: Store, days: int | None = None, full: bool = False):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.store = store
        self.days = days
        self.full = full
        self.stats = {"threads": 0, "channels": 0, "documents": 0, "unchanged": 0}

    async def on_ready(self) -> None:
        try:
            await self.archive_all()
        finally:
            await self.close()

    # ------------------------------------------------------------------ walk
    async def archive_all(self) -> None:
        allow = _allowed_guilds()
        for guild in self.guilds:
            if allow and guild.id not in allow:
                print(f"skip (not whitelisted): {guild.name}")
                continue
            print(f"guild: {guild.name}")
            for ch in guild.channels:
                if isinstance(ch, discord.ForumChannel):
                    await self._forum(guild, ch)
                elif isinstance(ch, discord.TextChannel):
                    await self._text_channel(guild, ch)

    async def _forum(self, guild, forum: discord.ForumChannel) -> None:
        if not forum.permissions_for(guild.me).read_message_history:
            return
        threads = list(forum.threads)
        try:
            async for t in forum.archived_threads(limit=None):
                threads.append(t)
        except discord.Forbidden:
            pass
        for t in threads:
            await self._thread(guild, forum, t)

    async def _thread(self, guild, forum, thread: discord.Thread) -> None:
        lines = []
        try:
            async for m in thread.history(limit=None, oldest_first=True):
                lines.append(_fmt(m))
        except discord.Forbidden:
            return
        if not lines:
            return
        self.stats["threads"] += 1
        text = "\n\n".join(lines)[:MAX_DOC_CHARS]
        tags = [t.name for t in getattr(thread, "applied_tags", [])]
        self._write(Document(
            id=f"discord:thread:{thread.id}",
            source="discord",
            origin=f"{guild.name}/{forum.name}",
            url=f"https://discord.com/channels/{guild.id}/{thread.id}",
            title=thread.name,
            author=str(thread.owner) if thread.owner else None,
            created_at=int(thread.created_at.timestamp()) if thread.created_at else None,
            tags=tags,
            meta={"guild_id": guild.id, "thread_id": thread.id, "messages": len(lines),
                  "archived": thread.archived},
            text=text,
        ))

    async def _text_channel(self, guild, ch: discord.TextChannel) -> None:
        """Rolling-window archive of a normal channel, batched into daily documents."""
        if not ch.permissions_for(guild.me).read_message_history:
            return
        cursor_key = f"discord:channel:{ch.id}:after"
        after = None
        if not self.full:
            saved = self.store.get_cursor(cursor_key)
            if saved:
                after = datetime.fromisoformat(saved)
            elif self.days:
                after = datetime.now(timezone.utc) - timedelta(days=self.days)
        buckets: dict[str, list[str]] = {}
        newest = after
        try:
            async for m in ch.history(limit=None, oldest_first=True, after=after):
                buckets.setdefault(m.created_at.strftime("%Y-%m-%d"), []).append(_fmt(m))
                newest = m.created_at
        except discord.Forbidden:
            return
        if not buckets:
            return
        self.stats["channels"] += 1
        for day, lines in buckets.items():
            doc_id = f"discord:channel:{ch.id}:{day}"
            # Resuming from a cursor mid-day: the bucket only holds messages AFTER the
            # cursor. Writing it as-is would replace the day's earlier messages, so
            # append to what is already stored for that day.
            if after is not None and (prev := self.store.get(doc_id)):
                lines = [prev["text"], *lines]
            self._write(Document(
                id=doc_id,
                source="discord",
                origin=f"{guild.name}/#{ch.name}",
                url=f"https://discord.com/channels/{guild.id}/{ch.id}",
                title=f"#{ch.name} — {day}",
                created_at=int(datetime.fromisoformat(day).timestamp()),
                meta={"guild_id": guild.id, "channel_id": ch.id, "messages": len(lines)},
                text="\n\n".join(lines)[:MAX_DOC_CHARS],
            ))
        if newest:
            self.store.set_cursor(cursor_key, newest.isoformat())

        # threads hanging off a text channel, including archived ones - most
        # troubleshooting threads are archived within a week of being solved
        threads = list(ch.threads)
        try:
            async for t in ch.archived_threads(limit=None):
                threads.append(t)
        except discord.Forbidden:
            pass
        for t in threads:
            await self._thread(guild, ch, t)

    def _write(self, doc: Document) -> None:
        doc.tags = list(dict.fromkeys([*doc.tags, *classify_sensitivity(doc.text)]))
        _, changed = self.store.upsert(doc)
        self.stats["documents" if changed else "unchanged"] += 1


def run(store: Store, days: int | None = 365, full: bool = False) -> dict:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set (bot tokens only, never a user token)")
    client = Archiver(store, days=days, full=full)
    asyncio.run(client.start(token))
    return client.stats
