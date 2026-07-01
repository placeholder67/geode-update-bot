"""
geode api so i dont forget: https://api.geode-sdk.org/swagger/
"""

import asyncio
import json
import logging
import os
import urllib.parse
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
    Mod("axiom.echoclip", "EchoClip", "🔵"),
    Mod("axiom.voicecontrol", "Voice Control", "⚫"),
    Mod("axiom.cube-abuse", "Cube Abuse", "🟡"),
    Mod("axiom.sticky-keys", "Sticky Keys", "🔴"),
    Mod("axiom.autoragequit", "AutoRageQuit", "🔵"),
)


def unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]

    return data if isinstance(data, dict) else {}


def is_pending(d: dict) -> bool:
    if not isinstance(d, dict):
        return False

    versions = d.get("versions")

    if not isinstance(versions, list) or not versions:
        return False

    first_version = versions[0]

    if not isinstance(first_version, dict):
        return False

    status = first_version.get("status")

    if status == "accepted":
        return False
    else:
        return True


def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None

    versions = d.get("versions")

    if not isinstance(versions, list) or not versions:
        return None

    first_version = versions[0]

    if not isinstance(first_version, dict):
        return None

    version = first_version.get("version")

    return version if isinstance(version, str) else None


def find_downloads(d: dict) -> Optional[int]:
    if not isinstance(d, dict):
        return None

    candidates = [
        d.get("downloads"),
        d.get("download_count"),
        d.get("downloads_total"),
    ]

    stats = d.get("stats")

    if isinstance(stats, dict):
        candidates.extend([
            stats.get("downloads"),
            stats.get("download_count"),
            stats.get("downloads_total"),
        ])

    for value in candidates:
        if isinstance(value, int):
            return value

        if isinstance(value, str) and value.isdigit():
            return int(value)

    return None


def find_mod_url(d: dict, mod: Mod) -> str:
    if isinstance(d, dict):
        for key in ("url", "page", "website", "mod_url"):
            value = d.get(key)

            if isinstance(value, str) and value:
                return value

        links = d.get("links")

        if isinstance(links, dict):
            for key in ("website", "page", "url"):
                value = links.get(key)

                if isinstance(value, str) and value:
                    return value

    return f"https://geode-sdk.org/mods/{mod.id}"


