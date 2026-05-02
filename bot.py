import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands

# =========================
# CONFIG
# =========================

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")

# =========================
# MODS
# =========================

@dataclass(frozen=True)
class Mod:
    id: str
    name: str
    emoji: str


MODS = (
    Mod("axiom.echochoke", "EchoChoke", "🟣"),
    Mod("axiom.echoclip", "EchoClip", "🔴"),
    Mod("axiom.voicecontrol", "Voice Control", "🔵"),
    Mod("axiom.cube-abuse", "Cube Abuse", "🟡"),
)


# =========================
# HELPERS
# =========================

def unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}


def get_text(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


# =========================
# 🔥 REAL PENDING DETECTION (FIXED)
# =========================

def is_pending(d: dict) -> bool:
    if not isinstance(d, dict):
        return False

    # 1. explicit flags
    for k in ("pending", "isPending", "is_pending"):
        if d.get(k) is True:
            return True

    # 2. status / state tells the truth
    status = get_text(d, ("status", "state"))
    if status:
        if any(x in status for x in ("pending", "unlisted", "not indexed", "indexing", "review")):
            return True

    # 3. index presence logic (IMPORTANT FIX FOR YOUR CASE)
    # if mod is not "released" but exists → still pending
    released = d.get("released")
    if released is False:
        return True

    # 4. api hint fields
    if d.get("listed") is False:
        return True

    if d.get("indexed") is False:
        return True

    return False


# =========================
# VERSION (ONLY FOR DISPLAY)
# =========================

def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None

    for k in ("version", "latestVersion", "currentVersion"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return None


# =========================
# BOT
# =========================

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        log.info("slash commands synced")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    # =========================
    # FETCH
    # =========================

    async def fetch_mod(self, mod: Mod):
        try:
            async with self.session.get(api_url.format(mod.id)) as r:
                data = unwrap(await r.json(content_type=None))

                version = find_version(data)
                pending = is_pending(data)

                return {
                    "mod": mod,
                    "version": version or "unknown",
                    "pending": pending,
                }

        except Exception as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "error": str(e),
            }

    async def fetch_all(self):
        return await asyncio.gather(*(self.fetch_mod(m) for m in MODS))

    # =========================
    # EMBED
    # =========================

    def build_embed(self, results):
        e = discord.Embed(
            title="geode version checker",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []

        for r in results:
            m = r["mod"]

            if r["pending"]:
                status = "⏳ pending (not indexed)"
            elif r["version"] == "error":
                status = "❌ error"
            else:
                status = "✅ indexed"

            lines.append(
                f"{m.emoji} **{m.name}** — `{r['version']}` • {status}"
            )

        e.description = "\n".join(lines)
        return e


bot = Bot()


# =========================
# COMMANDS
# =========================

@bot.tree.command(name="checkforupdates", description="live geode mod status")
async def checkforupdates(interaction: discord.Interaction):
    await interaction.response.defer()

    data = await bot.fetch_all()
    await interaction.followup.send(embed=bot.build_embed(data))


@bot.tree.command(name="debugmods", description="raw api output")
async def debugmods(interaction: discord.Interaction):
    await interaction.response.defer()

    data = await bot.fetch_all()
    await interaction.followup.send(
        "\n\n".join(f"{r['mod'].name}: {r}" for r in data)[:1900]
    )


# =========================
# RUN
# =========================

def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")
    bot.run(token)


if __name__ == "__main__":
    main()
