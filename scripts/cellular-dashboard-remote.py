#!/usr/bin/env python3
"""
Cellular Modem Signal Dashboard – Remote Edition
=================================================
Runs qmicli commands **on a remote host** via SSH and renders the live
terminal dashboard **locally**.  No Python is required on the remote device;
only the standard qmicli binary must be present there.

SSH connection is established through a jump/bastion host using a
passphrase-protected key.  The passphrase is prompted interactively the first
time you run the script; a persistent SSH ControlMaster keeps the connection
alive for subsequent polls so you only need to type the passphrase once.

Configuration is read from a .env file (see .env.example for all options).

Usage:
    python3 cellular-dashboard-remote.py                  # defaults from .env
    python3 cellular-dashboard-remote.py --interval 5     # 5-second refresh
    python3 cellular-dashboard-remote.py --sim 1          # monitor SIM 1 only
    python3 cellular-dashboard-remote.py --env my.env     # custom env file path

Requirements (local machine):
    - Python 3.6+
    - openssh client (ssh binary must be on PATH)
    - A .env file with SSH connection settings (copy .env.example)
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple


# ─── .env loader ──────────────────────────────────────────────────────────────


def load_env(path: str) -> Dict[str, str]:
    """Parse a simple KEY=VALUE .env file, ignoring comments and blank lines."""
    env: Dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return env


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


@dataclass
class CellInfo:
    physical_cell_id: Optional[int] = None
    global_cell_id: Optional[str] = None
    tac: Optional[str] = None
    earfcn: Optional[int] = None
    band_description: Optional[str] = None
    is_serving: bool = False
    timing_advance: Optional[int] = None


@dataclass
class BandInfo:
    radio_interface: Optional[str] = None
    band_class: Optional[str] = None
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None


@dataclass
class ModemStats:
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sim_slot: int = 1
    qmi_device: str = ""
    registration_state: str = "unknown"
    carrier: str = "Unknown"
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
        if self.nr5g and self.lte:
            return NetworkMode.NR5G_NSA
        if self.nr5g:
            return NetworkMode.NR5G_SA
        if self.lte:
            return NetworkMode.LTE
        return NetworkMode.UNKNOWN

    @property
    def network_mode_label(self) -> str:
        return self.network_mode.value

    @property
    def primary_signal(self) -> Optional[SignalInfo]:
        return self.nr5g or self.lte

    @property
    def signal_quality(self) -> int:
        sig = self.primary_signal
        if sig is None or sig.rsrp is None:
            return 0
        rsrp = sig.rsrp
        if rsrp >= -80:
            return 100
        if rsrp <= -140:
            return 0
        return int((rsrp + 140) / 60 * 100)


# ─── qmicli output parsers ────────────────────────────────────────────────────


def _extract_quoted_value(line: str) -> str:
    m = re.search(r"'([^']*)'", line)
    return m.group(1) if m else ""


def _extract_value(line: str) -> str:
    if ":" in line:
        return line.split(":", 1)[1].strip().strip("'")
    return ""


def parse_signal_info(
    output: str,
) -> Tuple[Optional[SignalInfo], Optional[SignalInfo]]:
    lte: Optional[SignalInfo] = None
    nr5g: Optional[SignalInfo] = None
    current: Optional[SignalInfo] = None

    for raw_line in output.splitlines():
        trimmed = raw_line.strip()
        low = trimmed.lower()

        if "[lte]" in low:
            lte = SignalInfo()
            current = lte
        elif "[5gnr]" in low or "[nr5g]" in low or "5g-nr" in low:
            nr5g = SignalInfo()
            current = nr5g
        elif current is None:
            continue

        if low.startswith("rsrp") and "'" in trimmed:
            try:
                current.rsrp = float(_extract_quoted_value(trimmed))
            except ValueError:
                pass
        elif low.startswith("rsrq") and "'" in trimmed:
            try:
                current.rsrq = float(_extract_quoted_value(trimmed))
            except ValueError:
                pass
        elif low.startswith("rssi") and "'" in trimmed:
            try:
                current.rssi = float(_extract_quoted_value(trimmed))
            except ValueError:
                pass
        elif low.startswith("snr") and "'" in trimmed:
            try:
                current.snr = float(_extract_quoted_value(trimmed))
            except ValueError:
                pass

    return lte, nr5g


def parse_serving_system(
    output: str,
) -> Tuple[str, str, str, str, bool]:
    state = "unknown"
    carrier = "Unknown"
    mcc = ""
    mnc = ""
    roaming = False

    for raw_line in output.splitlines():
        trimmed = raw_line.strip()
        low = trimmed.lower()

        if "registration state:" in low:
            state = _extract_quoted_value(trimmed)
        elif "description:" in low and carrier == "Unknown":
            carrier = _extract_quoted_value(trimmed)
        elif "mcc:" in low:
            mcc = _extract_quoted_value(trimmed)
        elif "mnc:" in low:
            mnc = _extract_quoted_value(trimmed)
        elif "roaming status:" in low:
            roaming = "roaming" in _extract_quoted_value(trimmed).lower()

    return state, carrier, mcc, mnc, roaming


def parse_cell_location_info(
    output: str,
) -> Tuple[Optional[CellInfo], List[CellInfo]]:
    serving: Optional[CellInfo] = None
    neighbors: List[CellInfo] = []
    current: Optional[CellInfo] = None
    in_serving = False
    in_neighbor = False

    for raw_line in output.splitlines():
        trimmed = raw_line.strip()
        low = trimmed.lower()

        if "serving cell info" in low:
            serving = CellInfo(is_serving=True)
            current = serving
            in_serving = True
            in_neighbor = False
        elif "neighbor cell" in low:
            cell = CellInfo()
            neighbors.append(cell)
            current = cell
            in_neighbor = True
            in_serving = False
        elif current is None:
            continue

        if "physical cell id:" in low:
            try:
                current.physical_cell_id = int(_extract_quoted_value(trimmed))
            except ValueError:
                pass
        elif "global cell id:" in low:
            current.global_cell_id = _extract_quoted_value(trimmed)
        elif "tracking area code:" in low or "location area code:" in low:
            current.tac = _extract_quoted_value(trimmed)
        elif "eutra absolute rf channel number:" in low or "earfcn:" in low:
            try:
                current.earfcn = int(_extract_quoted_value(trimmed))
            except ValueError:
                pass
        elif "band:" in low and "band class" not in low:
            current.band_description = _extract_quoted_value(trimmed)
        elif "timing advance:" in low:
            try:
                current.timing_advance = int(_extract_quoted_value(trimmed))
            except ValueError:
                pass

    return serving, neighbors


def parse_rf_band_info(output: str) -> Optional[BandInfo]:
    band: Optional[BandInfo] = None

    for raw_line in output.splitlines():
        trimmed = raw_line.strip()
        low = trimmed.lower()

        if "radio interface" in low and "'" in trimmed:
            band = BandInfo()
            band.radio_interface = _extract_quoted_value(trimmed)
        elif band is None:
            continue

        if "band class:" in low:
            band.band_class = _extract_quoted_value(trimmed)
        elif "active channel:" in low:
            try:
                band.channel = int(_extract_quoted_value(trimmed))
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


# ─── Remote qmicli execution via SSH ControlMaster ───────────────────────────


def build_qmicli_command(qmi_device: str) -> str:
    """Return the shell command that collects all qmicli data for one device."""
    return (
        f"echo '===SIGNAL===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-signal-info; "
        f"echo '===SERVING===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-serving-system; "
        f"echo '===CELL===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-cell-location-info; "
        f"echo '===BAND===' && qmicli -d {qmi_device} --device-open-proxy --nas-get-rf-band-info"
    )


def run_remote_command(
    control_socket: str, target_host: str, command: str, timeout: int = 30
) -> Optional[str]:
    """Execute *command* on the remote host via an existing ControlMaster socket."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-S", control_socket,
                "-o", "StrictHostKeyChecking=accept-new",
                target_host,
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0 and not result.stdout:
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        return None


