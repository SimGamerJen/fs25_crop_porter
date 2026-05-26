#!/usr/bin/env python3
"""
FS25_CropPorter v0.1-alpha

A conservative crop migration assistant for Farming Simulator 25 maps.

What this version does:
- Opens FS25 map folders or ZIP files.
- Scans source and target XMLs for fruitTypes, fillTypes, densityMapHeightTypes,
  growth/season entries, and obvious crop asset references.
- Builds a preflight report for selected crops.
- Creates a patched copy of the target map folder, injecting selected XML nodes where safe.
- Copies referenced local assets into an isolated CropPorter folder when they can be resolved.
- Generates Markdown and JSON reports.

What this version deliberately does NOT do:
- It does not edit densityMap_fruits.gdm.
- It does not edit map.i3d density channel capacity.
- It does not patch vehicle/tool compatibility.
- It does not patch sell points/contracts/economy intelligently.
- It does not modify a live savegame.

Recommended workflow:
1. Run scan-source.
2. Run preflight.
3. Run apply into a new output folder.
4. Test the patched map in a disposable savegame.

Example:
    python cropporter.py scan-source "D:/FS25_Mods/FS25_NewGloriaBrazil.zip"
    python cropporter.py preflight --source "D:/FS25_Mods/FS25_NewGloriaBrazil.zip" --target "D:/FS25_Mods/FS25_TargetMap.zip" --crops coffee blackbean
    python cropporter.py apply --source "D:/FS25_Mods/FS25_NewGloriaBrazil.zip" --target "D:/FS25_Mods/FS25_TargetMap.zip" --crops coffee blackbean --output "D:/FS25_Work/FS25_TargetMap_CropPorted"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET


VERSION = "0.1-alpha"
CROPPORTER_FOLDER = "maps/cropporter/imported"

# File-name patterns vary between maps, so this is intentionally permissive.
XML_HINTS = {
    "fruit_types": ["fruittypes", "maps_fruittypes"],
    "fill_types": ["filltypes", "maps_filltypes"],
    "height_types": ["densitymapheighttypes", "maps_densitymapheighttypes"],
    "growth": ["growth", "seasonal", "seasons"],
    "bales": ["bales", "maps_bales"],
    "weed": ["weed", "maps_weed"],
}

BASEGAME_FRUITS = {
    "wheat", "barley", "oat", "canola", "sunflower", "soybean", "maize", "potato", "sugarbeet",
    "cotton", "sugarcane", "grape", "olive", "sorghum", "grass", "drygrass", "straw", "poplar",
    "oilseedradish", "meadow", "carrot", "parsnip", "redbeet", "beetroot", "spinach", "peas", "pea",
    "greenbean", "rice", "longgrainrice", "ricelonggrain", "springonion", "onion",
}

# Fruit types commonly loaded by the FS25 engine/basegame/DLC into the fruit density layer,
# even when the target map's maps_fruitTypes.xml only lists a small map-specific subset.
# This is deliberately an estimate for preflight safety, not a replacement for the game log.
KNOWN_ENGINE_FRUITS = {
    "wheat", "barley", "oat", "canola", "sunflower", "soybean", "maize", "potato", "sugarbeet",
    "cotton", "sugarcane", "grape", "olive", "sorghum", "grass", "oilseedradish", "meadow", "poplar",
    "beetroot", "carrot", "parsnip", "greenbean", "pea", "spinach", "rice", "onion",
}

PATH_ATTR_NAMES = {
    "filename", "file", "xmlFilename", "xmlfilename", "imageFilename", "imagefilename",
    "hudOverlayFilename", "hudoverlayfilename", "diffuseFilename", "diffusefilename",
    "normalFilename", "normalfilename", "specularFilename", "specularfilename",
    "distanceFilename", "distancefilename", "heightFilename", "heightfilename",
    # FS fillTypes often use short attribute names, e.g. <image hud="..." />
    # and <textures diffuse="..." normal="..." />.
    "hud", "diffuse", "normal", "specular", "distanceMap", "distancemap", "alpha", "fmask",
}

NAME_ATTRS = ("name", "fruitType", "fruitTypeName", "fillType", "fillTypeName", "input", "output")


@dataclass
class XmlNodeRef:
    file_role: str
    relative_file: str
    tag: str
    attrs: dict[str, str]
    xml_text: str


@dataclass
class CropDefinition:
    fruit_name: str
    fruit_nodes: list[XmlNodeRef] = field(default_factory=list)
    fill_type_names: set[str] = field(default_factory=set)
    fill_type_nodes: list[XmlNodeRef] = field(default_factory=list)
    height_type_nodes: list[XmlNodeRef] = field(default_factory=list)
    growth_nodes: list[XmlNodeRef] = field(default_factory=list)
    other_nodes: list[XmlNodeRef] = field(default_factory=list)
    asset_paths: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        data = asdict(self)
        data["fill_type_names"] = sorted(self.fill_type_names)
        data["asset_paths"] = sorted(self.asset_paths)
        return data


@dataclass
class MapProfile:
    source_path: str
    work_dir: str
    is_temp: bool
    root: str
    xml_files: dict[str, list[str]] = field(default_factory=dict)
    fruit_names: set[str] = field(default_factory=set)
    fill_type_names: set[str] = field(default_factory=set)
    height_type_names: set[str] = field(default_factory=set)
    crop_defs: dict[str, CropDefinition] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        if self.is_temp:
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def to_jsonable(self) -> dict:
        return {
            "source_path": self.source_path,
            "root": self.root,
            "xml_files": self.xml_files,
            "fruit_names": sorted(self.fruit_names),
            "fill_type_names": sorted(self.fill_type_names),
            "height_type_names": sorted(self.height_type_names),
            "crop_defs": {k: v.to_jsonable() for k, v in sorted(self.crop_defs.items())},
            "warnings": self.warnings,
        }


@dataclass
class DensityLayerInfo:
    relative_file: str
    tag: str
    attrs: dict[str, str]
    density_map: str
    num_channels: Optional[int]
    num_type_index_channels: Optional[int]
    compression_channels: Optional[int]

    @property
    def estimated_capacity(self) -> Optional[int]:
        if self.num_type_index_channels is None:
            return None
        return 2 ** self.num_type_index_channels


class CropPorterError(RuntimeError):
    pass


def normalise_name(value: Optional[str]) -> str:
    return (value or "").strip()


def lower_name(value: Optional[str]) -> str:
    return normalise_name(value).lower()


def local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def element_to_string(elem: ET.Element) -> str:
    return ET.tostring(elem, encoding="unicode", short_empty_elements=True)


def parse_xml_safely(path: Path) -> Optional[ET.ElementTree]:
    try:
        return ET.parse(path)
    except ET.ParseError:
        return None
    except OSError:
        return None


def classify_xml_file(path: Path) -> set[str]:
    """Classify XML by filename hints first, then by actual XML content.

    The first alpha only used filename hints, which is too brittle for real FS25 maps.
    Some maps keep crop definitions in files with names such as cropData.xml,
    map_fruitTypes.xml, fruitTypes_custom.xml, or nested map-specific XML files.
    """
    name = path.name.lower().replace("_", "")
    roles: set[str] = set()

    for role, hints in XML_HINTS.items():
        for hint in hints:
            if hint.replace("_", "") in name:
                roles.add(role)

    content_roles = classify_xml_file_by_content(path)
    roles.update(content_roles)
    return roles


def classify_xml_file_by_content(path: Path) -> set[str]:
    """Inspect parsed XML to detect FS25 crop-related file roles.

    This catches map-specific XMLs whose filenames do not contain fruitTypes,
    fillTypes, densityMapHeightTypes, or growth.
    """
    tree = parse_xml_safely(path)
    if not tree:
        return set()

    roles: set[str] = set()
    root = tree.getroot()
    root_tag = local_name(root.tag).lower()

    fruit_hits = 0
    fill_hits = 0
    height_hits = 0
    growth_hits = 0
    bale_hits = 0
    weed_hits = 0

    for elem in root.iter():
        tag = local_name(elem.tag).lower()
        attrs_l = {k.lower(): v for k, v in elem.attrib.items()}

        if tag in {"fruittype", "fruit"} and any(k in attrs_l for k in ("name", "fruittype", "fruittypename")):
            fruit_hits += 1

        if tag in {"filltype", "fill"} and any(k in attrs_l for k in ("name", "filltype", "filltypename")):
            fill_hits += 1

        if "heighttype" in tag or root_tag == "densitymapheighttypes":
            if any(k in attrs_l for k in ("name", "filltype", "filltypename")):
                height_hits += 1

        # Growth XML structures vary, so detect by common crop-calendar attributes.
        if tag in {"fruit", "fruittype", "period", "growth", "season"}:
            if any(k in attrs_l for k in ("fruittype", "fruittypename", "name", "planting", "harvest")):
                if "growth" in root_tag or "season" in root_tag or "calendar" in root_tag:
                    growth_hits += 1

        if "bale" in tag or "bales" in root_tag:
            bale_hits += 1

        if "weed" in tag or "weed" in root_tag:
            weed_hits += 1

    if fruit_hits:
        roles.add("fruit_types")
    if fill_hits:
        roles.add("fill_types")
    if height_hits:
        roles.add("height_types")
    if growth_hits:
        roles.add("growth")
    if bale_hits:
        roles.add("bales")
    if weed_hits:
        roles.add("weed")

    return roles


def prepare_map_input(input_path: Path) -> MapProfile:
    if not input_path.exists():
        raise CropPorterError(f"Input path does not exist: {input_path}")

    if input_path.is_dir():
        root = input_path.resolve()
        return MapProfile(str(input_path), str(root), False, str(root))

    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        temp_dir = Path(tempfile.mkdtemp(prefix="cropporter_"))
        try:
            with zipfile.ZipFile(input_path, "r") as zf:
                zf.extractall(temp_dir)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise CropPorterError(f"Not a valid ZIP file: {input_path}") from exc

        root = find_map_root(temp_dir)
        return MapProfile(str(input_path), str(temp_dir), True, str(root))

    raise CropPorterError(f"Unsupported input. Use a folder or ZIP: {input_path}")


def find_map_root(extracted_dir: Path) -> Path:
    # Many FS ZIPs extract directly; some include one top-level folder.
    moddesc_candidates = list(extracted_dir.rglob("modDesc.xml"))
    if moddesc_candidates:
        return moddesc_candidates[0].parent
    return extracted_dir


def rel_to_root(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def discover_xml_files(profile: MapProfile) -> None:
    root = Path(profile.root)
    buckets: dict[str, list[str]] = {role: [] for role in XML_HINTS}
    buckets["other"] = []

    xml_count = 0
    parsed_count = 0

    for xml_path in root.rglob("*.xml"):
        xml_count += 1
        rel = rel_to_root(xml_path, root)
        roles = classify_xml_file(xml_path)
        if parse_xml_safely(xml_path):
            parsed_count += 1
        if not roles:
            buckets["other"].append(rel)
        else:
            for role in roles:
                buckets[role].append(rel)

    profile.xml_files = {k: sorted(set(v)) for k, v in buckets.items() if v}

    if xml_count == 0:
        profile.warnings.append("No XML files were found under the detected map root. Check whether the ZIP has an unusual nested structure.")
    elif parsed_count == 0:
        profile.warnings.append("XML files were found, but none could be parsed. Check whether the ZIP extraction root is correct.")


def node_ref(role: str, rel_file: str, elem: ET.Element) -> XmlNodeRef:
    return XmlNodeRef(
        file_role=role,
        relative_file=rel_file,
        tag=local_name(elem.tag),
        attrs={k: v for k, v in elem.attrib.items()},
        xml_text=element_to_string(elem),
    )


def find_first_attr(elem: ET.Element, candidates: Iterable[str]) -> Optional[str]:
    lower_map = {k.lower(): v for k, v in elem.attrib.items()}
    for candidate in candidates:
        if candidate in elem.attrib:
            return elem.attrib[candidate]
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None


def collect_path_attrs(elem: ET.Element) -> set[str]:
    paths: set[str] = set()
    path_exts = (".xml", ".i3d", ".shapes", ".dds", ".png", ".jpg", ".jpeg")
    for key, value in elem.attrib.items():
        key_l = key.lower()
        value_s = normalise_name(value)
        if not value_s or value_s.startswith("$"):
            continue
        value_norm = value_s.replace(chr(92), "/")
        key_is_path = key in PATH_ATTR_NAMES or key_l in {x.lower() for x in PATH_ATTR_NAMES} or key_l.endswith("filename") or key_l.endswith("file")
        value_looks_path = "/" in value_norm and value_norm.lower().endswith(path_exts)
        if key_is_path or value_looks_path:
            paths.add(value_norm)
    return paths


def collect_path_attrs_deep(elem: ET.Element) -> set[str]:
    paths: set[str] = set()
    for node in elem.iter():
        paths.update(collect_path_attrs(node))
    return paths


def value_references_name(value: str, name: str) -> bool:
    """Return True when an XML value references a name as a distinct token.

    Avoids false positives such as crop 'rye' matching 'greenrye' or 'vetchrye'.
    It still matches paths like foliage/rye/rye.xml and values like RYE_CUT.
    """
    value_l = (value or "").lower()
    name_l = (name or "").lower()
    if not value_l or not name_l:
        return False
    if value_l == name_l:
        return True

    tokens = [t for t in re.split(r"[^a-z0-9]+", value_l) if t]
    if name_l in tokens:
        return True

    # Preserve common FS fillType naming conventions like RYE_CUT, DRYALFALFA_WINDROW.
    if value_l.startswith(name_l + "_") or value_l.endswith("_" + name_l):
        return True

    # Path segment match: maps/foliage/rye/rye.xml should match rye.
    path_tokens = [t for t in value_l.replace("\\", "/").split("/") if t]
    if name_l in path_tokens:
        return True

    return False


def element_mentions_crop(elem: ET.Element, crop: str) -> bool:
    crop_l = crop.lower()
    if value_references_name(local_name(elem.tag), crop_l):
        return True
    for value in elem.attrib.values():
        if value_references_name(value, crop_l):
            return True
    return False


def extract_fruit_names_from_fruit_xml(tree: ET.ElementTree) -> set[str]:
    names: set[str] = set()
    root = tree.getroot()
    root_tag = local_name(root.tag).lower()

    for elem in root.iter():
        tag = local_name(elem.tag).lower()
        name = lower_name(find_first_attr(elem, ["name", "fruitType", "fruitTypeName"]))

        # Standard structure: <fruitType name="...">
        if tag == "fruittype" and name:
            names.add(name)
            continue

        # Some growth/calendar files use <fruit name="..."> or <fruit fruitType="...">.
        # Only treat <fruit> as a fruit definition when the surrounding file/root looks crop-related.
        if tag == "fruit" and name and ("fruit" in root_tag or "growth" in root_tag or "season" in root_tag or "calendar" in root_tag):
            names.add(name)
            continue

    return names


def extract_fill_names_from_fill_xml(tree: ET.ElementTree) -> set[str]:
    names: set[str] = set()
    root = tree.getroot()
    for elem in root.iter():
        tag = local_name(elem.tag).lower()
        if tag in {"filltype", "fill"}:
            name = normalise_name(find_first_attr(elem, ["name", "fillType", "fillTypeName"]))
            if name:
                names.add(name.upper())
    return names


def extract_height_names_from_height_xml(tree: ET.ElementTree) -> set[str]:
    names: set[str] = set()
    root = tree.getroot()
    for elem in root.iter():
        tag = local_name(elem.tag).lower()
        if "heighttype" in tag:
            name = normalise_name(find_first_attr(elem, ["name", "fillType", "fillTypeName"]))
            if name:
                names.add(name.upper())
    return names


def scan_profile(profile: MapProfile) -> MapProfile:
    discover_xml_files(profile)
    root = Path(profile.root)

    for rel in profile.xml_files.get("fruit_types", []):
        tree = parse_xml_safely(root / rel)
        if tree:
            profile.fruit_names.update(extract_fruit_names_from_fruit_xml(tree))

    for rel in profile.xml_files.get("fill_types", []):
        tree = parse_xml_safely(root / rel)
        if tree:
            profile.fill_type_names.update(extract_fill_names_from_fill_xml(tree))

    for rel in profile.xml_files.get("height_types", []):
        tree = parse_xml_safely(root / rel)
        if tree:
            profile.height_type_names.update(extract_height_names_from_height_xml(tree))

    for fruit in sorted(profile.fruit_names):
        profile.crop_defs[fruit] = build_crop_definition(profile, fruit)

    if not profile.xml_files.get("fruit_types"):
        profile.warnings.append("No fruitTypes XML file was detected by filename. Scanner may need a map-specific override.")
    if not profile.xml_files.get("fill_types"):
        profile.warnings.append("No fillTypes XML file was detected by filename. Scanner may need a map-specific override.")

    return profile


def build_crop_definition(profile: MapProfile, fruit: str) -> CropDefinition:
    root = Path(profile.root)
    crop = CropDefinition(fruit_name=fruit)

    # Fruit nodes: exact fruitType name match.
    for rel in profile.xml_files.get("fruit_types", []):
        tree = parse_xml_safely(root / rel)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            tag = local_name(elem.tag).lower()
            if tag in {"fruittype", "fruit"}:
                name = lower_name(find_first_attr(elem, ["name", "fruitType", "fruitTypeName"]))
                if name == fruit:
                    crop.fruit_nodes.append(node_ref("fruit_types", rel, elem))
                    crop.asset_paths.update(collect_path_attrs_deep(elem))
                    infer_fill_types_from_node(elem, crop)

    # If the map has a crop-specific foliage XML path, capture it even when the XML is not
    # directly referenced by maps_fruitTypes.xml. This is common in map folders such as
    # maps/foliage/blackbean/blackbean.xml.
    for candidate in find_crop_named_asset_files(root, fruit):
        try:
            crop.asset_paths.add(rel_to_root(candidate, root))
        except ValueError:
            pass


    # FillType nodes: match inferred fillTypes, or obvious crop name mentions.
    for rel in profile.xml_files.get("fill_types", []):
        tree = parse_xml_safely(root / rel)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            tag = local_name(elem.tag).lower()
            if tag not in {"filltype", "fill"}:
                continue
            fill_name = normalise_name(find_first_attr(elem, ["name", "fillType", "fillTypeName"])).upper()
            if not fill_name:
                continue
            if fill_name.lower() == fruit or fill_name in crop.fill_type_names or element_mentions_crop(elem, fruit):
                crop.fill_type_names.add(fill_name)
                crop.fill_type_nodes.append(node_ref("fill_types", rel, elem))
                crop.asset_paths.update(collect_path_attrs_deep(elem))

    # Density map height types: match crop or inferred fillTypes.
    for rel in profile.xml_files.get("height_types", []):
        tree = parse_xml_safely(root / rel)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            tag = local_name(elem.tag).lower()
            if "heighttype" not in tag:
                continue
            name = normalise_name(find_first_attr(elem, ["name", "fillType", "fillTypeName"])).upper()
            if name.lower() == fruit or name in crop.fill_type_names or element_mentions_crop(elem, fruit):
                crop.height_type_nodes.append(node_ref("height_types", rel, elem))
                crop.asset_paths.update(collect_path_attrs_deep(elem))

    # Growth/calendar entries: any element mentioning the crop.
    for rel in profile.xml_files.get("growth", []):
        tree = parse_xml_safely(root / rel)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            if element_mentions_crop(elem, fruit):
                crop.growth_nodes.append(node_ref("growth", rel, elem))
                crop.asset_paths.update(collect_path_attrs_deep(elem))

    # Related XML nodes from bales/weed/other crop systems.
    for role in ("bales", "weed"):
        for rel in profile.xml_files.get(role, []):
            tree = parse_xml_safely(root / rel)
            if not tree:
                continue
            for elem in tree.getroot().iter():
                if element_mentions_crop(elem, fruit):
                    crop.other_nodes.append(node_ref(role, rel, elem))
                    crop.asset_paths.update(collect_path_attrs_deep(elem))

    # Expand assets by parsing foliage XMLs and similar referenced XMLs.
    expanded = expand_asset_references(root, crop.asset_paths)
    crop.asset_paths.update(expanded)

    if not crop.fruit_nodes:
        crop.warnings.append("No exact fruitType XML node found for this crop.")
    if not crop.fill_type_nodes:
        crop.warnings.append("No fillType XML node was confidently matched. This may be normal for some crops, but verify manually.")
    if not crop.growth_nodes:
        crop.warnings.append("No growth/calendar entry was matched. Crop may not appear in the seasonal calendar unless added manually.")

    return crop


def infer_fill_types_from_node(elem: ET.Element, crop: CropDefinition) -> None:
    """Infer fillTypes from a fruitType node without grabbing unrelated child names.

    Earlier alpha builds treated child attributes named 'name' as possible fillTypes.
    That caused false positives, especially for short crop names such as rye matching
    greenrye/vetchrye references. This version only trusts explicit fillType-like
    attributes and converter input/output attributes.
    """
    trusted_attr_fragments = (
        "filltype",
        "windrow",
        "straw",
        "cut",
        "chaff",
        "literperqm",
    )
    trusted_exact_attrs = {"input", "output", "from", "to"}

    for key, value in elem.attrib.items():
        key_l = key.lower()
        val = normalise_name(value)
        if not val:
            continue
        if any(fragment in key_l for fragment in trusted_attr_fragments):
            if re.fullmatch(r"[A-Za-z0-9_]+", val):
                crop.fill_type_names.add(val.upper())

    for child in elem.iter():
        if child is elem:
            continue
        child_tag = local_name(child.tag).lower()
        converter_context = any(word in child_tag for word in ("converter", "windrow", "straw", "chaff", "cut", "filltype"))
        for key, value in child.attrib.items():
            key_l = key.lower()
            val = normalise_name(value)
            if not val or not re.fullmatch(r"[A-Za-z0-9_]+", val):
                continue
            if "filltype" in key_l:
                crop.fill_type_names.add(val.upper())
            elif converter_context and key_l in trusted_exact_attrs:
                crop.fill_type_names.add(val.upper())


def find_crop_named_asset_files(root: Path, fruit: str) -> list[Path]:
    fruit_l = fruit.lower()
    matches: list[Path] = []
    search_roots = [root / "maps" / "foliage", root / "foliage", root / "maps"]
    suffixes = {".xml", ".i3d", ".shapes", ".dds", ".png", ".jpg", ".jpeg"}
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for path in search_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            parts = [p.lower() for p in path.parts]
            stem = path.stem.lower()
            if (
                fruit_l == stem
                or fruit_l in parts
                or stem.startswith(fruit_l + "_")
                or stem.endswith("_" + fruit_l)
                or ("_" + fruit_l + "_") in stem
            ):
                matches.append(path)
    # Stable and unique.
    return sorted(set(matches), key=lambda p: str(p).lower())


def expand_asset_references(root: Path, initial_paths: set[str]) -> set[str]:
    discovered: set[str] = set()
    queue = list(initial_paths)
    seen: set[str] = set()

    while queue:
        raw = queue.pop(0)
        norm = raw.replace(chr(92), "/").lstrip("/")
        if norm in seen:
            continue
        seen.add(norm)

        candidate = resolve_asset_path(root, norm)
        if not candidate or not candidate.exists() or not candidate.is_file():
            continue

        rel = rel_to_root(candidate, root)
        discovered.add(rel)

        if candidate.suffix.lower() == ".xml":
            tree = parse_xml_safely(candidate)
            if tree:
                for elem in tree.getroot().iter():
                    for path_ref in collect_path_attrs(elem):
                        resolved = resolve_asset_path(candidate.parent, path_ref) or resolve_asset_path(root, path_ref)
                        if resolved and resolved.exists():
                            queue.append(rel_to_root(resolved, root if root in resolved.parents or resolved == root else candidate.parent))
                        else:
                            queue.append(path_ref)

    return discovered


def resolve_asset_path(base: Path, path_ref: str) -> Optional[Path]:
    clean = path_ref.replace(chr(92), "/")
    if clean.startswith("$"):
        return None
    p = Path(clean)
    if p.is_absolute():
        if p.exists():
            return p
        return resolve_nearby_asset(p)
    candidate = (base / clean).resolve()
    if candidate.exists():
        return candidate
    nearby = resolve_nearby_asset(candidate)
    if nearby:
        return nearby
    # If a path includes map root-ish prefixes, try under base root.
    parts = clean.split("/")
    for i in range(len(parts)):
        sub = Path(*parts[i:])
        candidate = (base / sub).resolve()
        if candidate.exists():
            return candidate
        nearby = resolve_nearby_asset(candidate)
        if nearby:
            return nearby
    return None


def resolve_nearby_asset(candidate: Path) -> Optional[Path]:
    """Resolve same-stem assets where XML references .png but the map ships .dds, etc."""
    parent = candidate.parent
    if not parent.exists():
        return None

    preferred_exts = [candidate.suffix.lower(), ".png", ".dds", ".jpg", ".jpeg", ".i3d", ".shapes", ".xml"]
    seen: set[str] = set()
    for ext in preferred_exts:
        if not ext or ext in seen:
            continue
        seen.add(ext)
        alt = parent / f"{candidate.stem}{ext}"
        if alt.exists():
            return alt

    target_stem = candidate.stem.lower()
    for child in parent.iterdir():
        if child.is_file() and child.stem.lower() == target_stem:
            return child
    return None


def preflight(source: MapProfile, target: MapProfile, crops: list[str]) -> dict:
    selected = [c.lower() for c in crops]
    report = {
        "version": VERSION,
        "source": source.source_path,
        "target": target.source_path,
        "selected_crops": selected,
        "crops": {},
        "summary": {"errors": 0, "warnings": 0},
    }

    for crop_name in selected:
        crop_report = {
            "status": "unknown",
            "errors": [],
            "warnings": [],
            "new_fill_types": [],
            "conflicting_fill_types": [],
            "asset_count": 0,
            "fruit_nodes": 0,
            "fill_type_nodes": 0,
            "growth_nodes": 0,
            "height_type_nodes": 0,
        }

        crop = source.crop_defs.get(crop_name)
        if not crop:
            crop_report["status"] = "error"
            crop_report["errors"].append(f"Crop '{crop_name}' was not found in the source map fruitTypes scan.")
        else:
            crop_report["status"] = "ready" if crop_name not in target.fruit_names else "target_already_has_crop"
            crop_report["warnings"].extend(crop.warnings)
            crop_report["fruit_nodes"] = len(crop.fruit_nodes)
            crop_report["fill_type_nodes"] = len(crop.fill_type_nodes)
            crop_report["growth_nodes"] = len(crop.growth_nodes)
            crop_report["height_type_nodes"] = len(crop.height_type_nodes)
            crop_report["asset_count"] = len(crop.asset_paths)

            if crop_name in target.fruit_names:
                crop_report["warnings"].append("Target already contains this fruitType name. Default apply mode will skip fruitType insertion.")

            for fill_name in sorted(crop.fill_type_names):
                if fill_name in target.fill_type_names:
                    crop_report["conflicting_fill_types"].append(fill_name)
                    crop_report["warnings"].append(f"FillType '{fill_name}' already exists in target. Default apply mode will skip duplicate fillType insertion.")
                else:
                    crop_report["new_fill_types"].append(fill_name)

            if not crop.fruit_nodes:
                crop_report["errors"].append("No fruitType node available to insert.")
            if not source.xml_files.get("height_types"):
                crop_report["warnings"].append("Source heightTypes XML not detected. Density height integration may be incomplete.")
            if not target.xml_files.get("height_types"):
                crop_report["warnings"].append("Target heightTypes XML not detected. Density height integration may require manual work.")

        report["summary"]["errors"] += len(crop_report["errors"])
        report["summary"]["warnings"] += len(crop_report["warnings"])
        report["crops"][crop_name] = crop_report

    return report


def print_scan_summary(profile: MapProfile, include_basegame: bool = False) -> None:
    print(f"FS25_CropPorter {VERSION}")
    print(f"Map: {profile.source_path}")
    print(f"Root: {profile.root}")
    print()
    print("Detected XML roles:")
    for role in sorted(profile.xml_files):
        if role == "other":
            continue
        primary = find_primary_xml_file(profile, role)
        primary_note = f"; primary: {primary}" if primary else ""
        print(f"- {role}: {len(profile.xml_files[role])} file(s){primary_note}")
    print()
    print(f"Detected fruitTypes: {len(profile.fruit_names)}")
    fruits = sorted(profile.fruit_names)
    if not include_basegame:
        fruits = [f for f in fruits if f not in BASEGAME_FRUITS]
    if not fruits:
        print("- No non-basegame/custom fruitTypes listed. Re-run with --include-basegame to see everything detected.")
    for name in fruits:
        crop = profile.crop_defs.get(name)
        marker = "custom?" if name not in BASEGAME_FRUITS else "basegame"
        fills = ", ".join(sorted(crop.fill_type_names)) if crop else ""
        print(f"- {name} [{marker}]" + (f" -> {fills}" if fills else ""))
    print()
    if profile.warnings:
        print("Warnings:")
        for warning in profile.warnings:
            print(f"- {warning}")


def write_reports(report: dict, output_dir: Path, prefix: str = "CropPorter_Preflight") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{prefix}.json"
    md_path = output_dir / f"{prefix}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {md_path}")


def render_markdown_report(report: dict) -> str:
    if "crops" in report:
        return render_preflight_markdown_report(report)
    if "actions" in report:
        return render_apply_markdown_report(report)
    return render_generic_markdown_report(report)


def render_preflight_markdown_report(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        f"# FS25 CropPorter Preflight Report",
        "",
        f"Version: `{report.get('version', VERSION)}`",
        "",
        f"Source: `{report.get('source')}`",
        f"Target: `{report.get('target')}`",
        "",
        "## Summary",
        "",
        f"- Errors: {summary.get('errors', 0)}",
        f"- Warnings: {summary.get('warnings', 0)}",
        "",
        "## Selected Crops",
        "",
    ]

    for crop_name, crop_report in report.get("crops", {}).items():
        lines.extend([
            f"### {crop_name}",
            "",
            f"- Status: `{crop_report.get('status', 'unknown')}`",
            f"- fruitType nodes: {crop_report.get('fruit_nodes', 0)}",
            f"- fillType nodes: {crop_report.get('fill_type_nodes', 0)}",
            f"- growth nodes: {crop_report.get('growth_nodes', 0)}",
            f"- heightType nodes: {crop_report.get('height_type_nodes', 0)}",
            f"- referenced assets: {crop_report.get('asset_count', 0)}",
            "",
        ])
        if crop_report.get("new_fill_types"):
            lines.append("New fillTypes:")
            for ft in crop_report["new_fill_types"]:
                lines.append(f"- `{ft}`")
            lines.append("")
        if crop_report.get("conflicting_fill_types"):
            lines.append("Conflicting fillTypes already present in target:")
            for ft in crop_report["conflicting_fill_types"]:
                lines.append(f"- `{ft}`")
            lines.append("")
        if crop_report.get("errors"):
            lines.append("Errors:")
            for item in crop_report["errors"]:
                lines.append(f"- {item}")
            lines.append("")
        if crop_report.get("warnings"):
            lines.append("Warnings:")
            for item in crop_report["warnings"]:
                lines.append(f"- {item}")
            lines.append("")

    lines.extend(common_report_notes())
    return chr(10).join(lines)


def render_apply_markdown_report(report: dict) -> str:
    summary = report.get("summary", {})
    errors = report.get("errors", [])
    warnings = report.get("warnings", [])
    lines = [
        f"# FS25 CropPorter Apply Report",
        "",
        f"Version: `{report.get('version', VERSION)}`",
        "",
        f"Source: `{report.get('source')}`",
        f"Target: `{report.get('target')}`",
        f"Output: `{report.get('output')}`",
        "",
        "## Summary",
        "",
        f"- Errors: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        f"- Inserted XML nodes: {summary.get('inserted_nodes', 0)}",
        f"- Copied assets: {summary.get('copied_assets', 0)}",
        "",
    ]

    if report.get("selected_crops"):
        lines.append("## Selected Crops")
        lines.append("")
        for crop in report["selected_crops"]:
            lines.append(f"- `{crop}`")
        lines.append("")

    if report.get("actions"):
        lines.append("## Actions")
        lines.append("")
        for action in report["actions"]:
            lines.append(f"- {action}")
        lines.append("")

    validation = report.get("validation")
    if validation:
        lines.append("## Validation")
        lines.append("")
        if validation.get("errors"):
            lines.append("Validation errors:")
            for item in validation["errors"]:
                lines.append(f"- {item}")
            lines.append("")
        if validation.get("warnings"):
            lines.append("Validation warnings:")
            for item in validation["warnings"]:
                lines.append(f"- {item}")
            lines.append("")

    if errors:
        lines.append("## Errors")
        lines.append("")
        for item in errors:
            lines.append(f"- {item}")
        lines.append("")

    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for item in warnings:
            lines.append(f"- {item}")
        lines.append("")

    lines.extend(common_report_notes())
    return chr(10).join(lines)


def render_generic_markdown_report(report: dict) -> str:
    return chr(10).join([
        "# FS25 CropPorter Report",
        "",
        "```json",
        json.dumps(report, indent=2),
        "```",
        "",
    ])


def common_report_notes() -> list[str]:
    return [
        "## Notes",
        "",
        "This version does not expand `densityMap_fruits.gdm` or edit map.i3d density channel capacity.",
        "If the target map does not already have sufficient fruit density capacity, XML insertion alone may not be enough.",
        "Always test the generated map in a disposable savegame first.",
        "",
    ]


def copy_target_to_output(target: MapProfile, output: Path) -> Path:
    output = output.resolve()
    if output.exists():
        raise CropPorterError(f"Output already exists. Choose a new folder or delete it first: {output}")
    shutil.copytree(target.root, output)
    return output


def find_primary_xml_file(profile: MapProfile, role: str) -> Optional[str]:
    files = profile.xml_files.get(role, [])
    if not files:
        return None

    role_name_hints = {
        "fruit_types": ("maps_fruittypes", "map_fruittypes", "fruittypes"),
        "fill_types": ("maps_filltypes", "map_filltypes", "filltypes"),
        "height_types": ("maps_densitymapheighttypes", "densitymapheighttypes", "heighttypes"),
        "growth": ("growth", "cropcalendar", "seasonal"),
        "bales": ("maps_bales", "bales"),
        "weed": ("maps_weed", "weed"),
    }

    def score(rel: str) -> tuple[int, int, int, int, int]:
        lower = rel.lower().replace(chr(92), "/")
        compact = lower.replace("_", "")
        filename = Path(lower).name.replace("_", "")
        hints = role_name_hints.get(role, ())

        # Strongly prefer explicit config files such as maps/config/maps_fruitTypes.xml.
        explicit_name = 0 if any(hint in compact or hint in filename for hint in hints) else 1
        under_config = 0 if "/config/" in lower or lower.startswith("config/") else 1

        # Foliage XMLs contain embedded <fruitType> definitions but are not the central fruitTypes registry.
        foliage_penalty = 1 if "/foliage/" in lower else 0

        # map.xml can reference many systems and should not be used as an insertion target unless no better file exists.
        map_xml_penalty = 1 if lower.endswith("/map.xml") or lower == "map.xml" else 0

        return (explicit_name, under_config, foliage_penalty, map_xml_penalty, len(rel))

    return sorted(files, key=score)[0]


def insert_nodes_into_xml(target_root: Path, rel_file: str, nodes: list[XmlNodeRef], existing_names: set[str], name_attr_candidates: list[str]) -> tuple[int, list[str]]:
    path = target_root / rel_file
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target XML: {rel_file}"]

    root = tree.getroot()
    container = get_insertion_container(root, rel_file, nodes)
    inserted = 0
    warnings: list[str] = []

    original_existing_keys = {x.lower() for x in existing_names}
    inserted_keys: set[str] = set()
    current_keys = set(original_existing_keys)

    for ref in nodes:
        try:
            elem = ET.fromstring(ref.xml_text)
        except ET.ParseError:
            warnings.append(f"Could not parse source node for insertion into {rel_file}: {ref.tag}")
            continue

        name = find_first_attr(elem, name_attr_candidates)
        name_key = lower_name(name) if name else ""
        if name_key and name_key in current_keys:
            if name_key in inserted_keys:
                warnings.append(f"Skipped repeated source node '{name}' in {rel_file}")
            else:
                warnings.append(f"Skipped target duplicate node '{name}' in {rel_file}")
            continue

        container.append(elem)
        if name_key:
            current_keys.add(name_key)
            inserted_keys.add(name_key)
            existing_names.add(name or name_key)
        inserted += 1

    if inserted:
        backup = path.with_suffix(path.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    return inserted, warnings


def get_insertion_container(root: ET.Element, rel_file: str, nodes: list[XmlNodeRef]) -> ET.Element:
    """Return the correct XML container for inserted nodes.

    Important: broad scans can see nodes anywhere in a file, but GIANTS loaders usually
    only consume nodes inside the correct parent container. For example, <fillType>
    must be inside <fillTypes>, not appended after </fillTypes>.
    """
    rel_l = rel_file.lower()
    node_tags = {local_name(ref.tag).lower() for ref in nodes}

    if "filltypes" in rel_l or "filltype" in node_tags:
        fill_types = find_child_container(root, "fillTypes")
        if fill_types is not None:
            return fill_types

    if "densitymapheighttypes" in rel_l or "densitymapheighttype" in node_tags:
        height_types = find_child_container(root, "densityMapHeightTypes")
        if height_types is not None:
            return height_types

    if "bales" in rel_l or "bale" in node_tags:
        bales = find_child_container(root, "bales")
        if bales is not None:
            return bales

    return root


def indent_xml(tree: ET.ElementTree) -> None:
    # ET.indent exists in Python 3.9+.
    try:
        ET.indent(tree, space="    ")
    except AttributeError:
        pass


def rewrite_asset_path(path_ref: str, source_map_name: str) -> str:
    clean = path_ref.replace(chr(92), "/").lstrip("/")
    return f"{CROPPORTER_FOLDER}/{source_map_name}/{clean}"


def patch_l10n_for_crop(target_root: Path, crop: CropDefinition, label: Optional[str] = None) -> list[str]:
    """Patch crop l10n entries into the mod's active localisation source.

    Preferred FS map pattern is a modDesc.xml l10n filenamePrefix such as
    <l10n filenamePrefix="language/l10n"/> with language/l10n_en.xml etc.
    We therefore patch/create files based on filenamePrefix when present, and ensure
    modDesc references the prefix when we create it.
    """
    warnings: list[str] = []
    fruit_l = crop.fruit_name.lower()
    display = label or make_display_label(crop.fruit_name)
    additions_by_lang = build_l10n_additions(fruit_l, display)

    moddesc = target_root / "modDesc.xml"
    prefix = None
    if moddesc.exists():
        prefix = get_or_create_moddesc_l10n_prefix(moddesc)

    if prefix:
        for lang, additions in additions_by_lang.items():
            l10n_file = target_root / f"{prefix}_{lang}.xml"
            patch_l10n_file(l10n_file, additions, lang)
        return warnings

    # Fallback: patch embedded <l10n> in modDesc if no filenamePrefix can be used.
    if moddesc.exists():
        ok, warning = patch_moddesc_l10n(moddesc, additions_by_lang["en"])
        if ok:
            return warnings
        warnings.append(warning)

    # Last fallback: create a conventional file. This may not be loaded unless referenced.
    l10n_file = find_l10n_file(target_root, "en") or create_l10n_file(target_root, "en")
    patch_l10n_file(l10n_file, additions_by_lang["en"], "en")
    return warnings


def build_l10n_additions(fruit_l: str, display_en: str) -> dict[str, dict[str, str]]:
    br = "Feijão Preto" if fruit_l == "blackbean" else display_en
    labels = {
        "en": display_en,
        "de": display_en,
        "fr": display_en,
        "br": br,
    }
    result: dict[str, dict[str, str]] = {}
    for lang, label in labels.items():
        result[lang] = {
            f"fillType_{fruit_l}": label,
            f"fillType_{fruit_l}_plural": label,
            f"fruitType_{fruit_l}": label,
        }
    return result


def get_or_create_moddesc_l10n_prefix(moddesc: Path) -> Optional[str]:
    tree = parse_xml_safely(moddesc)
    if not tree:
        return None
    root = tree.getroot()
    l10n = find_direct_child(root, "l10n")
    changed = False
    if l10n is None:
        l10n = ET.Element("l10n")
        l10n.set("filenamePrefix", "language/l10n")
        root.append(l10n)
        changed = True
    prefix = l10n.attrib.get("filenamePrefix")
    if not prefix:
        prefix = "language/l10n"
        l10n.set("filenamePrefix", prefix)
        changed = True
    if changed:
        backup = moddesc.with_suffix(moddesc.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(moddesc, backup)
        indent_xml(tree)
        tree.write(moddesc, encoding="utf-8", xml_declaration=True)
    return prefix.replace(chr(92), "/")


def patch_l10n_file(path: Path, additions: dict[str, str], lang: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        template = "".join([
            '<?xml version="1.0" encoding="utf-8"?>', chr(10),
            '<l10n>', chr(10),
            '    <texts>', chr(10),
            '    </texts>', chr(10),
            '</l10n>', chr(10),
        ])
        path.write_text(template, encoding="utf-8")

    tree = parse_xml_safely(path)
    if not tree:
        return

    root = tree.getroot()
    container = find_l10n_text_container(root)
    existing = collect_l10n_names(root)

    inserted = 0
    for key, value in additions.items():
        if key in existing:
            continue
        text = ET.Element("text")
        text.set("name", key)
        text.set("text", value)
        container.append(text)
        inserted += 1

    if inserted:
        backup = path.with_suffix(path.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)


def patch_moddesc_l10n(moddesc: Path, additions: dict[str, str]) -> tuple[bool, str]:
    tree = parse_xml_safely(moddesc)
    if not tree:
        return False, f"Could not parse modDesc.xml: {moddesc}"

    root = tree.getroot()
    l10n = find_direct_child(root, "l10n")
    if l10n is None:
        l10n = ET.Element("l10n")
        root.append(l10n)

    existing = collect_l10n_names(l10n)
    inserted = 0
    for key, value in additions.items():
        if key in existing:
            continue
        text = ET.Element("text")
        text.set("name", key)
        text.set("text", value)
        l10n.append(text)
        inserted += 1

    if inserted:
        backup = moddesc.with_suffix(moddesc.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(moddesc, backup)
        indent_xml(tree)
        tree.write(moddesc, encoding="utf-8", xml_declaration=True)
    return True, ""


def create_l10n_file(root: Path, lang: str) -> Path:
    l10n_dir = root / "l10n"
    l10n_dir.mkdir(parents=True, exist_ok=True)
    path = l10n_dir / f"l10n_{lang}.xml"
    if not path.exists():
        template = "".join([
            '<?xml version="1.0" encoding="utf-8"?>', chr(10),
            '<l10n>', chr(10),
            '</l10n>', chr(10),
        ])
        path.write_text(template, encoding="utf-8")
    return path


def find_l10n_file(root: Path, lang: str) -> Optional[Path]:
    candidates = [path for path in root.rglob(f"l10n_{lang}.xml") if path.is_file()]
    if not candidates:
        return None

    def score(path: Path) -> tuple[int, int]:
        rel = rel_to_root(path, root).lower().replace(chr(92), "/")
        preferred = 0 if rel.startswith("l10n/") or "/l10n/" in rel else 1
        return (preferred, len(rel))

    return sorted(candidates, key=score)[0]


def find_direct_child(root: ET.Element, tag_name: str) -> Optional[ET.Element]:
    tag_l = tag_name.lower()
    for child in list(root):
        if local_name(child.tag).lower() == tag_l:
            return child
    return None


def find_l10n_text_container(root: ET.Element) -> ET.Element:
    texts = find_child_container(root, "texts")
    if texts is not None:
        return texts
    l10n = find_child_container(root, "l10n")
    if l10n is not None:
        texts = ET.Element("texts")
        l10n.append(texts)
        return texts
    return root


def collect_l10n_names(root: ET.Element) -> set[str]:
    names: set[str] = set()
    for elem in root.iter():
        if local_name(elem.tag).lower() == "text" and elem.attrib.get("name"):
            names.add(elem.attrib["name"])
    return names


def make_display_label(name: str) -> str:
    clean = name.replace("_", " ").replace("-", " ").strip()
    if clean.lower() == "blackbean":
        return "Black Beans"
    return " ".join(part.capitalize() for part in clean.split())


def copy_assets_for_crop(source: MapProfile, output_root: Path, crop: CropDefinition) -> tuple[int, list[str]]:
    """Copy crop assets into the target map using their original relative paths.

    Preserving the original relative layout avoids needing to rewrite every internal
    XML/i3d/texture reference. Example: maps/foliage/blackbean/blackbean.xml stays
    at maps/foliage/blackbean/blackbean.xml in the patched target map.
    """
    source_root = Path(source.root)
    copied = 0
    warnings: list[str] = []

    for asset in sorted(crop.asset_paths):
        if asset.startswith("$"):
            continue
        src = resolve_asset_path(source_root, asset)
        if not src or not src.exists() or not src.is_file():
            warnings.append(f"Asset not found and was not copied: {asset}")
            continue
        try:
            rel = rel_to_root(src, source_root)
        except ValueError:
            rel = asset.replace(chr(92), "/").lstrip("/")
        dst = output_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and files_are_same(src, dst):
            continue
        shutil.copy2(src, dst)
        copied += 1

    return copied, warnings


def files_are_same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "source"


def apply_patch(source: MapProfile, target: MapProfile, crops: list[str], output: Path) -> dict:
    output_root = copy_target_to_output(target, output)
    patched_target = MapProfile(target.source_path, str(output_root), False, str(output_root))
    scan_profile(patched_target)

    apply_report = {
        "version": VERSION,
        "source": source.source_path,
        "target": target.source_path,
        "output": str(output_root),
        "selected_crops": [c.lower() for c in crops],
        "actions": [],
        "warnings": [],
        "errors": [],
        "summary": {"inserted_nodes": 0, "copied_assets": 0},
    }

    fruit_target_file = find_primary_xml_file(patched_target, "fruit_types")
    fill_target_file = find_primary_xml_file(patched_target, "fill_types")
    height_target_file = find_primary_xml_file(patched_target, "height_types")
    growth_target_file = find_primary_xml_file(patched_target, "growth")

    for rel_file, role in [
        (fruit_target_file, "fruit_types"),
        (fill_target_file, "fill_types"),
        (height_target_file, "height_types"),
        (growth_target_file, "growth"),
    ]:
        if not rel_file:
            apply_report["warnings"].append(f"No target {role} XML file detected. Related nodes cannot be inserted automatically.")

    for crop_name in [c.lower() for c in crops]:
        crop = source.crop_defs.get(crop_name)
        if not crop:
            apply_report["errors"].append(f"Crop not found in source: {crop_name}")
            continue

        copied, warnings = copy_assets_for_crop(source, output_root, crop)
        apply_report["summary"]["copied_assets"] += copied
        apply_report["warnings"].extend(warnings)
        apply_report["actions"].append(f"Copied {copied} asset(s) for crop '{crop_name}'.")

        l10n_warnings = patch_l10n_for_crop(output_root, crop)
        apply_report["warnings"].extend(l10n_warnings)
        apply_report["actions"].append(f"Patched l10n entries for crop '{crop_name}'.")

        patched_layers, layer_warnings = patch_i3d_foliage_layer_for_crop(source, output_root, crop)
        apply_report["warnings"].extend(layer_warnings)
        if patched_layers:
            apply_report["actions"].append(f"Patched {patched_layers} i3d foliage layer entry for crop '{crop_name}'.")

        if fruit_target_file:
            inserted, warnings = insert_fruit_nodes_into_xml(
                output_root,
                fruit_target_file,
                crop.fruit_nodes,
                set(patched_target.fruit_names),
            )
            apply_report["summary"]["inserted_nodes"] += inserted
            apply_report["warnings"].extend(warnings)
            apply_report["actions"].append(f"Inserted {inserted} fruitType node(s) for crop '{crop_name}'.")

            cat_inserted, cat_warnings = patch_fruit_type_categories(source, output_root, crop, fruit_target_file)
            apply_report["summary"]["inserted_nodes"] += cat_inserted
            apply_report["warnings"].extend(cat_warnings)
            apply_report["actions"].append(f"Patched {cat_inserted} fruitTypeCategory entry change(s) for crop '{crop_name}'.")

            fill_cat_inserted, fill_cat_warnings = patch_fill_type_categories(source, output_root, crop, fill_target_file)
            apply_report["summary"]["inserted_nodes"] += fill_cat_inserted
            apply_report["warnings"].extend(fill_cat_warnings)
            apply_report["actions"].append(f"Patched {fill_cat_inserted} fillTypeCategory entry change(s) for crop '{crop_name}'.")

        if fill_target_file:
            inserted, warnings = insert_nodes_into_xml(
                output_root,
                fill_target_file,
                crop.fill_type_nodes,
                set(patched_target.fill_type_names),
                ["name", "fillType", "fillTypeName"],
            )
            apply_report["summary"]["inserted_nodes"] += inserted
            apply_report["warnings"].extend(warnings)
            apply_report["actions"].append(f"Inserted {inserted} fillType node(s) for crop '{crop_name}'.")

        if height_target_file:
            inserted, warnings = insert_nodes_into_xml(
                output_root,
                height_target_file,
                crop.height_type_nodes,
                set(patched_target.height_type_names),
                ["name", "fillType", "fillTypeName"],
            )
            apply_report["summary"]["inserted_nodes"] += inserted
            apply_report["warnings"].extend(warnings)
            apply_report["actions"].append(f"Inserted {inserted} heightType node(s) for crop '{crop_name}'.")

        if growth_target_file:
            # Growth nodes are hard to de-duplicate safely, so insert by exact XML text check.
            inserted, warnings = insert_growth_nodes(output_root, growth_target_file, crop.growth_nodes)
            apply_report["summary"]["inserted_nodes"] += inserted
            apply_report["warnings"].extend(warnings)
            apply_report["actions"].append(f"Inserted {inserted} growth/calendar node(s) for crop '{crop_name}'.")

    # Re-scan output for a basic validation pass.
    validation_profile = MapProfile(str(output_root), str(output_root), False, str(output_root))
    scan_profile(validation_profile)
    validation = validate_output(source, validation_profile, [c.lower() for c in crops])
    apply_report["validation"] = validation
    apply_report["warnings"].extend(validation.get("warnings", []))
    apply_report["errors"].extend(validation.get("errors", []))

    write_reports(apply_report, output_root, prefix="CropPorter_Apply")
    return apply_report


def insert_fruit_nodes_into_xml(target_root: Path, rel_file: str, nodes: list[XmlNodeRef], existing_names: set[str]) -> tuple[int, list[str]]:
    """Insert fruit registry entries safely into the active fruitTypes registry.

    Some maps, including BR163, keep the active <fruitTypes> block inline in maps/mapAS.xml
    and only reference maps/config/maps_fruitTypes.xml as an <additionalFile>. For those
    maps, writing imported crops to maps/config/maps_fruitTypes.xml makes the file look
    correct but the engine never registers the fruitType. Resolve the active registry
    before writing.
    """
    active_rel_file = resolve_active_fruit_types_xml(target_root, rel_file)
    path = target_root / active_rel_file
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fruitTypes XML: {active_rel_file}"]

    root = tree.getroot()
    fruit_types = find_child_container(root, "fruitTypes")
    if fruit_types is None:
        fruit_types = root

    registry_style = detect_fruit_registry_style(fruit_types)
    inserted = 0
    warnings: list[str] = []
    current_names = {x.lower() for x in existing_names}
    current_files = collect_existing_fruit_registry_files(root)

    for ref in nodes:
        source_rel_norm = ref.relative_file.replace(chr(92), "/")
        try:
            elem = ET.fromstring(ref.xml_text)
        except ET.ParseError:
            warnings.append(f"Could not parse source fruitType node for insertion: {ref.tag}")
            continue

        name = find_first_attr(elem, ["name", "fruitType", "fruitTypeName"])
        name_key = lower_name(name) if name else ""
        file_key = source_rel_norm.lower()

        # Duplicate detection must only treat existing direct fruitType registry files/names
        # as registered crops. fruitTypeCategory text such as PINTOBEAN is not enough.
        if name_key and name_key in current_names:
            warnings.append(f"Skipped target duplicate fruitType '{name}' in {active_rel_file}")
            continue

        if "/foliage/" in source_rel_norm.lower() and source_rel_norm.lower().endswith(".xml"):
            if file_key in current_files:
                warnings.append(f"Skipped duplicate fruit registry file '{source_rel_norm}' in {active_rel_file}")
                continue
            if registry_style == "fruitTypeFilename":
                new_ref = ET.Element("fruitType")
                new_ref.set("filename", source_rel_norm)
                fruit_types.append(new_ref)
            else:
                container = ensure_additional_files_container(root)
                new_ref = ET.Element("additionalFile")
                new_ref.set("filename", source_rel_norm)
                container.append(new_ref)
            current_files.add(file_key)
            if name_key:
                current_names.add(name_key)
            inserted += 1
        else:
            fruit_types.append(elem)
            if name_key:
                current_names.add(name_key)
            inserted += 1

    if inserted:
        backup = path.with_suffix(path.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    return inserted, warnings


def resolve_active_fruit_types_xml(target_root: Path, fallback_rel_file: str) -> str:
    """Return the XML file whose <fruitTypes> block the game is most likely to load."""
    candidates: list[tuple[int, int, str]] = []

    for path in target_root.rglob("*.xml"):
        rel = rel_to_root(path, target_root).replace(chr(92), "/")
        rel_l = rel.lower()
        if not rel_l.startswith("maps/"):
            continue
        if "map" not in path.name.lower():
            continue
        tree = parse_xml_safely(path)
        if not tree:
            continue
        root = tree.getroot()
        fruit_types = find_direct_child(root, "fruitTypes") or find_child_container(root, "fruitTypes")
        if fruit_types is None:
            continue
        if count_direct_fruit_type_entries(fruit_types) <= 0:
            continue
        # Prefer short map XML files such as maps/mapAS.xml over nested config files.
        candidates.append((0, len(rel), rel))

    if candidates:
        return sorted(candidates)[0][2]
    return fallback_rel_file.replace(chr(92), "/")


def count_direct_fruit_type_entries(container: ET.Element) -> int:
    count = 0
    for child in list(container):
        if local_name(child.tag).lower() == "fruittype" and (child.attrib.get("filename") or child.attrib.get("name")):
            count += 1
    return count


def patch_fill_type_categories(source: MapProfile, target_root: Path, crop: CropDefinition, fallback_rel_file: Optional[str]) -> tuple[int, list[str]]:
    """Add imported crop fillTypes to relevant fillTypeCategory entries.

    fruitTypeCategory controls crop/field/implement support. fillTypeCategory controls
    the harvested product side: trailers, auger wagons, silos, sell points, shovels,
    and mods such as Fresh that inspect fillType/category metadata.
    """
    warnings: list[str] = []
    fill_type_names = collect_crop_primary_fill_types(crop)
    if not fill_type_names:
        return 0, [f"No primary fillType names found for crop '{crop.fruit_name}'. FillType categories were not patched."]

    target_filltypes_rel = resolve_target_fill_types_xml(target_root, fallback_rel_file)
    if not target_filltypes_rel:
        return 0, [f"No target fillTypes XML found for category patching for crop '{crop.fruit_name}'."]

    path = target_root / target_filltypes_rel
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fillTypes XML for category patching: {target_filltypes_rel}"]

    root = tree.getroot()
    categories = find_child_container(root, "fillTypeCategories")
    if categories is None:
        categories = ET.Element("fillTypeCategories")
        root.append(categories)

    source_categories = find_source_fill_categories_for_crop(source, fill_type_names)
    if not source_categories:
        source_categories = infer_default_fill_type_categories_for_field_crop(fill_type_names)
        warnings.append(
            f"No source fillTypeCategory membership found for crop '{crop.fruit_name}'. "
            f"Applied inferred field-crop categories: {', '.join(sorted(source_categories))}."
        )

    changed = 0
    for category_name in sorted(source_categories):
        category = find_named_category(categories, "fillTypeCategory", category_name)
        if category is None:
            category = ET.Element("fillTypeCategory")
            category.set("name", category_name)
            categories.append(category)
            changed += 1
        for fill_type_name in sorted(fill_type_names):
            if add_token_to_category_text(category, fill_type_name):
                changed += 1

    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.fillcategories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    return changed, warnings


def collect_crop_primary_fill_types(crop: CropDefinition) -> set[str]:
    """Return harvested product fillTypes for category patching.

    Exclude cut/windrow/straw by-products so BLACKBEAN gets category support but
    BLACKBEAN_CUT/SOYBEAN_CUT/STRAW do not get added to grain trailer categories.
    """
    crop_upper = crop.fruit_name.upper()
    names: set[str] = set()
    for ref in crop.fill_nodes:
        try:
            elem = ET.fromstring(ref.xml_text)
        except ET.ParseError:
            continue
        name = elem.attrib.get("name") or elem.attrib.get("fillType")
        if not name:
            continue
        name_u = name.upper()
        if name_u == crop_upper:
            names.add(name_u)
            continue
        if name_u.endswith("_CUT") or name_u.endswith("_WINDROW"):
            continue
        if name_u in {"STRAW", "GRASS_WINDROW", "DRYGRASS_WINDROW", "CHAFF"}:
            continue
        # Some source maps use case variants such as pintobean rather than PINTOBEAN.
        if name_u.replace("_", "") == crop_upper.replace("_", ""):
            names.add(name_u)
    if not names:
        names.add(crop_upper)
    return names


def resolve_target_fill_types_xml(target_root: Path, fallback_rel_file: Optional[str]) -> Optional[str]:
    if fallback_rel_file:
        rel = fallback_rel_file.replace(chr(92), "/")
        if (target_root / rel).exists():
            return rel
    candidates: list[tuple[int, int, str]] = []
    for path in target_root.rglob("*.xml"):
        rel = rel_to_root(path, target_root).replace(chr(92), "/")
        rel_l = rel.lower()
        if "filltype" not in rel_l:
            continue
        tree = parse_xml_safely(path)
        if not tree:
            continue
        root = tree.getroot()
        if find_child_container(root, "fillTypes") is not None or find_child_container(root, "fillTypeCategories") is not None:
            score = 0 if rel_l.endswith("maps_filltypes.xml") else 1
            candidates.append((score, len(rel), rel))
    if candidates:
        return sorted(candidates)[0][2]
    return None


def find_source_fill_categories_for_crop(source: MapProfile, fill_type_names: set[str]) -> set[str]:
    root = Path(source.root)
    wanted = {x.upper() for x in fill_type_names}
    categories: set[str] = set()
    for rels in source.xml_files.values():
        for rel in rels:
            tree = parse_xml_safely(root / rel)
            if not tree:
                continue
            for elem in tree.getroot().iter():
                if local_name(elem.tag).lower() != "filltypecategory":
                    continue
                category_name = normalise_name(elem.attrib.get("name"))
                if not category_name:
                    continue
                tokens = get_category_tokens(elem)
                if wanted.intersection(tokens):
                    categories.add(category_name)
    return categories


def infer_default_fill_type_categories_for_field_crop(fill_type_names: set[str]) -> set[str]:
    """Fallback categories for dry grain/bean field crops."""
    return {
        "BULK",
        "COMBINE",
        "AUGERWAGON",
        "TRAINWAGON",
        "SHOVEL",
        "FARMSILO",
        "LOADINGVEHICLE",
        "SELLINGSTATION_FIELDFRUITS",
    }


def find_named_category(parent: ET.Element, tag_name: str, category_name: str) -> Optional[ET.Element]:
    wanted = category_name.lower()
    for elem in list(parent):
        if local_name(elem.tag).lower() == tag_name.lower() and elem.attrib.get("name", "").lower() == wanted:
            return elem
    return None


def patch_fruit_type_categories(source: MapProfile, target_root: Path, crop: CropDefinition, fallback_rel_file: Optional[str]) -> tuple[int, list[str]]:
    """Add imported crop to seeder/planter fruitTypeCategory entries.

    Implements generally support crops by category, e.g. SOWINGMACHINE or PLANTER.
    If BLACKBEAN/PINTOBEAN are registered fruitTypes but missing from those category
    lists, they appear in the map/calendar but no seeder/planter can select them.
    """
    warnings: list[str] = []
    if not fallback_rel_file:
        return 0, [f"No target fruitTypes XML found for category patching for crop '{crop.fruit_name}'."]

    crop_upper = crop.fruit_name.upper()
    source_categories = find_source_fruit_categories_for_crop(source, crop.fruit_name)
    if not source_categories:
        warnings.append(f"No source fruitTypeCategory membership found for crop '{crop.fruit_name}'. Seeder/planter support may need manual configuration.")
        return 0, warnings

    active_rel_file = resolve_active_fruit_types_xml(target_root, fallback_rel_file)
    path = target_root / active_rel_file
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse active target fruitTypes XML for category patching: {active_rel_file}"]

    root = tree.getroot()
    changed = 0
    for category_name in sorted(source_categories):
        category = find_fruit_type_category(root, category_name)
        if category is None:
            category = ensure_fruit_type_category(root, category_name)
            changed += 1
        if add_token_to_category_text(category, crop_upper):
            changed += 1

    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.categories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    return changed, warnings


def find_source_fruit_categories_for_crop(source: MapProfile, crop_name: str) -> set[str]:
    root = Path(source.root)
    crop_upper = crop_name.upper()
    categories: set[str] = set()
    for rel in source.xml_files.get("fruit_types", []) + source.xml_files.get("other", []):
        tree = parse_xml_safely(root / rel)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "fruittypecategory":
                continue
            category_name = normalise_name(elem.attrib.get("name"))
            if not category_name:
                continue
            tokens = get_category_tokens(elem)
            if crop_upper in tokens:
                categories.add(category_name)
    return categories


def get_category_tokens(elem: ET.Element) -> set[str]:
    text = " ".join(part for part in [elem.text or "", elem.attrib.get("fruitTypes", ""), elem.attrib.get("fillTypes", "")] if part)
    return {token.upper() for token in re.split(r"[^A-Za-z0-9_]+", text) if token}


def find_fruit_type_category(root: ET.Element, category_name: str) -> Optional[ET.Element]:
    wanted = category_name.lower()
    for elem in root.iter():
        if local_name(elem.tag).lower() == "fruittypecategory" and elem.attrib.get("name", "").lower() == wanted:
            return elem
    return None


def ensure_fruit_type_category(root: ET.Element, category_name: str) -> ET.Element:
    categories = find_child_container(root, "fruitTypeCategories")
    if categories is None:
        fruit_types = find_child_container(root, "fruitTypes")
        categories = ET.Element("fruitTypeCategories")
        if fruit_types is not None:
            fruit_types.append(categories)
        else:
            root.append(categories)
    category = ET.Element("fruitTypeCategory")
    category.set("name", category_name)
    categories.append(category)
    return category


def add_token_to_category_text(category: ET.Element, token: str) -> bool:
    tokens = get_category_tokens(category)
    if token.upper() in tokens:
        return False
    existing = (category.text or "").strip()
    category.text = (existing + " " + token.upper()).strip() if existing else token.upper()
    return True


def detect_fruit_registry_style(fruit_types: ET.Element) -> str:
    for child in list(fruit_types):
        if local_name(child.tag).lower() == "fruittype" and child.attrib.get("filename"):
            return "fruitTypeFilename"
    for child in fruit_types.iter():
        if local_name(child.tag).lower() == "additionalfile" and child.attrib.get("filename"):
            return "additionalFile"
    return "fruitTypeFilename"


def collect_existing_fruit_registry_files(root: ET.Element) -> set[str]:
    files: set[str] = set()
    for elem in root.iter():
        tag = local_name(elem.tag).lower()
        if tag in {"fruittype", "additionalfile"}:
            filename = elem.attrib.get("filename")
            if filename:
                files.add(filename.replace(chr(92), "/").lower())
    return files


def find_child_container(root: ET.Element, tag_name: str) -> Optional[ET.Element]:
    tag_l = tag_name.lower()
    for elem in root.iter():
        if local_name(elem.tag).lower() == tag_l:
            return elem
    return None


def ensure_additional_files_container(root: ET.Element) -> ET.Element:
    existing = find_child_container(root, "additionalFiles")
    if existing is not None:
        return existing
    fruit_types = find_child_container(root, "fruitTypes")
    if fruit_types is None:
        fruit_types = root
    container = ET.Element("additionalFiles")
    fruit_types.append(container)
    return container


def collect_existing_additional_files(root: ET.Element) -> set[str]:
    files: set[str] = set()
    for elem in root.iter():
        if local_name(elem.tag).lower() == "additionalfile":
            filename = elem.attrib.get("filename")
            if filename:
                files.add(filename.replace(chr(92), "/"))
    return files


def insert_growth_nodes(target_root: Path, rel_file: str, nodes: list[XmlNodeRef]) -> tuple[int, list[str]]:
    path = target_root / rel_file
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target growth XML: {rel_file}"]

    root = tree.getroot()
    existing_xml = {element_to_string(elem).strip() for elem in root.iter()}
    inserted = 0
    warnings: list[str] = []

    for ref in nodes:
        xml_text = ref.xml_text.strip()
        if xml_text in existing_xml:
            warnings.append(f"Skipped duplicate growth node in {rel_file}: {ref.tag}")
            continue
        try:
            elem = ET.fromstring(xml_text)
        except ET.ParseError:
            warnings.append(f"Could not parse growth node for insertion: {ref.tag}")
            continue
        root.append(elem)
        existing_xml.add(xml_text)
        inserted += 1

    if inserted:
        backup = path.with_suffix(path.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    return inserted, warnings


def validate_output(source: MapProfile, output_profile: MapProfile, crops: list[str]) -> dict:
    result = {"errors": [], "warnings": []}
    for crop_name in crops:
        source_crop = source.crop_defs.get(crop_name)
        if not source_crop:
            result["errors"].append(f"Cannot validate missing source crop: {crop_name}")
            continue
        if crop_name not in output_profile.fruit_names:
            result["errors"].append(f"Output map does not contain fruitType after apply: {crop_name}")
        for fill_name in source_crop.fill_type_names:
            if fill_name not in output_profile.fill_type_names:
                result["warnings"].append(f"Output map may be missing fillType '{fill_name}' for crop '{crop_name}'.")
    result["warnings"].append("Density map binary/channel capacity was not validated. Check densityMap_fruits.gdm/map.i3d manually if adding new fruitTypes.")
    return result


def cmd_scan_source(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.source))
    try:
        scan_profile(profile)
        print_scan_summary(profile, include_basegame=args.include_basegame)
        if args.output:
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=True)
            (out / "CropPorter_SourceScan.json").write_text(json.dumps(profile.to_jsonable(), indent=2), encoding="utf-8")
            print(f"Wrote: {out / 'CropPorter_SourceScan.json'}")
        return 0
    finally:
        profile.cleanup()


def cmd_preflight(args: argparse.Namespace) -> int:
    source = prepare_map_input(Path(args.source))
    target = prepare_map_input(Path(args.target))
    try:
        scan_profile(source)
        scan_profile(target)
        report = preflight(source, target, args.crops)
        write_reports(report, Path(args.output), prefix="CropPorter_Preflight")
        print(render_markdown_report(report))
        return 1 if report["summary"]["errors"] else 0
    finally:
        source.cleanup()
        target.cleanup()


def cmd_apply(args: argparse.Namespace) -> int:
    source = prepare_map_input(Path(args.source))
    target = prepare_map_input(Path(args.target))
    try:
        scan_profile(source)
        scan_profile(target)
        pre = preflight(source, target, args.crops)
        if pre["summary"]["errors"] and not args.force:
            write_reports(pre, Path(args.output).parent, prefix="CropPorter_Preflight_FAILED")
            print("Preflight failed. Use --force only if you understand the risk.", file=sys.stderr)
            return 2
        report = apply_patch(source, target, args.crops, Path(args.output))
        print("Apply complete.")
        print(f"Output: {report['output']}")
        print(f"Inserted nodes: {report['summary']['inserted_nodes']}")
        print(f"Copied assets: {report['summary']['copied_assets']}")
        if report["errors"]:
            print("Errors:")
            for err in report["errors"]:
                print(f"- {err}")
            return 1
        if report["warnings"]:
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"- {warning}")
        return 0
    finally:
        source.cleanup()
        target.cleanup()


def cmd_selftest(args: argparse.Namespace) -> int:
    checks = [
        (value_references_name("RYE_CUT", "rye"), True, "RYE_CUT should match rye"),
        (value_references_name("maps/foliage/rye/rye.xml", "rye"), True, "rye path should match rye"),
        (value_references_name("GREENRYE", "rye"), False, "GREENRYE should not match rye"),
        (value_references_name("VETCHRYE", "rye"), False, "VETCHRYE should not match rye"),
    ]
    failed = 0
    for actual, expected, label in checks:
        if actual != expected:
            failed += 1
            print(f"FAIL: {label} -> got {actual}, expected {expected}")
        else:
            print(f"PASS: {label}")
    return 1 if failed else 0


def cmd_probe_crop(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        scan_profile(profile)
        crop_name = args.crop.lower()
        crop = profile.crop_defs.get(crop_name)
        if not crop:
            print(f"Crop not detected: {args.crop}")
            print("Detected fruitTypes:")
            for fruit in sorted(profile.fruit_names):
                print(f"- {fruit}")
            return 1
        print(f"FS25_CropPorter {VERSION}")
        print(f"Map: {profile.source_path}")
        print(f"Crop: {crop.fruit_name}")
        print()
        print(f"fruitType nodes: {len(crop.fruit_nodes)}")
        for ref in crop.fruit_nodes:
            print(f"- {ref.relative_file} <{ref.tag}>")
        print(f"fillType nodes: {len(crop.fill_type_nodes)}")
        for ref in crop.fill_type_nodes:
            name = find_first_attr(ET.fromstring(ref.xml_text), ["name", "fillType", "fillTypeName"]) if ref.xml_text else ""
            print(f"- {ref.relative_file} <{ref.tag}> {name or ''}")
        print(f"heightType nodes: {len(crop.height_type_nodes)}")
        for ref in crop.height_type_nodes:
            print(f"- {ref.relative_file} <{ref.tag}>")
        print(f"growth nodes: {len(crop.growth_nodes)}")
        for ref in crop.growth_nodes:
            print(f"- {ref.relative_file} <{ref.tag}>")
        print(f"asset paths: {len(crop.asset_paths)}")
        for asset in sorted(crop.asset_paths):
            print(f"- {asset}")
        if crop.warnings:
            print("Warnings:")
            for warning in crop.warnings:
                print(f"- {warning}")
        return 0
    finally:
        profile.cleanup()


def cmd_probe_map(args: argparse.Namespace) -> int:
    """Probe a map ZIP/folder for crop-system references and likely integration points."""
    profile = prepare_map_input(Path(args.map))
    try:
        scan_profile(profile)
        root = Path(profile.root)
        keywords = args.keywords or [
            "fruitTypes", "fillTypes", "densityMapHeightTypes", "heightTypes",
            "growth", "season", "seasonal", "cropCalendar", "densityMap_fruits",
            "BLACKBEAN", "blackbean",
        ]
        print(f"FS25_CropPorter {VERSION}")
        print(f"Map: {profile.source_path}")
        print(f"Root: {profile.root}")
        print()
        print("Detected XML roles:")
        for role in sorted(profile.xml_files):
            if role == "other":
                continue
            print(f"- {role}: {len(profile.xml_files[role])} file(s)")
            if args.verbose:
                for rel in profile.xml_files[role]:
                    print(f"  - {rel}")
        print()
        print("Keyword references:")
        hits = find_keyword_references(root, keywords, max_hits_per_file=args.max_hits)
        if not hits:
            print("- No keyword references found.")
        else:
            for rel, file_hits in hits.items():
                print(f"- {rel}")
                for line_no, keyword, line in file_hits:
                    print(f"  L{line_no}: [{keyword}] {line}")
        print()
        print("Likely primary files:")
        for role in ("fruit_types", "fill_types", "height_types", "growth", "bales", "weed"):
            primary = find_primary_xml_file(profile, role)
            print(f"- {role}: {primary or 'not detected'}")
        print()
        primary_fruit_types = find_primary_xml_file(profile, "fruit_types")
        if primary_fruit_types and "/foliage/" in primary_fruit_types.replace(chr(92), "/").lower():
            print("WARNING: primary fruit_types file resolved to a foliage XML. This usually means the central maps_fruitTypes.xml was not detected correctly.")
        return 0
    finally:
        profile.cleanup()


def cmd_probe_fruit_registry(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        scan_profile(profile)
        root = Path(profile.root)
        fruit_file = find_primary_xml_file(profile, "fruit_types")
        if not fruit_file:
            print("No primary fruitTypes XML detected.")
            return 1
        path = root / fruit_file
        tree = parse_xml_safely(path)
        if not tree:
            print(f"Could not parse fruitTypes XML: {fruit_file}")
            return 1
        print(f"FS25_CropPorter {VERSION}")
        print(f"Map: {profile.source_path}")
        print(f"Primary fruitTypes XML: {fruit_file}")
        print()
        root_elem = tree.getroot()
        fruit_types = find_child_container(root_elem, "fruitTypes")
        if fruit_types is None:
            fruit_types = root_elem
        print(f"fruitTypes children under <{local_name(fruit_types.tag)}>:")
        for idx, child in enumerate(list(fruit_types), start=1):
            tag = local_name(child.tag)
            filename = child.attrib.get("filename", "")
            name = child.attrib.get("name", "")
            marker = ""
            text = (filename or name or ET.tostring(child, encoding="unicode")[:120]).replace(chr(10), " ")
            if "blackbean" in text.lower():
                marker = "  <-- BLACKBEAN"
            print(f"{idx:03d}. <{tag}> {text}{marker}")
        return 0
    finally:
        profile.cleanup()


def cmd_fix_fruit_registry(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        if profile.is_temp:
            raise CropPorterError("fix-fruit-registry requires a folder target, not a ZIP.")
        scan_profile(profile)
        root = Path(profile.root)
        fruit_file = find_primary_xml_file(profile, "fruit_types")
        if not fruit_file:
            print("No primary fruitTypes XML detected.")
            return 1
        path = root / fruit_file
        tree = parse_xml_safely(path)
        if not tree:
            print(f"Could not parse fruitTypes XML: {fruit_file}")
            return 1
        root_elem = tree.getroot()
        fruit_types = find_child_container(root_elem, "fruitTypes")
        if fruit_types is None:
            fruit_types = root_elem
        wanted = args.filename.replace(chr(92), "/")

        remove_fruit_registry_refs(root_elem, wanted)

        new_elem = ET.Element("fruitType")
        new_elem.set("filename", wanted)
        fruit_types.append(new_elem)

        # Remove empty additionalFiles wrappers accidentally created by older alpha builds.
        remove_empty_additional_files(root_elem)

        backup = path.with_suffix(path.suffix + ".cropporter.bak2")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        print(f"Fixed fruit registry: {fruit_file}")
        print(f"Ensured direct fruitType filename reference: {wanted}")
        return 0
    finally:
        profile.cleanup()


def cmd_fix_crop_filltype_case(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        if profile.is_temp:
            raise CropPorterError("fix-crop-filltype-case requires a folder target, not a ZIP.")
        root = Path(profile.root)
        foliage = root / args.foliage_xml
        if not foliage.exists():
            print(f"Foliage XML not found: {args.foliage_xml}")
            return 1
        tree = parse_xml_safely(foliage)
        if not tree:
            print(f"Could not parse foliage XML: {args.foliage_xml}")
            return 1
        old = args.old
        new = args.new
        changed = 0
        for elem in tree.getroot().iter():
            for key, value in list(elem.attrib.items()):
                # Keep the fruitType name itself lower-case unless explicitly edited by hand.
                if key.lower() == "name" and local_name(elem.tag).lower() == "fruittype":
                    continue
                if value == old:
                    elem.set(key, new)
                    changed += 1
        if changed == 0:
            print(f"No exact attribute values '{old}' were found in {args.foliage_xml}.")
            return 1
        backup = foliage.with_suffix(foliage.suffix + ".cropporter.bak")
        if not backup.exists():
            shutil.copy2(foliage, backup)
        indent_xml(tree)
        tree.write(foliage, encoding="utf-8", xml_declaration=True)
        print(f"Patched {changed} fillType reference(s) in {args.foliage_xml}: {old} -> {new}")
        return 0
    finally:
        profile.cleanup()


def cmd_fix_filltype_registry_name(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        if profile.is_temp:
            raise CropPorterError("fix-filltype-registry-name requires a folder target, not a ZIP.")
        scan_profile(profile)
        root = Path(profile.root)
        fill_file = find_primary_xml_file(profile, "fill_types")
        if not fill_file:
            print("No primary fillTypes XML detected.")
            return 1
        path = root / fill_file
        tree = parse_xml_safely(path)
        if not tree:
            print(f"Could not parse fillTypes XML: {fill_file}")
            return 1
        old = args.old
        new = args.new
        changed = 0
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() == "filltype" and elem.attrib.get("name") == old:
                elem.set("name", new)
                changed += 1
        if changed == 0:
            print(f"No fillType name '{old}' found in {fill_file}.")
            return 1
        backup = path.with_suffix(path.suffix + ".cropporter.bak2")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        print(f"Patched {changed} fillType registry node(s) in {fill_file}: {old} -> {new}")
        return 0
    finally:
        profile.cleanup()


def cmd_patch_l10n(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        if profile.is_temp:
            raise CropPorterError("patch-l10n requires a folder target, not a ZIP.")
        crop = CropDefinition(fruit_name=args.crop.lower())
        warnings = patch_l10n_for_crop(Path(profile.root), crop, label=args.label)
        for warning in warnings:
            print(f"Warning: {warning}")
        if warnings:
            return 1
        print(f"Patched l10n entries for crop '{crop.fruit_name}'.")
        return 0
    finally:
        profile.cleanup()


def remove_empty_additional_files(root: ET.Element) -> None:
    for parent in root.iter():
        for child in list(parent):
            if local_name(child.tag).lower() == "additionalfiles" and len(list(child)) == 0:
                parent.remove(child)


def remove_fruit_registry_refs(root: ET.Element, filename: str) -> None:
    filename_l = filename.lower().replace(chr(92), "/")
    for parent in root.iter():
        for child in list(parent):
            tag = local_name(child.tag).lower()
            if tag in {"fruittype", "additionalfile"}:
                child_filename = child.attrib.get("filename", "").lower().replace(chr(92), "/")
                if child_filename == filename_l:
                    parent.remove(child)


def remove_additional_file_refs(root: ET.Element, filename: str) -> None:
    filename_l = filename.lower().replace(chr(92), "/")
    for parent in root.iter():
        for child in list(parent):
            if local_name(child.tag).lower() == "additionalfile":
                child_filename = child.attrib.get("filename", "").lower().replace(chr(92), "/")
                if child_filename == filename_l:
                    parent.remove(child)


def cmd_probe_density(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        scan_profile(profile)
        root = Path(profile.root)
        layers = find_density_fruit_layers(root)

        print(f"FS25_CropPorter {VERSION}")
        print(f"Map: {profile.source_path}")
        print(f"Root: {profile.root}")
        print()
        map_fruits = set(profile.fruit_names)
        estimated_engine_fruits = estimate_engine_fruits(profile)
        print(f"Detected map fruitTypes from XML scan: {len(map_fruits)}")
        print(f"Estimated engine fruitTypes sharing densityMap_fruits: {len(estimated_engine_fruits)}")
        if args.include_fruits:
            print("Map fruitTypes:")
            for fruit in sorted(map_fruits):
                print(f"- {fruit}")
            print("Estimated engine fruitTypes:")
            for fruit in sorted(estimated_engine_fruits):
                print(f"- {fruit}")
        print()

        if not layers:
            print("No densityMap_fruits layer was found in .i3d files.")
            print("Search manually for densityMap_fruits in the map.i3d, or check whether the map uses a non-standard density map filename.")
            return 1

        print("Detected fruit density layer(s):")
        for layer in layers:
            print(f"- {layer.relative_file} <{layer.tag}>")
            print(f"  density map: {layer.density_map}")
            print(f"  numChannels: {layer.num_channels if layer.num_channels is not None else 'not found'}")
            print(f"  numTypeIndexChannels: {layer.num_type_index_channels if layer.num_type_index_channels is not None else 'not found'}")
            print(f"  compressionChannels: {layer.compression_channels if layer.compression_channels is not None else 'not found'}")
            if layer.estimated_capacity is not None:
                print(f"  estimated type-index capacity: {layer.estimated_capacity}")
                remaining = layer.estimated_capacity - len(estimated_engine_fruits)
                print(f"  estimated spare slots before import: {remaining}")
                if args.add:
                    after = len(estimated_engine_fruits) + args.add
                    print(f"  estimated fruitTypes after +{args.add}: {after}")
                    print(f"  estimated spare slots after import: {layer.estimated_capacity - after}")
                    if after > layer.estimated_capacity:
                        print("  WARNING: selected import likely exceeds this density layer capacity.")
            if layer.num_channels is not None and layer.num_type_index_channels is not None and layer.compression_channels is not None:
                expected = layer.num_type_index_channels + layer.compression_channels
                if layer.num_channels != expected:
                    print(f"  WARNING: numChannels does not equal numTypeIndexChannels + compressionChannels ({expected}).")
            print()

        print("Notes:")
        print("- This probe does not modify the map.")
        print("- The engine error 'no more type indexes can be allocated' points at fruit density type-index capacity, not tipped-material height types.")
        print("- Estimated engine fruit count includes known basegame/DLC fruitTypes because they can consume the same multilayer indexes.")
        print("- If numTypeIndexChannels is increased, the density map file may also need conversion/expansion to match the new channel layout.")
        return 0
    finally:
        profile.cleanup()


def estimate_engine_fruits(profile: MapProfile) -> set[str]:
    return {f.lower() for f in KNOWN_ENGINE_FRUITS}.union({f.lower() for f in profile.fruit_names})


def find_density_fruit_layers(root: Path) -> list[DensityLayerInfo]:
    layers: list[DensityLayerInfo] = []
    for i3d_path in sorted(root.rglob("*.i3d")):
        tree = parse_xml_safely(i3d_path)
        if not tree:
            continue
        rel = rel_to_root(i3d_path, root)
        file_ids: dict[str, str] = {}
        fruit_file_ids: set[str] = set()

        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() == "file":
                file_id = elem.attrib.get("fileId") or elem.attrib.get("fileID") or elem.attrib.get("id")
                filename = elem.attrib.get("filename") or elem.attrib.get("file") or ""
                if file_id and filename:
                    file_ids[file_id] = filename
                    if "densitymap_fruits" in filename.lower():
                        fruit_file_ids.add(file_id)

        for elem in tree.getroot().iter():
            attrs = dict(elem.attrib)
            if not attrs:
                continue
            tag_l = local_name(elem.tag).lower()
            direct_hit = any("densitymap_fruits" in value.lower() for value in attrs.values())
            id_hit = any(value in fruit_file_ids for value in attrs.values())
            if not direct_hit and not id_hit:
                continue

            density_map = ""
            for value in attrs.values():
                if "densitymap_fruits" in value.lower():
                    density_map = value
                    break
            if not density_map:
                for value in attrs.values():
                    if value in fruit_file_ids:
                        density_map = file_ids.get(value, "densityMap_fruits reference found")
                        break

            # Only FoliageMultiLayer carries the channel capacity settings we care about.
            # Other references such as File, Material, and Shape are useful but noisy.
            if tag_l != "foliagemultilayer":
                continue

            layers.append(DensityLayerInfo(
                relative_file=rel,
                tag=local_name(elem.tag),
                attrs=attrs,
                density_map=density_map or "densityMap_fruits reference found",
                num_channels=parse_int_attr(attrs, "numChannels"),
                num_type_index_channels=parse_int_attr(attrs, "numTypeIndexChannels"),
                compression_channels=parse_int_attr(attrs, "compressionChannels"),
            ))
    return layers


def patch_i3d_foliage_layer_for_crop(source: MapProfile, target_root: Path, crop: CropDefinition, preferred_start_id: int = 5000) -> tuple[int, list[str]]:
    """Copy a crop terrain layer entry and its required <File> entry from source i3d.

    The source foliage entry commonly references a <File fileId="..." filename="..." />
    node. That fileId may already be used in the target map i3d, so both the copied
    <File> entry and the copied foliage entry must be remapped to a fresh ID.

    Some maps contain stale/template source <File> filenames, e.g. a pintobean terrain
    entry pointing at foliage/pintobean/blackbean.xml. When CropPorter has a canonical
    crop foliage XML from the fruit registry, prefer that path for the copied i3d <File>.
    """
    warnings: list[str] = []
    crop_l = crop.fruit_name.lower()

    source_bundle = find_source_foliage_layer_bundle(Path(source.root), crop_l)
    if source_bundle is None:
        warnings.append(f"No source map.i3d foliage layer entry found for crop '{crop.fruit_name}'. Terrain layer must be added manually.")
        return 0, warnings

    source_entry, source_file_entries = source_bundle

    target_i3d = find_primary_target_i3d_for_fruits(target_root)
    if target_i3d is None:
        warnings.append("No target map.i3d densityMap_fruits FoliageMultiLayer found. Terrain layer must be added manually.")
        return 0, warnings

    tree = parse_xml_safely(target_i3d)
    if not tree:
        warnings.append(f"Could not parse target i3d: {rel_to_root(target_i3d, target_root)}")
        return 0, warnings

    target_root_elem = tree.getroot()
    target_layer = find_density_fruits_foliage_multilayer(target_root_elem)
    if target_layer is None:
        warnings.append(f"No densityMap_fruits FoliageMultiLayer found in target i3d: {rel_to_root(target_i3d, target_root)}")
        return 0, warnings

    if foliage_layer_has_crop(target_layer, crop_l):
        return 0, warnings

    files_container = find_files_container(target_root_elem)
    if files_container is None:
        warnings.append(f"No <Files> container found in target i3d: {rel_to_root(target_i3d, target_root)}")
        return 0, warnings

    used_ids = collect_i3d_numeric_ids(target_root_elem)
    new_id = next_unused_id(used_ids, preferred_start_id)

    source_ids = collect_source_ids_to_remap(source_entry, source_file_entries)
    if not source_ids:
        source_ids = {str(new_id)}

    id_map = {old_id: str(new_id) for old_id in source_ids}
    canonical_foliage_xml = get_canonical_crop_foliage_xml_for_i3d(crop, target_i3d, target_root)

    # Copy required <File> entries first, remapping fileId/id to the fresh target ID.
    for file_entry in source_file_entries:
        new_file = ET.fromstring(ET.tostring(file_entry, encoding="unicode"))
        remap_i3d_ids_and_refs(new_file, id_map)
        if canonical_foliage_xml:
            set_i3d_file_filename(new_file, canonical_foliage_xml)
        if not file_entry_already_exists(files_container, new_file):
            files_container.append(new_file)

    new_entry = ET.fromstring(ET.tostring(source_entry, encoding="unicode"))
    remap_i3d_ids_and_refs(new_entry, id_map)
    target_layer.append(new_entry)

    backup = target_i3d.with_suffix(target_i3d.suffix + ".cropporter.bak")
    if not backup.exists():
        shutil.copy2(target_i3d, backup)
    indent_xml(tree)
    tree.write(target_i3d, encoding="utf-8", xml_declaration=True)
    return 1, warnings


def find_source_foliage_layer_bundle(root: Path, crop_l: str) -> Optional[tuple[ET.Element, list[ET.Element]]]:
    for i3d_path in sorted(root.rglob("*.i3d")):
        tree = parse_xml_safely(i3d_path)
        if not tree:
            continue
        root_elem = tree.getroot()
        layer = find_density_fruits_foliage_multilayer(root_elem)
        if layer is None:
            continue
        file_lookup = collect_file_entries_by_id(root_elem)
        for elem in list(layer):
            if element_is_foliage_crop_entry(elem, crop_l):
                referenced_ids = collect_file_ids_referenced_by_element(elem)
                file_entries = [file_lookup[x] for x in referenced_ids if x in file_lookup]
                # Fallback: also copy any <File> entry whose filename mentions the crop.
                if not file_entries:
                    for file_elem in file_lookup.values():
                        filename = file_elem.attrib.get("filename") or file_elem.attrib.get("file") or ""
                        if crop_l in filename.lower():
                            file_entries.append(file_elem)
                return elem, file_entries
    return None


def get_canonical_crop_foliage_xml_for_i3d(crop: CropDefinition, target_i3d: Path, target_root: Path) -> Optional[str]:
    """Return the crop's canonical foliage XML path relative to the target i3d folder.

    The canonical path comes from the selected crop's exact fruitType node source file,
    e.g. maps/foliage/pintobean/pintobean.xml. In an i3d located at maps/mapAS.i3d,
    GIANTS file paths are usually relative to the i3d folder, so this becomes
    foliage/pintobean/pintobean.xml.
    """
    if not crop.fruit_nodes:
        return None
    rel = crop.fruit_nodes[0].relative_file.replace(chr(92), "/")
    source_abs = (target_root / rel).resolve()
    try:
        return os.path.relpath(source_abs, target_i3d.parent.resolve()).replace(chr(92), "/")
    except ValueError:
        # Different drive or odd path; fall back to stripping leading maps/ for standard map i3d placement.
        if rel.startswith("maps/"):
            return rel[len("maps/"):]
        return rel


def set_i3d_file_filename(file_elem: ET.Element, filename: str) -> None:
    if "filename" in file_elem.attrib:
        file_elem.set("filename", filename)
    elif "file" in file_elem.attrib:
        file_elem.set("file", filename)
    else:
        file_elem.set("filename", filename)


def find_source_foliage_layer_entry(root: Path, crop_l: str) -> Optional[ET.Element]:
    bundle = find_source_foliage_layer_bundle(root, crop_l)
    return bundle[0] if bundle else None


def collect_file_entries_by_id(root: ET.Element) -> dict[str, ET.Element]:
    result: dict[str, ET.Element] = {}
    for elem in root.iter():
        if local_name(elem.tag).lower() == "file":
            file_id = elem.attrib.get("fileId") or elem.attrib.get("fileID") or elem.attrib.get("id")
            if file_id:
                result[file_id] = elem
    return result


def collect_file_ids_referenced_by_element(elem: ET.Element) -> set[str]:
    ids: set[str] = set()
    for node in elem.iter():
        for key, value in node.attrib.items():
            key_l = key.lower()
            if ("fileid" in key_l or key_l in {"file", "filenameid"}) and value.isdigit():
                ids.add(value)
    return ids


def collect_source_ids_to_remap(entry: ET.Element, file_entries: list[ET.Element]) -> set[str]:
    ids: set[str] = set()
    for file_entry in file_entries:
        for key in ("fileId", "fileID", "id"):
            value = file_entry.attrib.get(key)
            if value and value.isdigit():
                ids.add(value)
    ids.update(collect_file_ids_referenced_by_element(entry))
    for node in entry.iter():
        for key, value in node.attrib.items():
            key_l = key.lower()
            if key_l in {"id", "fruitid", "foliageid", "typeid"} and value.isdigit():
                ids.add(value)
    return ids


def find_files_container(root: ET.Element) -> Optional[ET.Element]:
    for elem in root.iter():
        if local_name(elem.tag).lower() == "files":
            return elem
    return None


def remap_i3d_ids_and_refs(elem: ET.Element, id_map: dict[str, str]) -> None:
    for node in elem.iter():
        for key, value in list(node.attrib.items()):
            if value in id_map:
                node.set(key, id_map[value])


def file_entry_already_exists(files_container: ET.Element, new_file: ET.Element) -> bool:
    new_filename = (new_file.attrib.get("filename") or new_file.attrib.get("file") or "").replace(chr(92), "/").lower()
    new_file_id = new_file.attrib.get("fileId") or new_file.attrib.get("fileID") or new_file.attrib.get("id")
    for elem in list(files_container):
        if local_name(elem.tag).lower() != "file":
            continue
        filename = (elem.attrib.get("filename") or elem.attrib.get("file") or "").replace(chr(92), "/").lower()
        file_id = elem.attrib.get("fileId") or elem.attrib.get("fileID") or elem.attrib.get("id")
        if new_filename and filename == new_filename:
            return True
        if new_file_id and file_id == new_file_id:
            return True
    return False


def find_primary_target_i3d_for_fruits(root: Path) -> Optional[Path]:
    candidates: list[Path] = []
    for i3d_path in sorted(root.rglob("*.i3d")):
        tree = parse_xml_safely(i3d_path)
        if not tree:
            continue
        if find_density_fruits_foliage_multilayer(tree.getroot()) is not None:
            candidates.append(i3d_path)
    if not candidates:
        return None
    def score(path: Path) -> tuple[int, int]:
        rel = rel_to_root(path, root).lower().replace(chr(92), "/")
        preferred = 0 if rel.startswith("maps/map") else 1
        return (preferred, len(rel))
    return sorted(candidates, key=score)[0]


def find_density_fruits_foliage_multilayer(root: ET.Element) -> Optional[ET.Element]:
    file_ids: dict[str, str] = {}
    fruit_file_ids: set[str] = set()
    for elem in root.iter():
        if local_name(elem.tag).lower() == "file":
            file_id = elem.attrib.get("fileId") or elem.attrib.get("fileID") or elem.attrib.get("id")
            filename = elem.attrib.get("filename") or elem.attrib.get("file") or ""
            if file_id and filename:
                file_ids[file_id] = filename
                if "densitymap_fruits" in filename.lower():
                    fruit_file_ids.add(file_id)

    for elem in root.iter():
        if local_name(elem.tag).lower() != "foliagemultilayer":
            continue
        attrs = dict(elem.attrib)
        direct_hit = any("densitymap_fruits" in value.lower() for value in attrs.values())
        id_hit = any(value in fruit_file_ids for value in attrs.values())
        if direct_hit or id_hit:
            return elem
    return None


def element_is_foliage_crop_entry(elem: ET.Element, crop_l: str) -> bool:
    tag_l = local_name(elem.tag).lower()
    if "foliage" not in tag_l and "fruit" not in tag_l:
        return False
    for key, value in elem.attrib.items():
        key_l = key.lower()
        value_l = value.lower()
        if key_l in {"name", "fruitname", "fruittypename", "type", "fruit"} and value_l == crop_l:
            return True
        if key_l in {"name", "filename", "file", "xmlfilename"} and crop_l in value_l:
            return True
    return False


def foliage_layer_has_crop(layer: ET.Element, crop_l: str) -> bool:
    for elem in list(layer):
        if element_is_foliage_crop_entry(elem, crop_l):
            return True
    return False


def collect_i3d_numeric_ids(root: ET.Element) -> set[int]:
    used: set[int] = set()
    id_attr_names = {"id", "nodeid", "shapeid", "fileid", "materialid", "fruitid", "foliageid", "foliageid", "typeid"}
    for elem in root.iter():
        for key, value in elem.attrib.items():
            if key.lower() in id_attr_names and value.isdigit():
                used.add(int(value))
    return used


def next_unused_id(used: set[int], start: int = 5000) -> int:
    candidate = start
    while candidate in used:
        candidate += 1
    return candidate


def remap_foliage_entry_ids(elem: ET.Element, new_id: int) -> None:
    id_attr_names = {
        "id", "fruitid", "fruitId", "fruitID", "foliageid", "foliageId", "foliageID",
        "typeid", "typeId", "typeID"
    }
    for node in elem.iter():
        for key in list(node.attrib.keys()):
            if key in id_attr_names or key.lower() in {x.lower() for x in id_attr_names}:
                if node.attrib[key].isdigit():
                    node.set(key, str(new_id))


def cmd_patch_i3d_foliage_layer(args: argparse.Namespace) -> int:
    source = prepare_map_input(Path(args.source))
    target = prepare_map_input(Path(args.target))
    try:
        if target.is_temp:
            raise CropPorterError("patch-i3d-foliage-layer requires a folder target, not a ZIP.")
        scan_profile(source)
        crop_key = args.crop.lower()
        crop = source.crop_defs.get(crop_key) or CropDefinition(fruit_name=crop_key)
        patched, warnings = patch_i3d_foliage_layer_for_crop(source, Path(target.root), crop, preferred_start_id=args.start_id)
        for warning in warnings:
            print(f"Warning: {warning}")
        if patched:
            print(f"Patched {patched} i3d foliage layer entry for crop '{crop.fruit_name}'.")
            return 0
        print(f"No i3d foliage layer entry patched for crop '{crop.fruit_name}'.")
        return 1 if warnings else 0
    finally:
        source.cleanup()
        target.cleanup()


def cmd_patch_density_config(args: argparse.Namespace) -> int:
    profile = prepare_map_input(Path(args.map))
    try:
        if profile.is_temp:
            raise CropPorterError("patch-density-config requires a folder target, not a ZIP. Run it against an extracted/patched map folder.")

        root = Path(profile.root)
        changed_files = 0
        patched_layers = 0
        backups = 0

        for i3d_path in sorted(root.rglob("*.i3d")):
            tree = parse_xml_safely(i3d_path)
            if not tree:
                continue

            file_ids: dict[str, str] = {}
            fruit_file_ids: set[str] = set()

            for elem in tree.getroot().iter():
                if local_name(elem.tag).lower() == "file":
                    file_id = elem.attrib.get("fileId") or elem.attrib.get("fileID") or elem.attrib.get("id")
                    filename = elem.attrib.get("filename") or elem.attrib.get("file") or ""
                    if file_id and filename:
                        file_ids[file_id] = filename
                        if "densitymap_fruits" in filename.lower():
                            fruit_file_ids.add(file_id)

            file_changed = False
            for elem in tree.getroot().iter():
                if local_name(elem.tag).lower() != "foliagemultilayer":
                    continue
                attrs = dict(elem.attrib)
                direct_hit = any("densitymap_fruits" in value.lower() for value in attrs.values())
                id_hit = any(value in fruit_file_ids for value in attrs.values())
                if not direct_hit and not id_hit:
                    continue

                before = dict(elem.attrib)
                elem.set("numChannels", str(args.num_channels))
                elem.set("numTypeIndexChannels", str(args.num_type_index_channels))
                elem.set("compressionChannels", str(args.compression_channels))
                if elem.attrib != before:
                    patched_layers += 1
                    file_changed = True

            if file_changed:
                backup = i3d_path.with_suffix(i3d_path.suffix + ".cropporter.bak")
                if not backup.exists():
                    shutil.copy2(i3d_path, backup)
                    backups += 1
                indent_xml(tree)
                tree.write(i3d_path, encoding="utf-8", xml_declaration=True)
                changed_files += 1
                print(f"Patched: {rel_to_root(i3d_path, root)}")

        if changed_files == 0:
            print("No FoliageMultiLayer densityMap_fruits config was patched.")
            print("This usually means the density map reference is indirect in a way the patcher has not learned yet.")
            return 1

        print(f"Patched {patched_layers} FoliageMultiLayer layer(s) in {changed_files} i3d file(s); created {backups} backup(s).")
        print("Important: this changes the i3d channel config only. If the density map image/channel data needs conversion, the game may still complain.")
        return 0
    finally:
        profile.cleanup()


def patch_density_foliage_multilayer_text(text: str, num_channels: int, num_type_index_channels: int, compression_channels: int) -> str:
    # Retained for compatibility with older notes, but no longer used by cmd_patch_density_config.
    def patch_match(match: re.Match) -> str:
        chunk = match.group(0)
        if "densityMap_fruits" not in chunk:
            return chunk
        chunk = set_xml_attr_in_text(chunk, "numChannels", str(num_channels))
        chunk = set_xml_attr_in_text(chunk, "numTypeIndexChannels", str(num_type_index_channels))
        chunk = set_xml_attr_in_text(chunk, "compressionChannels", str(compression_channels))
        return chunk
    return re.sub(r"<FoliageMultiLayer[^>]*>", patch_match, text)


def set_xml_attr_in_text(chunk: str, attr: str, value: str) -> str:
    pattern = r'' + re.escape(attr) + r'="[^"]*"'
    replacement = f'{attr}="{value}"'
    if re.search(pattern, chunk):
        return re.sub(pattern, replacement, chunk)
    if chunk.endswith("/>"):
        return chunk[:-2] + f' {replacement}/>'
    return chunk[:-1] + f' {replacement}>'


def parse_int_attr(attrs: dict[str, str], key: str) -> Optional[int]:
    for attr_key, value in attrs.items():
        if attr_key.lower() == key.lower():
            try:
                return int(value)
            except ValueError:
                return None
    return None


def find_keyword_references(root: Path, keywords: list[str], max_hits_per_file: int = 8) -> dict[str, list[tuple[int, str, str]]]:
    hits: dict[str, list[tuple[int, str, str]]] = {}
    lowered = [(k, k.lower()) for k in keywords]
    for path in root.rglob("*.xml"):
        rel = rel_to_root(path, root)
        file_hits: list[tuple[int, str, str]] = []
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, start=1):
            line_l = line.lower()
            for original, keyword_l in lowered:
                if keyword_l in line_l:
                    cleaned = line.strip()
                    if len(cleaned) > 180:
                        cleaned = cleaned[:177] + "..."
                    file_hits.append((idx, original, cleaned))
                    break
            if len(file_hits) >= max_hits_per_file:
                break
        if file_hits:
            hits[rel] = file_hits
    return hits


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"FS25_CropPorter {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    selftest = sub.add_parser("selftest", help="Run small internal checks for matching logic.")
    selftest.set_defaults(func=cmd_selftest)

    probe = sub.add_parser("probe-map", help="Probe a map for crop-system XML references and likely integration points.")
    probe.add_argument("map", help="Map folder or ZIP to inspect.")
    probe.add_argument("--keywords", nargs="+", help="Optional keywords to search for in XML files.")
    probe.add_argument("--max-hits", type=int, default=8, help="Maximum keyword hits shown per XML file.")
    probe.add_argument("--verbose", action="store_true", help="List every detected XML role file.")
    probe.set_defaults(func=cmd_probe_map)

    probe_crop = sub.add_parser("probe-crop", help="Inspect one detected crop and its extracted dependencies.")
    probe_crop.add_argument("map", help="Map folder or ZIP to inspect.")
    probe_crop.add_argument("crop", help="Crop fruitType name, e.g. blackbean.")
    probe_crop.set_defaults(func=cmd_probe_crop)

    probe_registry = sub.add_parser("probe-fruit-registry", help="Show the primary maps_fruitTypes.xml child structure.")
    probe_registry.add_argument("map", help="Map folder or ZIP to inspect.")
    probe_registry.set_defaults(func=cmd_probe_fruit_registry)

    fix_registry = sub.add_parser("fix-fruit-registry", help="Ensure an imported crop foliage XML is referenced as a direct <fruitType filename=...> entry.")
    fix_registry.add_argument("map", help="Extracted/patched map folder to modify. ZIP input is refused.")
    fix_registry.add_argument("--filename", default="maps/foliage/blackbean/blackbean.xml", help="Foliage XML reference to place directly under <fruitTypes>.")
    fix_registry.set_defaults(func=cmd_fix_fruit_registry)

    fix_filltype_case = sub.add_parser("fix-crop-filltype-case", help="Rewrite lower/upper-case fillType references inside an imported crop foliage XML.")
    fix_filltype_case.add_argument("map", help="Extracted/patched map folder to modify. ZIP input is refused.")
    fix_filltype_case.add_argument("--foliage-xml", default="maps/foliage/blackbean/blackbean.xml", help="Imported foliage XML to patch.")
    fix_filltype_case.add_argument("--old", default="blackbean", help="Old fillType reference value. Default: blackbean.")
    fix_filltype_case.add_argument("--new", default="BLACKBEAN", help="New fillType reference value. Default: BLACKBEAN.")
    fix_filltype_case.set_defaults(func=cmd_fix_crop_filltype_case)

    fix_filltype_registry = sub.add_parser("fix-filltype-registry-name", help="Rename an imported fillType in the target maps_fillTypes.xml registry.")
    fix_filltype_registry.add_argument("map", help="Extracted/patched map folder to modify. ZIP input is refused.")
    fix_filltype_registry.add_argument("--old", default="BLACKBEAN", help="Existing fillType name. Default: BLACKBEAN.")
    fix_filltype_registry.add_argument("--new", default="blackbean", help="New fillType name. Default: blackbean.")
    fix_filltype_registry.set_defaults(func=cmd_fix_filltype_registry_name)

    patch_l10n = sub.add_parser("patch-l10n", help="Add minimal English l10n entries for an imported crop.")
    patch_l10n.add_argument("map", help="Extracted/patched map folder to modify. ZIP input is refused.")
    patch_l10n.add_argument("crop", help="Crop fruitType name, e.g. blackbean.")
    patch_l10n.add_argument("--label", help="Display label, e.g. 'Black Beans'.")
    patch_l10n.set_defaults(func=cmd_patch_l10n)

    probe_density = sub.add_parser("probe-density", help="Inspect map.i3d densityMap_fruits channel/index settings.")
    probe_density.add_argument("map", help="Map folder or ZIP to inspect.")
    probe_density.add_argument("--add", type=int, default=0, help="Number of new fruitTypes you plan to add.")
    probe_density.add_argument("--include-fruits", action="store_true", help="List detected fruitTypes in the report.")
    probe_density.set_defaults(func=cmd_probe_density)

    patch_layer = sub.add_parser("patch-i3d-foliage-layer", help="Copy a crop FoliageMultiLayer terrain entry from source map.i3d to target map.i3d and remap IDs.")
    patch_layer.add_argument("--source", required=True, help="Source map folder or ZIP containing the crop terrain layer entry.")
    patch_layer.add_argument("--target", required=True, help="Extracted/patched target map folder to modify. ZIP input is refused for target.")
    patch_layer.add_argument("--crop", required=True, help="Crop fruitType name, e.g. blackbean.")
    patch_layer.add_argument("--start-id", type=int, default=5000, help="First ID to try when remapping fruitId/foliageId. Default: 5000.")
    patch_layer.set_defaults(func=cmd_patch_i3d_foliage_layer)

    patch_density = sub.add_parser("patch-density-config", help="Patch densityMap_fruits FoliageMultiLayer channel settings in an extracted map folder.")
    patch_density.add_argument("map", help="Extracted/patched map folder to modify. ZIP input is refused.")
    patch_density.add_argument("--num-channels", type=int, default=11, help="New numChannels value. Default: 11.")
    patch_density.add_argument("--num-type-index-channels", type=int, default=6, help="New numTypeIndexChannels value. Default: 6.")
    patch_density.add_argument("--compression-channels", type=int, default=5, help="New compressionChannels value. Default: 5.")
    patch_density.set_defaults(func=cmd_patch_density_config)

    scan = sub.add_parser("scan-source", help="Scan a source map/mod and list detected crops.")
    scan.add_argument("source", help="Source map folder or ZIP.")
    scan.add_argument("--include-basegame", action="store_true", help="Include basegame fruit names in output.")
    scan.add_argument("--output", help="Optional output folder for JSON scan report.")
    scan.set_defaults(func=cmd_scan_source)

    pre = sub.add_parser("preflight", help="Compare selected source crops against a target map.")
    pre.add_argument("--source", required=True, help="Source map folder or ZIP.")
    pre.add_argument("--target", required=True, help="Target map folder or ZIP.")
    pre.add_argument("--crops", nargs="+", required=True, help="Crop fruitType names to import, e.g. coffee blackbean.")
    pre.add_argument("--output", default="CropPorter_Output", help="Output folder for reports.")
    pre.set_defaults(func=cmd_preflight)

    apply = sub.add_parser("apply", help="Create a patched copy of the target map folder.")
    apply.add_argument("--source", required=True, help="Source map folder or ZIP.")
    apply.add_argument("--target", required=True, help="Target map folder or ZIP.")
    apply.add_argument("--crops", nargs="+", required=True, help="Crop fruitType names to import, e.g. coffee blackbean.")
    apply.add_argument("--output", required=True, help="Output folder for patched target map.")
    apply.add_argument("--force", action="store_true", help="Apply even if preflight reports errors. Not recommended.")
    apply.set_defaults(func=cmd_apply)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CropPorterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