def format_error_reason(error: Any) -> str:
    text = str(error).strip() if error is not None else "unknown error"
    text = " ".join(text.split())

    if not text:
        text = "unknown error"

    return text[:180]


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=discord.Intents.default(),
        )

        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()

        await self.tree.sync()

        log.info("slash commands synced")

    async def close(self):
        if self.session:
            await self.session.close()

        await super().close()

    async def fetch_mod(self, mod: Mod):
        try:
            async with self.session.get(api_url.format(mod.id)) as r:
                raw_text = await r.text()

                if r.status != 200:
                    return {
                        "mod": mod,
                        "version": "error",
                        "pending": False,
                        "downloads": None,
                        "url": f"https://geode-sdk.org/mods/{mod.id}",
                        "error": (
                            f"HTTP {r.status} {r.reason}"
                            + (
                                f" — {raw_text.strip()[:120]}"
                                if raw_text.strip()
                                else ""
                            )
                        ),
                    }

                try:
                    raw_data = json.loads(raw_text)

                except json.JSONDecodeError as e:
                    return {
                        "mod": mod,
                        "version": "error",
                        "pending": False,
                        "downloads": None,
                        "url": f"https://geode-sdk.org/mods/{mod.id}",
                        "error": (
                            f"invalid json response: "
                            f"{format_error_reason(e)}"
                        ),
                    }

                data = unwrap(raw_data)

                version = find_version(data)
                pending = is_pending(data)
                downloads = find_downloads(data)
                url = find_mod_url(data, mod)

                return {
                    "mod": mod,
                    "version": version or "unknown",
                    "pending": pending,
                    "downloads": downloads,
                    "url": url,
                }

        except asyncio.TimeoutError as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "downloads": None,
                "url": f"https://geode-sdk.org/mods/{mod.id}",
                "error": f"timeout: {format_error_reason(e)}",
            }

        except aiohttp.ClientError as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "downloads": None,
                "url": f"https://geode-sdk.org/mods/{mod.id}",
                "error": f"network error: {format_error_reason(e)}",
            }

        except Exception as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "downloads": None,
                "url": f"https://geode-sdk.org/mods/{mod.id}",
                "error": format_error_reason(e),
            }

    async def fetch_all(self):
        return await asyncio.gather(
            *(self.fetch_mod(m) for m in MODS)
        )

    def build_embed(self, results):
        e = discord.Embed(
            title="geode version checker",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []

        for r in results:
            m = r["mod"]

            if r["version"] == "error":
                error_reason = r.get("error", "unknown error")
                status = f"❌ error: {error_reason}"

            elif r["pending"]:
                status = "⏳ Pending"

            else:
                status = "✅ On the index"

            downloads = (
                f"{r['downloads']:,}"
                if isinstance(r.get("downloads"), int)
                else "unknown"
            )

            lines.append(
                f"{m.emoji} "
                f"[{m.name}]({r['url']}) — "
                f"{r['version']} • "
                f"{downloads} downloads • "
                f"{status}"
            )

        e.description = "\n".join(lines)

        e.set_footer(
            text=(
                f"last updated • "
                f"{datetime.now().strftime('%H:%M:%S UTC')}"
            )
        )

        return e


bot = Bot()


@bot.tree.command(
    name="checkforupdates",
    description="live geode mod status",
)
@discord.app_commands.describe(
    mod="choose one mod to check"
)
async def checkforupdates(
    interaction: discord.Interaction,
    mod: Optional[str] = None,
):
    await interaction.response.defer()

    if mod is None:
        data = await bot.fetch_all()

    else:
        selected_mod = next(
            (m for m in MODS if m.id == mod),
            None,
        )

        if selected_mod is None:
            data = await bot.fetch_all()

        else:
            data = [await bot.fetch_mod(selected_mod)]

    await interaction.followup.send(
        embed=bot.build_embed(data)
    )


@checkforupdates.autocomplete("mod")
async def checkforupdates_mod_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    current = current.lower()

    return [
        discord.app_commands.Choice(
            name=f"{m.emoji} {m.name}",
            value=m.id,
        )
        for m in MODS
        if current in m.id.lower()
        or current in m.name.lower()
    ][:25]


# ==========================================
# GEODE DEVELOPER TOOLS (/dev subcommand group)
# ==========================================

# 1. THIS CREATES THE ROOT COMMAND: /dev
dev_group = discord.app_commands.Group(name="dev", description="Developer utilities for the Geode SDK")
bot.tree.add_command(dev_group)


# 2. THIS BECOMES: /dev docs <topic>
@dev_group.command(name="docs", description="Sends a link to the official Geode SDK Documentation.")
@discord.app_commands.describe(topic="Fetch a specific topic/search term")
async def dev_docs(interaction: discord.Interaction, topic: Optional[str] = None):
    base_url = "https://docs.geode-sdk.org/"
    if topic:
        # VitePress uses standard query parameters for some native searches, or directs the user nicely
        query = urllib.parse.quote(topic)
        await interaction.response.send_message(f"📚 Search the Geode Docs for **{topic}**: {base_url}?q={query}")
    else:
        await interaction.response.send_message(f"📚 Official Geode SDK Documentation: {base_url}")


# 3. THIS BECOMES: /dev cli
@dev_group.command(name="cli", description="Quick-start snippets for the Geode CLI.")
async def dev_cli(interaction: discord.Interaction):
    embed = discord.Embed(title="Geode CLI Quick-Start", color=discord.Color.green())
    embed.add_field(name="`geode new`", value="Create a new Geode project with the setup wizard.", inline=False)
    embed.add_field(name="`geode build`", value="Configure and build the current project.", inline=False)
    embed.add_field(name="`geode package`", value="Package the compiled mod into a `.geode` file.", inline=False)
    embed.add_field(name="`geode run`", value="Run Geometry Dash with Geode.", inline=False)
    embed.add_field(name="`geode profile`", value="Manage your Geometry Dash profiles.", inline=False)
    await interaction.response.send_message(embed=embed)


# 4. THIS BECOMES: /dev status
@dev_group.command(name="status", description="Displays current Geode API version and Server Status.")
async def dev_status(interaction: discord.Interaction):
    await interaction.response.defer()
    
    api_status = "Unknown"
    loader_ver = "Unknown"

    try:
        # Check general API health
        async with bot.session.get("https://api.geode-sdk.org/") as r:
            if r.status in (200, 404): # If it responds with anything properly routed, it's alive
                api_status = "✅ Online"
            else:
                api_status = f"⚠️ HTTP {r.status}"
    except Exception:
        api_status = "❌ Offline / Unreachable"

    try:
        # Fetch the latest loader version using the standard mods endpoint
        async with bot.session.get(api_url.format("geode.loader")) as r:
            if r.status == 200:
                data = await r.json()
                payload = data.get("payload", {})
                versions = payload.get("versions", [])
                if versions:
                    loader_ver = versions[0].get("version", "Unknown")
    except Exception:
        pass

    embed = discord.Embed(title="Geode Index & Server Status", color=discord.Color.blurple())
    embed.add_field(name="Geode Index API", value=api_status, inline=True)
    embed.add_field(name="Latest Loader Ver", value=loader_ver, inline=True)
    embed.add_field(name="API Documentation", value="[Swagger UI](https://api.geode-sdk.org/swagger/)", inline=False)
    
    await interaction.followup.send(embed=embed)


# 5. THIS BECOMES: /dev template
@dev_group.command(name="template", description="Provides a standard 'Hello World' Geode boilerplate.")
async def dev_template(interaction: discord.Interaction):
    code = (
        "```cpp\n"
        "#include <Geode/Geode.hpp>\n"
        "#include <Geode/modify/MenuLayer.hpp>\n\n"
        "using namespace geode::prelude;\n\n"
        "class $modify(MyMenuLayer, MenuLayer) {\n"
        "    bool init() {\n"
        "        if (!MenuLayer::init()) return false;\n\n"
        "        FLAlertLayer::create(\"Geode\", \"Hello World from Geode!\", \"OK\")->show();\n\n"
        "        return true;\n"
        "    }\n"
        "};\n"
        "```"
    )
    await interaction.response.send_message(f"Here is a standard Geode `Hello World` boilerplate:\n{code}")


# 6. THIS BECOMES: /dev repo <mod_id>
@dev_group.command(name="repo", description="Pulls the GitHub/Source code link for a specific mod.")
@discord.app_commands.describe(mod_id="The ID of the mod (e.g. geode.loader)")
async def dev_repo(interaction: discord.Interaction, mod_id: str):
    await interaction.response.defer()
    
    try:
        async with bot.session.get(api_url.format(mod_id)) as r:
            if r.status == 200:
                data = await r.json()
                payload = data.get("payload", {})
                links = payload.get("links", {})
                source_url = links.get("source")
                
                if source_url:
                    await interaction.followup.send(f"🔗 **Source code for `{mod_id}`:**\n{source_url}")
                else:
                    await interaction.followup.send(f"❌ No source code link was found on the index for `{mod_id}`.")
            elif r.status == 404:
                await interaction.followup.send(f"❌ Mod `{mod_id}` not found on the index.")
            else:
                await interaction.followup.send(f"❌ API Error: HTTP {r.status}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching mod repository: {format_error_reason(e)}")


# 7. THIS BECOMES: /dev help
@dev_group.command(name="help", description="Lists common C++ or CMake errors and troubleshooting steps.")
async def dev_help(interaction: discord.Interaction):
    embed = discord.Embed(title="Geode Developer - Common Issues", color=discord.Color.red())
    embed.add_field(
        name="Missing Headers / Bindings Not Found", 
        value="Ensure you ran `geode build` (or your CMake configure step) to generate the GD bindings. If your IDE still warns, try reloading your CMake project.", 
        inline=False
    )
    embed.add_field(
        name="CMake Not Found", 
        value="Make sure CMake is installed and added to your system `PATH` variable.", 
        inline=False
    )
    embed.add_field(
        name="Linker Errors (LNK2001 / LNK2019)", 
        value="Usually caused by an incorrect function signature inside your `$modify` block, or missing a `GEODE_API` macro on an exported class.", 
        inline=False
    )
    embed.add_field(
        name="Game Crashes Immediately", 
        value="Double check your dependencies in `mod.json` and ensure you aren't trying to access layers before they are fully initialized.", 
        inline=False
    )
    embed.add_field(
        name="Need More Info?", 
        value="Check out the [Troubleshooting Guide](https://docs.geode-sdk.org/troubleshooting) in the official docs.", 
        inline=False
    )
    await interaction.response.send_message(embed=embed)


def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")

    bot.run(token)


if __name__ == "__main__":
    main()
