#!/usr/bin/env python3
"""
Cellular Modem Signal Dashboard for UniFi U5G-Max-Outdoor

Runs qmicli commands locally and displays a live terminal dashboard showing
signal statistics for both SIM card slots.

Based on the QmicliParser and CellularModemStats from the NetworkOptimizer project.

Usage:
    python3 cellular-dashboard.py                  # Default: both SIMs, 10s refresh
    python3 cellular-dashboard.py --interval 5     # 5 second refresh
    python3 cellular-dashboard.py --sim 1          # Monitor SIM 1 only
    python3 cellular-dashboard.py --device /dev/wwan0qmi0  # Custom QMI device path

Requirements:
    - Must be run locally on the U5G-Max-Outdoor device
    - qmicli must be installed and accessible
    - Python 3.6+
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ─── Models (mirroring CellularModemStats.cs) ────────────────────────────────


class NetworkMode(Enum):
    UNKNOWN = "Unknown"
    LTE = "LTE (4G)"
    NR5G_NSA = "5G NSA (EN-DC)"
    NR5G_SA = "5G SA"


@dataclass
class SignalInfo:
    rsrp: Optional[float] = None
    rsrq: Optional[float] = None
    rssi: Optional[float] = None
    snr: Optional[float] = None

    @property
    def bars(self) -> int:
        if self.rsrp is None:
            return 0
        rsrp = self.rsrp
        if rsrp >= -80:
            return 5
        if rsrp >= -90:
            return 4
        if rsrp >= -100:
            return 3
        if rsrp >= -110:
            return 2
        if rsrp >= -120:
            return 1
        return 0

    @property
    def quality(self) -> str:
        return {5: "Excellent", 4: "Good", 3: "Fair", 2: "Poor", 1: "Very Poor"}.get(
            self.bars, "No Signal"
        )


@dataclass
class CellInfo:
    physical_cell_id: int = 0
    global_cell_id: Optional[str] = None
    tac: Optional[str] = None
    earfcn: Optional[int] = None
    band_description: Optional[str] = None
    plmn: Optional[str] = None
    signal: Optional[SignalInfo] = None
    timing_advance: Optional[int] = None
    is_serving: bool = False


@dataclass
class BandInfo:
    radio_interface: str = ""
    band_class: str = ""
    channel: int = 0
    bandwidth_mhz: Optional[int] = None

    @property
    def band_name(self) -> str:
        mapping = {
            "eutran-2": "Band 2 (1900 MHz PCS)",
            "eutran-3": "Band 3 (1800 MHz)",
            "eutran-4": "Band 4 (AWS-1)",
            "eutran-5": "Band 5 (850 MHz)",
            "eutran-7": "Band 7 (2600 MHz)",
            "eutran-12": "Band 12 (700 MHz)",
            "eutran-13": "Band 13 (700 MHz)",
            "eutran-14": "Band 14 (700 MHz FirstNet)",
            "eutran-17": "Band 17 (700 MHz)",
            "eutran-25": "Band 25 (1900 MHz)",
            "eutran-26": "Band 26 (850 MHz)",
            "eutran-30": "Band 30 (2300 MHz)",
            "eutran-41": "Band 41 (2500 MHz TDD)",
            "eutran-66": "Band 66 (AWS-3)",
            "eutran-71": "Band 71 (600 MHz)",
            "n2": "n2 (1900 MHz)",
            "n5": "n5 (850 MHz)",
            "n41": "n41 (2500 MHz)",
            "n71": "n71 (600 MHz)",
            "n77": "n77 (3700 MHz C-Band)",
            "n78": "n78 (3500 MHz)",
            "n260": "n260 (39 GHz mmWave)",
            "n261": "n261 (28 GHz mmWave)",
        }
        return mapping.get(self.band_class.lower(), self.band_class)


@dataclass
class ModemStats:
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sim_slot: int = 1
    qmi_device: str = ""

    registration_state: str = ""
    carrier: str = ""
    carrier_mcc: str = ""
    carrier_mnc: str = ""
    is_roaming: bool = False

    lte: Optional[SignalInfo] = None
    nr5g: Optional[SignalInfo] = None

    serving_cell: Optional[CellInfo] = None
    neighbor_cells: List[CellInfo] = field(default_factory=list)

    active_band: Optional[BandInfo] = None

    error: Optional[str] = None

    @property
    def network_mode(self) -> NetworkMode:
        has_lte = self.lte is not None and self.lte.rsrp is not None
        has_nr5g = self.nr5g is not None and self.nr5g.rsrp is not None
        if has_lte and has_nr5g:
            return NetworkMode.NR5G_NSA
        if has_nr5g and not has_lte:
            return NetworkMode.NR5G_SA
        if has_lte:
            return NetworkMode.LTE
        return NetworkMode.UNKNOWN

    @property
    def network_mode_label(self) -> str:
        return {
            NetworkMode.LTE: "LTE",
            NetworkMode.NR5G_NSA: "5G NSA",
            NetworkMode.NR5G_SA: "5G SA",
        }.get(self.network_mode, "?")

    @property
    def primary_signal(self) -> Optional[SignalInfo]:
        if self.nr5g and self.nr5g.rsrp is not None:
            return self.nr5g
        return self.lte

    @property
    def signal_quality(self) -> int:
        signal = self.primary_signal
        if signal is None:
            return 0
        is_5g = self.nr5g is not None and self.nr5g.rsrp is not None
        total_weight = 0.0
        weighted_score = 0.0

        if signal.rsrp is not None:
            if is_5g:
                rsrp_score = max(0, min(100, (signal.rsrp + 110) * (100.0 / 30.0)))
            else:
                rsrp_score = max(0, min(100, (signal.rsrp + 120) * (100.0 / 30.0)))
            weighted_score += rsrp_score * 0.5
            total_weight += 0.5

        if signal.snr is not None:
            snr_score = max(0, min(100, signal.snr * (100.0 / 30.0)))
            weighted_score += snr_score * 0.3
            total_weight += 0.3

        if signal.rsrq is not None:
            rsrq_score = max(0, min(100, (signal.rsrq + 20) * (100.0 / 17.0)))
            weighted_score += rsrq_score * 0.2
            total_weight += 0.2

        if total_weight == 0:
            return 0
        return int(weighted_score / total_weight)


# ─── QMI CLI Parser (mirroring QmicliParser.cs) ──────────────────────────────


def _extract_quoted_value(line: str) -> str:
    match = re.search(r"'([^']*)'", line)
    return match.group(1) if match else ""


def _try_parse_db_value(line: str, prefix: str) -> Optional[float]:
    if not line.startswith(prefix):
        return None
    match = re.search(r"'(-?\d+\.?\d*)\s*dB", line)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _try_parse_db_value_alt(line: str) -> Optional[float]:
    match = re.search(r"'(-?\d+\.?\d*)'", line)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def parse_signal_info(output: str) -> Tuple[Optional[SignalInfo], Optional[SignalInfo]]:
    """Parse --nas-get-signal-info output."""
    lte: Optional[SignalInfo] = None
    nr5g: Optional[SignalInfo] = None
    current_section: Optional[str] = None

    for line in output.split("\n"):
        trimmed = line.strip()
        if trimmed == "LTE:":
            current_section = "LTE"
            lte = SignalInfo()
        elif trimmed == "5G:":
            current_section = "5G"
            nr5g = SignalInfo()
        elif current_section:
            signal = lte if current_section == "LTE" else nr5g
            if signal is None:
                continue
            val = _try_parse_db_value(trimmed, "RSRP:")
            if val is not None:
                signal.rsrp = val
                continue
            val = _try_parse_db_value(trimmed, "RSRQ:")
            if val is not None:
                signal.rsrq = val
                continue
            val = _try_parse_db_value(trimmed, "RSSI:")
            if val is not None:
                signal.rssi = val
                continue
            val = _try_parse_db_value(trimmed, "SNR:")
            if val is not None:
                signal.snr = val

    return lte, nr5g


def parse_serving_system(
    output: str,
) -> Tuple[str, str, str, str, bool]:
    """Parse --nas-get-serving-system output."""
    registration_state = ""
    carrier = ""
    mcc = ""
    mnc = ""
    is_roaming = False

    for line in output.split("\n"):
        trimmed = line.strip()
        if trimmed.startswith("Registration state:"):
            registration_state = _extract_quoted_value(trimmed)
        elif trimmed.startswith("Description:"):
            carrier = _extract_quoted_value(trimmed)
        elif trimmed.startswith("MCC:"):
            mcc = _extract_quoted_value(trimmed)
        elif trimmed.startswith("MNC:"):
            mnc = _extract_quoted_value(trimmed)
        elif trimmed.startswith("Roaming status:"):
            is_roaming = _extract_quoted_value(trimmed) != "off"

    return registration_state, carrier, mcc, mnc, is_roaming


def parse_cell_location_info(
    output: str,
) -> Tuple[Optional[CellInfo], List[CellInfo]]:
    """Parse --nas-get-cell-location-info output."""
    serving_cell: Optional[CellInfo] = None
    neighbor_cells: List[CellInfo] = []
    in_intra_freq = False
    in_inter_freq = False
    current_earfcn: Optional[int] = None
    current_band_desc: Optional[str] = None

    for line in output.split("\n"):
        trimmed = line.strip()

        if trimmed.startswith("Intrafrequency LTE Info"):
            in_intra_freq = True
            in_inter_freq = False
        elif trimmed.startswith("Interfrequency LTE Info"):
            in_intra_freq = False
            in_inter_freq = True
        elif trimmed.startswith("LTE Info Neighboring"):
            in_intra_freq = False
            in_inter_freq = False

        if in_intra_freq:
            if trimmed.startswith("PLMN:") and serving_cell is None:
                serving_cell = CellInfo(is_serving=True)
                serving_cell.plmn = _extract_quoted_value(trimmed)
            elif trimmed.startswith("Tracking Area Code:") and serving_cell:
                serving_cell.tac = _extract_quoted_value(trimmed)
            elif trimmed.startswith("Global Cell ID:") and serving_cell:
                serving_cell.global_cell_id = _extract_quoted_value(trimmed)
            elif (
                trimmed.startswith("EUTRA Absolute RF Channel Number:")
                and serving_cell
            ):
                match = re.search(r"'(\d+)'.*\((.+)\)", trimmed)
                if match:
                    serving_cell.earfcn = int(match.group(1))
                    serving_cell.band_description = match.group(2)
            elif trimmed.startswith("Serving Cell ID:") and serving_cell:
                val = _extract_quoted_value(trimmed)
                try:
                    serving_cell.physical_cell_id = int(val)
                except ValueError:
                    pass
            elif (
                trimmed.startswith("Physical Cell ID:")
                and serving_cell
                and serving_cell.signal is None
            ):
                val = _extract_quoted_value(trimmed)
                try:
                    serving_cell.physical_cell_id = int(val)
                except ValueError:
                    pass
            elif trimmed.startswith("RSRP:") and serving_cell:
                if serving_cell.signal is None:
                    serving_cell.signal = SignalInfo()
                val = _try_parse_db_value_alt(trimmed)
                if val is not None:
                    serving_cell.signal.rsrp = val
            elif trimmed.startswith("RSRQ:") and serving_cell:
                if serving_cell.signal is None:
                    serving_cell.signal = SignalInfo()
                val = _try_parse_db_value_alt(trimmed)
                if val is not None:
                    serving_cell.signal.rsrq = val
            elif trimmed.startswith("RSSI:") and serving_cell:
                if serving_cell.signal is None:
                    serving_cell.signal = SignalInfo()
                val = _try_parse_db_value_alt(trimmed)
                if val is not None:
                    serving_cell.signal.rssi = val

        if in_inter_freq:
            if trimmed.startswith("EUTRA Absolute RF Channel Number:"):
                match = re.search(r"'(\d+)'.*\((.+)\)", trimmed)
                if match:
                    current_earfcn = int(match.group(1))
                    current_band_desc = match.group(2)
            elif trimmed.startswith("Physical Cell ID:"):
                cell = CellInfo(
                    is_serving=False,
                    earfcn=current_earfcn,
                    band_description=current_band_desc,
                    signal=SignalInfo(),
                )
                val = _extract_quoted_value(trimmed)
                try:
                    cell.physical_cell_id = int(val)
                except ValueError:
                    pass
                neighbor_cells.append(cell)
            elif trimmed.startswith("RSRP:") and neighbor_cells:
                last = neighbor_cells[-1]
                if last.signal is None:
                    last.signal = SignalInfo()
                val = _try_parse_db_value_alt(trimmed)
                if val is not None:
                    last.signal.rsrp = val
            elif trimmed.startswith("RSRQ:") and neighbor_cells:
                last = neighbor_cells[-1]
                if last.signal is None:
                    last.signal = SignalInfo()
                val = _try_parse_db_value_alt(trimmed)
                if val is not None:
                    last.signal.rsrq = val
            elif trimmed.startswith("RSSI:") and neighbor_cells:
                last = neighbor_cells[-1]
                if last.signal is None:
                    last.signal = SignalInfo()
                val = _try_parse_db_value_alt(trimmed)
                if val is not None:
                    last.signal.rssi = val

        if trimmed.startswith("LTE Timing Advance:") and serving_cell:
            match = re.search(r"'(\d+)'", trimmed)
            if match:
                serving_cell.timing_advance = int(match.group(1))

    return serving_cell, neighbor_cells


def parse_rf_band_info(output: str) -> Optional[BandInfo]:
    """Parse --nas-get-rf-band-info output."""
    band: Optional[BandInfo] = None

    for line in output.split("\n"):
        trimmed = line.strip()
        if trimmed.startswith("Radio Interface:"):
            if band is None:
                band = BandInfo()
            band.radio_interface = _extract_quoted_value(trimmed)
        elif trimmed.startswith("Active Band Class:") and band:
            band.band_class = _extract_quoted_value(trimmed)
        elif trimmed.startswith("Active Channel:") and band:
            val = _extract_quoted_value(trimmed)
            try:
                band.channel = int(val)
            except ValueError:
                pass
        elif (
            trimmed.startswith("Bandwidth:")
            and band
            and "Radio" not in trimmed
        ):
            val = _extract_quoted_value(trimmed)
            try:
                band.bandwidth_mhz = int(val)
            except ValueError:
                pass

    return band


# ─── QMI CLI Execution ───────────────────────────────────────────────────────


def run_qmicli(qmi_device: str) -> Optional[str]:
    """Run all qmicli commands for a given QMI device and return combined output."""
    command = (
        f"echo '===SIGNAL===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-signal-info; "
        f"echo '===SERVING===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-serving-system; "
        f"echo '===CELL===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-cell-location-info; "
        f"echo '===BAND===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-rf-band-info"
    )
    try:
        result = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None


def parse_combined_output(output: str) -> Dict[str, str]:
    """Split combined qmicli output into sections."""
    sections: Dict[str, str] = {}
    markers = ["===SIGNAL===", "===SERVING===", "===CELL===", "===BAND==="]
    keys = ["SIGNAL", "SERVING", "CELL", "BAND"]

    for i, marker in enumerate(markers):
        start = output.find(marker)
        if start == -1:
            continue
        start += len(marker)

        end = len(output)
        for j in range(i + 1, len(markers)):
            next_marker = output.find(markers[j], start)
            if next_marker != -1:
                end = next_marker
                break

        sections[keys[i]] = output[start:end].strip()

    return sections


def poll_modem(qmi_device: str, sim_slot: int) -> ModemStats:
    """Poll a single modem and return parsed stats."""
    stats = ModemStats(
        timestamp=datetime.now(timezone.utc),
        sim_slot=sim_slot,
        qmi_device=qmi_device,
    )

    raw = run_qmicli(qmi_device)
    if raw is None:
        stats.error = f"Failed to run qmicli on {qmi_device}"
        return stats

    sections = parse_combined_output(raw)

    if "SIGNAL" in sections:
        lte, nr5g = parse_signal_info(sections["SIGNAL"])
        stats.lte = lte
        stats.nr5g = nr5g

    if "SERVING" in sections:
        reg, carrier, mcc, mnc, roaming = parse_serving_system(sections["SERVING"])
        stats.registration_state = reg
        stats.carrier = carrier
        stats.carrier_mcc = mcc
        stats.carrier_mnc = mnc
        stats.is_roaming = roaming

    if "CELL" in sections:
        serving, neighbors = parse_cell_location_info(sections["CELL"])
        stats.serving_cell = serving
        stats.neighbor_cells = neighbors

    if "BAND" in sections:
        stats.active_band = parse_rf_band_info(sections["BAND"])

    return stats


# ─── Terminal Dashboard Renderer ──────────────────────────────────────────────

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_RED = "\033[41m"
BG_GREEN = "\033[42m"
BG_YELLOW = "\033[43m"
BG_BLUE = "\033[44m"
BG_MAGENTA = "\033[45m"
BG_CYAN = "\033[46m"


def get_quality_color(quality: int) -> str:
    if quality >= 80:
        return GREEN
    if quality >= 60:
        return YELLOW
    if quality >= 40:
        return YELLOW
    return RED


def get_bars_display(bars: int) -> str:
    filled = "█" * bars
    empty = "░" * (5 - bars)
    if bars >= 4:
        color = GREEN
    elif bars >= 3:
        color = YELLOW
    else:
        color = RED
    return f"{color}{filled}{DIM}{empty}{RESET}"


def get_mode_badge(mode: NetworkMode) -> str:
    if mode == NetworkMode.NR5G_SA:
        return f"{BG_MAGENTA}{WHITE}{BOLD} 5G SA {RESET}"
    if mode == NetworkMode.NR5G_NSA:
        return f"{BG_CYAN}{WHITE}{BOLD} 5G NSA {RESET}"
    if mode == NetworkMode.LTE:
        return f"{BG_BLUE}{WHITE}{BOLD} LTE {RESET}"
    return f"{DIM} ? {RESET}"


def format_db(value: Optional[float], unit: str = "dBm") -> str:
    if value is None:
        return f"{DIM}---{RESET}"
    return f"{value:+.1f} {unit}"


def render_signal_section(
    label: str, signal: Optional[SignalInfo], color: str
) -> List[str]:
    """Render a signal info block (LTE or 5G)."""
    lines: List[str] = []
    if signal is None or signal.rsrp is None:
        lines.append(f"  {color}{BOLD}{label}{RESET}  {DIM}No signal{RESET}")
        return lines

    lines.append(
        f"  {color}{BOLD}{label}{RESET}  {get_bars_display(signal.bars)}  {signal.quality}"
    )
    lines.append(
        f"    RSRP: {format_db(signal.rsrp)}    "
        f"RSRQ: {format_db(signal.rsrq, 'dB')}    "
        f"RSSI: {format_db(signal.rssi)}    "
        f"SNR: {format_db(signal.snr, 'dB')}"
    )
    return lines


def render_sim_panel(stats: ModemStats, width: int) -> List[str]:
    """Render a complete panel for one SIM slot."""
    lines: List[str] = []
    sep = "─" * (width - 2)

    # Header
    sim_label = f"SIM {stats.sim_slot}"
    device_label = stats.qmi_device

    if stats.error:
        lines.append(f"┌{sep}┐")
        lines.append(
            f"│ {BOLD}{sim_label}{RESET} ({device_label})"
            + " " * max(0, width - len(sim_label) - len(device_label) - 7)
            + "│"
        )
        lines.append(f"├{sep}┤")
        lines.append(
            f"│ {RED}⚠  {stats.error}{RESET}"
            + " " * max(0, width - len(stats.error) - 7)
            + "│"
        )
        lines.append(f"└{sep}┘")
        return lines

    mode_badge = get_mode_badge(stats.network_mode)
    quality = stats.signal_quality
    quality_color = get_quality_color(quality)
    quality_bar_filled = quality // 5
    quality_bar_empty = 20 - quality_bar_filled

    # Box top
    lines.append(f"┌{sep}┐")

    # SIM header with mode badge
    carrier_info = stats.carrier or "Unknown Carrier"
    reg_state = stats.registration_state
    roaming_flag = f" {YELLOW}[ROAMING]{RESET}" if stats.is_roaming else ""

    lines.append(
        f"│ {BOLD}{sim_label}{RESET}  {mode_badge}  "
        f"{CYAN}{carrier_info}{RESET}{roaming_flag}"
        f"  {DIM}({device_label}){RESET}"
    )

    if reg_state:
        plmn = ""
        if stats.carrier_mcc and stats.carrier_mnc:
            plmn = f"  MCC/MNC: {stats.carrier_mcc}/{stats.carrier_mnc}"
        lines.append(
            f"│ Registration: {reg_state}{plmn}"
        )

    lines.append(f"├{sep}┤")

    # Signal Quality Bar
    quality_bar = (
        f"{quality_color}{'█' * quality_bar_filled}"
        f"{DIM}{'░' * quality_bar_empty}{RESET}"
    )
    lines.append(
        f"│ Signal Quality: {quality_bar} {quality_color}{BOLD}{quality}%{RESET}"
    )
    lines.append(f"│")

    # LTE Signal
    for line in render_signal_section("LTE ", stats.lte, BLUE):
        lines.append(f"│{line}")

    # 5G NR Signal
    for line in render_signal_section("5G NR", stats.nr5g, MAGENTA):
        lines.append(f"│{line}")

    lines.append(f"│")

    # Band Info
    if stats.active_band:
        band = stats.active_band
        bw_str = f", {band.bandwidth_mhz} MHz" if band.bandwidth_mhz else ""
        lines.append(
            f"│ {BOLD}Band:{RESET} {band.band_name}  "
            f"Ch: {band.channel}{bw_str}  "
            f"Interface: {band.radio_interface}"
        )
    else:
        lines.append(f"│ {BOLD}Band:{RESET} {DIM}N/A{RESET}")

    # Serving Cell Info
    if stats.serving_cell:
        cell = stats.serving_cell
        cell_parts = [f"PCI: {cell.physical_cell_id}"]
        if cell.tac:
            cell_parts.append(f"TAC: {cell.tac}")
        if cell.earfcn is not None:
            cell_parts.append(f"EARFCN: {cell.earfcn}")
        if cell.global_cell_id:
            cell_parts.append(f"CID: {cell.global_cell_id}")
        if cell.timing_advance is not None:
            cell_parts.append(f"TA: {cell.timing_advance}")
        lines.append(f"│ {BOLD}Cell:{RESET} {', '.join(cell_parts)}")
        if cell.band_description:
            lines.append(f"│       {DIM}{cell.band_description}{RESET}")

    # Neighbor cells count
    if stats.neighbor_cells:
        lines.append(
            f"│ {BOLD}Neighbors:{RESET} {len(stats.neighbor_cells)} cell(s) detected"
        )

    lines.append(f"└{sep}┘")
    return lines


def render_dashboard(
    sim_stats: List[ModemStats], refresh_count: int, interval: int
) -> str:
    """Render the full dashboard."""
    term_width = shutil.get_terminal_size((80, 24)).columns
    panel_width = min(term_width, 100)

    output_lines: List[str] = []

    # Title
    title = " U5G-Max-Outdoor Signal Dashboard "
    pad = max(0, panel_width - len(title) - 2)
    left_pad = pad // 2
    right_pad = pad - left_pad
    output_lines.append(
        f"{BG_BLUE}{WHITE}{BOLD}"
        f"{'═' * left_pad}{title}{'═' * right_pad}"
        f"{RESET}"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_lines.append(
        f" {DIM}Updated: {now}  │  Refresh #{refresh_count}  │  Interval: {interval}s  │  Ctrl+C to exit{RESET}"
    )
    output_lines.append("")

    # SIM panels
    for stats in sim_stats:
        panel_lines = render_sim_panel(stats, panel_width)
        output_lines.extend(panel_lines)
        output_lines.append("")

    # Comparison summary (only if we have data from both SIMs)
    valid_stats = [s for s in sim_stats if s.error is None and s.primary_signal is not None]
    if len(valid_stats) >= 2:
        output_lines.append(f"┌{'─' * (panel_width - 2)}┐")
        output_lines.append(f"│ {BOLD}Comparison{RESET}")
        output_lines.append(f"├{'─' * (panel_width - 2)}┤")

        header = f"│ {'Metric':<14}"
        for s in valid_stats:
            header += f"  {'SIM ' + str(s.sim_slot):>12}"
        output_lines.append(header)
        output_lines.append(f"│ {'─' * 14}" + "  " + "  ".join(["─" * 12] * len(valid_stats)))

        # Quality
        row = f"│ {'Quality':<14}"
        for s in valid_stats:
            q = s.signal_quality
            c = get_quality_color(q)
            row += f"  {c}{q:>10}%{RESET} "
        output_lines.append(row)

        # Mode
        row = f"│ {'Mode':<14}"
        for s in valid_stats:
            row += f"  {s.network_mode_label:>12}"
        output_lines.append(row)

        # Primary RSRP
        row = f"│ {'RSRP':<14}"
        for s in valid_stats:
            ps = s.primary_signal
            val = format_db(ps.rsrp if ps else None)
            row += f"  {val:>12}"
        output_lines.append(row)

        # Primary SNR
        row = f"│ {'SNR':<14}"
        for s in valid_stats:
            ps = s.primary_signal
            val = format_db(ps.snr if ps else None, "dB")
            row += f"  {val:>12}"
        output_lines.append(row)

        # Carrier
        row = f"│ {'Carrier':<14}"
        for s in valid_stats:
            row += f"  {s.carrier:>12}"
        output_lines.append(row)

        # Best SIM recommendation
        best = max(valid_stats, key=lambda s: s.signal_quality)
        output_lines.append(f"│")
        output_lines.append(
            f"│ {GREEN}{BOLD}★ Best signal: SIM {best.sim_slot}{RESET}"
            f" ({best.signal_quality}% quality, {best.network_mode_label})"
        )

        output_lines.append(f"└{'─' * (panel_width - 2)}┘")

    return "\n".join(output_lines)


# ─── Main Loop ────────────────────────────────────────────────────────────────


def detect_qmi_devices() -> List[str]:
    """Auto-detect available QMI device paths."""
    devices: List[str] = []
    # U5G-Max-Outdoor typically has /dev/wwan0qmi0 and /dev/wwan1qmi0
    for candidate in ["/dev/wwan0qmi0", "/dev/wwan1qmi0"]:
        if os.path.exists(candidate):
            devices.append(candidate)
    return devices


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cellular modem signal dashboard for UniFi U5G-Max-Outdoor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Monitor both SIMs, refresh every 10s
  %(prog)s --interval 5             Refresh every 5 seconds
  %(prog)s --sim 1                  Monitor SIM 1 only
  %(prog)s --device /dev/wwan0qmi0  Use specific QMI device
  %(prog)s --demo                   Run with simulated data (no device needed)
        """,
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Refresh interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--sim",
        type=int,
        choices=[1, 2],
        default=None,
        help="Monitor specific SIM slot only (default: both)",
    )
    parser.add_argument(
        "--device",
        type=str,
        action="append",
        default=None,
        help="QMI device path(s). Can be specified multiple times. Auto-detects if not specified.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run with simulated data for demonstration",
    )
    args = parser.parse_args()

    if args.demo:
        run_demo_mode(args.interval)
        return

    # Determine QMI devices to poll
    if args.device:
        qmi_devices = args.device
    elif args.sim:
        idx = args.sim - 1
        dev = f"/dev/wwan{idx}qmi0"
        qmi_devices = [dev]
    else:
        qmi_devices = detect_qmi_devices()

    if not qmi_devices:
        print(f"{RED}Error: No QMI devices found.{RESET}")
        print("Make sure you are running this on a U5G-Max-Outdoor device,")
        print("or specify the device path with --device /dev/wwan0qmi0")
        print(f"\nUse {BOLD}--demo{RESET} to run with simulated data.")
        sys.exit(1)

    print(f"{CYAN}Starting cellular dashboard...{RESET}")
    print(f"Monitoring devices: {', '.join(qmi_devices)}")
    print(f"Refresh interval: {args.interval}s")
    print()

    refresh_count = 0
    try:
        while True:
            refresh_count += 1
            sim_stats: List[ModemStats] = []

            for i, device in enumerate(qmi_devices):
                sim_slot = args.sim if args.sim else i + 1
                stats = poll_modem(device, sim_slot)
                sim_stats.append(stats)

            # Clear screen and render
            print("\033[2J\033[H", end="")
            print(render_dashboard(sim_stats, refresh_count, args.interval))

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{CYAN}Dashboard stopped.{RESET}")
        sys.exit(0)