def check_control_socket(control_socket: str, target_host: str) -> bool:
    """Return True if the ControlMaster socket is alive."""
    try:
        result = subprocess.run(
            ["ssh", "-S", control_socket, "-O", "check", target_host],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


class SSHMaster:
    """
    Manages a persistent SSH ControlMaster connection.

    The master process is started with stdin/stdout/stderr inherited from the
    current terminal so that the passphrase prompt is displayed to the user
    naturally.  Once the master is authenticated the socket file appears and
    all subsequent commands are multiplexed over the same connection without
    re-authentication.
    """

    def __init__(
        self,
        ssh_key: str,
        jump_host: str,
        target_host: str,
        control_socket: str,
    ) -> None:
        self.ssh_key = os.path.expanduser(ssh_key)
        self.jump_host = jump_host
        self.target_host = target_host
        self.control_socket = control_socket
        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]

    def connect(self) -> None:
        """
        Start the SSH master process.  Blocks until the ControlMaster socket
        appears (i.e. authentication is complete), then returns.
        """
        # Remove stale socket if present
        try:
            os.unlink(self.control_socket)
        except FileNotFoundError:
            pass

        cmd = [
            "ssh",
            "-i", self.ssh_key,
            "-J", self.jump_host,
            "-M",
            "-S", self.control_socket,
            "-N",                                   # no remote command; keep tunnel open
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ControlPersist=no",              # let us manage lifecycle explicitly
            self.target_host,
        ]

        # Inherit the terminal so passphrase prompt works interactively
        self._proc = subprocess.Popen(cmd)

        print("Waiting for SSH authentication...", flush=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"SSH master process exited prematurely with code {self._proc.returncode}. "
                    "Check your SSH credentials and connection settings."
                )
            if os.path.exists(self.control_socket):
                break
            time.sleep(0.25)
        else:
            self.close()
            raise RuntimeError(
                "Timed out waiting for SSH master connection to be established."
            )

        print("SSH connection established.\n", flush=True)

    def run(self, command: str, timeout: int = 30) -> Optional[str]:
        """Run a command on the remote host and return its stdout."""
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("SSH master is not running.")
        return run_remote_command(
            self.control_socket, self.target_host, command, timeout=timeout
        )

    def close(self) -> None:
        """Send a stop signal to the ControlMaster and wait for it to exit."""
        if self.control_socket and os.path.exists(self.control_socket):
            subprocess.run(
                ["ssh", "-S", self.control_socket, "-O", "exit", self.target_host],
                capture_output=True,
            )
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        try:
            os.unlink(self.control_socket)
        except FileNotFoundError:
            pass


