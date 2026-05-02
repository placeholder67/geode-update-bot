import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# config
# =========================

token = os.getenv("DISCORD_TOKEN")

api_url = "https://api.geode-sdk.org/v1/mods/{}"
state_file = Path("geode_version_state.json")
check_interval_minutes = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("geode-bot")

version_re = re.compile(r"^\s*v?(\d+(?:\.\d+)+(?:[-+][\w.]+)?)\s*$", re.IGNORECASE)

# =========================
# tracked mods
# =========================

@dataclass(frozen=True)
class TrackedMod:
    id: str
    label: str
    emoji: str


tracked_mods = (
    TrackedMod("axiom.echochoke", "EchoChoke", "🟣"),
    TrackedMod("axiom.echoclip", "EchoClip", "🔴"),
    TrackedMod("axiom.voicecontrol", "Voice Control", "🔵"),
    TrackedMod("axiom.cube-abuse", "Cube Abuse", "🟡"),
)

# =========================
# helpers
# =========================

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def first_text(data: dict, keys):
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def unwrap(data):
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}


# =========================
# FIXED pending detection
# =========================

def is_pending(data: dict) -> bool:
    if not isinstance(data, dict):
        return False

    # direct flags
    for k in ("pending", "isPending", "is_pending"):
        if isinstance(data.get(k), bool):
            return data[k]

    # status string detection
    status = first_text(data, ("status", "state"))
    if status and "pending" in status.lower():
        return True

    # tags / categories
    tags = data.get("tags") or data.get("categories")
    if isinstance(tags, list):
        joined = " ".join(map(str, tags)).lower()
        if "pending" in joined or "beta" in joined:
            return True

    # description / changelog fallback
    text = first_text(data, ("changelog", "description", "notes"))
    if text and ("pending" in text.lower() or "not released" in text.lower()):
        return True

    return False


def is_released(data: dict, pending: bool, version: Optional[str]) -> bool:
    if isinstance(data.get("released"), bool):
        return data["released"]
    return bool(version) and not pending


def extract(mod: TrackedMod, data: dict):
    name = first_text(data, ("name", "title")) or mod.label
    version = first_text(data, ("version", "latestVersion", "currentVersion"))

    pending = is_pending(data)
    released = is_released(data, pending, version)

    return {
        "id": mod.id,
        "name": name,
        "version": version or "unknown",
        "pending": pending,
        "released": released,
        "raw": data,
    }


# =========================
# bot
# =========================

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        self.session: Optional[aiohttp.ClientSession] = None
        self.last = {}

        self.state = self.load_state()

    # -------------------------
    # lifecycle
    # -------------------------

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()

        # IMPORTANT: this is what fixes "commands don't exist"
        await self.tree.sync()
        log.info("slash commands synced")

        self.poll.start()

    async def on_ready(self):
        log.info(f"logged in as {self.user}")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    # -------------------------
    # api
    # -------------------------

    async def fetch_one(self, mod: TrackedMod):
        try:
            async with self.session.get(api_url.format(mod.id)) as r:
                data = await r.json(content_type=None)
                data = unwrap(data)
                return mod.id, extract(mod, data)
        except Exception as e:
            return mod.id, {
                "id": mod.id,
                "name": mod.label,
                "version": "error",
                "pending": False,
                "released": False,
                "error": str(e),
            }

    async def fetch_all(self):
        return dict(await asyncio.gather(*(self.fetch_one(m) for m in tracked_mods)))

    # -------------------------
    # state
    # -------------------------

    def load_state(self):
        if not state_file.exists():
            return {"mods": {}}
        try:
            return json.loads(state_file.read_text())
        except:
            return {"mods": {}}

    def save_state(self):
        state_file.write_text(json.dumps(self.state, indent=2))

    # -------------------------
    # loop
    # -------------------------

    @tasks.loop(minutes=check_interval_minutes)
    async def poll(self):
        snaps = await self.fetch_all()

        mods = self.state.setdefault("mods", {})

        for k, v in snaps.items():
            if v.get("pending"):
                continue

            old = mods.get(k)
            if not old or old.get("version") != v.get("version"):
                mods[k] = {
                    "version": v.get("version"),
                    "saved_at": utc_now(),
                }

        self.save_state()
        self.last = snaps

    # -------------------------
    # embeds
    # -------------------------

    def build_embed(self, snaps):
        e = discord.Embed(
            title="geode mod tracker",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []
        for m in tracked_mods:
            s = snaps.get(m.id, {})
            status = "⏳ pending" if s.get("pending") else "✅ released"
            lines.append(f"{m.emoji} **{m.label}** — `{s.get('version')}` • {status}")

        e.description = "\n".join(lines)
        return e


bot = Bot()

# =========================
# slash commands (FIXED)
# =========================

@bot.tree.command(name="checkforupdates", description="check geode mods")
async def checkforupdates(interaction: discord.Interaction):
    await interaction.response.defer()

    snaps = await bot.fetch_all()
    await interaction.followup.send(embed=bot.build_embed(snaps))


@bot.tree.command(name="debugmods", description="raw api debug")
async def debugmods(interaction: discord.Interaction):
    await interaction.response.defer()

    snaps = await bot.fetch_all()
    await interaction.followup.send(
        "\n".join(f"{k}: {v.get('raw')}" for k, v in snaps.items())[:1900]
    )


@bot.tree.command(name="debugstate", description="saved json state")
async def debugstate(interaction: discord.Interaction):
    await interaction.response.defer()

    await interaction.followup.send(
        f"```json\n{json.dumps(bot.state, indent=2)[:1900]}```"
    )


# =========================
# run
# =========================

def main():
    if not token:
        raise RuntimeError("missing DISCORD_TOKEN")
    bot.run(token)


if __name__ == "__main__":
    main()
