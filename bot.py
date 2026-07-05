import asyncio
import logging
import os
import re
import random
import string
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

# --- ENV VARS ---
token = os.getenv("DISCORD_TOKEN")
cf_account = os.getenv("CF_ACCOUNT_ID")
cf_db = os.getenv("CF_DATABASE_ID")
cf_token = os.getenv("CF_API_TOKEN")

api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("geode")

# banned words filter
BANNED_WORDS = [
    "nigger", "nigga", "faggot", "fag", 
    "dyke", "tranny", "kys", "retard"
]

def contains_banned_word(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    for banned in BANNED_WORDS:
        if re.search(rf"\b{re.escape(banned)}\b", text_lower):
            return True
    return False

# --- CLOUDFLARE D1 TRACKER ---
class D1Tracker:
    def __init__(self):
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/d1/database/{cf_db}/query"
        self.headers = {
            "Authorization": f"Bearer {cf_token}",
            "Content-Type": "application/json"
        }

    async def query(self, session: aiohttp.ClientSession, sql: str, params: list = None) -> dict:
        payload = {"sql": sql, "params": params or []}
        try:
            async with session.post(self.base_url, headers=self.headers, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.warning(f"d1 query hiccup: {text}")
                    return {}
                return await resp.json()
        except Exception as e:
            log.warning(f"d1 connection snag: {e}")
            return {}

    async def add_tracking(self, session: aiohttp.ClientSession, mod_id: str, version: str, user_id: int) -> bool:
        uid_str = str(user_id)
        check_sql = "SELECT * FROM tracking WHERE mod_id = ? AND user_id = ?"
        res = await self.query(session, check_sql, [mod_id, uid_str])
        
        results = res.get("result", [{}])[0].get("results", [])
        if results:
            return False # already tracking

        insert_sql = "INSERT INTO tracking (mod_id, version, user_id) VALUES (?, ?, ?)"
        await self.query(session, insert_sql, [mod_id, version, uid_str])
        return True

    async def remove_tracking(self, session: aiohttp.ClientSession, mod_id: str, user_id: int) -> bool:
        uid_str = str(user_id)
        del_sql = "DELETE FROM tracking WHERE mod_id = ? AND user_id = ? RETURNING *"
        res = await self.query(session, del_sql, [mod_id, uid_str])
        results = res.get("result", [{}])[0].get("results", [])
        return len(results) > 0

    async def get_user_tracked_mods(self, session: aiohttp.ClientSession, user_id: int) -> list:
        uid_str = str(user_id)
        sql = "SELECT mod_id FROM tracking WHERE user_id = ?"
        res = await self.query(session, sql, [uid_str])
        results = res.get("result", [{}])[0].get("results", [])
        return [row["mod_id"] for row in results]

    async def get_all_tracking(self, session: aiohttp.ClientSession) -> dict:
        sql = "SELECT mod_id, version, user_id FROM tracking"
        res = await self.query(session, sql)
        results = res.get("result", [{}])[0].get("results", [])
        
        # group by mod_id to match old logic
        grouped = {}
        for row in results:
            m_id = row["mod_id"]
            if m_id not in grouped:
                grouped[m_id] = {"version": row["version"], "users": []}
            grouped[m_id]["users"].append(row["user_id"])
        return grouped

    async def update_version(self, session: aiohttp.ClientSession, mod_id: str, new_version: str):
        sql = "UPDATE tracking SET version = ? WHERE mod_id = ?"
        await self.query(session, sql, [new_version, mod_id])

tracker = D1Tracker()

# --- HELPER FUNCTIONS ---
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
    status = versions[0].get("status") if isinstance(versions[0], dict) else None
    return status not in ("accepted", "approved") if status else False

def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict): return None
    if "version" in d and isinstance(d["version"], str): return d["version"]
    if "latest_version" in d and isinstance(d["latest_version"], str): return d["latest_version"]
    versions = d.get("versions")
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        return versions[0].get("version")
    return None

def find_developer(mod_data: dict) -> str:
    if not isinstance(mod_data, dict): return "unknown"
    dev = mod_data.get("developer")
    if dev and isinstance(dev, str): return dev
    developers = mod_data.get("developers")
    if isinstance(developers, list) and developers:
        first = developers[0]
        if isinstance(first, str): return first
        if isinstance(first, dict): return first.get("display_name") or first.get("username") or "unknown"
    return mod_data.get("owner") if isinstance(mod_data.get("owner"), str) else "unknown"

def find_downloads(d: dict) -> Optional[int]:
    if not isinstance(d, dict): return None
    candidates = [d.get("downloads"), d.get("download_count"), d.get("downloads_total")]
    stats = d.get("stats")
    if isinstance(stats, dict):
        candidates.extend([stats.get("downloads"), stats.get("download_count"), stats.get("downloads_total")])
    for value in candidates:
        if isinstance(value, int): return value
        if isinstance(value, str) and value.isdigit(): return int(value)
    return None

def find_mod_url(d: dict, mod_id: str) -> str:
    if isinstance(d, dict):
        for key in ("url", "page", "website", "mod_url"):
            value = d.get(key)
            if isinstance(value, str) and value: return value
        links = d.get("links")
        if isinstance(links, dict):
            for key in ("website", "page", "url"):
                value = links.get(key)
                if isinstance(value, str) and value: return value
    return f"https://geode-sdk.org/mods/{mod_id}"

def find_description(d: dict) -> str:
    if not isinstance(d, dict): return "no description"
    candidates = [d.get("description"), d.get("summary")]
    versions = d.get("versions")
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        candidates.extend([versions[0].get("description"), versions[0].get("summary")])
    for c in candidates:
        if isinstance(c, str) and c.strip(): return c.strip()
    return "no description"

def find_name(d: dict, mod_id: str) -> str:
    if not isinstance(d, dict): return mod_id
    if d.get("name") and isinstance(d.get("name"), str): return d["name"]
    versions = d.get("versions")
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        if versions[0].get("name") and isinstance(versions[0].get("name"), str):
            return versions[0]["name"]
    return mod_id

def find_logo(mod_id: str) -> str:
    return f"https://api.geode-sdk.org/v1/mods/{mod_id}/logo"

def format_error_reason(error: Any) -> str:
    text = str(error).strip() if error is not None else "unknown error"
    return " ".join(text.split())[:180] or "unknown error"

# --- UI & EMBED BUILDERS ---
class NotifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="notify me!", style=discord.ButtonStyle.success, emoji="🔔", custom_id="persistent_notify_btn")
    async def notify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        
        mod_id = None
        for field in embed.fields:
            if field.name == "id":
                mod_id = field.value.strip("`")
                break
                
        version = "unknown"
        if embed.title:
            m = re.search(r'\((.*?)\)$', embed.title)
            if m: version = m.group(1)

        if not mod_id:
            return await interaction.response.send_message("hmm, couldn't find the mod id on this one.", ephemeral=True)

        added = await tracker.add_tracking(interaction.client.session, mod_id, version, interaction.user.id)
        if added:
            await interaction.response.send_message(
                f"🔔 **all set!** i'll dm you when `{mod_id}` updates.\n*(use `/untrack {mod_id}` in my dms if you change your mind)*", 
                ephemeral=True
            )
        else:
            await interaction.response.send_message("you're already tracking this one, all good!", ephemeral=True)