# ─── Parsing helpers ──────────────────────────────────────────────────────────


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


def poll_modem_remote(master: SSHMaster, qmi_device: str, sim_slot: int) -> ModemStats:
    """Query a single modem over SSH and return parsed stats."""
    stats = ModemStats(
        timestamp=datetime.now(timezone.utc),
        sim_slot=sim_slot,
        qmi_device=qmi_device,
    )

    raw = master.run(build_qmicli_command(qmi_device))
    if raw is None:
        stats.error = f"Failed to run qmicli on {qmi_device} (SSH error or timeout)"
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
    if quality >= 40:
        return YELLOW
    return RED


def get_rsrp_color(rsrp: Optional[float]) -> str:
    if rsrp is None:
        return DIM
    if rsrp >= -80:
        return GREEN
    if rsrp >= -100:
        return YELLOW
    if rsrp >= -110:
        return YELLOW
    return RED


def format_db(value: Optional[float], unit: str = "dBm") -> str:
    if value is None:
        return f"{DIM}N/A{RESET}"
    return f"{value:.1f} {unit}"


def format_optional(value: Optional[str], default: str = "N/A") -> str:
    if value is None or value == "":
        return default
    return value


def render_signal_panel(
    stats: ModemStats, panel_width: int = 52
) -> List[str]:
    """Render a single SIM's signal panel and return lines."""
    lines: List[str] = []
    w = panel_width

    # Header
    roam = f" {YELLOW}[ROAMING]{RESET}" if stats.is_roaming else ""
    if stats.error:
        header = f"SIM {stats.sim_slot}  {RED}ERROR{RESET}"
    elif stats.registration_state == "registered":
        header = f"SIM {stats.sim_slot}  {GREEN}●{RESET}  {stats.carrier}{roam}"
    else:
        header = f"SIM {stats.sim_slot}  {RED}○{RESET}  {stats.registration_state}"

    lines.append(f"┌{'─' * (w - 2)}┐")
    lines.append(f"│ {BOLD}{header:<{w - 3}}{RESET}│")
    lines.append(f"├{'─' * (w - 2)}┤")

    if stats.error:
        lines.append(f"│ {RED}{stats.error:<{w - 3}}{RESET}│")
        lines.append(f"└{'─' * (w - 2)}┘")
        return lines

    # Network mode & quality
    quality = stats.signal_quality
    qc = get_quality_color(quality)
    lines.append(
        f"│ {'Mode':<12} {stats.network_mode_label:<{w - 17}} │"
    )
    lines.append(
        f"│ {'Quality':<12} {qc}{quality:>3}%{RESET}{'':<{w - 20}} │"
    )

    # LTE signal
    if stats.lte:
        lines.append(f"├{'─' * (w - 2)}┤")
        lines.append(f"│ {BOLD}{'LTE / 4G':<{w - 3}}{RESET}│")
        rsrp_c = get_rsrp_color(stats.lte.rsrp)
        lines.append(
            f"│   {'RSRP':<10} {rsrp_c}{format_db(stats.lte.rsrp):<{w - 18}}{RESET} │"
        )
        lines.append(
            f"│   {'RSRQ':<10} {format_db(stats.lte.rsrq, 'dB'):<{w - 18}} │"
        )
        lines.append(
            f"│   {'RSSI':<10} {format_db(stats.lte.rssi):<{w - 18}} │"
        )
        lines.append(
            f"│   {'SNR':<10} {format_db(stats.lte.snr, 'dB'):<{w - 18}} │"
        )

    # 5G NR signal
    if stats.nr5g:
        lines.append(f"├{'─' * (w - 2)}┤")
        lines.append(f"│ {BOLD}{'5G NR':<{w - 3}}{RESET}│")
        rsrp_c = get_rsrp_color(stats.nr5g.rsrp)
        lines.append(
            f"│   {'RSRP':<10} {rsrp_c}{format_db(stats.nr5g.rsrp):<{w - 18}}{RESET} │"
        )
        lines.append(
            f"│   {'RSRQ':<10} {format_db(stats.nr5g.rsrq, 'dB'):<{w - 18}} │"
        )
        lines.append(
            f"│   {'SNR':<10} {format_db(stats.nr5g.snr, 'dB'):<{w - 18}} │"
        )

    # Serving cell
    if stats.serving_cell:
        cell = stats.serving_cell
        lines.append(f"├{'─' * (w - 2)}┤")
        lines.append(f"│ {BOLD}{'Serving Cell':<{w - 3}}{RESET}│")
        lines.append(
            f"│   {'PCI':<10} {format_optional(str(cell.physical_cell_id) if cell.physical_cell_id is not None else None):<{w - 18}} │"
        )
        lines.append(
            f"│   {'TAC':<10} {format_optional(cell.tac):<{w - 18}} │"
        )
        lines.append(
            f"│   {'EARFCN':<10} {format_optional(str(cell.earfcn) if cell.earfcn is not None else None):<{w - 18}} │"
        )
        if cell.band_description:
            desc = cell.band_description
            if len(desc) > w - 18:
                desc = desc[: w - 21] + "..."
            lines.append(f"│   {'Band':<10} {desc:<{w - 18}} │")

    # Active RF band
    if stats.active_band:
        band = stats.active_band
        lines.append(f"├{'─' * (w - 2)}┤")
        lines.append(f"│ {BOLD}{'RF Band':<{w - 3}}{RESET}│")
        lines.append(
            f"│   {'Interface':<10} {format_optional(band.radio_interface):<{w - 18}} │"
        )
        lines.append(
            f"│   {'Class':<10} {format_optional(band.band_class):<{w - 18}} │"
        )
        bw = f"{band.bandwidth_mhz} MHz" if band.bandwidth_mhz else "N/A"
        lines.append(f"│   {'Bandwidth':<10} {bw:<{w - 18}} │")

    lines.append(f"└{'─' * (w - 2)}┘")
    return lines


