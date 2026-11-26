#!/usr/bin/env python3
"""Build a containerlab (cEOS) topology from Arista AVD generated switch configs.

Reads the <hostname>.cfg files AVD produces, works out which switch ports are
cabled to which, and writes a containerlab topology file with one cEOS node
per switch and one link per cable. Nothing else is configured: the lab comes
up with the fabric's physical connectivity and blank switches.

Cabling is taken from the interface descriptions AVD generates, which name the
peer switch and peer port, for example:
    P2P_LINK_TO_DC1-SPINE1_Ethernet1      MLAG_PEER_dc1-leaf1b_Ethernet3
    P2P_dc1-spine1_Ethernet1              MLAG_dc1-leaf1b_Ethernet3

Only the Python standard library is used.

Example:
    ./avd_to_containerlab.py --configs-directory intended/configs \
        --lab-name fabric-lab --ceos-image ceos:4.32.2F
    sudo containerlab deploy -t fabric-lab.clab.yml
"""

import argparse
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Patterns that pick the interesting lines out of an EOS config
# --------------------------------------------------------------------------

#   hostname dc1-leaf1a
HOSTNAME_PATTERN = re.compile(r"^hostname (?P<hostname>\S+)")

#   interface Ethernet1        interface Ethernet3/1
# Front-panel ports only: sub-interfaces, Port-Channels, Vlans, Loopbacks and
# Management ports do not match.
ETHERNET_INTERFACE_PATTERN = re.compile(r"^interface (?P<interface_name>Ethernet\d+(?:/\d+)*)$")

#      description P2P_LINK_TO_DC1-SPINE1_Ethernet1
DESCRIPTION_PATTERN = re.compile(r"^\s+description (?P<description>.+)$")

