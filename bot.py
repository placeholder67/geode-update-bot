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
from discord.ext import commands, tasks

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"
state_file = Path("geode_version_state.json")
check_interval_minutes = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("geode-version-checker")

version_re = re.compile(r"^\s*v?(\d+(?:\.\d+)+(?:[-+][\w.]+)?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class TrackedMod:
    id: str
    label: str
    emoji: str


tracked_mods: tuple[TrackedMod, ...] = (
    TrackedMod("axiom.echochoke", "EchoChoke", "🟣"),
    TrackedMod("axiom.echoclip", "EchoClip", "🔴"),
    TrackedMod("axiom.voicecontrol", "Voice Control", "🔵"),
    TrackedMod("axiom.cube-abuse", "Cube Abuse", "🟡"),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def version_from_changelog(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in strip_tags(text).splitlines():
        m = version_re.match(line.strip())
        if m:
            return m.group(1)
    return None


def first_text(data: Any, keys: tuple[str, ...]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in keys:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float, bool)):
            return str(v)
    return None


def first_bool(data: Any, keys: tuple[str, ...]) -> Optional[bool]:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key not in data:
            continue
        v = data.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            t = v.strip().lower()
            if t in {"true", "1", "yes"}:
                return True
            if t in {"false", "0", "no"}:
                return False
        if isinstance(v, (int, float)):
            return bool(v)
    return None


def unwrap_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
        return data
    return {}


def status_text(snapshot: dict[str, Any]) -> str:
    if snapshot.get("pending"):
        return "pending"
    if snapshot.get("released"):
        return "released"
    return snapshot.get("status") or "unknown"


def compare_versions(saved: Optional[dict[str, Any]], current: dict[str, Any]) -> str:
    cur = current.get("display_version") or current.get("version") or "unknown"
    if not saved:
        return "new"
    old = saved.get("display_version") or saved.get("version") or "unknown"
    if old == cur:
        return "same"
    return f"{old} → {cur}"


def load_state() -> dict[str, Any]:
    if not state_file.exists():
        return {"mods": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"mods": {}}
        data.setdefault("mods", {})
        if not isinstance(data["mods"], dict):
            data["mods"] = {}
        return data
    except Exception:
        return {"mods": {}}


def save_state(state: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def compact_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot.get("version"),
        "display_version": snapshot.get("display_version"),
        "saved_at": utc_now_iso(),
    }


def extract_snapshot(mod: TrackedMod, data: dict[str, Any]) -> dict[str, Any]:
    name = first_text(data, ("name", "title", "displayName", "display_name")) or mod.label
    author = first_text(data, ("author", "developer", "creator", "owner"))

    version = (
        first_text(data, ("version", "latestVersion", "latest_version", "currentVersion", "current_version"))
        or version_from_changelog(first_text(data, ("changelog",)))
    )

    pending = first_bool(data, ("pending", "isPending", "is_pending")) or False
    released = first_bool(data, ("released", "isReleased", "is_released"))
    if released is None:
        released = bool(version) and not pending

    return {
        "id": mod.id,
        "label": mod.label,
        "emoji": mod.emoji,
        "name": name,
        "author": author,
        "version": version,
        "display_version": f"{version} (pending)" if pending and version else (version or "unknown"),
        "pending": pending,
        "released": released,
        "status": "pending" if pending else "released" if released else "unknown",
        "raw": data,
        "parse_failed": not bool(version),
    }


def compact_block(data: Any, limit: int = 700) -> str:
    try:
        txt = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception:
        txt = repr(data)
    if len(txt) > limit:
        txt = txt[: limit - 3] + "..."
    return f"```json\n{txt}\n```"


class GeodeVersionBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: Optional[aiohttp.ClientSession] = None
        self.state = load_state()
        self.last_snapshot: dict[str, dict[str, Any]] = {}

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "geode-version-checker/1.0"},
        )
        try:
            synced = await self.tree.sync()
            log.info("synced %d application commands", len(synced))
        except Exception:
            log.exception("failed to sync application commands")
        if not self.poll_versions.is_running():
            self.poll_versions.start()

    async def close(self) -> None:
        try:
            if self.poll_versions.is_running():
                self.poll_versions.cancel()
        except Exception:
            pass
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def fetch_one(self, mod: TrackedMod) -> tuple[str, dict[str, Any]]:
        if not self.session:
            return mod.id, {
                "id": mod.id,
                "label": mod.label,
                "emoji": mod.emoji,
                "name": mod.label,
                "author": None,
                "version": None,
                "display_version": "unknown",
                "pending": False,
                "released": False,
                "status": "error",
                "raw": {},
                "parse_failed": True,
                "error": "http session not ready",
            }

        try:
            async with self.session.get(api_url.format(mod.id)) as res:
                if res.status != 200:
                    return mod.id, {
                        "id": mod.id,
                        "label": mod.label,
                        "emoji": mod.emoji,
                        "name": mod.label,
                        "author": None,
                        "version": None,
                        "display_version": "unknown",
                        "pending": False,
                        "released": False,
                        "status": f"http {res.status}",
                        "raw": await res.text(),
                        "parse_failed": True,
                        "error": f"http {res.status}",
                    }

                data = unwrap_payload(await res.json(content_type=None))
                snap = extract_snapshot(mod, data)
                snap["raw"] = data
                return mod.id, snap

        except Exception as e:
            return mod.id, {
                "id": mod.id,
                "label": mod.label,
                "emoji": mod.emoji,
                "name": mod.label,
                "author": None,
                "version": None,
                "display_version": "unknown",
                "pending": False,
                "released": False,
                "status": "error",
                "raw": {},
                "parse_failed": True,
                "error": str(e),
            }

    async def fetch_snapshots(self) -> dict[str, dict[str, Any]]:
        pairs = await asyncio.gather(*(self.fetch_one(m) for m in tracked_mods))
        return dict(pairs)

    def apply_snapshot_to_state(self, snapshots: dict[str, dict[str, Any]]) -> list[str]:
        changed: list[str] = []
        mods = self.state.setdefault("mods", {})

        for mod_id, snap in snapshots.items():
            self.last_snapshot[mod_id] = snap
            if snap.get("parse_failed") or snap.get("pending"):
                continue

            saved = mods.get(mod_id)
            if not isinstance(saved, dict) or saved.get("version") != snap.get("version"):
                mods[mod_id] = compact_state(snap)
                changed.append(mod_id)

        if changed:
            self.state["last_updated"] = utc_now_iso()
            save_state(self.state)

        return changed

    async def build_report(self) -> tuple[dict[str, dict[str, Any]], Optional[str]]:
        try:
            return await self.fetch_snapshots(), None
        except Exception as e:
            log.exception("failed to fetch snapshots")
            return self.last_snapshot.copy(), f"{type(e).__name__}: {e}"

    @tasks.loop(minutes=check_interval_minutes)
    async def poll_versions(self) -> None:
        snaps = await self.fetch_snapshots()
        changed = self.apply_snapshot_to_state(snaps)
        if changed:
            log.info("updated: %s", ", ".join(changed))

    @poll_versions.before_loop
    async def before_poll_versions(self) -> None:
        await self.wait_until_ready()

    def make_check_embed(self, snapshots: dict[str, dict[str, Any]], error: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(
            title="geode version checker",
            description="compact live status",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        if error:
            embed.add_field(name="warning", value=f"`{error}`", inline=False)

        saved = self.state.get("mods", {})
        lines = []

        for mod in tracked_mods:
            snap = snapshots.get(mod.id)
            if not snap:
                lines.append(f"{mod.emoji} **{mod.label}** — failed")
                continue

            if snap.get("parse_failed"):
                lines.append(f"{mod.emoji} **{mod.label}** — parse failed")
                continue

            current = snap.get("display_version") or "unknown"
            status = "⏳ pending" if snap.get("pending") else "✅ released"
            change = compare_versions(saved.get(mod.id) if isinstance(saved, dict) else None, snap)

            line = f"{mod.emoji} **{snap.get('name') or mod.label}** — `{current}` • {status}"
            if change != "same":
                line += f" • `{change}`"
            lines.append(line)

        embed.description = "\n".join(lines) if lines else "no mods tracked."
        embed.set_footer(text="pending versions are shown but not saved")
        return embed

    def make_debugmods_embed(self, snapshots: dict[str, dict[str, Any]], error: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(
            title="mod debug",
            description="raw parser output",
            color=discord.Color.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )

        if error:
            embed.add_field(name="warning", value=f"`{error}`", inline=False)

        for mod in tracked_mods:
            snap = snapshots.get(mod.id)
            if not snap:
                continue

            if snap.get("parse_failed"):
                embed.add_field(
                    name=f"{mod.emoji} {mod.label}",
                    value=compact_block(snap.get("raw", {"note": "no data"})),
                    inline=False,
                )
                continue

            candidates = snap.get("raw", {})
            embed.add_field(
                name=f"{mod.emoji} {mod.label}",
                value=(
                    f"**version:** `{snap.get('version') or 'unknown'}`\n"
                    f"**status:** `{snap.get('status')}`\n"
                    f"**author:** `{snap.get('author') or 'unknown'}`\n"
                    f"**raw:** {compact_block(candidates, 260)}"
                )[:1024],
                inline=False,
            )

        return embed

    def make_debugstate_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="saved state",
            description="stored versions",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        pretty = json.dumps(self.state, indent=2, ensure_ascii=False, sort_keys=True)
        embed.description = f"```json\n{pretty[:3900]}{'...' if len(pretty) > 3900 else ''}\n```"
        return embed


bot = GeodeVersionBot()


async def safe_defer(interaction: discord.Interaction) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except Exception:
        log.exception("failed to defer interaction")


@bot.tree.command(name="checkforupdates", description="check the tracked geode mods for version changes")
async def checkforupdates(interaction: discord.Interaction) -> None:
    await safe_defer(interaction)
    snaps, error = await bot.build_report()
    await interaction.followup.send(embed=bot.make_check_embed(snaps, error))


@bot.tree.command(name="debugmods", description="show parsed version info from the geode api")
async def debugmods(interaction: discord.Interaction) -> None:
    await safe_defer(interaction)
    snaps, error = await bot.build_report()
    await interaction.followup.send(embed=bot.make_debugmods_embed(snaps, error))


@bot.tree.command(name="debugstate", description="show the saved json state")
async def debugstate(interaction: discord.Interaction) -> None:
    await safe_defer(interaction)
    await interaction.followup.send(embed=bot.make_debugstate_embed())


@bot.event
async def on_ready() -> None:
    log.info("logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


def main() -> None:
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)


if __name__ == "__main__":
    main()