def render_dashboard(
    sim_stats: List[ModemStats],
    refresh_count: int,
    interval: int,
    target_host: str,
) -> str:
    """Render the full dashboard and return it as a string."""
    output_lines: List[str] = []
    panel_width = 54
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Title bar
    title = "Cellular Signal Dashboard (Remote)"
    output_lines.append(f"{BOLD}{CYAN}{title}{RESET}")
    output_lines.append(
        f"{DIM}Host: {target_host}  │  Refresh #{refresh_count}  │  {now}  │  interval: {interval}s{RESET}"
    )
    output_lines.append("")

    # Side-by-side panels
    panels = [render_signal_panel(s, panel_width) for s in sim_stats]

    if not panels:
        output_lines.append(f"{RED}No modem data available.{RESET}")
        return "\n".join(output_lines)

    if len(panels) == 1:
        output_lines.extend(panels[0])
    else:
        # Pad shorter panel with blank lines
        max_h = max(len(p) for p in panels)
        for p in panels:
            while len(p) < max_h:
                p.append(" " * panel_width)
        sep = "  "
        for row in zip(*panels):
            output_lines.append(sep.join(row))

    output_lines.append("")

    # Summary bar (only when both SIMs have data and no errors)
    valid_stats = [s for s in sim_stats if not s.error and s.primary_signal]
    if len(valid_stats) > 1:
        best = max(valid_stats, key=lambda s: s.signal_quality)
        output_lines.append(
            f"{GREEN}{BOLD}★ Best signal: SIM {best.sim_slot}{RESET}"
            f" ({best.signal_quality}% quality, {best.network_mode_label})"
        )

    return "\n".join(output_lines)


# ─── Auto-detect remote QMI devices ──────────────────────────────────────────


DEFAULT_QMI_DEVICES = ["/dev/wwan0qmi0", "/dev/wwan1qmi0"]


