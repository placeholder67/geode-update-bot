import asyncio
import logging
import os
import re
import random
import string
import time
from datetime import datetime, timezone
from typing import Any, Optional, Dict, Tuple, List

import aiohttp
import discord
from discord.ext import commands, tasks

# --- env vars ---
token = (os.getenv("DISCORD_TOKEN") or "").strip()
cf_account = (os.getenv("CF_ACCOUNT_ID") or "").strip()
cf_db = (os.getenv("CF_DATABASE_ID") or "").strip()
cf_token = (os.getenv("CF_API_TOKEN") or "").strip()

if cf_token.lower().startswith("bearer "):
    cf_token = cf_token[7:].strip()

api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("geode")

_API_CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL = 300  

_ALL_MODS_CACHE: List[dict] = []
_TAGS_CACHE: List[str] = [
    "universal", "gameplay", "editor", "offline", "online", 
    "enhancement", "music", "interface", "bugfix", "utility", 
    "performance", "customization", "content", "developer", 
    "cheat", "paid", "joke"
]

def get_cached_response(key: str):
    if key in _API_CACHE:
        timestamp, data = _API_CACHE[key]
        if time.time() - timestamp < CACHE_TTL:
            return data
        else:
            del _API_CACHE[key]
    return None

def set_cached_response(key: str, data: Any):
    _API_CACHE[key] = (time.time(), data)

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

# --- cloudflare d1 tracker ---
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
            return False

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

# --- helper funcs ---
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
    if not isinstance(d, dict): return "no description provided."
    candidates = [d.get("description"), d.get("summary")]
    versions = d.get("versions")
    if isinstance(versions, list) and versions and isinstance(versions[0], dict):
        candidates.extend([versions[0].get("description"), versions[0].get("summary")])
    for c in candidates:
        if isinstance(c, str) and c.strip(): return c.strip()
    return "no description provided."

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

# --- ui & embed builders ---
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
            return await interaction.response.send_message("couldn't track down the mod id on this one, my bad.", ephemeral=True)

        added = await tracker.add_tracking(interaction.client.session, mod_id, version, interaction.user.id)
        if added:
            await interaction.response.send_message(
                f"🔔 **got it.** i'll slide into your dms when `{mod_id}` updates.\n*(just use `/untrack {mod_id}` in my dms if you want me to stop)*", 
                ephemeral=True
            )
        else:
            await interaction.response.send_message("you're already tracking this one, you're good.", ephemeral=True)

def build_single_mod_embed(mod_data: dict, ms_time: float, show_invite: bool) -> discord.Embed:
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
        
    footer_text = f"geode index | ⏱️ {ms_time:.1f}ms"
    if show_invite:
        footer_text += " | run /invite to add me to your server!"
    
    embed.set_footer(text=footer_text)
    return embed

def build_list_embeds(title: str, mods: list, page: int, total_pages: int, per_page: int, total_mods: int, ms_time: float, show_invite: bool) -> list[discord.Embed]:
    footer_text = f"page {page}/{max(1, total_pages)} • ⏱️ {ms_time:.1f}ms"
    if show_invite:
        footer_text += " • run /invite to add me!"

    if not mods:
        embed = discord.Embed(title=title, color=0x5865F2, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=f"total mods: {total_mods:,}")
        embed.description = "*couldn't find any mods with that.*"
        embed.set_footer(text=footer_text)
        return [embed]

    embeds = []
    start_idx = (page - 1) * per_page + 1
    
    for i, m in enumerate(mods, start_idx):
        mod_id = m.get("id") or "unknown.id"
        desc = find_description(m)
        desc = desc[:110] + "..." if len(desc) > 115 else desc
        
        mod_url = f"https://geode-sdk.org/mods/{mod_id}"
        
        e = discord.Embed(color=0x5865F2)
        
        if i == start_idx:
            e.title = title
            e.set_author(name=f"total mods: {total_mods:,}")
            
        e.description = (
            f"**{i}. [{find_name(m, mod_id)}]({mod_url})** — by {find_developer(m)}\n"
            f"📦 **id:** `{mod_id}`\n"
            f"⬇️ **downloads:** {find_downloads(m) or 0:,}\n"
            f"📖 *{desc}*"
        )
        
        e.set_thumbnail(url=find_logo(mod_id))

        if i == start_idx + len(mods) - 1:
            e.timestamp = datetime.now(timezone.utc)
            e.set_footer(text=footer_text)
            
        embeds.append(e)

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
        
        start = time.perf_counter()
        mod_data = await interaction.client.fetch_single_mod(self.values[0])
        ms = (time.perf_counter() - start) * 1000
        show_invite = random.random() < 0.15
        
        if "error" in mod_data:
            return await interaction.followup.send(f"ah man, ran into an issue fetching that mod: {mod_data['error']}", ephemeral=True)
            
        await interaction.followup.send(embed=build_single_mod_embed(mod_data, ms, show_invite), view=NotifyView(), ephemeral=True)

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
            await interaction.response.send_message("doesn't look like a valid number, give it another go.", ephemeral=True)