def run_demo_mode(interval: int) -> None:
    """Run dashboard with simulated data for demonstration/testing."""
    import random

    refresh_count = 0
    try:
        while True:
            refresh_count += 1

            # SIM 1: Good 5G NSA signal
            sim1 = ModemStats(
                timestamp=datetime.now(timezone.utc),
                sim_slot=1,
                qmi_device="/dev/wwan0qmi0",
                registration_state="registered",
                carrier="T-Mobile",
                carrier_mcc="310",
                carrier_mnc="260",
                is_roaming=False,
                lte=SignalInfo(
                    rsrp=-92 + random.uniform(-3, 3),
                    rsrq=-9 + random.uniform(-2, 2),
                    rssi=-62 + random.uniform(-3, 3),
                    snr=24.6 + random.uniform(-4, 4),
                ),
                nr5g=SignalInfo(
                    rsrp=-85 + random.uniform(-4, 4),
                    rsrq=-7 + random.uniform(-2, 2),
                    snr=28.0 + random.uniform(-5, 5),
                ),
                serving_cell=CellInfo(
                    physical_cell_id=132,
                    global_cell_id="12AB34CD",
                    tac="1A2B",
                    earfcn=875,
                    band_description="E-UTRA band 2: 1900 PCS",
                    is_serving=True,
                    timing_advance=12,
                ),
                neighbor_cells=[
                    CellInfo(physical_cell_id=133, earfcn=875),
                    CellInfo(physical_cell_id=210, earfcn=5230),
                ],
                active_band=BandInfo(
                    radio_interface="lte",
                    band_class="eutran-2",
                    channel=875,
                    bandwidth_mhz=20,
                ),
            )

            # SIM 2: Moderate LTE signal
            sim2 = ModemStats(
                timestamp=datetime.now(timezone.utc),
                sim_slot=2,
                qmi_device="/dev/wwan1qmi0",
                registration_state="registered",
                carrier="Verizon",
                carrier_mcc="311",
                carrier_mnc="480",
                is_roaming=False,
                lte=SignalInfo(
                    rsrp=-105 + random.uniform(-3, 3),
                    rsrq=-14 + random.uniform(-2, 2),
                    rssi=-75 + random.uniform(-3, 3),
                    snr=12.0 + random.uniform(-4, 4),
                ),
                serving_cell=CellInfo(
                    physical_cell_id=44,
                    global_cell_id="5E6F7890",
                    tac="3C4D",
                    earfcn=5230,
                    band_description="E-UTRA band 13: 700 MHz",
                    is_serving=True,
                    timing_advance=28,
                ),
                neighbor_cells=[
                    CellInfo(physical_cell_id=45, earfcn=5230),
                ],
                active_band=BandInfo(
                    radio_interface="lte",
                    band_class="eutran-13",
                    channel=5230,
                    bandwidth_mhz=10,
                ),
            )

            # Clear screen and render
            print("\033[2J\033[H", end="")
            print(render_dashboard([sim1, sim2], refresh_count, interval))

            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{CYAN}Dashboard stopped.{RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
