"""ASC MHL v2.0 emitter.

References:
  https://github.com/ascmitc/mhl-specification (XSD + spec)
  https://github.com/ascmitc/mhl (Python reference implementation)

This module emits MHL files; it does not consume them. Verification of
existing chains is left to the official `ascmhl` tool — `pip install ascmhl
&& ascmhl verify <root>` reads what we write here.

Design notes:
  - Pure stdlib (xml.etree + hashlib + manual base58).
  - Each side (src/dst) has an independent `<root>/ascmhl/` folder with
    its own monotonically-increasing generation counter.
  - Manifest files are immutable; chain.xml is append-only.
  - `process` enum mapping:
      rmig op=hash, op=check  →  in-place
      rmig op=copy (dst side) →  transfer
      rmig op=copy (src side) →  in-place (post-copy verify)
"""
from __future__ import annotations

import hashlib
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree as ET

# `defusedxml` shields parse_chain from XML attacks (entity expansion,
# external entity resolution) when the chain file is written by an
# external tool. ET is still used for *emitting* — the writer side
# never touches user-controlled data through the parser.
from defusedxml.ElementTree import fromstring as _safe_fromstring

from . import __version__

NS_MANIFEST = "urn:ASC:MHL:v2.0"
NS_DIRECTORY = "urn:ASC:MHL:DIRECTORY:v2.0"

# Algorithms that MHL v2.0 schema enumerates. Names match rmig's
# lowercase rclone hash names.
MHL_ALGORITHMS = frozenset({"c4", "md5", "sha1", "xxh3", "xxh64", "xxh128"})

# Canonical XSD-required ordering of algorithm sub-elements within <hash>
# / <directoryhash>. Anything not in this list is omitted.
_ALGO_ORDER = ("c4", "md5", "sha1", "xxh128", "xxh3", "xxh64")

# Bitcoin-style base58 alphabet, used by C4 ID encoding.
_C4_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_C4_PAD = "1"
_C4_BODY_LEN = 88


# --- C4 ID --------------------------------------------------------------------

def compute_c4_id(data: bytes) -> str:
    """Compute the C4 ID per CINE spec: SHA-512 → base58 → '1'-pad to 88
    → prefix 'c4'. Total 90 chars. Same input always yields same output.
    """
    h = hashlib.sha512(data).digest()
    n = int.from_bytes(h, "big")
    if n == 0:
        body = ""
    else:
        out = []
        while n > 0:
            n, r = divmod(n, 58)
            out.append(_C4_B58_ALPHA[r])
        body = "".join(reversed(out))
    return "c4" + body.rjust(_C4_BODY_LEN, _C4_PAD)


# --- Time helpers -------------------------------------------------------------

def _now_iso_utc() -> str:
    """ISO-8601 datetime with explicit UTC offset (+00:00)."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _now_filename_ts() -> str:
    """Filename-safe UTC timestamp: YYYY-MM-DD_HHMMSSZ."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")


def _modtime_to_iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


# --- Data classes -------------------------------------------------------------

@dataclass
class HashEntry:
    """One file's record within a generation."""
    path: str
    size: int
    hashes: dict          # algo (lowercase) -> hex hash string
    actions: dict = field(default_factory=dict)  # algo -> "original"|"verified"|"failed"
    modtime: Optional[float] = None


@dataclass
class CreatorInfo:
    tool_name: str = "rclone-migrate"
    tool_version: str = __version__
    hostname: str = ""
    author_name: Optional[str] = None
    author_email: Optional[str] = None
    author_phone: Optional[str] = None
    author_role: Optional[str] = None
    location: Optional[str] = None
    comment: Optional[str] = None

    @classmethod
    def default(cls, **overrides) -> "CreatorInfo":
        d = {"hostname": socket.gethostname()}
        d.update(overrides)
        return cls(**d)


