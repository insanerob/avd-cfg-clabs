#!/usr/bin/env python3
"""Build a containerlab (cEOS) lab from Arista AVD generated switch configs.

Input : a directory of AVD intended configs (one <hostname>.cfg per switch).
Output: <output-directory>/<lab-name>.clab.yml       containerlab topology
        <output-directory>/configs/<hostname>.cfg    cEOS-ready startup configs

Links between switches are discovered from the configs themselves:
  1. routed point-to-point links  - two Ethernet interfaces sharing a /31 or /30
  2. everything else (MLAG peer links, L2 uplinks, unnumbered underlay)
     - the interface description names the peer switch and peer interface,
       e.g. "P2P_LINK_TO_DC1-SPINE1_Ethernet1" or "MLAG_dc1-leaf1b_Ethernet3"

Each config is adapted for cEOS: the management interface becomes Management0
with a lab address, the management default route points at the lab gateway,
and hardware-only commands are removed.

Only the Python standard library is used (Python 3.7+).

Example:
    ./avd_to_containerlab.py --configs-directory intended/configs \
        --output-directory lab --lab-name fabric-lab --ceos-image ceos:4.32.2F
    sudo containerlab deploy -t lab/fabric-lab.clab.yml
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# Constants and patterns
# --------------------------------------------------------------------------

# cEOS always calls its management port Management0, whatever the hardware
# switch calls it (Management1, Management1/1 ...).
CEOS_MANAGEMENT_INTERFACE_NAME = "Management0"

# A subnet this small can only hold two addresses, so two interfaces sharing
# it must be the two ends of one cable. /30 and /31 both qualify.
LARGEST_POINT_TO_POINT_PREFIX_LENGTH = 30

# Top-level commands (and everything indented beneath them) that cEOS cannot run.
UNSUPPORTED_TOP_LEVEL_COMMAND_PATTERNS = [
    re.compile(r"^platform "),
    re.compile(r"^hardware "),
    re.compile(r"^queue-monitor "),
    re.compile(r"^transceiver "),
]

# Indented commands (inside interface stanzas etc.) that cEOS cannot run.
UNSUPPORTED_INDENTED_COMMAND_PATTERNS = [
    re.compile(r"^\s+speed "),
    re.compile(r"^\s+error-correction encoding "),
    re.compile(r"^\s+transceiver "),
    re.compile(r"^\s+mac timestamp "),
]

# Patterns that pick the interesting lines out of an EOS config.
#   hostname dc1-leaf1a
#   interface Ethernet1
#      description P2P_LINK_TO_DC1-SPINE1_Ethernet1
#      ip address 10.255.255.1/31
HOSTNAME_PATTERN = re.compile(r"^hostname (?P<hostname>\S+)")
INTERFACE_HEADER_PATTERN = re.compile(r"^interface (?P<interface_name>\S+)")
DESCRIPTION_PATTERN = re.compile(r"^\s+description (?P<description>.+)$")
IPV4_ADDRESS_PATTERN = re.compile(r"^\s+ip address (?P<address>\d+\.\d+\.\d+\.\d+/\d+)")
VRF_MEMBERSHIP_PATTERN = re.compile(r"^\s+vrf (?:forwarding )?(?P<vrf_name>\S+)")

# A physical front-panel port: Ethernet1, Ethernet3/1, Ethernet3/1/1.
ETHERNET_INTERFACE_PATTERN = re.compile(r"^Ethernet\d+(?:/\d+)*$")

# A description that ends in an interface name, e.g. "..._Ethernet3/1".
# Whatever comes before the interface is checked for a switch hostname later.
DESCRIPTION_PEER_INTERFACE_PATTERN = re.compile(
    r"^(?P<text_before_interface>.*?)[\s_:\-]*"
    r"(?:Ethernet|Eth|Et)(?P<port_numbers>\d+(?:/\d+)*)$",
    re.IGNORECASE,
)

# A static default route, with or without a VRF:
#   ip route vrf MGMT 0.0.0.0/0 10.0.0.1
DEFAULT_ROUTE_PATTERN = re.compile(
    r"^(?P<route_prefix>ip route (?:vrf \S+ )?0\.0\.0\.0/0 )"
    r"(?P<next_hop>\d+\.\d+\.\d+\.\d+)(?P<route_suffix>.*)$"
)

# An endpoint is one end of a cable: (switch hostname, EOS interface name).
Endpoint = Tuple[str, str]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

# What we need to know about one front-panel port in order to work out what
# it is cabled to: its description and any subnets configured on it
# (including those on its sub-interfaces).
@dataclass
class EthernetInterface:
    interface_name: str
    description: str = ""
    ipv4_networks: List[ipaddress.IPv4Network] = field(default_factory=list)


# Everything read from one AVD .cfg file: the raw lines (kept so the config
# can be rewritten later), the front-panel ports, and the real management
# port name and subnet (needed to swap them for lab values).
@dataclass
class SwitchConfig:
    hostname: str
    source_path: Path
    config_lines: List[str]
    ethernet_interfaces: Dict[str, EthernetInterface] = field(default_factory=dict)
    management_interface_name: Optional[str] = None
    management_network: Optional[ipaddress.IPv4Network] = None


# --------------------------------------------------------------------------
# Parsing the AVD configs
# --------------------------------------------------------------------------

# EOS config is indentation based: a top-level command (e.g. "interface
# Ethernet1") owns the indented lines beneath it. This groups the flat list
# of lines into those blocks so a whole stanza can be read, rewritten or
# dropped as one unit. Each block is [top-level line, child line, ...].
def split_into_config_blocks(config_lines: List[str]) -> List[List[str]]:
    config_blocks: List[List[str]] = []
    for config_line in config_lines:
        is_indented = config_line[:1] in (" ", "\t")
        if is_indented and config_blocks:
            config_blocks[-1].append(config_line)
        else:
            config_blocks.append([config_line])
    return config_blocks


# Read one AVD .cfg file and pull out the hostname, every front-panel port
# (description + subnets) and the management port. Nothing is modified here.
def parse_switch_config(config_path: Path) -> SwitchConfig:
    # Until a "hostname" line is found, fall back to the file name.
    config_lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    switch = SwitchConfig(hostname=config_path.stem, source_path=config_path,
                          config_lines=config_lines)

    for config_block in split_into_config_blocks(config_lines):
        header_line = config_block[0]

        # "hostname X" - the name used for the lab node.
        hostname_match = HOSTNAME_PATTERN.match(header_line)
        if hostname_match:
            switch.hostname = hostname_match.group("hostname")
            continue

        # Only interface stanzas matter from here on.
        interface_match = INTERFACE_HEADER_PATTERN.match(header_line)
        if not interface_match:
            continue
        interface_name = interface_match.group("interface_name")

        # Collect the description and IPv4 subnets from the stanza's children.
        description = ""
        ipv4_networks: List[ipaddress.IPv4Network] = []
        for stanza_line in config_block[1:]:
            description_match = DESCRIPTION_PATTERN.match(stanza_line)
            if description_match:
                description = description_match.group("description").strip()
            address_match = IPV4_ADDRESS_PATTERN.match(stanza_line)
            if address_match:
                ipv4_networks.append(
                    ipaddress.ip_interface(address_match.group("address")).network)

        # Management ports are remembered separately, never cabled.
        # The first one carrying an address is treated as "the" management port.
        if interface_name.startswith("Management"):
            if switch.management_interface_name is None and ipv4_networks:
                switch.management_interface_name = interface_name
                switch.management_network = ipv4_networks[0]
            continue

        # Sub-interfaces (Ethernet1.100) ride on the parent's cable, so their
        # subnets are recorded against the parent port. Port-Channels, Vlans,
        # Loopbacks etc. are not physical ports and are skipped.
        parent_interface_name = interface_name.split(".")[0]
        if not ETHERNET_INTERFACE_PATTERN.match(parent_interface_name):
            continue
        ethernet_interface = switch.ethernet_interfaces.setdefault(
            parent_interface_name, EthernetInterface(parent_interface_name))
        if interface_name == parent_interface_name:
            ethernet_interface.description = description
        ethernet_interface.ipv4_networks.extend(ipv4_networks)

    return switch


# --------------------------------------------------------------------------
# Link discovery
# --------------------------------------------------------------------------

# Sort key that orders numbers by value instead of alphabetically, so
# Ethernet10 comes after Ethernet9 and leaf10 after leaf9. Keeps the
# generated files in a stable, readable order (clean diffs in CI).
def natural_sort_key(text: str) -> List[object]:
    return [int(piece) if piece.isdigit() else piece.lower()
            for piece in re.split(r"(\d+)", text)]


# Sort key for one cable end: by switch name, then by interface.
def endpoint_sort_key(endpoint: Endpoint) -> List[object]:
    hostname, interface_name = endpoint
    return natural_sort_key(hostname) + natural_sort_key(interface_name)


# Build a link from two cable ends, always in the same order. Both switches
# describe the same cable from opposite sides; ordering the ends means the
# cable is stored once rather than twice.
def make_link(first_endpoint: Endpoint, second_endpoint: Endpoint) -> Tuple[Endpoint, Endpoint]:
    ordered_endpoints = sorted([first_endpoint, second_endpoint], key=endpoint_sort_key)
    return ordered_endpoints[0], ordered_endpoints[1]


# Work out which switch and port an interface description points at.
# Accepts any description shaped "<anything><known hostname><separator>
# <Ethernet interface>", which covers the AVD default formats:
#   AVD 4.x  P2P_LINK_TO_DC1-SPINE1_Ethernet1   MLAG_PEER_dc1-leaf1b_Ethernet3
#   AVD 5.x  P2P_dc1-spine1_Ethernet1           MLAG_dc1-leaf1b_Ethernet3
# Returns (peer hostname, peer interface), or None when the description does
# not name a switch in this fabric (a server port, for example).
def find_peer_endpoint_in_description(
    description: str, hostname_by_lowercase: Dict[str, str]
) -> Optional[Endpoint]:
    # Step 1: the description must end with an interface name.
    description_match = DESCRIPTION_PEER_INTERFACE_PATTERN.match(description.strip())
    if not description_match:
        return None
    text_before_interface = description_match.group("text_before_interface").lower()

    # Step 2: the text just before the interface must end with a known
    # hostname. Matching ignores case (AVD 4.x upper-cases the peer name) and
    # the longest hostname wins, so "dc1-leaf1" beats a shorter "leaf1".
    matched_hostname: Optional[str] = None
    for lowercase_hostname, hostname in hostname_by_lowercase.items():
        if not text_before_interface.endswith(lowercase_hostname):
            continue
        character_before_hostname = text_before_interface[
            len(text_before_interface) - len(lowercase_hostname) - 1:
            len(text_before_interface) - len(lowercase_hostname)]
        if character_before_hostname.isalnum():
            continue  # e.g. "leaf1" must not match the tail of "l2leaf1"
        if matched_hostname is None or len(hostname) > len(matched_hostname):
            matched_hostname = hostname

    if matched_hostname is None:
        return None
    return matched_hostname, "Ethernet" + description_match.group("port_numbers")


# Work out every switch-to-switch cable in the fabric, in two passes:
#   pass 1 - routed links, matched by a shared /31 or /30 (most reliable)
#   pass 2 - whatever is left, matched by interface descriptions
# A port can only hold one cable, so each cable end is used at most once.
# Anything ambiguous is skipped and reported in `warnings`, never guessed.
def discover_links(
    switches: Dict[str, SwitchConfig], warnings: List[str]
) -> List[Tuple[Endpoint, Endpoint]]:
    # links: the cables found so far. connected_endpoints: ports already used.
    links: Set[Tuple[Endpoint, Endpoint]] = set()
    connected_endpoints: Set[Endpoint] = set()

    # Pass 1: routed point-to-point links, matched by shared subnet.
    # First index every point-to-point subnet by the ports it is configured on.
    endpoints_by_network: Dict[ipaddress.IPv4Network, Set[Endpoint]] = {}
    for switch in switches.values():
        for ethernet_interface in switch.ethernet_interfaces.values():
            for ipv4_network in ethernet_interface.ipv4_networks:
                if ipv4_network.prefixlen >= LARGEST_POINT_TO_POINT_PREFIX_LENGTH:
                    endpoints_by_network.setdefault(ipv4_network, set()).add(
                        (switch.hostname, ethernet_interface.interface_name))

    # A subnet seen on exactly two ports of two different switches is a cable.
    for ipv4_network, network_endpoints in sorted(endpoints_by_network.items()):
        if len(network_endpoints) < 2:
            continue  # the other end is not a switch in this fabric
        if len(network_endpoints) > 2:
            warnings.append(
                f"subnet {ipv4_network} appears on {len(network_endpoints)} interfaces "
                f"({sorted(network_endpoints)}); not using it to infer a link")
            continue
        first_endpoint, second_endpoint = sorted(network_endpoints, key=endpoint_sort_key)
        if first_endpoint[0] == second_endpoint[0]:
            continue
        if first_endpoint in connected_endpoints or second_endpoint in connected_endpoints:
            link = make_link(first_endpoint, second_endpoint)
            if link not in links:
                warnings.append(
                    f"subnet {ipv4_network} suggests {first_endpoint} <-> {second_endpoint} "
                    f"but one end is already cabled elsewhere; skipped")
            continue
        links.add(make_link(first_endpoint, second_endpoint))
        connected_endpoints.update((first_endpoint, second_endpoint))

    # Pass 2: remaining interfaces, matched by what their descriptions claim.
    # First record, for every port, the peer port its description points at.
    hostname_by_lowercase = {hostname.lower(): hostname for hostname in switches}
    claimed_peer_by_endpoint: Dict[Endpoint, Endpoint] = {}
    for switch in switches.values():
        for ethernet_interface in switch.ethernet_interfaces.values():
            claimed_peer_endpoint = find_peer_endpoint_in_description(
                ethernet_interface.description, hostname_by_lowercase)
            if claimed_peer_endpoint and claimed_peer_endpoint[0] != switch.hostname:
                claimed_peer_by_endpoint[
                    (switch.hostname, ethernet_interface.interface_name)] = claimed_peer_endpoint

    # Then turn each claim into a cable, unless the cable already exists, a
    # port is already in use, or the far end's description points elsewhere.
    for local_endpoint in sorted(claimed_peer_by_endpoint, key=endpoint_sort_key):
        peer_endpoint = claimed_peer_by_endpoint[local_endpoint]
        if make_link(local_endpoint, peer_endpoint) in links:
            continue
        if local_endpoint in connected_endpoints or peer_endpoint in connected_endpoints:
            warnings.append(
                f"description on {local_endpoint} points at {peer_endpoint} "
                f"but one end is already cabled elsewhere; skipped")
            continue
        peer_claim = claimed_peer_by_endpoint.get(peer_endpoint)
        if peer_claim is not None and peer_claim != local_endpoint:
            warnings.append(
                f"descriptions disagree: {local_endpoint} points at {peer_endpoint}, "
                f"which points at {peer_claim}; skipped")
            continue
        links.add(make_link(local_endpoint, peer_endpoint))
        connected_endpoints.update((local_endpoint, peer_endpoint))

    return sorted(links, key=lambda link: endpoint_sort_key(link[0]) + endpoint_sort_key(link[1]))


# --------------------------------------------------------------------------
# cEOS adaptation
# --------------------------------------------------------------------------

# Translate an EOS port name into the Linux interface name containerlab
# creates inside the cEOS container: Ethernet1 -> eth1, Ethernet3/1 -> eth3_1.
# cEOS turns the underscore back into a slash when it boots.
def eos_interface_to_container_interface(eos_interface_name: str) -> str:
    port_numbers = eos_interface_name[len("Ethernet"):]
    return "eth" + port_numbers.replace("/", "_")


# Produce the startup config for the lab copy of one switch. The production
# config is kept as-is except for what cannot work in a container:
#   - hardware-only commands are removed
#   - the management port becomes Management0 with its lab address
#   - the management default route is pointed at the lab gateway
# Returns the new config as a single string.
def adapt_config_for_ceos(
    switch: SwitchConfig,
    lab_management_address: ipaddress.IPv4Interface,
    lab_management_gateway: ipaddress.IPv4Address,
) -> str:
    adapted_lines: List[str] = []
    management_interface_written = False

    for config_block in split_into_config_blocks(switch.config_lines):
        header_line = config_block[0]

        # Drop hardware-only top-level commands together with their children.
        if any(pattern.match(header_line) for pattern in UNSUPPORTED_TOP_LEVEL_COMMAND_PATTERNS):
            continue

        # Rewrite the management port stanza: rename it to Management0 and
        # swap the real address for the lab one, keeping description/VRF etc.

        interface_match = INTERFACE_HEADER_PATTERN.match(header_line)
        if interface_match and interface_match.group("interface_name").startswith("Management"):
            if interface_match.group("interface_name") != switch.management_interface_name:
                continue  # extra management ports do not exist on cEOS
            adapted_lines.append(f"interface {CEOS_MANAGEMENT_INTERFACE_NAME}")
            for stanza_line in config_block[1:]:
                if IPV4_ADDRESS_PATTERN.match(stanza_line):
                    continue
                adapted_lines.append(stanza_line)
            adapted_lines.append(f"   ip address {lab_management_address}")
            management_interface_written = True
            continue

        # Repoint the management default route at the lab gateway. Only a
        # default route whose next hop sits in the real management subnet is
        # touched, so genuine fabric default routes are left alone.
        default_route_match = DEFAULT_ROUTE_PATTERN.match(header_line)
        if default_route_match and switch.management_network is not None:
            original_next_hop = ipaddress.ip_address(default_route_match.group("next_hop"))
            if original_next_hop in switch.management_network:
                adapted_lines.append(
                    default_route_match.group("route_prefix")
                    + str(lab_management_gateway)
                    + default_route_match.group("route_suffix"))
                continue

        # Every other block is copied through unchanged, minus any
        # hardware-only lines inside it (interface speed, FEC ...).
        for config_line in config_block:
            if any(pattern.match(config_line)
                   for pattern in UNSUPPORTED_INDENTED_COMMAND_PATTERNS):
                continue
            adapted_lines.append(config_line)

    if not management_interface_written:
        # The source config had no addressed management port: add one so the
        # lab node is still reachable, placed before the closing "end".
        insert_position = len(adapted_lines)
        while insert_position > 0 and adapted_lines[insert_position - 1].strip() in ("end", ""):
            insert_position -= 1
        adapted_lines[insert_position:insert_position] = [
            "!",
            f"interface {CEOS_MANAGEMENT_INTERFACE_NAME}",
            f"   ip address {lab_management_address}",
            "!",
        ]

    adapted_config = "\n".join(adapted_lines) + "\n"

    # Other commands (ntp, tacacs, logging source-interface ...) still refer to
    # the hardware management port by name.
    if switch.management_interface_name:
        adapted_config = re.sub(
            rf"\b{re.escape(switch.management_interface_name)}(?![\d/])",
            CEOS_MANAGEMENT_INTERFACE_NAME,
            adapted_config)
    return adapted_config


# Hand out lab management addresses. containerlab gives the first usable
# address of the subnet to the docker bridge (the lab gateway); the switches
# take the following addresses in hostname order, so reruns give every switch
# the same address as long as the set of switches is unchanged.
# Returns (gateway address, {hostname: address with prefix length}).
def allocate_lab_management_addresses(
    hostnames: List[str], lab_management_subnet: ipaddress.IPv4Network
) -> Tuple[ipaddress.IPv4Address, Dict[str, ipaddress.IPv4Interface]]:
    usable_addresses = lab_management_subnet.hosts()
    lab_management_gateway = next(usable_addresses)
    address_by_hostname: Dict[str, ipaddress.IPv4Interface] = {}
    for hostname in hostnames:
        try:
            switch_address = next(usable_addresses)
        except StopIteration:
            raise SystemExit(
                f"error: management subnet {lab_management_subnet} is too small "
                f"for {len(hostnames)} switches")
        address_by_hostname[hostname] = ipaddress.ip_interface(
            f"{switch_address}/{lab_management_subnet.prefixlen}")
    return lab_management_gateway, address_by_hostname


# --------------------------------------------------------------------------
# Topology file
# --------------------------------------------------------------------------

# Build the text of the containerlab topology file (<lab-name>.clab.yml):
# the management network, one cEOS node per switch (with its fixed management
# address and startup config) and one entry per discovered cable.
# The YAML is written by hand so the script needs no third-party packages.
def render_topology_yaml(
    lab_name: str,
    ceos_image: str,
    lab_management_subnet: ipaddress.IPv4Network,
    management_address_by_hostname: Dict[str, ipaddress.IPv4Interface],
    links: List[Tuple[Endpoint, Endpoint]],
    startup_configs_directory_name: str,
) -> str:
    # Header: lab name, management network and the image shared by all nodes.
    yaml_lines = [
        "# Generated by avd_to_containerlab.py - do not edit, regenerate instead.",
        f'name: "{lab_name}"',
        "",
        "mgmt:",
        f'  network: "{lab_name}-mgmt"',
        f'  ipv4-subnet: "{lab_management_subnet}"',
        "",
        "topology:",
        "  kinds:",
        "    ceos:",
        f'      image: "{ceos_image}"',
        "",
        "  nodes:",
    ]
    # One node per switch. startup-config paths are relative to this file.
    for hostname, management_address in management_address_by_hostname.items():
        yaml_lines += [
            f'    "{hostname}":',
            "      kind: ceos",
            f'      mgmt-ipv4: "{management_address.ip}"',
            f'      startup-config: "{startup_configs_directory_name}/{hostname}.cfg"',
        ]
    # One link per cable, using the container-side interface names.
    yaml_lines += ["", "  links:" if links else "  links: []"]
    for (first_hostname, first_interface), (second_hostname, second_interface) in links:
        first_container_interface = eos_interface_to_container_interface(first_interface)
        second_container_interface = eos_interface_to_container_interface(second_interface)
        yaml_lines.append(
            f'    - endpoints: ["{first_hostname}:{first_container_interface}", '
            f'"{second_hostname}:{second_container_interface}"]')
    return "\n".join(yaml_lines) + "\n"


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

# Define and read the command-line options. Only --configs-directory is
# required; everything else has a default.
def parse_command_line() -> argparse.Namespace:
    argument_parser = argparse.ArgumentParser(
        description="Generate a containerlab cEOS lab from Arista AVD switch configs.")
    argument_parser.add_argument(
        "--configs-directory", type=Path, required=True,
        help="directory of AVD generated configs (usually intended/configs)")
    argument_parser.add_argument(
        "--output-directory", type=Path, default=Path("lab"),
        help="where to write the topology file and startup configs (default: ./lab)")
    argument_parser.add_argument(
        "--lab-name", default="fabric-lab",
        help="containerlab lab name (default: fabric-lab)")
    argument_parser.add_argument(
        "--ceos-image", default="ceos:latest",
        help="cEOS container image as imported into docker/podman (default: ceos:latest)")
    argument_parser.add_argument(
        "--management-subnet", type=ipaddress.ip_network, default="172.31.255.0/24",
        help="lab management subnet; must NOT overlap the real management network "
             "(default: 172.31.255.0/24)")
    argument_parser.add_argument(
        "--config-file-pattern", default="*.cfg",
        help="glob for config files inside the configs directory (default: *.cfg)")
    return argument_parser.parse_args()


# Run the whole conversion: read the configs, find the cables, write the
# startup configs and topology file, then print a summary.
# Returns the process exit code (0 = success, 1 = nothing was generated).
def main() -> int:
    arguments = parse_command_line()
    lab_management_subnet = ipaddress.ip_network(arguments.management_subnet)

    # Step 1: find the AVD config files.
    config_paths = sorted(arguments.configs_directory.glob(arguments.config_file_pattern))
    if not config_paths:
        print(f"error: no files matching {arguments.config_file_pattern!r} in "
              f"{arguments.configs_directory}", file=sys.stderr)
        return 1

    # Step 2: parse each config. Two files claiming one hostname is an error.
    warnings: List[str] = []
    switches: Dict[str, SwitchConfig] = {}
    for config_path in config_paths:
        switch = parse_switch_config(config_path)
        if switch.hostname in switches:
            print(f"error: hostname {switch.hostname!r} is used by both "
                  f"{switches[switch.hostname].source_path} and {config_path}", file=sys.stderr)
            return 1
        switches[switch.hostname] = switch

    # Step 3: refuse a lab management subnet that overlaps a real one, since
    # the lab bridge would then capture this server's traffic to production.
    sorted_hostnames = sorted(switches, key=natural_sort_key)
    for hostname in sorted_hostnames:
        real_management_network = switches[hostname].management_network
        if real_management_network and real_management_network.overlaps(lab_management_subnet):
            print(f"error: lab management subnet {lab_management_subnet} overlaps the real "
                  f"management network {real_management_network} of {hostname}; "
                  f"choose another with --management-subnet", file=sys.stderr)
            return 1

    # Step 4: work out the cabling and the lab management addressing.
    links = discover_links(switches, warnings)
    lab_management_gateway, management_address_by_hostname = (
        allocate_lab_management_addresses(sorted_hostnames, lab_management_subnet))

    # Step 5: write a cEOS-ready startup config for every switch.
    startup_configs_directory_name = "configs"
    startup_configs_directory = arguments.output_directory / startup_configs_directory_name
    startup_configs_directory.mkdir(parents=True, exist_ok=True)
    for hostname in sorted_hostnames:
        adapted_config = adapt_config_for_ceos(
            switches[hostname], management_address_by_hostname[hostname], lab_management_gateway)
        (startup_configs_directory / f"{hostname}.cfg").write_text(adapted_config, encoding="utf-8")

    # Step 6: write the containerlab topology file.
    topology_path = arguments.output_directory / f"{arguments.lab_name}.clab.yml"
    topology_path.write_text(
        render_topology_yaml(
            arguments.lab_name, arguments.ceos_image, lab_management_subnet,
            management_address_by_hostname, links, startup_configs_directory_name),
        encoding="utf-8")

    # Step 7: report what was built, and which configured ports ended up with
    # no cable (normally server-facing or unused ports - worth a glance, as a
    # missed fabric link would show up here too).
    connected_endpoints = {endpoint for link in links for endpoint in link}
    print(f"switches : {len(sorted_hostnames)}")
    print(f"links    : {len(links)}")
    print(f"topology : {topology_path}")
    print(f"configs  : {startup_configs_directory}/")
    for hostname in sorted_hostnames:
        uncabled_interfaces = sorted(
            (interface_name for interface_name in switches[hostname].ethernet_interfaces
             if (hostname, interface_name) not in connected_endpoints),
            key=natural_sort_key)
        if uncabled_interfaces:
            print(f"  {hostname}: {len(uncabled_interfaces)} configured port(s) with no "
                  f"fabric peer (hosts/unused): {', '.join(uncabled_interfaces)}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


# Only run when executed as a script, not when imported.
if __name__ == "__main__":
    sys.exit(main())