def build_single_mod_embed(mod_data: dict) -> discord.Embed:
    mod_id = mod_data.get("id") or "unknown.id"
    name = find_name(mod_data, mod_id)
    dev = find_developer(mod_data)
    desc = find_description(mod_data)
    version = find_version(mod_data) or "unknown"
    downloads = find_downloads(mod_data)
    pending = is_pending(mod_data)
    
    embed = discord.Embed(
        title=f"{name} ({version})",
        description=desc,
        color=0xffd700 if pending else 0x2ecc71,
        url=find_mod_url(mod_data, mod_id),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(name=f"by {dev}")
    embed.set_thumbnail(url=find_logo(mod_id))
    
    embed.add_field(name="id", value=f"`{mod_id}`", inline=True)
    embed.add_field(name="downloads", value=f"{downloads:,}" if downloads is not None else "n/a", inline=True)
    embed.add_field(name="status", value="pending" if pending else "verified", inline=True)

    tags = mod_data.get("tags", [])
    if tags: embed.add_field(name="tags", value=", ".join(tags), inline=False)
        
    embed.set_footer(text="geode index")
    return embed

def build_list_embeds(title: str, mods: list, page: int, total_pages: int, per_page: int, total_mods: int) -> list[discord.Embed]:
    title_embed = discord.Embed(title=title, color=0x5865F2)
    title_embed.set_author(name=f"total mods: {total_mods:,}")
    embeds = [title_embed]

    if not mods:
        title_embed.description = "*couldn't find any mods.*"
        title_embed.set_footer(text=f"page {page}/{max(1, total_pages)}")
        return embeds

    start_idx = (page - 1) * per_page + 1
    for i, m in enumerate(mods, start_idx):
        mod_id = m.get("id") or "unknown.id"
        desc = find_description(m)
        desc = desc[:82] + "..." if len(desc) > 85 else desc
        
        text = f"*{desc}*\n📦 `{mod_id}` • ⬇️ {find_downloads(m) or 0:,}"
        embed = discord.Embed(description=text, color=0x5865F2)
        embed.set_author(name=f"{i}. {find_name(m, mod_id)} (by {find_developer(m)})", icon_url=find_logo(mod_id), url=f"https://geode-sdk.org/mods/{mod_id}")
        embeds.append(embed)

    embeds[-1].set_footer(text=f"page {page}/{max(1, total_pages)}")
    return embeds

class ModSelect(discord.ui.Select):
    def __init__(self, mods: list):
        options = []
        for m in mods:
            mod_id = (m.get("id") or "unknown.id")[:90]
            desc = find_description(m)
            options.append(discord.SelectOption(
                label=find_name(m, mod_id)[:90], 
                description=desc[:92] + "..." if len(desc) > 95 else desc, 
                value=mod_id
            ))
        super().__init__(placeholder="pick a mod to view...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        mod_data = await interaction.client.fetch_single_mod(self.values[0])
        
        if "error" in mod_data:
            return await interaction.followup.send(f"yikes, error fetching mod: {mod_data['error']}", ephemeral=True)
            
        await interaction.followup.send(embed=build_single_mod_embed(mod_data), view=NotifyView(), ephemeral=True)

class PageModal(discord.ui.Modal, title="jump to page"):
    page_num = discord.ui.TextInput(label="page number", style=discord.TextStyle.short, placeholder="enter a page...", required=True)

    def __init__(self, view: "ModSearchView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_page = max(1, min(int(self.page_num.value.strip()), self.view.total_pages))
            self.view.page = new_page
            await interaction.response.edit_message(embeds=await self.view.generate_view(), view=self.view)
        except ValueError:
            await interaction.response.send_message("that doesn't look like a number, try again.", ephemeral=True)

class ModSearchView(discord.ui.View):
    def __init__(self, bot, query: str = None, sort_mode: str = "downloads", per_page: int = 3):
        super().__init__(timeout=300)
        self.bot = bot
        self.query = query
        self.sort_mode = sort_mode
        self.page = 1
        self.per_page = per_page
        self.total_pages = 1
        self.total_mods = 0
        self.mods = []

    async def load_data(self):
        featured = (self.sort_mode == "featured")
        data = await self.bot.fetch_mods_list(query=self.query, sort="downloads" if featured else self.sort_mode, featured=featured, page=self.page, per_page=self.per_page)
        self.mods = data.get("data", [])
        self.total_mods = data.get("count", 0)
        self.total_pages = max(1, (self.total_mods + self.per_page - 1) // self.per_page)

    def update_items(self):
        self.clear_items()
        self.btn_prev.disabled = self.page <= 1
        self.btn_jump.disabled = self.total_pages <= 1
        self.btn_next.disabled = self.page >= self.total_pages
        
        self.add_item(self.btn_prev)
        self.add_item(self.btn_jump)
        self.add_item(self.btn_next)
        if self.mods: self.add_item(ModSelect(self.mods))

    async def generate_view(self):
        await self.load_data()
        self.update_items()
        
        titles = {
            "featured": "featured mods",
            "recently_updated": "recently updated mods",
            "recently_published": "the recent tab!",
            "downloads": "trending mods"
        }
        title = f"search: {self.query} ({titles.get(self.sort_mode, '')})" if self.query else titles.get(self.sort_mode, "trending mods")
        return build_list_embeds(title, self.mods, self.page, self.total_pages, self.per_page, self.total_mods)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await interaction.response.edit_message(embeds=await self.generate_view(), view=self)

    @discord.ui.button(label="page...", style=discord.ButtonStyle.secondary, custom_id="jump")
    async def btn_jump(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PageModal(self))

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary, custom_id="next")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embeds=await self.generate_view(), view=self)

# --- BOT CLASS & SETUP ---
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.add_view(NotifyView())
        await self.tree.sync()
        self.check_mod_updates.start()
        log.info("bot is up, slash commands synced.")

    async def update_presence(self):
        total_members = sum(guild.member_count or 0 for guild in self.guilds)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"the geode index <3 | {len(self.guilds):,} servers"
        ))

    async def on_ready(self):
        await self.update_presence()
        log.info(f"bro is {self.user}")

    async def on_guild_join(self, guild): await self.update_presence()
    async def on_guild_remove(self, guild): await self.update_presence()

    async def close(self):
        if self.session: await self.session.close()
        await super().close()

    async def fetch_single_mod(self, mod_id: str) -> dict:
        try:
            async with self.session.get(api_url.format(mod_id)) as r:
                if r.status == 404: return {"error": "mod not found"}
                r.raise_for_status()
                return normalize_single_mod_response(await r.json())
        except Exception as e:
            return {"error": format_error_reason(e)}

    async def fetch_mods_list(self, query: str = None, developer: str = None, sort: str = "downloads", featured: bool = False, page: int = 1, per_page: int = 3) -> dict:
        params = {"page": page, "per_page": per_page, "sort": sort}
        if query: params["query"] = query
        if developer: params["developer"] = developer
        if featured: params["featured"] = "true"

        try:
            async with self.session.get("https://api.geode-sdk.org/v1/mods", params=params) as r:
                return normalize_list_response(await r.json()) if r.status == 200 else {"count": 0, "data": []}
        except Exception:
            return {"count": 0, "data": []}

    @tasks.loop(minutes=10)
    async def check_mod_updates(self):
        if not self.session: return
        
        # Pull all tracking records from D1
        tracking_data = await tracker.get_all_tracking(self.session)
        
        for mod_id, data in tracking_data.items():
            if not data["users"]: continue
                
            mod_resp = await self.fetch_single_mod(mod_id)
            if "error" in mod_resp: continue

            latest_v = find_version(mod_resp) or data["version"]
            if latest_v != data["version"]:
                await tracker.update_version(self.session, mod_id, latest_v)
                embed = build_single_mod_embed(mod_resp)
                
                for uid in data["users"]:
                    try:
                        user_id = int(uid)
                        user = self.get_user(user_id) or await self.fetch_user(user_id)
                        if user:
                            await user.send(f"🔔 **update alert!**\n**{find_name(mod_resp, mod_id)}** just updated to **{latest_v}**!", embed=embed, view=NotifyView())
                    except discord.Forbidden:
                        pass # DMs closed, ugh
                    except Exception as e:
                        log.warning(f"couldn't send update to {uid}: {e}")
                    await asyncio.sleep(0.5)