def parse_author(s: Optional[str]) -> tuple:
    """Split a git-style author string into (name, email).

    "Name <email@host.dom>" → ("Name", "email@host.dom")
    "Name Only"             → ("Name Only", None)
    "<email@host.dom>"      → ("",           "email@host.dom")
    "Me <me@local>"         → ("Me <me@local>", None)
        ↑ email lacks '.' in domain → fails MHL schema's pattern,
          so we keep the full string as the name and emit no email.
    None / ""               → (None, None)
    """
    if not s:
        return (None, None)
    s = s.strip()
    if not s:
        return (None, None)
    m = re.match(r"^(.*?)\s*<([^>]+)>\s*$", s)
    if not m:
        return (s, None)
    name = m.group(1).strip()
    email = m.group(2).strip()
    # Validate email loosely against the MHL XSD pattern: needs `@` and a
    # `.` in the domain part. Otherwise the attribute would fail xsd
    # validation downstream — better to keep the original string intact.
    if "@" in email:
        local, _, domain = email.partition("@")
        if local and "." in domain and not domain.startswith("."):
            return (name, email)
    return (s, None)


_DEFAULT_IGNORE = (".DS_Store", "ascmhl", "ascmhl/", ".rmig-cache.db")


@dataclass
class Generation:
    sequencenr: int
    process: str   # "in-place" | "transfer" | "flatten"
    creator: CreatorInfo
    entries: List[HashEntry]
    ignore: List[str] = field(default_factory=lambda: list(_DEFAULT_IGNORE))


@dataclass
class ChainEntry:
    sequencenr: int
    path: str
    c4: str


# --- Filesystem layout --------------------------------------------------------

def ascmhl_dir(root: Path) -> Path:
    """Path of the `ascmhl/` folder within the data root."""
    return Path(root) / "ascmhl"


def filename_for(seq: int, root_name: str, ts: Optional[str] = None) -> str:
    """`NNNN_<rootname>_YYYY-MM-DD_HHMMSSZ.mhl`."""
    ts = ts or _now_filename_ts()
    safe_root = re.sub(r"[^A-Za-z0-9._-]", "_", root_name) or "root"
    return f"{seq:04d}_{safe_root}_{ts}.mhl"


def next_sequencenr(root: Path) -> int:
    """Next free generation number for a side. Empty dir → 1."""
    d = ascmhl_dir(root)
    if not d.is_dir():
        return 1
    seen = []
    for p in d.glob("*.mhl"):
        m = re.match(r"^(\d+)_", p.name)
        if m:
            seen.append(int(m.group(1)))
    return (max(seen) + 1) if seen else 1


# --- XML rendering ------------------------------------------------------------

def render_manifest(gen: Generation) -> bytes:
    """Serialize a generation as ASC MHL v2.0 manifest XML (UTF-8 bytes)."""
    ET.register_namespace("", NS_MANIFEST)
    root = ET.Element(f"{{{NS_MANIFEST}}}hashlist", attrib={"version": "2.0"})

    ci = ET.SubElement(root, f"{{{NS_MANIFEST}}}creatorinfo")
    ET.SubElement(ci, f"{{{NS_MANIFEST}}}creationdate").text = _now_iso_utc()
    ET.SubElement(ci, f"{{{NS_MANIFEST}}}hostname").text = gen.creator.hostname
    tool = ET.SubElement(
        ci, f"{{{NS_MANIFEST}}}tool",
        attrib={"version": gen.creator.tool_version},
    )
    tool.text = gen.creator.tool_name
    # `author` is optional; emit only when there's at least a name to
    # carry. Multiple authors are allowed by the XSD but the config
    # surface here only supports one.
    if gen.creator.author_name:
        attrs = {}
        if gen.creator.author_email:
            attrs["email"] = gen.creator.author_email
        if gen.creator.author_phone:
            attrs["phone"] = gen.creator.author_phone
        if gen.creator.author_role:
            attrs["role"] = gen.creator.author_role
        author = ET.SubElement(
            ci, f"{{{NS_MANIFEST}}}author", attrib=attrs,
        )
        author.text = gen.creator.author_name
    if gen.creator.location:
        ET.SubElement(ci, f"{{{NS_MANIFEST}}}location").text = gen.creator.location
    if gen.creator.comment:
        ET.SubElement(ci, f"{{{NS_MANIFEST}}}comment").text = gen.creator.comment

    pi = ET.SubElement(root, f"{{{NS_MANIFEST}}}processinfo")
    ET.SubElement(pi, f"{{{NS_MANIFEST}}}process").text = gen.process
    if gen.ignore:
        ig = ET.SubElement(pi, f"{{{NS_MANIFEST}}}ignore")
        for pat in gen.ignore:
            ET.SubElement(ig, f"{{{NS_MANIFEST}}}pattern").text = pat

    if gen.entries:
        hashes_el = ET.SubElement(root, f"{{{NS_MANIFEST}}}hashes")
        hashdate = _now_iso_utc()
        for entry in gen.entries:
            mhl_algos = [a for a in entry.hashes if a in MHL_ALGORITHMS]
            if not mhl_algos:
                # Defensive: caller should have filtered already. Skip silently
                # here rather than emit a <hash> with no algorithm child (would
                # fail xsd validation).
                continue
            h_el = ET.SubElement(hashes_el, f"{{{NS_MANIFEST}}}hash")
            path_attrs = {"size": str(entry.size)}
            mod_iso = _modtime_to_iso(entry.modtime)
            if mod_iso is not None:
                path_attrs["lastmodificationdate"] = mod_iso
            path_el = ET.SubElement(
                h_el, f"{{{NS_MANIFEST}}}path", attrib=path_attrs,
            )
            path_el.text = entry.path
            for algo in _ALGO_ORDER:
                if algo not in entry.hashes:
                    continue
                attrs = {
                    "action": entry.actions.get(algo, "original"),
                    "hashdate": hashdate,
                }
                a_el = ET.SubElement(
                    h_el, f"{{{NS_MANIFEST}}}{algo}", attrib=attrs,
                )
                a_el.text = entry.hashes[algo]

    return _serialize(root)


