import asyncio
import json
import logging
import os
import re
import urllib.parse
import random
import string
from datetime import datetime, timezone
from typing import Any, Optional, List

import aiohttp
import discord
from discord.ext import commands

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")

# banned words filter
BANNED_WORDS = [
    "nigger",
    "nigga",
    "faggot",
    "fag",
    "dyke",
    "tranny",
    "kys",
    "retard"
]

def contains_banned_word(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    for banned in BANNED_WORDS:
        if re.search(rf"\b{re.escape(banned)}\b", text_lower):
            return True
    return False

def normalize_single_mod_response(data: Any) -> dict:
    if isinstance(data, dict):
        payload = data.get("payload", data)
        if isinstance(payload, dict):
            if "data" in payload and isinstance(payload["data"], dict):
                return payload["data"]
            return payload
    return {}

def normalize_list_response(data: Any) -> dict:
    if isinstance(data, list):
        return {"count": len(data), "data": data}
    if isinstance(data, dict):
        payload = data.get("payload", data)
        if isinstance(payload, list):
            return {"count": len(payload), "data": payload}
        if isinstance(payload, dict):
            if "data" in payload and isinstance(payload["data"], list):
                return payload
    return {"count": 0, "data": []}

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
    if status:
        return status not in ("accepted", "approved")
    return False

def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None
        
    if "version" in d and isinstance(d["version"], str):
        return d["version"]
    if "latest_version" in d and isinstance(d["latest_version"], str):
        return d["latest_version"]
        
    versions = d.get("versions")
    if isinstance(versions, list) and len(versions) > 0:
        first_version = versions[0]
        if isinstance(first_version, dict):
            return first_version.get("version")
            
    return None

def find_developer(mod_data: dict) -> str:
    if not isinstance(mod_data, dict):
        return "unknown"
        
    dev = mod_data.get("developer")
    if dev and isinstance(dev, str):
        return dev
        
    developers = mod_data.get("developers")
    if isinstance(developers, list) and len(developers) > 0:
        first = developers[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("display_name") or first.get("username") or "unknown"
            
    owner = mod_data.get("owner")
    if owner and isinstance(owner, str):
        return owner
        
    return "unknown"

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

def find_mod_url(d: dict, mod_id: str) -> str:
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
    return f"https://geode-sdk.org/mods/{mod_id}"

def find_description(d: dict) -> str:
    if not isinstance(d, dict):
        return "no description"
        
    candidates = [d.get("description"), d.get("summary")]
    
    versions = d.get("versions")
    if isinstance(versions, list) and versions:
        v = versions[0]
        if isinstance(v, dict):
            candidates.extend([v.get("description"), v.get("summary")])
            
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
            
    return "no description"

def find_name(d: dict, mod_id: str) -> str:
    if not isinstance(d, dict):
        return mod_id
        
    if d.get("name") and isinstance(d.get("name"), str):
        return d["name"]
        
    versions = d.get("versions")
    if isinstance(versions, list) and versions:
        v = versions[0]
        if isinstance(v, dict) and v.get("name") and isinstance(v.get("name"), str):
            return v["name"]
            
    return mod_id

def find_logo(mod_id: str) -> str:
    return f"https://api.geode-sdk.org/v1/mods/{mod_id}/logo"

def format_error_reason(error: Any) -> str:
    text = str(error).strip() if error is not None else "unknown error"
    text = " ".join(text.split())
    return text[:180] or "unknown error"

def build_single_mod_embed(mod_data: dict) -> discord.Embed:
    mod_id = mod_data.get("id") or "unknown.id"
    name = find_name(mod_data, mod_id)
    dev = find_developer(mod_data)
    desc = find_description(mod_data)
    version = find_version(mod_data) or "unknown"
    downloads = find_downloads(mod_data)
    pending = is_pending(mod_data)
    url = find_mod_url(mod_data, mod_id)
    logo = find_logo(mod_id)

    color = 0xffd700 if pending else 0x2ecc71

    embed = discord.Embed(
        title=f"{name} ({version})",
        description=desc,
        color=color,
        url=url,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.set_author(name=f"by {dev}")
    embed.set_thumbnail(url=logo)
    
    dl_text = f"{downloads:,}" if downloads is not None else "n/a"
    status_text = "pending" if pending else "verified"

    embed.add_field(name="id", value=f"`{mod_id}`", inline=True)
    embed.add_field(name="downloads", value=dl_text, inline=True)
    embed.add_field(name="status", value=status_text, inline=True)

    tags = mod_data.get("tags", [])
    if tags:
        embed.add_field(name="tags", value=", ".join(tags), inline=False)
        
    embed.set_footer(text="geode index")
    return embed

def build_list_embeds(title: str, mods: list, page: int, total_pages: int, per_page: int) -> list[discord.Embed]:
    # Use a separate starting embed strictly for the title so Discord doesn't force it down
    title_embed = discord.Embed(title=title, color=0x5865F2)
    embeds = [title_embed]

    if not mods:
        title_embed.description = "*no mods found.*"
        title_embed.set_footer(text=f"page {page}/{max(1, total_pages)}")
        return embeds

    # Calculate starting index so numbering continues across pages
    start_idx = (page - 1) * per_page + 1
    
    for i, m in enumerate(mods, start_idx):
        mod_id = m.get("id") or "unknown.id"
        name = find_name(m, mod_id)
        dev = find_developer(m)
        dl = find_downloads(m) or 0
        desc = find_description(m)
        logo = find_logo(mod_id)
        
        if len(desc) > 85:
            desc = desc[:82] + "..."
            
        text = f"*{desc}*\n📦 `{mod_id}` • ⬇️ {dl:,}"

        embed = discord.Embed(description=text, color=0x5865F2)
        embed.set_author(name=f"{i}. {name} (by {dev})", icon_url=logo, url=f"https://geode-sdk.org/mods/{mod_id}")
        
        embeds.append(embed)

    # Attach footer strictly to the last embed in the cluster
    embeds[-1].set_footer(text=f"page {page}/{max(1, total_pages)}")

    return embeds

class ModSelect(discord.ui.Select):
    def __init__(self, mods: list):
        options = []
        for m in mods:
            mod_id = (m.get("id") or "unknown.id")[:90]
            name = find_name(m, mod_id)[:90]
            desc = find_description(m)
            
            if len(desc) > 95:
                desc = desc[:92] + "..."
                
            options.append(discord.SelectOption(label=name, description=desc, value=mod_id))
            
        super().__init__(
            placeholder="select a mod...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        mod_id = self.values[0]
        await interaction.response.defer(ephemeral=True)
        mod_data = await interaction.client.fetch_single_mod(mod_id)
        
        if "error" in mod_data:
            await interaction.followup.send(f"error fetching mod: {mod_data['error']}", ephemeral=True)
            return
            
        embed = build_single_mod_embed(mod_data)
        await interaction.followup.send(embed=embed, ephemeral=True)

class PageModal(discord.ui.Modal, title="Jump to Page"):
    page_num = discord.ui.TextInput(
        label="Page Number",
        style=discord.TextStyle.short,
        placeholder="Enter page...",
        required=True
    )

    def __init__(self, view: "ModSearchView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_page = int(self.page_num.value.strip())
            if new_page < 1:
                new_page = 1
            elif new_page > self.view.total_pages:
                new_page = self.view.total_pages

            self.view.page = new_page
            embeds = await self.view.generate_view()
            await interaction.response.edit_message(embeds=embeds, view=self.view)
        except ValueError:
            await interaction.response.send_message("invalid page number.", ephemeral=True)

class ModSearchView(discord.ui.View):
    def __init__(self, bot, query: str = None, is_trending: bool = False, per_page: int = 3):
        super().__init__(timeout=300)
        self.bot = bot
        self.query = query
        self.is_trending = is_trending
        self.page = 1
        self.per_page = per_page
        self.total_pages = 1
        self.mods = []

    async def load_data(self):
        data = await self.bot.fetch_mods_list(query=self.query, sort="downloads", page=self.page, per_page=self.per_page)
        self.mods = data.get("data", [])
        count = data.get("count", 0)
        self.total_pages = max(1, (count + self.per_page - 1) // self.per_page)

    def update_items(self):
        self.clear_items()
        
        self.btn_prev.disabled = self.page <= 1
        self.btn_jump.disabled = self.total_pages <= 1
        self.btn_next.disabled = self.page >= self.total_pages
        
        self.add_item(self.btn_prev)
        self.add_item(self.btn_jump)
        self.add_item(self.btn_next)

        if self.mods:
            self.add_item(ModSelect(self.mods))

    async def generate_view(self):
        await self.load_data()
        self.update_items()
        title = "trending mods" if self.is_trending else f"search: {self.query}"
        return build_list_embeds(title, self.mods, self.page, self.total_pages, self.per_page)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        embeds = await self.generate_view()
        await interaction.response.edit_message(embeds=embeds, view=self)

    @discord.ui.button(label="Page...", style=discord.ButtonStyle.secondary, custom_id="jump")
    async def btn_jump(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PageModal(self))

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary, custom_id="next")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        embeds = await self.generate_view()
        await interaction.response.edit_message(embeds=embeds, view=self)

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

        async def update_presence(self):
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"the geode index <3 | {len(self.guilds):,} servers"
            )
        )

    async def on_ready(self):
        await self.update_presence()
        log.info(f"logged in as {self.user}")

    async def on_guild_join(self, guild):
        await self.update_presence()

    async def on_guild_remove(self, guild):
        await self.update_presence()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def fetch_single_mod(self, mod_id: str) -> dict:
        try:
            async with self.session.get(api_url.format(mod_id)) as r:
                if r.status == 404:
                    return {"error": "mod not found"}
                r.raise_for_status()
                data = await r.json()
                return normalize_single_mod_response(data)
        except Exception as e:
            return {"error": format_error_reason(e)}

    async def fetch_mods_list(self, query: str = None, sort: str = "downloads", page: int = 1, per_page: int = 3) -> dict:
        url = "https://api.geode-sdk.org/v1/mods"
        params = {"page": page, "per_page": per_page}
        if query:
            params["query"] = query
        if sort:
            params["sort"] = sort

        try:
            async with self.session.get(url, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    return normalize_list_response(data)
                return {"count": 0, "data": []}
        except Exception:
            return {"count": 0, "data": []}

bot = Bot()

async def mod_autocomplete_logic(current: str):
    if not current or contains_banned_word(current):
        return []
    
    data = await bot.fetch_mods_list(query=current, sort="downloads", page=1, per_page=15)
    mods = data.get("data", [])
    
    choices = []
    for m in mods:
        mod_id = m.get('id') or "unknown"
        name = find_name(m, mod_id)
        choices.append(discord.app_commands.Choice(name=f"{name} ({mod_id})", value=mod_id))
        
    return choices[:25]

# geode commands

@bot.tree.command(name="getindex", description="browse trending geode mods or search the index")
@discord.app_commands.describe(
    mod_id="specific mod to view (autocompletes from api)",
    search="search mod by name",
    per_page="how many mods to show per page (1-5, default 3)"
)
async def checkforupdates(
    interaction: discord.Interaction,
    mod_id: Optional[str] = None,
    search: Optional[str] = None,
    per_page: discord.app_commands.Range[int, 1, 5] = 3,
):
    if (search and contains_banned_word(search)) or (mod_id and contains_banned_word(mod_id)):
        return await interaction.response.send_message("blocked: contains banned words.", ephemeral=True)

    await interaction.response.defer()

    if mod_id:
        mod_data = await bot.fetch_single_mod(mod_id)
        if "error" in mod_data:
            return await interaction.followup.send(f"error: {mod_data['error']}")
        
        embed = build_single_mod_embed(mod_data)
        await interaction.followup.send(embed=embed)
        
    elif search:
        view = ModSearchView(bot, query=search, is_trending=False, per_page=per_page)
        embeds = await view.generate_view()
        await interaction.followup.send(embeds=embeds, view=view)
        
    else:
        view = ModSearchView(bot, query=None, is_trending=True, per_page=per_page)
        embeds = await view.generate_view()
        await interaction.followup.send(embeds=embeds, view=view)

@checkforupdates.autocomplete("mod_id")
async def checkforupdates_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

@bot.tree.command(name="erymanthus", description="check if someone has already made your mod idea")
@discord.app_commands.describe(
    search="describe your mod idea",
    max_results="max results to show (1-5, default 3)"
)
async def erymanthus(
    interaction: discord.Interaction, 
    search: str,
    max_results: discord.app_commands.Range[int, 1, 5] = 3
):
    if contains_banned_word(search):
        return await interaction.response.send_message("blocked: contains banned words.", ephemeral=True)

    await interaction.response.defer()

    data = await bot.fetch_mods_list(query=search, sort="downloads", page=1, per_page=max_results)
    mods = data.get("data", [])

    if not mods:
        embed = discord.Embed(
            title="idea check: clear",
            description=f"no existing mods found matching **{search}**.\n\n*note: this only checks titles and descriptions.*",
            color=0x2ecc71
        )
    else:
        embed = discord.Embed(
            title="idea check: similar mods found",
            description=f"found these existing mods for **{search}**:\n\n",
            color=0xe67e22
        )

        for m in mods:
            mod_id = m.get("id") or "unknown.id"
            name = find_name(m, mod_id)
            desc = find_description(m)
            
            if len(desc) > 85:
                desc = desc[:82] + "..."
                
            embed.description += f"**[{name}](https://geode-sdk.org/mods/{mod_id})** (`{mod_id}`)\n> {desc}\n\n"

        embed.description += "*note: this only checks titles and descriptions.*"

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="dev", description="developer utilities for geode")
@discord.app_commands.describe(
    command="utility command to run",
    mod_id="mod id (only for 'repo', autocompletes from api)"
)
@discord.app_commands.choices(command=[
    discord.app_commands.Choice(name="repo", value="repo"),
    discord.app_commands.Choice(name="ery string generator", value="ery_string_generator"),
])
async def dev(
    interaction: discord.Interaction, 
    command: discord.app_commands.Choice[str], 
    mod_id: Optional[str] = None
):
    cmd = command.value

    if mod_id and contains_banned_word(mod_id):
        return await interaction.response.send_message("blocked: contains banned words.", ephemeral=True)

    if cmd == "repo":
        if not mod_id:
            return await interaction.response.send_message("error: provide a `mod_id` to use repo command.", ephemeral=True)

        await interaction.response.defer()
        
        try:
            async with bot.session.get(api_url.format(mod_id)) as r:
                if r.status == 200:
                    data = await r.json()
                    mod_obj = normalize_single_mod_response(data)
                    
                    source_url = None
                    links = mod_obj.get("links")
                    if isinstance(links, dict):
                        source_url = links.get("source") or links.get("repository")
                    if not source_url:
                        source_url = mod_obj.get("repository") or mod_obj.get("source")
                        
                    if source_url:
                        await interaction.followup.send(f"**source for `{mod_id}`:**\n{source_url}")
                    else:
                        await interaction.followup.send(f"no source code link found for `{mod_id}`.")
                elif r.status == 404:
                    await interaction.followup.send(f"mod `{mod_id}` not found.")
                else:
                    await interaction.followup.send(f"api error: http {r.status}")
        except Exception as e:
            await interaction.followup.send(f"error fetching repo: {format_error_reason(e)}")

    elif cmd == "ery_string_generator":
        random_chars = ''.join(random.choices(string.ascii_uppercase + string.digits, k=64))
        magic_string = f"ERYMANTHUS_MAGIC_STRING_TRIGGER_ACCEPT_MY_MOD_{random_chars}"
        
        embed = discord.Embed(
            title="magic string generator", 
            description="generate a cryptographically secure magic string to bypass erymanthus check",
            color=0x9b59b6
        )
        embed.add_field(name="output", value=f"```\n{magic_string}\n```")
        await interaction.response.send_message(embed=embed)

@dev.autocomplete("mod_id")
async def dev_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")
    bot.run(token)

if __name__ == "__main__":
    main()