bot = Bot()

async def mod_autocomplete_logic(current: str):
    if not current or contains_banned_word(current): return []
    data = await bot.fetch_mods_list(query=current, sort="downloads", page=1, per_page=15)
    return [discord.app_commands.Choice(name=f"{find_name(m, m.get('id') or 'unknown')} ({m.get('id') or 'unknown'})", value=m.get('id') or "unknown") for m in data.get("data", [])][:25]

# --- COMMANDS ---

@bot.tree.command(name="getindex", description="browse geode mods or search the index")
@discord.app_commands.describe(mod_id="specific mod to view", search="search mod by name", sort_by="sort the mod list", per_page="mods per page (1-5)")
@discord.app_commands.choices(sort_by=[
    discord.app_commands.Choice(name="featured", value="featured"),
    discord.app_commands.Choice(name="recently updated", value="recently_updated"),
    discord.app_commands.Choice(name="recent", value="recently_published"),
])
async def checkforupdates(interaction: discord.Interaction, mod_id: Optional[str] = None, search: Optional[str] = None, sort_by: Optional[discord.app_commands.Choice[str]] = None, per_page: discord.app_commands.Range[int, 1, 5] = 3):
    if search or sort_by: mod_id = None
    if (search and contains_banned_word(search)) or (mod_id and contains_banned_word(mod_id)):
        return await interaction.response.send_message("hey, let's keep it clean. banned word detected.", ephemeral=True)

    await interaction.response.defer()

    if mod_id:
        mod_data = await bot.fetch_single_mod(mod_id)
        if "error" in mod_data:
            return await interaction.followup.send(f"hmm, {mod_data['error']}")
        await interaction.followup.send(embed=build_single_mod_embed(mod_data), view=NotifyView())
    else:
        view = ModSearchView(bot, query=search, sort_mode=sort_by.value if sort_by else "downloads", per_page=per_page)
        await interaction.followup.send(embeds=await view.generate_view(), view=view)

