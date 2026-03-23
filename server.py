#!/usr/bin/env python3
"""Wi-Fi presence monitor server.

Monitors known devices by MAC address using local ARP/neighbor tables and
emits connect/disconnect events.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse


DEFAULT_CONFIG_PATH = Path(os.getenv("WIFI_MONITOR_CONFIG", "config.yaml"))
CONNECTED_STATES = {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT"}
MAC_RE = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", re.IGNORECASE)


@dataclass
class Device:
    name: str
    mac: str


@dataclass
class PresenceEvent:
    ts: str
    device_name: str
    mac: str
    online: bool
    source: str


class PresenceTracker:
    def __init__(self, devices: list[Device], max_events: int = 500):
        self.devices = {d.mac.lower(): d for d in devices}
        self.status = {d.mac.lower(): False for d in devices}
        self.events: deque[PresenceEvent] = deque(maxlen=max_events)

    def apply_snapshot(self, connected_macs: set[str], source: str) -> None:
        for mac, device in self.devices.items():
            now_online = mac in connected_macs
            if now_online != self.status[mac]:
                self.status[mac] = now_online
                self.events.append(
                    PresenceEvent(
                        ts=datetime.now(timezone.utc).isoformat(),
                        device_name=device.name,
                        mac=mac,
                        online=now_online,
                        source=source,
                    )
                )

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mac, device in self.devices.items():
            out[device.name] = {
                "mac": mac,
                "online": self.status[mac],
            }
        return out


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml first."
        )
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_cmd(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def parse_ip_neigh(output: str) -> set[str]:
    macs: set[str] = set()
    for line in output.splitlines():
        line_upper = line.upper()
        if not any(state in line_upper for state in CONNECTED_STATES):
            continue
        m = MAC_RE.search(line)
        if m:
            macs.add(m.group(0).lower())
    return macs


def parse_arp_scan(output: str) -> set[str]:
    macs: set[str] = set()
    for line in output.splitlines():
        m = MAC_RE.search(line)
        if m:
            macs.add(m.group(0).lower())
    return macs


def collect_connected_macs(active_scan: bool) -> tuple[set[str], str]:
    """Return (mac_set, source)."""
    neighbors = parse_ip_neigh(run_cmd(["ip", "neigh", "show"]))
    if neighbors:
        return neighbors, "ip-neigh"

    if active_scan:
        scanned = parse_arp_scan(run_cmd(["arp-scan", "--localnet"]))
        if scanned:
            return scanned, "arp-scan"

    return set(), "none"


cfg = load_config(DEFAULT_CONFIG_PATH)
interval_seconds = int(cfg.get("interval_seconds", 15))
use_active_scan = bool(cfg.get("use_active_scan", False))
devices = [Device(name=d["name"], mac=d["mac"].lower()) for d in cfg.get("devices", [])]
tracker = PresenceTracker(devices)

app = FastAPI(title="Home Wi-Fi Presence Monitor", version="0.1.0")


@app.on_event("startup")
async def startup_event() -> None:
    async def poll_loop() -> None:
        while True:
            connected, source = collect_connected_macs(use_active_scan)
            tracker.apply_snapshot(connected, source)
            await asyncio.sleep(interval_seconds)

    asyncio.create_task(poll_loop())


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "devices": len(devices), "interval_seconds": interval_seconds}


@app.get("/status")
async def status() -> dict[str, Any]:
    return tracker.snapshot()


@app.get("/events")
async def events(limit: int = 100) -> JSONResponse:
    limit = max(1, min(limit, len(tracker.events) or 1))
    payload = [asdict(evt) for evt in list(tracker.events)[-limit:]]
    return JSONResponse(content=json.loads(json.dumps(payload)))
