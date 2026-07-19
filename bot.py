import asyncio
import logging
import os
import re
import random
import string
import time
import base64
from datetime import datetime, timezone
from typing import Any, Optional, Dict, Tuple, List, Union

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

_PENDING_TRACKS: Dict[str, str] = {}

def cache_pending_track(track_id: str) -> str:
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    _PENDING_TRACKS[nonce] = track_id
    return nonce

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

# --- banned words filter ---
BANNED_WORDS = [
    "nigger", "nigga", "faggot", "fag", 
    "dyke", "tranny", "kys", "retard"
]

def contains_banned_word(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()
    for banned in BANNED_WORDS:
        if re.search(rf"\b{re.escape(banned)}\b", text_lower):
            return True
    return False

# --- state management for immortal views ---
SORT_MAP_ENC = {"recently_updated": "ru", "recently_published": "rp", "downloads": "dl", "pending": "pd", "featured": "ft"}
SORT_MAP_DEC = {v: k for k, v in SORT_MAP_ENC.items()}

def encode_state(action: str, query: str, sort_mode: str, page: int, per_page: int, platform: str, tag: str, developer: str) -> str:
    q = (query or "").replace("|", "")[:20]
    sm = SORT_MAP_ENC.get(sort_mode, "dl")
    p = (platform or "").replace("|", "")
    t = (tag or "").replace("|", "")
    d = (developer or "").replace("|", "")[:20]
    return f"modnav|{action}|{q}|{sm}|{page}|{per_page}|{p}|{t}|{d}"

def decode_state(custom_id: str) -> Optional[dict]:
    parts = custom_id.split("|")
    if len(parts) != 9: return None
    _, action, q, sm, page, per_page, p, t, d = parts
    return {
        "action": action,
        "query": q if q else None,
        "sort_mode": SORT_MAP_DEC.get(sm, "downloads"),
        "page": int(page) if page.isdigit() else 1,
        "per_page": int(per_page) if per_page.isdigit() else 3,
        "platform": p if p else None,
        "tag": t if t else None,
        "developer": d if d else None
    }

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
                    log.warning(f"D1 query failure: {text}")
                    return {}
                return await resp.json()
        except Exception as e:
            log.warning(f"D1 connection error: {e}")
            return {}

    async def add_tracking(self, session: aiohttp.ClientSession, mods: str, version: str, track_id: str) -> bool:
        if track_id.startswith("guild:"):
            parts = track_id.split(":")
            if len(parts) >= 4:
                # Stop double rows by updating if a tracking instance for this specific channel already exists
                prefix = f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}:%"
                check_sql = "SELECT * FROM tracking WHERE mods = ? AND id LIKE ?"
                res = await self.query(session, check_sql, [mods, prefix])
                
                results = res.get("result", [{}])[0].get("results", [])
                if results:
                    old_id = results[0]["id"]
                    if old_id == track_id:
                        return False
                    
                    upd_sql = "UPDATE tracking SET id = ? WHERE mods = ? AND id = ?"
                    await self.query(session, upd_sql, [track_id, mods, old_id])
                    return True 

        check_sql = "SELECT * FROM tracking WHERE mods = ? AND id = ?"
        res = await self.query(session, check_sql, [mods, track_id])
        results = res.get("result", [{}])[0].get("results", [])
        if results: return False

        insert_sql = "INSERT INTO tracking (mods, version, id) VALUES (?, ?, ?)"
        await self.query(session, insert_sql, [mods, version, track_id])
        return True

    async def remove_tracking(self, session: aiohttp.ClientSession, mods: str, track_id: str) -> bool:
        del_sql = "DELETE FROM tracking WHERE mods = ? AND id = ? RETURNING *"
        res = await self.query(session, del_sql, [mods, track_id])
        return len(res.get("result", [{}])[0].get("results", [])) > 0
        
    async def remove_tracking_prefix(self, session: aiohttp.ClientSession, mods: str, guild_id: int, channel_id: str) -> bool:
        prefix = f"guild:{guild_id}:channel:{channel_id}:%"
        del_sql = "DELETE FROM tracking WHERE mods = ? AND id LIKE ? RETURNING *"
        res = await self.query(session, del_sql, [mods, prefix])
        return len(res.get("result", [{}])[0].get("results", [])) > 0

    async def get_user_tracked_mods(self, session: aiohttp.ClientSession, track_id: str) -> list:
        sql = "SELECT mods FROM tracking WHERE id = ?"
        res = await self.query(session, sql, [track_id])
        return [row["mods"] for row in res.get("result", [{}])[0].get("results", [])]

    async def get_all_tracking(self, session: aiohttp.ClientSession) -> dict:
        sql = "SELECT mods, version, id FROM tracking"
        res = await self.query(session, sql)
        results = res.get("result", [{}])[0].get("results", [])
        
        grouped = {}
        for row in results:
            m_id = row["mods"]
            if m_id not in grouped:
                grouped[m_id] = {"version": row["version"], "users": []}
            grouped[m_id]["users"].append(row["id"])
        return grouped

    async def update_version(self, session: aiohttp.ClientSession, mods: str, new_version: str):
        sql = "UPDATE tracking SET version = ? WHERE mods = ?"
        await self.query(session, sql, [new_version, mods])

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
    if not isinstance(d, dict): return False
    versions = d.get("versions")
    if not isinstance(versions, list) or not versions: return False
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
            return await interaction.response.send_message("couldn't track down the mod id on this one.", ephemeral=True)

        added = await tracker.add_tracking(interaction.client.session, mod_id, version, str(interaction.user.id))
        if added:
            await interaction.response.send_message(
                f"🔔 **got it.** i'll dm you when `{mod_id}` updates.\n*(use `/untrack {mod_id}` in my dms if you want me to stop)*", 
                ephemeral=True
            )
        else:
            await interaction.response.send_message("you're already tracking this mod.", ephemeral=True)

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