# A description that ends in an interface name, e.g. "..._Ethernet3/1".
# Whatever comes before the interface is then checked for a switch hostname.
DESCRIPTION_PEER_INTERFACE_PATTERN = re.compile(
    r"^(?P<text_before_interface>.*?)[\s_:\-]*"
    r"(?:Ethernet|Eth|Et)(?P<port_numbers>\d+(?:/\d+)*)$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Reading the AVD configs
# --------------------------------------------------------------------------

# Read one AVD .cfg file and return the switch hostname plus the description
# of every front-panel port: (hostname, {interface name: description}).
# If the file has no "hostname" line the file name is used instead.
def read_switch_config(config_path):
    hostname = config_path.stem
    description_by_interface = {}
    current_interface_name = None

    for config_line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        hostname_match = HOSTNAME_PATTERN.match(config_line)
        if hostname_match:
            hostname = hostname_match.group("hostname")

        # A line with no indentation starts a new top-level block, so we are
        # either entering an Ethernet interface stanza or leaving one.
        if not config_line.startswith((" ", "\t")):
            interface_match = ETHERNET_INTERFACE_PATTERN.match(config_line)
            current_interface_name = (
                interface_match.group("interface_name") if interface_match else None)
            continue

        # An indented line inside an Ethernet stanza: keep its description.
        description_match = DESCRIPTION_PATTERN.match(config_line)
        if current_interface_name and description_match:
            description_by_interface[current_interface_name] = (
                description_match.group("description").strip())

    return hostname, description_by_interface


# --------------------------------------------------------------------------
# Working out the cabling
# --------------------------------------------------------------------------

# Sort key that orders numbers by value instead of alphabetically, so
# Ethernet10 comes after Ethernet9. Keeps the topology file in a stable,
# readable order from one run to the next.
def natural_sort_key(text):
    return [int(piece) if piece.isdigit() else piece.lower()
            for piece in re.split(r"(\d+)", text)]


# Work out which switch and port an interface description points at.
# The description must end with "<known hostname><separator><Ethernet port>".
# Returns (peer hostname, peer interface), or None when the description does
# not name a switch in this fabric (a server port, for example).
def find_peer_in_description(description, known_hostnames):
    description_match = DESCRIPTION_PEER_INTERFACE_PATTERN.match(description)
    if not description_match:
        return None
    text_before_interface = description_match.group("text_before_interface").lower()

    # Try the longest hostnames first so "dc1-leaf1" wins over a shorter
    # "leaf1". Case is ignored because AVD 4.x upper-cases the peer name.
    for hostname in sorted(known_hostnames, key=len, reverse=True):
        if not text_before_interface.endswith(hostname.lower()):
            continue
        # The hostname must not be the tail of a longer word: "leaf1" must
        # not match the end of "l2leaf1".
        text_before_hostname = text_before_interface[:-len(hostname)]
        if text_before_hostname[-1:].isalnum():
            continue
        return hostname, "Ethernet" + description_match.group("port_numbers")
    return None


# Work out every switch-to-switch cable in the fabric. Each cable is a pair of
# ends, and each end is (hostname, interface name). Both switches describe the
# same cable from opposite sides, so the two ends are sorted before storing to
# make sure the cable is only recorded once. A port that appears in two
# different cables is reported in `warnings` and the second cable is skipped.
def discover_links(descriptions_by_hostname, warnings):
    links = set()
    cabled_ports = set()

    for hostname in sorted(descriptions_by_hostname, key=natural_sort_key):
        description_by_interface = descriptions_by_hostname[hostname]
        for interface_name in sorted(description_by_interface, key=natural_sort_key):
            local_port = (hostname, interface_name)
            peer_port = find_peer_in_description(
                description_by_interface[interface_name], descriptions_by_hostname)
            if peer_port is None or peer_port[0] == hostname:
                continue

            link = tuple(sorted(
                [local_port, peer_port],
                key=lambda port: natural_sort_key(port[0]) + natural_sort_key(port[1])))
            if link in links:
                continue  # already recorded from the other switch's side
            if local_port in cabled_ports or peer_port in cabled_ports:
                warnings.append(
                    f"{hostname} {interface_name} points at {peer_port[0]} {peer_port[1]}, "
                    f"but one of those ports is already cabled elsewhere; skipped")
                continue
            links.add(link)
            cabled_ports.update(link)

    return sorted(links, key=lambda link: [natural_sort_key(text) for port in link for text in port])


# --------------------------------------------------------------------------
# Writing the containerlab topology
# --------------------------------------------------------------------------

# Translate an EOS port name into the Linux interface name containerlab
# creates inside the cEOS container: Ethernet1 -> eth1, Ethernet3/1 -> eth3_1.
# cEOS turns the underscore back into a slash when it boots.
def eos_interface_to_container_interface(eos_interface_name):
    return eos_interface_name.replace("Ethernet", "eth").replace("/", "_")


# Build the text of the containerlab topology file: one cEOS node per switch
# and one link per cable. The YAML is written by hand so the script needs no
# third-party packages.
def render_topology_yaml(lab_name, ceos_image, hostnames, links):
    yaml_lines = [
        "# Generated by avd_to_containerlab.py - do not edit, regenerate instead.",
        f'name: "{lab_name}"',
        "",
        "topology:",
        "  kinds:",
        "    ceos:",
        f'      image: "{ceos_image}"',
        "",
        "  nodes:",
    ]
    for hostname in hostnames:
        yaml_lines += [f'    "{hostname}":', "      kind: ceos"]

    yaml_lines += ["", "  links:" if links else "  links: []"]
    for (first_hostname, first_interface), (second_hostname, second_interface) in links:
        yaml_lines.append(
            f'    - endpoints: ["{first_hostname}:'
            f'{eos_interface_to_container_interface(first_interface)}", '
            f'"{second_hostname}:{eos_interface_to_container_interface(second_interface)}"]')
    return "\n".join(yaml_lines) + "\n"


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

# Read the configs, work out the cabling, write the topology file and print a
# short summary. Returns the process exit code (0 = success).
def main():
    argument_parser = argparse.ArgumentParser(
        description="Generate a containerlab cEOS topology from Arista AVD switch configs.")
    argument_parser.add_argument(
        "--configs-directory", type=Path, required=True,
        help="directory of AVD generated .cfg files (usually intended/configs)")
    argument_parser.add_argument(
        "--lab-name", default="fabric-lab",
        help="containerlab lab name (default: fabric-lab)")
    argument_parser.add_argument(
        "--ceos-image", default="ceos:latest",
        help="cEOS container image as imported into docker/podman (default: ceos:latest)")
    argument_parser.add_argument(
        "--topology-file", type=Path,
        help="where to write the topology (default: ./<lab-name>.clab.yml)")
    arguments = argument_parser.parse_args()
    topology_path = arguments.topology_file or Path(f"{arguments.lab_name}.clab.yml")

    # Step 1: read every .cfg file in the configs directory.
    descriptions_by_hostname = {}
    for config_path in sorted(arguments.configs_directory.glob("*.cfg")):
        hostname, description_by_interface = read_switch_config(config_path)
        if hostname in descriptions_by_hostname:
            print(f"error: more than one config has hostname {hostname!r}", file=sys.stderr)
            return 1
        descriptions_by_hostname[hostname] = description_by_interface
    if not descriptions_by_hostname:
        print(f"error: no .cfg files found in {arguments.configs_directory}", file=sys.stderr)
        return 1

    # Step 2: work out the cabling between the switches.
    warnings = []
    links = discover_links(descriptions_by_hostname, warnings)

    # Step 3: write the containerlab topology file.
    hostnames = sorted(descriptions_by_hostname, key=natural_sort_key)
    topology_path.write_text(
        render_topology_yaml(arguments.lab_name, arguments.ceos_image, hostnames, links),
        encoding="utf-8")

    # Step 4: summarise. A switch with no links at all is worth a look, as it
    # usually means its descriptions were not in a recognised format.
    print(f"switches : {len(hostnames)}")
    print(f"links    : {len(links)}")
    print(f"topology : {topology_path}")
    linked_hostnames = {port[0] for link in links for port in link}
    for hostname in hostnames:
        if hostname not in linked_hostnames:
            warnings.append(f"{hostname} has no links to any other switch")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


# Only run when executed as a script, not when imported.
if __name__ == "__main__":
    sys.exit(main())
