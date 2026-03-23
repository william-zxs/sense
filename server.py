#!/usr/bin/env python3
"""Wi-Fi presence monitor server.

Monitors known devices by MAC address using local ARP/neighbor tables and
emits connect/disconnect events.
"""
from __future__ import annotations

import asyncio
import json
import logging
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
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LOGGER = logging.getLogger(__name__)


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


@dataclass
class NetworkDevice:
    ip: str | None
    mac: str
    hostname: str | None
    interface: str | None
    state: str | None
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


def run_cmd(cmd: list[str], timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def parse_ip_neigh_devices(output: str) -> list[NetworkDevice]:
    devices: list[NetworkDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        ip = parts[0] if parts else None
        mac_match = MAC_RE.search(line)
        if not ip or not mac_match:
            continue
        interface = None
        state = parts[-1].upper() if parts else None
        if "dev" in parts:
            dev_index = parts.index("dev")
            if dev_index + 1 < len(parts):
                interface = parts[dev_index + 1]
        devices.append(
            NetworkDevice(
                ip=ip,
                mac=mac_match.group(0).lower(),
                hostname=None,
                interface=interface,
                state=state,
                source="ip-neigh",
            )
        )
    return devices


def parse_ip_neigh(output: str) -> set[str]:
    macs: set[str] = set()
    for device in parse_ip_neigh_devices(output):
        line_upper = (device.state or "").upper()
        if not any(state in line_upper for state in CONNECTED_STATES):
            continue
        macs.add(device.mac)
    return macs


def parse_arp_table_devices(output: str) -> list[NetworkDevice]:
    devices: list[NetworkDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        mac_match = MAC_RE.search(line)
        ip_match = IPV4_RE.search(line)
        if not mac_match or not ip_match:
            continue

        hostname: str | None = None
        if " (" in line:
            hostname = line.split(" (", 1)[0].strip()
            if hostname == "?":
                hostname = None

        interface = None
        interface_match = re.search(r"\bon\s+(\S+)", line)
        if interface_match:
            interface = interface_match.group(1)

        devices.append(
            NetworkDevice(
                ip=ip_match.group(0),
                mac=mac_match.group(0).lower(),
                hostname=hostname,
                interface=interface,
                state="REACHABLE",
                source="arp",
            )
        )
    return devices


def parse_arp_scan(output: str) -> set[str]:
    macs: set[str] = set()
    for line in output.splitlines():
        m = MAC_RE.search(line)
        if m:
            macs.add(m.group(0).lower())
    return macs


def parse_arp_scan_devices(output: str) -> list[NetworkDevice]:
    devices: list[NetworkDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        mac_match = MAC_RE.search(line)
        ip_match = IPV4_RE.search(line)
        if not mac_match or not ip_match:
            continue
        parts = re.split(r"\s+", line, maxsplit=2)
        hostname = parts[2].strip() if len(parts) >= 3 else None
        devices.append(
            NetworkDevice(
                ip=ip_match.group(0),
                mac=mac_match.group(0).lower(),
                hostname=hostname or None,
                interface=None,
                state="REACHABLE",
                source="arp-scan",
            )
        )
    return devices


def merge_network_devices(device_groups: list[list[NetworkDevice]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], NetworkDevice] = {}
    for devices in device_groups:
        for device in devices:
            key = (device.mac, device.ip or "")
            existing = merged.get(key)
            if existing is None:
                merged[key] = device
                continue
            if not existing.hostname and device.hostname:
                existing.hostname = device.hostname
            if not existing.interface and device.interface:
                existing.interface = device.interface
            if not existing.state and device.state:
                existing.state = device.state
            if device.source not in existing.source.split(","):
                existing.source = f"{existing.source},{device.source}"

    known_devices = tracker.devices
    results: list[dict[str, Any]] = []
    for item in sorted(
        merged.values(),
        key=lambda device: ((device.ip or "255.255.255.255"), device.mac),
    ):
        known = known_devices.get(item.mac)
        results.append(
            {
                "ip": item.ip,
                "mac": item.mac,
                "hostname": item.hostname,
                "interface": item.interface,
                "state": item.state,
                "source": item.source,
                "known": known is not None,
                "device_name": known.name if known else None,
                "online": tracker.status.get(item.mac, False),
            }
        )
    return results


def collect_network_devices(active_scan: bool) -> list[dict[str, Any]]:
    device_groups = [
        parse_ip_neigh_devices(run_cmd(["ip", "neigh", "show"])),
        parse_arp_table_devices(run_cmd(["arp", "-a"])),
    ]
    if active_scan:
        device_groups.append(parse_arp_scan_devices(run_cmd(["arp-scan", "--localnet"])))
    return merge_network_devices(device_groups)


def ping_host(ip: str) -> bool:
    return bool(run_cmd(["ping", "-c", "1", ip], timeout=2.0))


def verify_arp_known_devices(arp_devices: list[NetworkDevice]) -> set[str]:
    arp_by_mac = {device.mac: device for device in arp_devices if device.ip}
    verified: set[str] = set()
    for mac in tracker.devices:
        device = arp_by_mac.get(mac)
        if device and ping_host(device.ip or ""):
            verified.add(mac)
    return verified


def collect_connected_macs(active_scan: bool) -> tuple[set[str], str]:
    """Return (mac_set, source)."""
    neighbors = parse_ip_neigh(run_cmd(["ip", "neigh", "show"]))
    if neighbors:
        return neighbors, "ip-neigh"

    arp_devices = parse_arp_table_devices(run_cmd(["arp", "-a"]))
    arp_neighbors = verify_arp_known_devices(arp_devices)
    if arp_neighbors:
        return arp_neighbors, "arp+ping"

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
            try:
                connected, source = collect_connected_macs(use_active_scan)
                tracker.apply_snapshot(connected, source)
            except Exception:
                LOGGER.exception("Failed to poll Wi-Fi device presence")
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


@app.get("/devices")
async def list_devices(active_scan: bool | None = None) -> JSONResponse:
    scan_enabled = use_active_scan if active_scan is None else active_scan
    payload = {
        "devices": collect_network_devices(scan_enabled),
        "active_scan": scan_enabled,
    }
    return JSONResponse(content=json.loads(json.dumps(payload)))
