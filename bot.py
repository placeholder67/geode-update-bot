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
        return data.get("payload") if isinstance(data.get("payload"), dict) else data
    return {}


# 🔥 improved pending detection (this fixes your main issue)
def detect_pending(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False

    # direct flags
    direct = first_bool(data, ("pending", "isPending", "is_pending"))
    if direct is not None:
        return direct

    # status-based detection
    status = first_text(data, ("status", "state"))
    if status:
        s = status.lower()
        if "pending" in s or "beta" in s or "draft" in s:
            return True

    # tags/categories
    tags = data.get("tags") or data.get("categories")
    if isinstance(tags, list):
        joined = " ".join(str(t).lower() for t in tags)
        if "pending" in joined or "beta" in joined:
            return True

    # changelog hints
    changelog = first_text(data, ("changelog", "notes", "description"))
    if changelog:
        c = changelog.lower()
        if "pending" in c or "not released" in c:
            return True

    return False


def detect_released(data: dict[str, Any], pending: bool, version: Optional[str]) -> bool:
    released = first_bool(data, ("released", "isReleased", "is_released"))
    if released is not None:
        return released
    return bool(version) and not pending


def extract_snapshot(mod: TrackedMod, data: dict[str, Any]) -> dict[str, Any]:
    name = first_text(data, ("name", "title", "displayName", "display_name")) or mod.label
    author = first_text(data, ("author", "developer", "creator", "owner"))

    version = (
        first_text(data, ("version", "latestVersion", "latest_version", "currentVersion", "current_version"))
        or version_from_changelog(first_text(data, ("changelog",)))
    )

    pending = detect_pending(data)
    released = detect_released(data, pending, version)

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
        "parse_failed": not bool(version) and not pending,
    }


def version_diff(saved: Optional[dict[str, Any]], current: dict[str, Any]) -> str:
    cur = current.get("version") or "unknown"
    if not saved:
        return "new"
    old = saved.get("version") or "unknown"
    return "same" if old == cur else f"{old} → {cur}"


def load_state() -> dict[str, Any]:
    if not state_file.exists():
        return {"mods": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {"mods": data.get("mods", {}) if isinstance(data, dict) else {}}
    except Exception:
        return {"mods": {}}


def save_state(state: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def compact_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot.get("version"),
        "saved_at": utc_now_iso(),
    }


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
        self.poll_versions.start()
        await self.tree.sync()

    async def fetch_one(self, mod: TrackedMod) -> tuple[str, dict[str, Any]]:
        if not self.session:
            return mod.id, {"id": mod.id, "error": "no session"}

        try:
            async with self.session.get(api_url.format(mod.id)) as res:
                data = await res.json(content_type=None)
                data = unwrap_payload(data)
                snap = extract_snapshot(mod, data)
                return mod.id, snap

        except Exception as e:
            return mod.id, {"id": mod.id, "error": str(e)}

    async def fetch_all(self) -> dict[str, dict[str, Any]]:
        return dict(await asyncio.gather(*(self.fetch_one(m) for m in tracked_mods)))

    def apply(self, snaps: dict[str, dict[str, Any]]) -> None:
        mods = self.state.setdefault("mods", {})

        for k, v in snaps.items():
            self.last_snapshot[k] = v
            if v.get("pending"):
                continue

            if mods.get(k, {}).get("version") != v.get("version"):
                mods[k] = compact_state(v)

        save_state(self.state)

    @tasks.loop(minutes=check_interval_minutes)
    async def poll_versions(self):
        snaps = await self.fetch_all()
        self.apply(snaps)


bot = GeodeVersionBot()


def main():
    if not token:
        raise RuntimeError("missing DISCORD_TOKEN")
    bot.run(token)


if __name__ == "__main__":
    main()
