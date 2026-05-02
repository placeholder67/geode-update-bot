import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# =========================
# CONFIG
# =========================

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")

version_re = re.compile(r"v?(\d+(?:\.\d+)+)", re.IGNORECASE)

# =========================
# MOD LIST (ONLY 4)
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

def get_text(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}


# =========================
# 🔥 FIXED VERSION DETECTION
# =========================

def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None

    # direct fields
    for k in ("version", "latestVersion", "currentVersion", "modVersion"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # nested search
    for v in d.values():
        if isinstance(v, dict):
            res = find_version(v)
            if res:
                return res

    # changelog fallback
    text = get_text(d, ("changelog", "description", "notes"))
    if text:
        m = version_re.search(text)
        if m:
            return m.group(1)

    return None


# =========================
# 🔥 FIXED PENDING DETECTION (IMPORTANT PART)
# =========================

def is_pending(d: dict) -> bool:
    if not isinstance(d, dict):
        return False

    # explicit flags
    for k in ("pending", "isPending", "is_pending"):
        if isinstance(d.get(k), bool):
            return d[k]

    status = get_text(d, ("status", "state"))
    if status and status.lower() in {"pending", "in review", "review"}:
        return True

    tags = d.get("tags") or d.get("categories")
    if isinstance(tags, list):
        joined = " ".join(map(str, tags)).lower()
        if any(x in joined for x in ("pending", "beta", "wip", "review")):
            return True

    text = get_text(d, ("description", "changelog", "notes"))
    if text and any(x in text.lower() for x in ("pending", "not released", "in review")):
        return True

    return False


def is_released(version: Optional[str], pending: bool) -> bool:
    return bool(version) and not pending


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

                # 🔥 KEY FIX: echoclip becomes pending even if it has version 1.5.0
                released = is_released(version, pending)

                return {
                    "mod": mod,
                    "version": version or "unknown",
                    "pending": pending,
                    "released": released,
                }

        except Exception as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "released": False,
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

            if r.get("pending"):
                status = "⏳ pending"
            elif r["version"] in ("unknown", "error"):
                status = "❓ unknown"
            else:
                status = "✅ released"

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


@bot.tree.command(name="debugmods", description="raw api debug")
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
        raise RuntimeError("missing DISCORD_TOKEN")
    bot.run(token)


if __name__ == "__main__":
    main()