def render_chain(entries: List[ChainEntry]) -> bytes:
    """Serialize a list of chain entries as `ascmhl_chain.xml`."""
    ET.register_namespace("", NS_DIRECTORY)
    root = ET.Element(f"{{{NS_DIRECTORY}}}ascmhldirectory")
    for ent in sorted(entries, key=lambda e: e.sequencenr):
        hl = ET.SubElement(
            root, f"{{{NS_DIRECTORY}}}hashlist",
            attrib={"sequencenr": str(ent.sequencenr)},
        )
        ET.SubElement(hl, f"{{{NS_DIRECTORY}}}path").text = ent.path
        ET.SubElement(hl, f"{{{NS_DIRECTORY}}}c4").text = ent.c4
    return _serialize(root)


def _serialize(root: ET.Element) -> bytes:
    """Indent + serialize as UTF-8 bytes with XML declaration."""
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


# --- Parsing (chain only — manifest parsing is delegated to ascmhl) ----------

def parse_chain(xml_bytes: bytes) -> List[ChainEntry]:
    """Parse an existing ascmhl_chain.xml into a sorted ChainEntry list.

    Uses defusedxml because the chain file may have been written by a
    third-party tool (Silverstack / Hedge / `ascmhl`) sharing the same
    `ascmhl/` directory.
    """
    root = _safe_fromstring(xml_bytes)
    out = []
    for hl in root.findall(f"{{{NS_DIRECTORY}}}hashlist"):
        try:
            seq = int(hl.attrib["sequencenr"])
        except (KeyError, ValueError):
            continue
        path = hl.findtext(f"{{{NS_DIRECTORY}}}path") or ""
        c4 = hl.findtext(f"{{{NS_DIRECTORY}}}c4") or ""
        out.append(ChainEntry(sequencenr=seq, path=path, c4=c4))
    return sorted(out, key=lambda e: e.sequencenr)


# --- High-level write ---------------------------------------------------------

def write_generation(root: Path, gen: Generation) -> Path:
    """Write a manifest file and append to `ascmhl_chain.xml`. Returns the
    new manifest's path. Raises `FileExistsError` if the manifest filename
    collides — sequencenr should always be fresh.
    """
    d = ascmhl_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    root_name = Path(root).name or "root"
    fn = filename_for(gen.sequencenr, root_name)
    mhl_path = d / fn
    if mhl_path.exists():
        raise FileExistsError(f"manifest already exists: {mhl_path}")

    body = render_manifest(gen)
    mhl_path.write_bytes(body)

    chain_path = d / "ascmhl_chain.xml"
    if chain_path.exists():
        existing = parse_chain(chain_path.read_bytes())
    else:
        existing = []
    existing.append(ChainEntry(
        sequencenr=gen.sequencenr,
        path=fn,
        c4=compute_c4_id(body),
    ))
    chain_path.write_bytes(render_chain(existing))
    return mhl_path
