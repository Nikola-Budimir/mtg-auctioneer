import os
import re
import json
from urllib.parse import urljoin

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ROLE_ID = int(os.environ["ROLE_ID"])
URL = os.environ.get("WATCH_URL", "http://forum.mtgsrbija.rs/viewforum.php?f=17")
INTERVAL = int(os.environ.get("CHECK_MINUTES", "10"))
STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; auctioneer/1.0)"}
MAX_TOPICS_PER_ALERT = 10


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def parse_topics(html: str) -> list[dict]:
    """Extract regular (non-announcement) topics from a phpBB forum page.

    Only the topic ID, title, link and author are read. Views, reply counts,
    "posted X days ago" and last-post times are never looked at, so they
    cannot trigger alerts.
    """
    soup = BeautifulSoup(html, "html.parser")
    topics = []

    # "div.forumbg.announcement" holds pinned/global announcements; skip it.
    for row in soup.select("div.forumbg:not(.announcement) ul.topics > li.row"):
        link = row.select_one("a.topictitle")
        if link is None:
            continue
        match = re.search(r"[?&]t=(\d+)", link.get("href", ""))
        if not match:
            continue

        poster = row.select_one("div.topic-poster")
        author_tag = poster.select_one("a.username-coloured, a.username") if poster else None

        topics.append({
            "id": int(match.group(1)),
            "title": link.get_text(strip=True),
            "url": urljoin(URL, link["href"]),
            "author": author_tag.get_text(strip=True) if author_tag else "unknown",
        })
    return topics


async def fetch_topics(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(URL, headers=HEADERS,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        html = await resp.text()

    topics = parse_topics(html)
    if not topics:
        # Layout changed, or we got an error/maintenance page.
        raise ValueError("No topics found on page (layout changed or site down?)")
    return topics


class WatcherBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.check_forum.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    @tasks.loop(minutes=INTERVAL)
    async def check_forum(self):
        try:
            topics = await fetch_topics(self.session)
        except Exception as e:
            print(f"[check failed] {e}")
            return  # never alert on temporary errors

        state = load_state()
        newest_id = max(t["id"] for t in topics)

        # First run: remember the current newest topic, don't alert.
        if "max_id" not in state:
            save_state({"max_id": newest_id})
            print(f"Baseline saved (newest topic id = {newest_id}).")
            return

        # Topic IDs only ever go up, so "new topic" == "ID higher than any seen".
        # This also ignores old topics that jump back onto page 1 because
        # someone replied to them, or because another topic was deleted.
        new_topics = sorted(
            (t for t in topics if t["id"] > state["max_id"]),
            key=lambda t: t["id"],
        )
        if not new_topics:
            return

        shown = new_topics[:MAX_TOPICS_PER_ALERT]
        lines = [f"[{t['title']}]({t['url']}) — by **{t['author']}**" for t in shown]
        if len(new_topics) > len(shown):
            lines.append(f"…and {len(new_topics) - len(shown)} more")

        embed = discord.Embed(
            title="New auction topic" if len(new_topics) == 1 else "New auction topics",
            url=URL,
            description="\n".join(lines),
            color=discord.Color.green(),
        )

        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        await channel.send(
            content=f"<@&{ROLE_ID}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        # Only save after the message was sent successfully.
        save_state({"max_id": newest_id})

    @check_forum.before_loop
    async def before_check(self):
        await self.wait_until_ready()


bot = WatcherBot()
bot.run(TOKEN)