class StatefulPageModal(discord.ui.Modal, title="jump to page"):
    page_num = discord.ui.TextInput(label="page number", style=discord.TextStyle.short, placeholder="enter a page...", required=True)

    def __init__(self, state: dict):
        super().__init__()
        self.state = state

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_page = int(self.page_num.value.strip())
            self.state["page"] = max(1, new_page)
            kwargs = {k: v for k, v in self.state.items() if k != "action"}
            
            view = ModSearchView(interaction.client, **kwargs)
            embeds = await view.generate_view()
            await interaction.response.edit_message(embeds=embeds, view=view)
        except ValueError:
            await interaction.response.send_message("invalid page number.", ephemeral=True)

class ServerNotifySearchView(discord.ui.View):
    def __init__(self, bot, nonce: str, query: str = None, sort_mode: str = "downloads", platform: str = None, tag: str = None, developer: str = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.nonce = nonce
        self.query = query
        self.sort_mode = sort_mode
        self.platform = platform
        self.tag = tag
        self.developer = developer

    async def generate_view(self):
        start = time.perf_counter()
        featured = (self.sort_mode == "featured")
        status_val = "pending" if self.sort_mode == "pending" else None
        actual_sort = "recently_updated" if self.sort_mode == "pending" else ("downloads" if featured else self.sort_mode)
        
        data = await self.bot.fetch_mods_list(
            query=self.query, developer=self.developer, sort=actual_sort, status=status_val,
            featured=featured, platforms=self.platform, tags=self.tag,
            page=1, per_page=5
        )
        
        mods = data.get("data", [])
        total_mods = data.get("count", 0)
        ms = (time.perf_counter() - start) * 1000
        
        self.clear_items()
        
        if mods:
            options = []
            for m in mods:
                mod_id = (m.get("id") or "unknown.id")[:90]
                desc = find_description(m)
                options.append(discord.SelectOption(
                    label=find_name(m, mod_id)[:90], 
                    description=desc[:92] + "..." if len(desc) > 95 else desc, 
                    value=mod_id
                ))
            self.add_item(discord.ui.Select(
                placeholder="pick a mod to track in the server...", min_values=1, max_values=1,
                options=options, custom_id=f"sn_mod_select|{self.nonce}"
            ))

        base_title = "server notify search"
        title = f"search: {self.query}" if self.query else base_title
        
        return build_list_embeds(title, mods, 1, 1, 5, total_mods, ms, False)

class ModSearchView(discord.ui.View):
    def __init__(self, bot, query: str = None, sort_mode: str = "downloads", page: int = 1, per_page: int = 3, platform: str = None, tag: str = None, developer: str = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.query = query
        self.sort_mode = sort_mode
        self.page = page
        self.per_page = per_page
        self.platform = platform
        self.tag = tag
        self.developer = developer
        
        self.total_pages = 1
        self.total_mods = 0
        self.mods = []

    async def generate_view(self):
        start = time.perf_counter()
        
        featured = (self.sort_mode == "featured")
        status_val = "pending" if self.sort_mode == "pending" else None
        actual_sort = "recently_updated" if self.sort_mode == "pending" else ("downloads" if featured else self.sort_mode)
        
        data = await self.bot.fetch_mods_list(
            query=self.query, developer=self.developer, sort=actual_sort, status=status_val,
            featured=featured, platforms=self.platform, tags=self.tag,
            page=self.page, per_page=self.per_page
        )
        
        self.mods = data.get("data", [])
        self.total_mods = data.get("count", 0)
        self.total_pages = max(1, (self.total_mods + self.per_page - 1) // self.per_page)
        
        # bounds check fix
        if self.page > self.total_pages and self.total_pages > 0:
            self.page = self.total_pages
            data = await self.bot.fetch_mods_list(
                query=self.query, developer=self.developer, sort=actual_sort, status=status_val,
                featured=featured, platforms=self.platform, tags=self.tag,
                page=self.page, per_page=self.per_page
            )
            self.mods = data.get("data", [])
            
        ms = (time.perf_counter() - start) * 1000
        show_invite = random.random() < 0.15
        
        self.clear_items()
        
        self.add_item(discord.ui.Button(
            label="<", style=discord.ButtonStyle.secondary,
            custom_id=encode_state("prev", self.query, self.sort_mode, self.page, self.per_page, self.platform, self.tag, self.developer),
            disabled=self.page <= 1
        ))
        self.add_item(discord.ui.Button(
            label="page...", style=discord.ButtonStyle.secondary,
            custom_id=encode_state("jump", self.query, self.sort_mode, self.page, self.per_page, self.platform, self.tag, self.developer),
            disabled=self.total_pages <= 1
        ))
        self.add_item(discord.ui.Button(
            label=">", style=discord.ButtonStyle.secondary,
            custom_id=encode_state("next", self.query, self.sort_mode, self.page, self.per_page, self.platform, self.tag, self.developer),
            disabled=self.page >= self.total_pages
        ))
        
        if self.mods:
            options = []
            for m in self.mods:
                mod_id = (m.get("id") or "unknown.id")[:90]
                desc = find_description(m)
                options.append(discord.SelectOption(
                    label=find_name(m, mod_id)[:90], 
                    description=desc[:92] + "..." if len(desc) > 95 else desc, 
                    value=mod_id
                ))
            self.add_item(discord.ui.Select(
                placeholder="pick a mod to view...", min_values=1, max_values=1,
                options=options, custom_id="mod_select"
            ))

        titles = {
            "featured": "featured mods",
            "recently_updated": "recently updated mods",
            "recently_published": "the recent tab!",
            "downloads": "trending mods",
            "pending": "pending mods"
        }
        
        base_title = titles.get(self.sort_mode, "trending mods")
        if self.query:
            title = f"search: {self.query} ({base_title})"
        elif self.platform:
            title = f"platform: {self.platform} ({base_title})"
        elif self.tag:
            title = f"tag: {self.tag} ({base_title})"
        elif self.developer:
            title = f"developer: {self.developer} ({base_title})"
        else:
            title = base_title
        
        return build_list_embeds(title, self.mods, self.page, self.total_pages, self.per_page, self.total_mods, ms, show_invite)


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
        log.info("bot online and commands synced.")

    async def update_presence(self):
        total_members = sum(guild.member_count or 0 for guild in self.guilds)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"geode index | {len(self.guilds):,} servers | {total_members:,} members"
        ))

    async def on_ready(self):
        await self.update_presence()
        log.info(f"bro is {self.user}")

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
                log.warning(f"failed to update tags cache: {e}")

            all_mods = []
            page = 1
            per_page = 100
            
            while True:
                data = await self.fetch_mods_list(sort="downloads", page=page, per_page=per_page)
                mods = data.get("data", [])
                if not mods: break
                
                for m in mods:
                    mod_id = m.get('id') or 'unknown'
                    name = find_name(m, mod_id)
                    mod_tags = m.get('tags', [])
                    dev = find_developer(m)
                    featured = bool(m.get('featured', False))
                    all_mods.append({"id": mod_id, "name": name, "tags": mod_tags, "developer": dev, "featured": featured})
                
                if len(mods) < per_page: break
                page += 1
                await asyncio.sleep(0.5) 
                
            global _ALL_MODS_CACHE
            if all_mods:
                _ALL_MODS_CACHE = all_mods
                log.info(f"refreshed in-memory cache with {len(_ALL_MODS_CACHE)} mods.")
        except Exception as e:
            log.warning(f"cache pipeline error: {e}")

    @refresh_all_mods_cache.before_loop
    async def before_refresh_all(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def check_mod_updates(self):
        if not self.session: return
        
        try:
            tracking_data = await tracker.get_all_tracking(self.session)
            
            for mods_val, data in tracking_data.items():
                try:
                    if not data["users"]: continue
                    
                    is_tag = mods_val.startswith("tag:")
                    is_dev = mods_val.startswith("dev:")
                    
                    latest_v = ""
                    current_sig = ""
                    embed = None
                    mod_data_for_name = None
                    
                    if is_tag or is_dev:
                        query_val = mods_val.split(":", 1)[1]
                        if is_tag:
                            list_data = await self.fetch_mods_list(tags=query_val, sort="recently_updated", per_page=1)
                        else:
                            list_data = await self.fetch_mods_list(developer=query_val, sort="recently_updated", per_page=1)
                            
                        mods_list = list_data.get("data", [])
                        if not mods_list: continue
                        
                        latest_mod = mods_list[0]
                        mod_data_for_name = latest_mod
                        latest_mod_id = latest_mod.get("id", "unknown")
                        latest_v = find_version(latest_mod) or "unknown"
                        current_sig = f"{latest_mod_id}:{latest_v}"
                        
                        if current_sig != data["version"]:
                            embed = build_single_mod_embed(latest_mod, ms_time=0.0, show_invite=False)
                            embed.title = f"🌟 New in {mods_val}! {embed.title}"
                    else:
                        mod_resp = await self.fetch_single_mod(mods_val, bypass_cache=True)
                        if "error" in mod_resp: continue
                        mod_data_for_name = mod_resp
                        latest_v = find_version(mod_resp) or data["version"]
                        current_sig = latest_v
                        
                        if latest_v != data["version"]:
                            embed = build_single_mod_embed(mod_resp, ms_time=0.0, show_invite=False)

                    if embed: 
                        for uid in data["users"]:
                            try:
                                uid_str = str(uid)
                                if uid_str.startswith("guild:"):
                                    # Format: guild:{g_id}:channel:{c_id}:ping:{p_id}:msg:{msg_b64}
                                    parts = uid_str.split(":")
                                    if len(parts) >= 8:
                                        c_id = int(parts[3])
                                        
                                        try:
                                            ping_text = base64.urlsafe_b64decode(parts[5]).decode('utf-8')
                                        except:
                                            ping_text = ""
                                            
                                        try:
                                            custom_msg = base64.urlsafe_b64decode(parts[7]).decode('utf-8')
                                        except:
                                            custom_msg = ""
                                            
                                        channel = self.get_channel(c_id) or await self.fetch_channel(c_id)
                                        if channel:
                                            ping_prefix = f"{ping_text} " if ping_text and ping_text != "none" else ""
                                            msg_content = f"🔔 **dude is mod!**\n"
                                            if custom_msg and custom_msg != "none":
                                                msg_content += f"{custom_msg}\n\n"
                                                
                                            if is_tag or is_dev:
                                                msg_content = f"{ping_prefix}{msg_content}Something new in **{mods_val}**!"
                                            else:
                                                msg_content = f"{ping_prefix}{msg_content}**{find_name(mod_data_for_name, mods_val)}** just updated to **{latest_v}**!"
                                            
                                            await channel.send(msg_content, embed=embed)
                                else:
                                    user_id = int(uid_str)
                                    user = self.get_user(user_id) or await self.fetch_user(user_id)
                                    if user:
                                        if is_tag or is_dev:
                                            await user.send(f"🔔 **update alert!**\nsomething new in **{mods_val}**!", embed=embed, view=NotifyView())
                                        else:
                                            await user.send(f"🔔 **update alert!**\n**{find_name(mod_data_for_name, mods_val)}** just updated to **{latest_v}**!", embed=embed, view=NotifyView())
                            except discord.Forbidden: pass
                            except Exception as e: log.warning(f"failed update send to {uid}: {e}")
                            
                            await asyncio.sleep(0.2)
                            
                        await tracker.update_version(self.session, mods_val, current_sig)
                except Exception as e:
                    log.error(f"error processing mod {mods_val} updates: {e}")
        except Exception as e:
            log.error(f"error fetching tracking data: {e}")

    @check_mod_updates.before_loop
    async def before_check_mod_updates(self):
        await self.wait_until_ready()

bot = Bot()

# --- immortal global listener for pagination/selects ---
@bot.listen("on_interaction")
async def global_component_handler(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
        
    custom_id = interaction.data.get("custom_id", "")
    
    if custom_id.startswith("modnav|"):
        state = decode_state(custom_id)
        if not state: return
        
        if state["action"] == "prev": state["page"] -= 1
        elif state["action"] == "next": state["page"] += 1
        elif state["action"] == "jump":
            return await interaction.response.send_modal(StatefulPageModal(state))
        
        kwargs = {k: v for k, v in state.items() if k != "action"}
        view = ModSearchView(bot, **kwargs)
        embeds = await view.generate_view()
        
        try:
            await interaction.response.edit_message(embeds=embeds, view=view)
        except discord.NotFound:
            pass
            
    elif custom_id == "mod_select":
        values = interaction.data.get("values", [])
        if not values: return
        mod_id = values[0]
        
        await interaction.response.defer(ephemeral=True)
        start = time.perf_counter()
        mod_data = await bot.fetch_single_mod(mod_id)
        ms = (time.perf_counter() - start) * 1000
        
        if "error" in mod_data:
            return await interaction.followup.send(f"failed to fetch mod: {mod_data['error']}", ephemeral=True)
            
        await interaction.followup.send(
            embed=build_single_mod_embed(mod_data, ms, random.random() < 0.15), 
            view=NotifyView(), 
            ephemeral=True
        )

    elif custom_id.startswith("sn_mod_select|"):
        values = interaction.data.get("values", [])
        if not values: return
        mod_id = values[0]
        nonce = custom_id.split("|", 1)[1]
        
        track_id = _PENDING_TRACKS.get(nonce)
        await interaction.response.defer(ephemeral=True)
        if not track_id:
            return await interaction.followup.send("⚠️ this menu has expired. please run the command again.", ephemeral=True)
            
        mod_data = await bot.fetch_single_mod(mod_id)
        if "error" in mod_data:
            return await interaction.followup.send(f"failed to fetch mod: {mod_data['error']}", ephemeral=True)
            
        version = find_version(mod_data) or "unknown"
        added = await tracker.add_tracking(bot.session, mod_id, version, track_id)
        if added:
            await interaction.followup.send(f"✅ successfully set up server notifications for `{mod_id}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ updated tracking configuration for `{mod_id}`.", ephemeral=True)
            
    elif custom_id == "sn_manage_remove":
        values = interaction.data.get("values", [])
        if not values: return
        val = values[0]
        m_id, c_id = val.split("|", 1)
        
        await interaction.response.defer(ephemeral=True)
        removed = await tracker.remove_tracking_prefix(bot.session, m_id, interaction.guild.id, c_id)
        if removed:
            await interaction.followup.send(f"✅ removed server tracking for `{m_id}` in <#{c_id}>.", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ failed to remove tracking.", ephemeral=True)

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
    
    local_matches = [
        m for m in _ALL_MODS_CACHE 
        if current_lower in m["id"].lower() or current_lower in m["name"].lower()
    ]
    return [discord.app_commands.Choice(name=f"{m['name']} ({m['id']})", value=m['id']) for m in local_matches][:25]

# --- commands ---

@bot.tree.command(name="getindex", description="browse geode mods or search the index")
@discord.app_commands.describe(
    sort_by="sort or filter mode", 
    search="search query, tag, platform, or developer name", 
    mod_id="specific mod to view", 
    per_page="mods per page (1-5)"
)
@discord.app_commands.choices(sort_by=[
    discord.app_commands.Choice(name="featured", value="featured"),
    discord.app_commands.Choice(name="recently updated", value="recently_updated"),
    discord.app_commands.Choice(name="recent", value="recently_published"),
    discord.app_commands.Choice(name="pending", value="pending"),
    discord.app_commands.Choice(name="by tag", value="tags"),
    discord.app_commands.Choice(name="by platform", value="platform"),
    discord.app_commands.Choice(name="by developer", value="developer"),
])
async def checkforupdates(
    interaction: discord.Interaction, 
    sort_by: Optional[discord.app_commands.Choice[str]] = None, 
    search: Optional[str] = None, 
    mod_id: Optional[str] = None, 
    per_page: discord.app_commands.Range[int, 1, 5] = 3
):
    if search or sort_by: mod_id = None
    if (search and contains_banned_word(search)) or (mod_id and contains_banned_word(mod_id)):
        return await interaction.response.send_message("lets not", ephemeral=True)

    await interaction.response.defer()

    if mod_id:
        start = time.perf_counter()
        mod_data = await bot.fetch_single_mod(mod_id)
        ms = (time.perf_counter() - start) * 1000
        show_invite = random.random() < 0.15
        
        if "error" in mod_data:
            return await interaction.followup.send(f"error: {mod_data['error']}")
        await interaction.followup.send(embed=build_single_mod_embed(mod_data, ms, show_invite), view=NotifyView())
    else:
        query = search
        sort_mode = "downloads"
        tag = None
        platform_val = None
        developer = None

        if sort_by:
            val = sort_by.value
            if val in ("featured", "recently_updated", "recently_published", "pending"):
                sort_mode = val
            elif val == "tags":
                tag = search
                query = None
            elif val == "platform":
                platform_val = search
                query = None
            elif val == "developer":
                developer = search
                query = None

        view = ModSearchView(
            bot, 
            query=query, 
            sort_mode=sort_mode, 
            page=1,
            per_page=per_page,
            platform=platform_val,
            tag=tag,
            developer=developer
        )
        embeds = await view.generate_view()
        await interaction.followup.send(embeds=embeds, view=view)

@checkforupdates.autocomplete("mod_id")
async def checkforupdates_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

@checkforupdates.autocomplete("search")
async def checkforupdates_search_autocomplete(interaction: discord.Interaction, current: str):
    sort_by = getattr(interaction.namespace, 'sort_by', None)
    
    if sort_by == "tags":
        matches = [t for t in _TAGS_CACHE if current.lower() in t.lower()][:25]
        return [discord.app_commands.Choice(name=t, value=t) for t in matches]
    elif sort_by == "platform":
        platforms_map = {
            "windows": "windows", "mac": "macos", "android": "android",
            "ios": "ios", "android32": "android32", "android64": "android64"
        }
        matches = [discord.app_commands.Choice(name=name, value=val)
                for name, val in platforms_map.items()
                if current.lower() in name.lower() or current.lower() in val.lower()][:25]
        return matches
    elif sort_by == "developer":
        devs = list({m.get("developer", "unknown") for m in _ALL_MODS_CACHE if m.get("developer") and m.get("developer") != "unknown"})
        matches = [d for d in devs if current.lower() in d.lower()][:25]
        return [discord.app_commands.Choice(name=d, value=d) for d in matches]
    
    return []

@bot.tree.command(name="servernotify", description="manage server notifications for mod updates")
@discord.app_commands.describe(
    action="add or manage server notifications",
    channel="channel to send updates in (required for add)",
    ping="member or role to ping on update (optional for add)",
    custom_message="custom message to attach (optional for add)",
    track_entire="track the entire tag/developer from your search",
    sort_by="sort or filter mode", 
    search="search query, tag, platform, or developer name", 
    mod_id="specific mod to view"
)
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="add", value="add"),
    discord.app_commands.Choice(name="manage", value="manage")
])
@discord.app_commands.choices(sort_by=[
    discord.app_commands.Choice(name="featured", value="featured"),
    discord.app_commands.Choice(name="recently updated", value="recently_updated"),
    discord.app_commands.Choice(name="recent", value="recently_published"),
    discord.app_commands.Choice(name="pending", value="pending"),
    discord.app_commands.Choice(name="by tag", value="tags"),
    discord.app_commands.Choice(name="by platform", value="platform"),
    discord.app_commands.Choice(name="by developer", value="developer"),
])
@discord.app_commands.guild_only()
async def servernotify_cmd(
    interaction: discord.Interaction, 
    action: discord.app_commands.Choice[str],
    channel: Optional[discord.TextChannel] = None,
    ping: Optional[Union[discord.Member, discord.Role]] = None,
    custom_message: Optional[str] = None,
    track_entire: bool = False,
    sort_by: Optional[discord.app_commands.Choice[str]] = None, 
    search: Optional[str] = None, 
    mod_id: Optional[str] = None
):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ you need 'Manage Server' permissions to do this.", ephemeral=True)
        
    if action.value == "manage":
        await interaction.response.defer(ephemeral=True)
        tracking_data = await tracker.get_all_tracking(bot.session)
        
        guild_tracks = []
        prefix = f"guild:{interaction.guild.id}:"
        for m_id, data in tracking_data.items():
            for t_id in data["users"]:
                if t_id.startswith(prefix):
                    guild_tracks.append((m_id, t_id))
        
        if not guild_tracks:
            return await interaction.followup.send("this server doesn't have any active notifications.", ephemeral=True)
            
        options = []
        unique_channels_per_mod = set()
        
        for m_id, t_id in guild_tracks:
            parts = t_id.split(":")
            c_id = parts[3]
            sig = f"{m_id}|{c_id}"
            
            if sig not in unique_channels_per_mod:
                unique_channels_per_mod.add(sig)
                options.append(discord.SelectOption(
                    label=f"remove: {m_id}"[:100],
                    description=f"in channel {c_id}"[:100],
                    value=sig[:100]
                ))
                
            if len(options) >= 25:
                break
        
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Select(
            placeholder="select a notification to remove...",
            min_values=1, max_values=1,
            options=options, custom_id="sn_manage_remove"
        ))
        
        return await interaction.followup.send("select a tracked mod to remove from server notifications:", view=view, ephemeral=True)

    if not channel:
        return await interaction.response.send_message("❌ you need to provide a `channel` when adding a notification.", ephemeral=True)

    if (search and contains_banned_word(search)) or (mod_id and contains_banned_word(mod_id)):
        return await interaction.response.send_message("lets not", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    ping_str = "none"
    if ping:
        if isinstance(ping, discord.Role):
            ping_str = f"<@&{ping.id}>"
        else:
            ping_str = f"<@{ping.id}>"
            
    b64_ping = base64.urlsafe_b64encode(ping_str.encode()).decode()
    b64_msg = base64.urlsafe_b64encode((custom_message or "none").encode()).decode()
    
    track_id = f"guild:{interaction.guild.id}:channel:{channel.id}:ping:{b64_ping}:msg:{b64_msg}"

    if track_entire:
        if not sort_by or not search or sort_by.value not in ("tags", "developer"):
            return await interaction.followup.send("❌ to track an entire thing, you must set `sort_by` to 'by tag' or 'by developer' and provide a `search` query.", ephemeral=True)
        
        if sort_by.value == "tags":
            mods_col = f"tag:{search.lower()}"
            data = await bot.fetch_mods_list(tags=search, sort="recently_updated", per_page=1)
        else:
            mods_col = f"dev:{search.lower()}"
            data = await bot.fetch_mods_list(developer=search, sort="recently_updated", per_page=1)
            
        latest_mod = data.get("data", [])
        if not latest_mod:
            return await interaction.followup.send("⚠️ couldn't find any mods matching that to establish a baseline.", ephemeral=True)
            
        version = f"{latest_mod[0].get('id', 'none')}:{find_version(latest_mod[0]) or '0'}"
        added = await tracker.add_tracking(bot.session, mods_col, version, track_id)
        if added:
            await interaction.followup.send(f"✅ successfully set up notifications for the entire `{mods_col}` category in {channel.mention}.")
        else:
            await interaction.followup.send(f"✅ updated tracking configuration for `{mods_col}` in {channel.mention}.")
        return

    if mod_id:
        mod_data = await bot.fetch_single_mod(mod_id)
        if "error" in mod_data:
            return await interaction.followup.send(f"error: {mod_data['error']}")
        
        version = find_version(mod_data) or "unknown"
        added = await tracker.add_tracking(bot.session, mod_id, version, track_id)
        if added:
            await interaction.followup.send(f"✅ successfully set up notifications for `{mod_id}` in {channel.mention}.")
        else:
            await interaction.followup.send(f"✅ updated tracking configuration for `{mod_id}` in {channel.mention}.")
    else:
        query = search
        sort_mode = "downloads"
        tag = None
        platform_val = None
        developer = None

        if sort_by:
            val = sort_by.value
            if val in ("featured", "recently_updated", "recently_published", "pending"):
                sort_mode = val
            elif val == "tags":
                tag = search
                query = None
            elif val == "platform":
                platform_val = search
                query = None
            elif val == "developer":
                developer = search
                query = None

        nonce = cache_pending_track(track_id)
        view = ServerNotifySearchView(
            bot,
            nonce,
            query=query, 
            sort_mode=sort_mode, 
            platform=platform_val,
            tag=tag,
            developer=developer
        )
        embeds = await view.generate_view()
        await interaction.followup.send(embeds=embeds, view=view)

@servernotify_cmd.autocomplete("mod_id")
async def servernotify_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

@servernotify_cmd.autocomplete("search")
async def servernotify_search_autocomplete(interaction: discord.Interaction, current: str):
    sort_by = getattr(interaction.namespace, 'sort_by', None)
    
    if sort_by == "tags":
        matches = [t for t in _TAGS_CACHE if current.lower() in t.lower()][:25]
        return [discord.app_commands.Choice(name=t, value=t) for t in matches]
    elif sort_by == "platform":
        platforms_map = {
            "windows": "windows", "mac": "macos", "android": "android",
            "ios": "ios", "android32": "android32", "android64": "android64"
        }
        matches = [discord.app_commands.Choice(name=name, value=val)
                for name, val in platforms_map.items()
                if current.lower() in name.lower() or current.lower() in val.lower()][:25]
        return matches
    elif sort_by == "developer":
        devs = list({m.get("developer", "unknown") for m in _ALL_MODS_CACHE if m.get("developer") and m.get("developer") != "unknown"})
        matches = [d for d in devs if current.lower() in d.lower()][:25]
        return [discord.app_commands.Choice(name=d, value=d) for d in matches]
    
    return []

@bot.tree.command(name="daily", description="discover a hand-picked, featured geode mod of the day! (credit to night_zack on discord)")
async def daily_cmd(interaction: discord.Interaction):
    if not _ALL_MODS_CACHE:
        return await interaction.response.send_message("still warming up the mod database, give me a minute.", ephemeral=True)
    
    # --- lock the daily drop to the current date ---
    today = datetime.now(timezone.utc).date()
    daily_seed = int(today.strftime("%Y%m%d"))
    rng = random.Random(daily_seed)
    
    # --- filter out bugfix mods, geode.loader, and featured mods before picking ---
    valid_mods = []
    for m in _ALL_MODS_CACHE:
        tags = [str(t).lower() for t in m.get("tags", [])]
        mod_id = m.get("id", "")
        is_featured = m.get("featured", False)
        
        if "bugfix" not in tags and mod_id != "geode.loader" and not is_featured:
            valid_mods.append(m)
            
    if not valid_mods: 
        valid_mods = [m for m in _ALL_MODS_CACHE if m.get("id") != "geode.loader"]
        if not valid_mods:
            valid_mods = _ALL_MODS_CACHE
            
    # --- sort by id to guarantee deterministic choices across cache updates ---
    valid_mods = sorted(valid_mods, key=lambda x: x["id"])
    
    chosen = rng.choice(valid_mods)
    mod_id = chosen["id"]
    
    await interaction.response.defer()
    
    start = time.perf_counter()
    mod_data = await bot.fetch_single_mod(mod_id)
    ms = (time.perf_counter() - start) * 1000
    
    if "error" in mod_data:
        return await interaction.followup.send(f"failed to fetch today's daily mod: {mod_data['error']}")
        
    embed = build_single_mod_embed(mod_data, ms, False)
    embed.title = f"🌟 daily pick: {embed.title}"
    
    await interaction.followup.send(embed=embed, view=NotifyView())

@bot.tree.command(name="help", description="need help? join the support server")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="geode index help",
        description="stuck? got a bug? want a new feature?\n\n**[join the support server!](https://discord.gg/wQFAmqgx8B)**",
        color=0x5865F2
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="invite", description="add this bot to your own servers")
async def invite_cmd(interaction: discord.Interaction):
    link = f"https://discord.com/oauth2/authorize?client_id={bot.user.id}&permissions=274877975552&scope=bot%20applications.commands"
    embed = discord.Embed(
        title="put me in your server!",
        description=f"stop manually checking for geode mod updates like a caveman.\n\n[click here to invite me]({link}) and let me do the heavy lifting.",
        color=0x5865F2
    )
    embed.set_footer(text="the best geode tracker on discord.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="untrack", description="stop getting dm notifications for a specific mod")
@discord.app_commands.describe(mod_id="the id of the mod to stop tracking")
async def untrack_cmd(interaction: discord.Interaction, mod_id: str):
    if interaction.guild is not None:
        return await interaction.response.send_message("❌ hop into my dms to use this, keeps it between us.", ephemeral=True)

    removed = await tracker.remove_tracking(bot.session, mod_id, str(interaction.user.id))
    if removed:
        await interaction.response.send_message(f"✅ done, i won't bother you about `{mod_id}` anymore.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ you aren't tracking `{mod_id}`.", ephemeral=True)

@untrack_cmd.autocomplete("mod_id")
async def untrack_autocomplete(interaction: discord.Interaction, current: str):
    user_tracked = await tracker.get_user_tracked_mods(bot.session, str(interaction.user.id))
    return [discord.app_commands.Choice(name=m, value=m) for m in user_tracked if current.lower() in m.lower()][:25]

@bot.tree.command(name="tracked", description="view all mods you are tracking")
async def tracked_cmd(interaction: discord.Interaction):
    if interaction.guild is not None:
        return await interaction.response.send_message("❌ hop into my dms for this one!", ephemeral=True)

    user_tracked = await tracker.get_user_tracked_mods(bot.session, str(interaction.user.id))
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
        return await interaction.response.send_message("let's keep the language clean.", ephemeral=True)

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
    if mod_id and contains_banned_word(mod_id): return await interaction.response.send_message("let's keep it clean.", ephemeral=True)

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
                    await interaction.followup.send(f"mod `{mod_id}` doesn't exist. *(took {ms:.1f}ms)*")
                else:
                    await interaction.followup.send(f"api error: http {r.status} *(took {ms:.1f}ms)*")
        except Exception as e:
            await interaction.followup.send(f"issue grabbing the repo: {format_error_reason(e)}")

    elif cmd == "ery_string_generator":
        magic = f"ERYMANTHUS_MAGIC_STRING_TRIGGER_ACCEPT_MY_MOD_{''.join(random.choices(string.ascii_uppercase + string.digits, k=64))}"
        await interaction.response.send_message(embed=discord.Embed(title="magic string generator", description="here's your bypass string", color=0x9b59b6).add_field(name="output", value=f"```\n{magic}\n```"))

@dev.autocomplete("mod_id")
async def dev_mod_autocomplete(interaction: discord.Interaction, current: str):
    return await mod_autocomplete_logic(current)

def main():
    if not all([token, cf_account, cf_db, cf_token]):
        raise RuntimeError("Missing environment variables (discord token or cloudflare credentials).")
    bot.run(token)

if __name__ == "__main__":
    main()