class ModSearchView(discord.ui.View):
    def __init__(self, bot, query: str = None, sort_mode: str = "downloads", per_page: int = 3, platform: str = None, tag: str = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.query = query
        self.sort_mode = sort_mode
        self.page = 1
        self.per_page = per_page
        self.platform = platform
        self.tag = tag
        self.total_pages = 1
        self.total_mods = 0
        self.mods = []

    async def load_data(self):
        featured = (self.sort_mode == "featured")
        status_val = "pending" if self.sort_mode == "pending" else None
        actual_sort = "recently_updated" if self.sort_mode == "pending" else ("downloads" if featured else self.sort_mode)
        
        data = await self.bot.fetch_mods_list(
            query=self.query, 
            sort=actual_sort, 
            status=status_val,
            featured=featured,
            platforms=self.platform,
            tags=self.tag,
            page=self.page, 
            per_page=self.per_page
        )
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
        start = time.perf_counter()
        await self.load_data()
        ms = (time.perf_counter() - start) * 1000
        show_invite = random.random() < 0.15
        
        self.update_items()
        
        titles = {
            "featured": "featured mods",
            "recently_updated": "recently updated mods",
            "recently_published": "the recent tab!",
            "downloads": "trending mods",
            "pending": "pending mods"
        }
        title = f"search: {self.query} ({titles.get(self.sort_mode, '')})" if self.query else titles.get(self.sort_mode, "trending mods")
        if self.platform: title += f" | {self.platform}"
        if self.tag: title += f" | tag: {self.tag}"
        
        return build_list_embeds(title, self.mods, self.page, self.total_pages, self.per_page, self.total_mods, ms, show_invite)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await interaction.response.edit_message(embeds=await self.generate_view(), view=self)

    @discord.ui.button(label="page...", style=discord.ButtonStyle.secondary)
    async def btn_jump(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PageModal(self))

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embeds=await self.generate_view(), view=self)

