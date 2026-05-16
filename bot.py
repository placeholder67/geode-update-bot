import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")

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

def unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}



def is_pending(d: dict) -> bool:
    if not isinstance(d, dict):
        return False

    status = d.get("versions")[0].get("status")
    if status == "accepted":
        return False
    else:
        return True


# find latest ver
def find_version(d: dict) -> Optional[str]:
    return d.get("versions")[0].get("version")



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

    # api call
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

    # embeds
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
                status = "⏳ Pending"
            elif r["version"] == "error":
                status = "❌ error"
            else:
                status = "✅ On the index"

            lines.append(
                f"{m.emoji} **{m.name}** — `{r['version']}` • {status}"
            )

        e.description = "\n".join(lines)
        return e


bot = Bot()


# cmds

@bot.tree.command(name="checkforupdates", description="live geode mod status")
async def checkforupdates(interaction: discord.Interaction):
    await interaction.response.defer()

    data = await bot.fetch_all()
    await interaction.followup.send(embed=bot.build_embed(data))

def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")
    bot.run(token)


if __name__ == "__main__":
    main()