def detect_remote_qmi_devices(master: SSHMaster) -> List[str]:
    """Return QMI device paths that exist on the remote host."""
    devices: List[str] = []
    for candidate in DEFAULT_QMI_DEVICES:
        raw = master.run(f"test -e {candidate} && echo yes || echo no", timeout=10)
        if raw and raw.strip() == "yes":
            devices.append(candidate)
    return devices


# ─── Main ─────────────────────────────────────────────────────────────────────


def resolve_env_file(explicit: Optional[str]) -> str:
    """Return the path to the .env file to use."""
    if explicit:
        return explicit
    # Look next to this script first, then the current directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        os.path.join(script_dir, ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return os.path.join(script_dir, ".env")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remote cellular modem signal dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Use .env in script directory, both SIMs
  %(prog)s --interval 5             Refresh every 5 seconds
  %(prog)s --sim 1                  Monitor SIM 1 only
  %(prog)s --env /path/to/my.env    Use a custom .env file
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
        "--env",
        type=str,
        default=None,
        help="Path to .env config file (default: .env next to this script)",
    )
    args = parser.parse_args()

    # ── Load configuration ──
    env_path = resolve_env_file(args.env)
    config = load_env(env_path)

    ssh_key = config.get("SSH_KEY", "~/.ssh/id_ed25519")
    jump_host = config.get("JUMP_HOST", "")
    target_host = config.get("TARGET_HOST", "")
    qmi_devices_raw = config.get("QMI_DEVICES", "")

    if not jump_host or not target_host:
        print(
            f"{RED}Error: JUMP_HOST and TARGET_HOST must be set in the .env file.{RESET}"
        )
        print(f"Looking for config at: {env_path}")
        print(f"Copy .env.example to .env and fill in your values.")
        sys.exit(1)

    if qmi_devices_raw:
        qmi_devices = [d.strip() for d in qmi_devices_raw.split(",") if d.strip()]
    else:
        qmi_devices = []  # will auto-detect after connecting

    # Separate user@host into just the host portion for display
    display_host = target_host.split("@", 1)[-1] if "@" in target_host else target_host

    print(f"{CYAN}Cellular Signal Dashboard – Remote Edition{RESET}")
    print(f"Config : {env_path}")
    print(f"Jump   : {jump_host}")
    print(f"Target : {target_host}")
    print(f"Key    : {ssh_key}")
    print()

    # ── Set up SSH ControlMaster ──
    control_socket = os.path.join(
        tempfile.gettempdir(), f"ssh_cellular_{os.getpid()}.sock"
    )
    master = SSHMaster(
        ssh_key=ssh_key,
        jump_host=jump_host,
        target_host=target_host,
        control_socket=control_socket,
    )

    def _cleanup(signum=None, frame=None) -> None:  # type: ignore[assignment]
        print(f"\n{CYAN}Closing SSH connection...{RESET}")
        master.close()
        print(f"{CYAN}Dashboard stopped.{RESET}")
        sys.exit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    try:
        master.connect()
    except RuntimeError as exc:
        print(f"{RED}Error: {exc}{RESET}")
        sys.exit(1)

    # ── Auto-detect QMI devices if not specified ──
    if not qmi_devices:
        print("Auto-detecting QMI devices on remote host...", flush=True)
        qmi_devices = detect_remote_qmi_devices(master)
        if not qmi_devices:
            print(
                f"{RED}No QMI devices found on remote host.{RESET}\n"
                "Set QMI_DEVICES in your .env file, e.g.:\n"
                "  QMI_DEVICES=/dev/wwan0qmi0,/dev/wwan1qmi0"
            )
            master.close()
            sys.exit(1)
        print(f"Found: {', '.join(qmi_devices)}\n", flush=True)

    # Apply --sim filter
    if args.sim is not None:
        idx = args.sim - 1
        if idx < len(qmi_devices):
            qmi_devices = [qmi_devices[idx]]
        else:
            print(
                f"{YELLOW}Warning: SIM {args.sim} not found; monitoring all detected devices.{RESET}"
            )

    print(f"Monitoring: {', '.join(qmi_devices)}  (interval: {args.interval}s)")
    print()

    # ── Poll loop ──
    refresh_count = 0
    while True:
        refresh_count += 1
        sim_stats: List[ModemStats] = []

        for i, device in enumerate(qmi_devices):
            slot = args.sim if (args.sim is not None) else i + 1
            stats = poll_modem_remote(master, device, slot)
            sim_stats.append(stats)

        # Clear screen and render
        print("\033[2J\033[H", end="")
        print(render_dashboard(sim_stats, refresh_count, args.interval, display_host))

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
