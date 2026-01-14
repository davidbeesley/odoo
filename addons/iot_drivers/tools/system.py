"""Operating system-related utilities for the IoT"""

import subprocess
from platform import system, release, machine

IOT_SYSTEM = system()
IOT_MACHINE = machine()

IOT_RPI_CHAR, IOT_WINDOWS_CHAR, IOT_X86_CHAR, IOT_TEST_CHAR = "L", "W", "X", "T"

IS_WINDOWS = IOT_SYSTEM[0] == IOT_WINDOWS_CHAR
IS_RPI = 'rpi' in release()
IS_X86 = IOT_SYSTEM == 'Linux' and not IS_RPI and IOT_MACHINE in ('x86_64', 'amd64', 'AMD64')
"""x86/amd64 Linux IoT system - first-class IoT platform for standard Linux servers/workstations."""
IS_TEST = not IS_RPI and not IS_WINDOWS and not IS_X86
"""IoT system "Test" correspond to macOS or other non-production platforms.
Expected to be used locally for development purposes only."""

IOT_CHAR = IOT_RPI_CHAR if IS_RPI else IOT_WINDOWS_CHAR if IS_WINDOWS else IOT_X86_CHAR if IS_X86 else IOT_TEST_CHAR
"""IoT system character used in the identifier and version.
- 'L' for Raspberry Pi (Linux ARM)
- 'W' for Windows
- 'X' for x86/amd64 Linux
- 'T' for Test (macOS, development platforms)"""

if IS_RPI:
    def rpi_only(function):
        """Decorator to check if the system is raspberry pi before running the function."""
        return function
else:
    def rpi_only(_):
        """No-op decorator for non raspberry pi systems."""
        return lambda *args, **kwargs: None


def mtr(host):
    """Run mtr command to the given host to get both
    packet loss (%) and average latency (ms).

    Note: we use ``-4`` in order to force IPv4, to avoid
    empty results on IPv6 networks.

    :param host: The host to ping.
    :return: A tuple of (packet_loss, avg_latency) or (None, None) if the command failed.
    """
    if IS_WINDOWS or IS_TEST:
        return None, None

    # sudo is required for probe interval < 1s, which almost divides execution time by 2
    command = ["sudo", "mtr", "-r", "-C", "--no-dns", "-c", "3", "-i", "0.2", "-4", "-G", "1", host]
    p = subprocess.run(command, stdout=subprocess.PIPE, text=True, check=False)
    if p.returncode != 0:
        return None, None

    output = p.stdout.strip()
    last_line = output.splitlines()[-1].split(",")
    try:
        return float(last_line[6]), float(last_line[10])
    except (IndexError, ValueError):
        return None, None