# --- bot class & setup ---
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.add_view(NotifyView())
        await self.tree.sync()
        self.refresh_all_mods_cache.start()
        self.check_mod_updates.start()
        log.info("bot is online and commands are synced, ready to roll.")

    async def update_presence(self):
        total_members = sum(guild.member_count or 0 for guild in self.guilds)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"the geode index <3 | {len(self.guilds):,} servers | {total_members:,} members"
        ))

    async def on_ready(self):
        await self.update_presence()
        log.info(f"we are logged in as {self.user}, let's go.")

    async def on_guild_join(self, guild): await self.update_presence()
    async def on_guild_remove(self, guild): await self.update_presence()

    async def close(self):
        if self.session: await self.session.close()
        await super().close()

    async def fetch_single_mod(self, mod_id: str, bypass_cache: bool = False) -> dict:
        cache_key = f"mod_{mod_id}"
        if not bypass_cache:
            cached = get_cached_response(cache_key)
            if cached: return cached

        try:
            async with self.session.get(api_url.format(mod_id)) as r:
                if r.status == 404: return {"error": "mod not found"}
                r.raise_for_status()
                data = normalize_single_mod_response(await r.json())
                set_cached_response(cache_key, data)
                return data
        except Exception as e:
            return {"error": format_error_reason(e)}

    async def fetch_mods_list(self, query: str = None, developer: str = None, sort: str = "downloads", status: str = None, featured: bool = False, platforms: str = None, tags: str = None, page: int = 1, per_page: int = 3) -> dict:
        cache_key = f"list_{query}_{developer}_{sort}_{status}_{featured}_{platforms}_{tags}_{page}_{per_page}"
        cached = get_cached_response(cache_key)
        if cached: return cached

        params = {"page": page, "per_page": per_page, "sort": sort}
        if query: params["query"] = query
        if developer: params["developer"] = developer
        if featured: params["featured"] = "true"
        if status: params["status"] = status
        if platforms: params["platforms"] = platforms
        if tags: params["tags"] = tags

        try:
            async with self.session.get("https://api.geode-sdk.org/v1/mods", params=params) as r:
                data = normalize_list_response(await r.json()) if r.status == 200 else {"count": 0, "data": []}
                if r.status == 200:
                    set_cached_response(cache_key, data)
                return data
        except Exception:
            return {"count": 0, "data": []}

    @tasks.loop(minutes=30)
    async def refresh_all_mods_cache(self):
        if not self.session: return
        try:
            # refresh tags first directly from geode api
            try:
                async with self.session.get("https://api.geode-sdk.org/v1/tags") as r:
                    if r.status == 200:
                        data = await r.json()
                        tags_list = []
                        if isinstance(data, dict):
                            payload = data.get("payload", data.get("data", data))
                            if isinstance(payload, list): tags_list = payload
                        elif isinstance(data, list):
                            tags_list = data
                        
                        if tags_list:
                            global _TAGS_CACHE
                            new_tags = []
                            for t in tags_list:
                                if isinstance(t, str): new_tags.append(t)
                                elif isinstance(t, dict) and "name" in t: new_tags.append(str(t["name"]))
                            if new_tags:
                                _TAGS_CACHE = new_tags
            except Exception as e:
                log.warning(f"couldn't update tags cache: {e}")

            all_mods = []
            page = 1
            per_page = 100
            
            while True:
                data = await self.fetch_mods_list(sort="downloads", page=page, per_page=per_page)
                mods = data.get("data", [])
                if not mods:
                    break
                
                for m in mods:
                    mod_id = m.get('id') or 'unknown'
                    name = find_name(m, mod_id)
                    all_mods.append({"id": mod_id, "name": name})
                
                if len(mods) < per_page:
                    break
                    
                page += 1
                await asyncio.sleep(0.5) 
                
            global _ALL_MODS_CACHE
            if all_mods:
                _ALL_MODS_CACHE = all_mods
                log.info(f"refreshed in-memory cache with {len(_ALL_MODS_CACHE)} mods and updated tags.")
        except Exception as e:
            log.warning(f"cache pipeline exception: {e}")

    @refresh_all_mods_cache.before_loop
    async def before_refresh_all(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def check_mod_updates(self):
        if not self.session: return
        
        try:
            tracking_data = await tracker.get_all_tracking(self.session)
            
            for mod_id, data in tracking_data.items():
                try:
                    if not data["users"]: continue
                        
                    mod_resp = await self.fetch_single_mod(mod_id, bypass_cache=True)
                    if "error" in mod_resp: continue

                    latest_v = find_version(mod_resp) or data["version"]
                    if latest_v != data["version"]:
                        embed = build_single_mod_embed(mod_resp, ms_time=0.0, show_invite=False)
                        
                        for uid in data["users"]:
                            try:
                                user_id = int(uid)
                                user = self.get_user(user_id) or await self.fetch_user(user_id)
                                if user:
                                    await user.send(f"🔔 **update alert!**\n**{find_name(mod_resp, mod_id)}** just updated to **{latest_v}**!", embed=embed, view=NotifyView())
                            except discord.Forbidden:
                                pass
                            except discord.HTTPException as e:
                                log.warning(f"http error sending update to {uid}: {e}")
                            except Exception as e:
                                log.warning(f"couldn't send update to {uid}: {e}")
                            
                            await asyncio.sleep(0.2)
                        
                        await tracker.update_version(self.session, mod_id, latest_v)
                except Exception as e:
                    log.error(f"error processing mod {mod_id} updates: {e}")
        except Exception as e:
            log.error(f"error fetching tracking data: {e}")

    @check_mod_updates.before_loop
    async def before_check_mod_updates(self):
        await self.wait_until_ready()

bot = Bot()

async def mod_autocomplete_logic(current: str):
    if contains_banned_word(current):
        return []
        
    if not _ALL_MODS_CACHE:
        data = await bot.fetch_mods_list(query=current, sort="downloads", page=1, per_page=15)
        mods = data.get("data", [])
        return [discord.app_commands.Choice(name=f"{find_name(m, m.get('id') or 'unknown')} ({m.get('id') or 'unknown'})", value=m.get('id') or "unknown") for m in mods][:25]
    
    if not current:
        return [discord.app_commands.Choice(name=f"{m['name']} ({m['id']})", value=m['id']) for m in _ALL_MODS_CACHE[:25]]
        
    current_lower = current.lower()
    
    # fast native substring check replaces the map logic
    local_matches = [
        m for m in _ALL_MODS_CACHE 
        if current_lower in m["id"].lower() or current_lower in m["name"].lower()
    ]
    
    return [discord.app_commands.Choice(name=f"{m['name']} ({m['id']})", value=m['id']) for m in local_matches][:25]

# --- commands ---

@bot.tree.command(name="getindex", description="browse geode mods or search the index")
@discord.app_commands.describe(
    mod_id="specific mod to view", 
    search="search mod by name", 
    sort_by="sort the mod list", 
    tags="filter by tag", 
    platform="filter by platform", 
    per_page="mods per page (1-5)"
)
@discord.app_commands.choices(sort_by=[
    discord.app_commands.Choice(name="featured", value="featured"),
    discord.app_commands.Choice(name="recently updated", value="recently_updated"),
    discord.app_commands.Choice(name="recent", value="recently_published"),
    discord.app_commands.Choice(name="pending", value="pending"),
])
async def checkforupdates(
    interaction: discord.Interaction, 
    mod_id: Optional[str] = None, 
    search: Optional[str] = None, 
    sort_by: Optional[discord.app_commands.Choice[str]] = None, 
    tags: Optional[str] = None, 
    platform: Optional[str] = None, 
    per_page: discord.app_commands.Range[int, 1, 5] = 3
):
    if search or sort_by or platform or tags: mod_id = None
    if (search and contains_banned_word(search)) or (mod_id and contains_banned_word(mod_id)) or (tags and contains_banned_word(tags)):
        return await interaction.response.send_message("let's keep the words clean, man.", ephemeral=True)

    await interaction.response.defer()

    if mod_id:
        start = time.perf_counter()
        mod_data = await bot.fetch_single_mod(mod_id)
        ms = (time.perf_counter() - start) * 1000
        show_invite = random.random() < 0.15
        
        if "error" in mod_data:
            return await interaction.followup.send(f"ah, {mod_data['error']}")
        await interaction.followup.send(embed=build_single_mod_embed(mod_data, ms, show_invite), view=NotifyView())
    else:
        view = ModSearchView(
            bot, 
            query=search, 
            sort_mode=sort_by.value if sort_by else "downloads", 
            per_page=per_page,
            platform=platform,
            tag=tags
        )
        await interaction.followup.send(embeds=await view.generate_view(), view=view)

@checkforupdates.autocomplete("mod_id")
async def checkforupdates_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

@checkforupdates.autocomplete("tags")
async def checkforupdates_tags_autocomplete(interaction: discord.Interaction, current: str):
    matches = [t for t in _TAGS_CACHE if current.lower() in t.lower()][:25]
    return [discord.app_commands.Choice(name=t, value=t) for t in matches]

@checkforupdates.autocomplete("platform")
async def checkforupdates_platform_autocomplete(interaction: discord.Interaction, current: str):
    platforms_map = {
        "windows": "windows",
        "mac": "macos",
        "android": "android",
        "ios": "ios",
        "android32": "android32",
        "android64": "android64"
    }
    matches = [
        discord.app_commands.Choice(name=name, value=val)
        for name, val in platforms_map.items()
        if current.lower() in name.lower() or current.lower() in val.lower()
    ][:25]
    return matches

@bot.tree.command(name="invite", description="add this bot to your own servers!")
async def invite_cmd(interaction: discord.Interaction):
    link = f"https://discord.com/oauth2/authorize?client_id={bot.user.id}&permissions=274877975552&scope=bot%20applications.commands"
    embed = discord.Embed(
        title="put me in your server!",
        description=f"stop manually checking for geode mod updates like a caveman.\n\n[click here to invite me]({link}) and let me do the heavy lifting. your server needs this.",
        color=0x5865F2
    )
    embed.set_footer(text="the best geode tracker on discord.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="untrack", description="stop getting dm notifications for a specific mod")
@discord.app_commands.describe(mod_id="the id of the mod to stop tracking")
async def untrack_cmd(interaction: discord.Interaction, mod_id: str):
    if interaction.guild is not None:
        return await interaction.response.send_message("❌ hop into my dms to use this, keeps it between us.", ephemeral=True)

    removed = await tracker.remove_tracking(bot.session, mod_id, interaction.user.id)
    if removed:
        await interaction.response.send_message(f"✅ done deal, i won't bother you about `{mod_id}` anymore.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ doesn't look like you're tracking `{mod_id}` anyway.", ephemeral=True)

@untrack_cmd.autocomplete("mod_id")
async def untrack_autocomplete(interaction: discord.Interaction, current: str):
    user_tracked = await tracker.get_user_tracked_mods(bot.session, interaction.user.id)
    return [discord.app_commands.Choice(name=m, value=m) for m in user_tracked if current.lower() in m.lower()][:25]

@bot.tree.command(name="tracked", description="view all mods you are tracking")
async def tracked_cmd(interaction: discord.Interaction):
    if interaction.guild is not None:
        return await interaction.response.send_message("❌ hop into my dms for this one!", ephemeral=True)

    user_tracked = await tracker.get_user_tracked_mods(bot.session, interaction.user.id)
    if not user_tracked:
        return await interaction.response.send_message("you aren't tracking anything yet. just run `/getindex` in a server and hit 'notify me!' to start.", ephemeral=True)

    mod_list = "\n".join([f"• `{m}`" for m in user_tracked])
    await interaction.response.send_message(embed=discord.Embed(
        title="your tracked mods",
        description=f"i'll let you know when these update:\n\n{mod_list}",
        color=0x9b59b6
    ))

@bot.tree.command(name="erymanthus", description="check if your mod idea exists already")
async def erymanthus(interaction: discord.Interaction, search: str, max_results: discord.app_commands.Range[int, 1, 5] = 3):
    if contains_banned_word(search):
        return await interaction.response.send_message("nah, let's keep the language clean.", ephemeral=True)

    await interaction.response.defer()
    
    start = time.perf_counter()
    data = await bot.fetch_mods_list(query=search, sort="downloads", page=1, per_page=max_results)
    ms = (time.perf_counter() - start) * 1000
    
    mods = data.get("data", [])

    if not mods:
        embed = discord.Embed(title="idea check: you're clear", description=f"didn't find anything for **{search}**. you're good to start cooking!", color=0x2ecc71)
    else:
        embed = discord.Embed(title="idea check: found some similar stuff", description=f"you might want to peek at these first:\n\n", color=0xe67e22)
        for m in mods:
            mod_id, desc = m.get("id") or "unknown.id", find_description(m)
            embed.description += f"**[{find_name(m, mod_id)}](https://geode-sdk.org/mods/{mod_id})** (`{mod_id}`)\n> {desc[:82] + '...' if len(desc)>85 else desc}\n\n"

    embed.description += "\n*note: this just checks titles and descriptions.*"
    embed.set_author(name=f"total mods checked: {data.get('count', 0):,}")
    embed.set_footer(text=f"⏱️ {ms:.1f}ms")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="dev", description="dev tools for geode")
@discord.app_commands.choices(command=[
    discord.app_commands.Choice(name="repo", value="repo"),
    discord.app_commands.Choice(name="ery string generator", value="ery_string_generator"),
])
async def dev(interaction: discord.Interaction, command: discord.app_commands.Choice[str], mod_id: Optional[str] = None):
    cmd = command.value
    if cmd != "repo": mod_id = None
    if mod_id and contains_banned_word(mod_id): return await interaction.response.send_message("let's keep it clean, please.", ephemeral=True)

    if cmd == "repo":
        if not mod_id: return await interaction.response.send_message("gonna need a `mod_id` for that to work.", ephemeral=True)
        await interaction.response.defer()
        
        start = time.perf_counter()
        try:
            async with bot.session.get(api_url.format(mod_id)) as r:
                ms = (time.perf_counter() - start) * 1000
                if r.status == 200:
                    mod_obj = normalize_single_mod_response(await r.json())
                    links = mod_obj.get("links", {})
                    src = links.get("source") or links.get("repository") or mod_obj.get("repository") or mod_obj.get("source")
                    await interaction.followup.send(f"**source for `{mod_id}`:**\n{src}\n*(took {ms:.1f}ms)*" if src else f"couldn't find a repo link for `{mod_id}`. *(took {ms:.1f}ms)*")
                elif r.status == 404:
                    await interaction.followup.send(f"doesn't look like mod `{mod_id}` exists. *(took {ms:.1f}ms)*")
                else:
                    await interaction.followup.send(f"api is being a bit weird right now: http {r.status} *(took {ms:.1f}ms)*")
        except Exception as e:
            await interaction.followup.send(f"ran into a snag grabbing the repo: {format_error_reason(e)}")

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