@checkforupdates.autocomplete("mod_id")
async def checkforupdates_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

@bot.tree.command(name="untrack", description="stop getting dm notifications for a specific mod")
@discord.app_commands.describe(mod_id="the id of the mod to stop tracking")
async def untrack_cmd(interaction: discord.Interaction, mod_id: str):
    if interaction.guild is not None:
        return await interaction.response.send_message("❌ slide into my dms to use this command, keeps things private!", ephemeral=True)

    removed = await tracker.remove_tracking(bot.session, mod_id, interaction.user.id)
    if removed:
        await interaction.response.send_message(f"✅ all good, you won't hear about `{mod_id}` anymore.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ you aren't actually tracking `{mod_id}` right now.", ephemeral=True)

@untrack_cmd.autocomplete("mod_id")
async def untrack_autocomplete(interaction: discord.Interaction, current: str):
    user_tracked = await tracker.get_user_tracked_mods(bot.session, interaction.user.id)
    return [discord.app_commands.Choice(name=m, value=m) for m in user_tracked if current.lower() in m.lower()][:25]

@bot.tree.command(name="tracked", description="view all mods you are tracking")
async def tracked_cmd(interaction: discord.Interaction):
    if interaction.guild is not None:
        return await interaction.response.send_message("❌ jump into my dms to use this!", ephemeral=True)

    user_tracked = await tracker.get_user_tracked_mods(bot.session, interaction.user.id)
    if not user_tracked:
        return await interaction.response.send_message("you aren't tracking anything yet. run `/getindex` in a server and click 'notify me!' to start.", ephemeral=True)

    mod_list = "\n".join([f"• `{m}`" for m in user_tracked])
    await interaction.response.send_message(embed=discord.Embed(
        title="your tracked mods",
        description=f"i'll let you know when these update:\n\n{mod_list}",
        color=0x9b59b6
    ))

@bot.tree.command(name="erymanthus", description="check if your mod idea exists already")
async def erymanthus(interaction: discord.Interaction, search: str, max_results: discord.app_commands.Range[int, 1, 5] = 3):
    if contains_banned_word(search):
        return await interaction.response.send_message("nope, let's keep it clean.", ephemeral=True)

    await interaction.response.defer()
    data = await bot.fetch_mods_list(query=search, sort="downloads", page=1, per_page=max_results)
    mods = data.get("data", [])

    if not mods:
        embed = discord.Embed(title="idea check: clear", description=f"nothing found for **{search}**. you're good to go!", color=0x2ecc71)
    else:
        embed = discord.Embed(title="idea check: similar stuff found", description=f"take a look at these first:\n\n", color=0xe67e22)
        for m in mods:
            mod_id, desc = m.get("id") or "unknown.id", find_description(m)
            embed.description += f"**[{find_name(m, mod_id)}](https://geode-sdk.org/mods/{mod_id})** (`{mod_id}`)\n> {desc[:82] + '...' if len(desc)>85 else desc}\n\n"

    embed.description += "\n*note: this just checks titles and descriptions.*"
    embed.set_author(name=f"total mods checked: {data.get('count', 0):,}")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="dev", description="dev tools for geode")
@discord.app_commands.choices(command=[
    discord.app_commands.Choice(name="repo", value="repo"),
    discord.app_commands.Choice(name="ery string generator", value="ery_string_generator"),
])
async def dev(interaction: discord.Interaction, command: discord.app_commands.Choice[str], mod_id: Optional[str] = None):
    cmd = command.value
    if cmd != "repo": mod_id = None
    if mod_id and contains_banned_word(mod_id): return await interaction.response.send_message("clean language only, please.", ephemeral=True)

    if cmd == "repo":
        if not mod_id: return await interaction.response.send_message("you need to give me a `mod_id` for that.", ephemeral=True)
        await interaction.response.defer()
        try:
            async with bot.session.get(api_url.format(mod_id)) as r:
                if r.status == 200:
                    mod_obj = normalize_single_mod_response(await r.json())
                    links = mod_obj.get("links", {})
                    src = links.get("source") or links.get("repository") or mod_obj.get("repository") or mod_obj.get("source")
                    await interaction.followup.send(f"**source for `{mod_id}`:**\n{src}" if src else f"couldn't find a repo link for `{mod_id}`.")
                elif r.status == 404:
                    await interaction.followup.send(f"mod `{mod_id}` doesn't seem to exist.")
                else:
                    await interaction.followup.send(f"api is acting up: http {r.status}")
        except Exception as e:
            await interaction.followup.send(f"error grabbing repo: {format_error_reason(e)}")

    elif cmd == "ery_string_generator":
        magic = f"ERYMANTHUS_MAGIC_STRING_TRIGGER_ACCEPT_MY_MOD_{''.join(random.choices(string.ascii_uppercase + string.digits, k=64))}"
        await interaction.response.send_message(embed=discord.Embed(title="magic string generator", description="here's your bypass string", color=0x9b59b6).add_field(name="output", value=f"```\n{magic}\n```"))

@dev.autocomplete("mod_id")
async def dev_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

def main():
    if not all([token, cf_account, cf_db, cf_token]):
        raise RuntimeError("missing some environment variables (discord token or cloudflare credentials). check your setup!")
    bot.run(token)

if __name__ == "__main__":
    main()
