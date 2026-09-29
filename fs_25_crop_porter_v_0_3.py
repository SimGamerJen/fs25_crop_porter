#!/usr/bin/env python3
"""
FS25_CropPorter v0.3-alpha

A conservative crop migration assistant for Farming Simulator 25 maps.

v0.2 adds experimental inspection support for plantation/vine-style crops such as row-planted coffee.

What this version does:
- Opens FS25 map folders or ZIP files.
- Scans source and target XMLs for fruitTypes, fillTypes, densityMapHeightTypes,
  growth/season entries, and obvious crop asset references.
- Builds a preflight report for selected crops.
- Creates a patched copy of the target map folder, injecting selected XML nodes where safe.
- Copies referenced local assets into an isolated CropPorter folder when they can be resolved.
- Generates Markdown and JSON reports.

What this version deliberately does NOT do:
- It can convert densityMap_fruits.gdm/GRLE to PNG with GIANTS grleConverter when density capacity must expand.
- It can expand map.i3d fruit-density type-index capacity while preserving compressionChannels.
- It synchronizes densityMapHeightTypes with terrainDetailHeight and repacks GIANTS RGB8/gray16 PNG height data when type-index bits expand.
- It does not patch vehicle/tool compatibility.
- It does not patch sell points/contracts/economy intelligently.
- It only modifies a savegame when the explicit migrate-save command is used; that command creates a backup before replacement.

Recommended workflow:
1. Run scan-source.
2. Run preflight.
3. Run apply into a new output folder.
4. Test the patched map in a disposable savegame.
5. If an established save predates a height-channel expansion, run migrate-save before opening it against the patched map.

Example:
    python cropporter.py scan-source "D:/FS25_Mods/FS25_NewGloriaBrazil.zip"
    python cropporter.py preflight --source "D:/FS25_Mods/FS25_NewGloriaBrazil.zip" --target "D:/FS25_Mods/FS25_TargetMap.zip" --crops coffee blackbean
    python cropporter.py apply --source "D:/FS25_Mods/FS25_NewGloriaBrazil.zip" --target "D:/FS25_Mods/FS25_TargetMap.zip" --crops coffee blackbean --output "D:/FS25_Work/FS25_TargetMap_CropPorted"
"""

from __future__ import annotations

import argparse
import binascii
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET


VERSION = "0.3-alpha"
BUILD_MARKER = "supplemental-height-registry-pipeline-v17"
CROPPORTER_FOLDER = "maps/cropporter/imported"

# Proven Brazil/Estancia density-map expansion workflow.  The user keeps the
# GIANTS GRLE Converter here; callers can override it with --grle-converter.
DEFAULT_GRLE_CONVERTER = r"E:\FarmSim_Content\grleConverter_7.0.2_win\convert.cmd"

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
    "shapeSource", "shapesource", "shapeFilename", "shapefilename",
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

        if tag in {"fruittype", "fruit"} and any(
            k in attrs_l for k in ("name", "fruittype", "fruittypename", "filename", "file")
        ):
            # Central map descriptors can keep the active fruit registry inline as
            # <fruitTypes><fruitType filename="..."/></fruitTypes>.  Filename-only
            # entries are therefore registry evidence just as named definitions are.
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


def collect_target_known_fill_type_names(target_root: Path, fill_target_file: Optional[str] = None) -> set[str]:
    """Collect fillType names already available to the target before import.

    A vanilla map may omit ``<fillTypes>`` entirely and rely on FS25's implicit
    base-game registry. In that case the local scanner cannot see definitions such
    as STRAW. Existing densityMapHeightTypes are a useful authoritative signal that
    those fillTypes are already valid on the target, so include them when
    de-duplicating imported fillType nodes.
    """
    names: set[str] = set()

    if fill_target_file:
        path = target_root / fill_target_file
        tree = parse_xml_safely(path)
        if tree:
            names.update(extract_fill_names_from_fill_xml(tree))

    for path in target_root.rglob("*.xml"):
        rel_l = rel_to_root(path, target_root).replace(chr(92), "/").lower()
        if "densitymapheighttypes" not in rel_l:
            continue
        tree = parse_xml_safely(path)
        if tree:
            names.update(extract_height_names_from_height_xml(tree))

    return {name.upper() for name in names if name}


def collect_target_existing_height_type_names(target_root: Path, height_target_file: Optional[str]) -> set[str]:
    if not height_target_file:
        return set()
    tree = parse_xml_safely(target_root / height_target_file)
    return extract_height_names_from_height_xml(tree) if tree else set()


def scan_profile(profile: MapProfile) -> MapProfile:
    discover_xml_files(profile)
    root = Path(profile.root)

    for rel in profile.xml_files.get("fruit_types", []):
        tree = parse_xml_safely(root / rel)
        if tree:
            profile.fruit_names.update(extract_fruit_names_from_fruit_xml(tree))

    for rel in profile.xml_files.get("fill_types", []):
        path = root / rel
        # Broad role discovery intentionally sees <fillType> in bale/placeable XMLs.
        # Only a structural fillTypes registry defines the map's canonical fillTypes.
        if not _looks_like_registry_xml(path, "fill_types"):
            continue
        tree = parse_xml_safely(path)
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
        path = root / rel
        if not _looks_like_registry_xml(path, "fill_types"):
            continue
        tree = parse_xml_safely(path)
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

        if candidate.suffix.lower() in {".xml", ".i3d"}:
            tree = parse_xml_safely(candidate)
            if tree:
                # A map-level i3d is a semantic template, not an asset dependency
                # root.  Its global <Files> table references most/all map resources.
                # Following those references made a single crop export balloon to
                # ~1000 assets.  Keep the template itself, but never recurse through
                # its global file table.  Crop-local foliage i3ds do not contain the
                # densityMap_fruits FoliageMultiLayer and remain fully traversable.
                if candidate.suffix.lower() == ".i3d" and find_density_fruits_foliage_multilayer(tree.getroot()) is not None:
                    continue
                for elem in tree.getroot().iter():
                    for path_ref in collect_path_attrs(elem):
                        resolved = resolve_asset_path(candidate.parent, path_ref) or resolve_asset_path(root, path_ref)
                        if resolved and resolved.exists():
                            try:
                                queue.append(rel_to_root(resolved, root))
                            except ValueError:
                                # Never package arbitrary files outside the source map root.
                                continue
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
        f"Source: `{report.get('source') or report.get('package') or 'package'}`",
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
        f"Source: `{report.get('source')}`",        f"Target: `{report.get('target')}`",
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


def _looks_like_registry_xml(path: Path, role: str) -> bool:
    """Return True only for XML files that structurally act as map registries.

    Broad content classification intentionally finds crop/fill references in many
    files (selling points, placeables, productions, etc.). Those files must never
    become the target map's primary fruit/fill registry merely because they contain
    <fillType> or <fruitType> nodes.
    """
    tree = parse_xml_safely(path)
    if not tree:
        return False
    root = tree.getroot()
    wanted = {
        "fruit_types": "fruittypes",
        "fill_types": "filltypes",
        "height_types": "densitymapheighttypes",
    }.get(role)
    if not wanted:
        return True
    root_tag = local_name(root.tag).lower()
    if root_tag == wanted:
        return True
    if root_tag == "map":
        return any(local_name(child.tag).lower() == wanted for child in list(root))
    return False


def find_primary_xml_file(profile: MapProfile, role: str) -> Optional[str]:
    files = profile.xml_files.get(role, [])
    if not files:
        return None

    if role in {"fruit_types", "fill_types"}:
        structural = [rel for rel in files if _looks_like_registry_xml(Path(profile.root) / rel, role)]
        if structural:
            files = structural
        elif role == "fill_types":
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



_L10N_REF_PATTERN = re.compile(r"\$l10n_([A-Za-z0-9_.-]+)")


def collect_l10n_keys_from_refs(refs: Iterable[XmlNodeRef]) -> set[str]:
    """Collect the exact, case-sensitive l10n keys referenced by XML nodes."""
    keys: set[str] = set()
    for ref in refs:
        keys.update(_L10N_REF_PATTERN.findall(ref.xml_text or ""))
        for value in ref.attrs.values():
            keys.update(_L10N_REF_PATTERN.findall(value or ""))
    return keys


def extract_l10n_text_value(elem: ET.Element, lang: str) -> Optional[str]:
    """Read a language value without normalising its key or wording."""
    value = elem.attrib.get("text")
    if value is not None:
        return value
    lang_l = lang.lower()
    for child in list(elem):
        if local_name(child.tag).lower() == lang_l and child.text is not None:
            return child.text
    if elem.text and elem.text.strip():
        return elem.text.strip()
    return None


def collect_source_l10n_entries(source_root: Path, wanted_keys: set[str]) -> tuple[dict[str, dict[str, str]], set[str]]:
    """Return exact source translations for the requested l10n keys.

    Keys are intentionally case-sensitive.  FS maps commonly use names such as
    fillType_fieldGrass, fillType_greenRye and fillType_catchCrop; generating keys
    from an upper/lower-cased fruitType silently breaks those crops in the PDA.
    """
    wanted = {x for x in wanted_keys if x}
    by_lang: dict[str, dict[str, str]] = {}
    found: set[str] = set()

    for rel in probe_l10n_files(source_root):
        path = source_root / rel
        tree = parse_xml_safely(path)
        if not tree:
            continue
        lang = extract_l10n_suffix(path.name)
        additions = by_lang.setdefault(lang, {})
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "text":
                continue
            name = elem.attrib.get("name")
            if not name or name not in wanted:
                continue
            value = extract_l10n_text_value(elem, lang)
            if value is None:
                continue
            additions[name] = value
            found.add(name)

    # Some older maps keep translations directly in modDesc.xml instead of l10n_*.xml.
    moddesc = source_root / "modDesc.xml"
    tree = parse_xml_safely(moddesc) if moddesc.exists() else None
    if tree:
        additions = by_lang.setdefault("en", {})
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "text":
                continue
            name = elem.attrib.get("name")
            if not name or name not in wanted or name in additions:
                continue
            value = extract_l10n_text_value(elem, "en")
            if value is not None:
                additions[name] = value
                found.add(name)

    return {lang: values for lang, values in by_lang.items() if values}, found


def collect_crop_family_l10n_keys(source: MapProfile, crop: CropDefinition) -> set[str]:
    """Collect exact l10n keys required by every fruitType/fillType in a crop family."""
    source_root = Path(source.root)
    family_names = {x.lower() for x in source_crop_family(source, crop)}
    if not family_names:
        family_names = {crop.fruit_name.lower()}

    refs: list[XmlNodeRef] = []
    for family_name in sorted(family_names):
        family_crop = source.crop_defs.get(family_name)
        if family_crop is None:
            continue
        refs.extend(family_crop.fruit_nodes)
        refs.extend(family_crop.growth_nodes)
        refs.extend(family_crop.other_nodes)

    fill_names = collect_package_fill_type_names(source_root, {x.upper() for x in family_names}, crop)
    refs.extend(collect_refs_for_fill_types(source_root, fill_names))
    refs.extend(collect_refs_for_height_types(source_root, fill_names))
    return collect_l10n_keys_from_refs(refs)


def patch_l10n_from_source_for_crop_family(source: MapProfile, target_root: Path, crop: CropDefinition) -> tuple[int, list[str], list[str]]:
    """Patch the target with exact source l10n keys/values for the crop family."""
    warnings: list[str] = []
    keys = collect_crop_family_l10n_keys(source, crop)
    if not keys:
        return 0, [f"No l10n references were found for crop family '{crop.fruit_name}'."], []

    additions_by_lang, found = collect_source_l10n_entries(Path(source.root), keys)
    # Referenced keys absent from the source translation files are often deliberate
    # base-game keys (for example fillType_straw/fillType_grass).  Do not manufacture
    # or warn on those individually; only fail to contribute l10n when none of the
    # crop-family keys are supplied by the source map.
    if not additions_by_lang:
        return 0, warnings + [f"No matching source l10n entries were found for crop family '{crop.fruit_name}'."], sorted(keys)

    moddesc = target_root / "modDesc.xml"
    prefix = get_or_create_moddesc_l10n_prefix(moddesc) if moddesc.exists() else None
    inserted = 0
    if prefix:
        for lang, additions in sorted(additions_by_lang.items()):
            inserted += patch_l10n_file(target_root / f"{prefix}_{lang}.xml", additions, lang)
    else:
        # Keep the legacy fallback only for maps without a usable l10n prefix.
        for lang, additions in sorted(additions_by_lang.items()):
            path = find_l10n_file(target_root, lang) or create_l10n_file(target_root, lang)
            inserted += patch_l10n_file(path, additions, lang)
    return inserted, warnings, sorted(keys)


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


def patch_l10n_file(path: Path, additions: dict[str, str], lang: str) -> int:
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
        return 0

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
    return inserted


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


def collect_runtime_asset_dependencies(source_root: Path, crop: CropDefinition) -> set[str]:
    """Collect local runtime files reachable from a crop's actual foliage files.

    This walk preserves the directory context of every XML/I3D reference. That is
    essential for sibling references such as ``clover/../alfalfa/...``,
    ``mustard/../canola/distance/...`` and ``silageMaize/../maize/distance/...``.

    The older generic ``expand_asset_references`` queue stored only the raw reference
    text, so a relative path such as ``../alfalfa/meadowUS_diffuse.png`` could lose
    the parent file that made the reference meaningful after package namespace/layout
    adaptation. The package could therefore contain the dependency while apply failed
    to copy it into the target.
    """
    source_root = Path(source_root).resolve()
    discovered: set[str] = set()
    queue: list[Path] = []
    queued: set[str] = set()

    def enqueue_path(path: Optional[Path]) -> None:
        if path is None or not path.exists() or not path.is_file():
            return
        try:
            rel = rel_to_root(path.resolve(), source_root).replace(chr(92), "/")
        except ValueError:
            return
        key = rel.lower()
        if key in queued:
            return
        queued.add(key)
        queue.append(path.resolve())

    # Seed with the complete crop foliage directory because this is the authoritative
    # runtime root for a field crop after package layout adaptation.
    for rel in collect_crop_foliage_tree(source_root, crop):
        enqueue_path(source_root / rel)

    # Also retain any already-discovered assets (HUDs, fill-plane textures, etc.).
    for raw in crop.asset_paths:
        if not raw or raw.startswith("$"):
            continue
        enqueue_path(resolve_asset_path(source_root, raw))

    while queue:
        current = queue.pop(0)
        try:
            rel = rel_to_root(current, source_root).replace(chr(92), "/")
        except ValueError:
            continue
        discovered.add(rel)

        if current.suffix.lower() not in {".xml", ".i3d"}:
            continue

        tree = parse_xml_safely(current)
        if not tree:
            continue

        # A primary map I3D is a semantic template with a global Files table. Never
        # recurse through it or a single crop would pull most of the source map.
        if current.suffix.lower() == ".i3d" and find_density_fruits_foliage_multilayer(tree.getroot()) is not None:
            continue

        for elem in tree.getroot().iter():
            for path_ref in collect_path_attrs(elem):
                if not path_ref or path_ref.startswith("$"):
                    continue
                hit = resolve_asset_path(current.parent, path_ref) or resolve_asset_path(source_root, path_ref)
                enqueue_path(hit)

    return discovered


def copy_assets_for_crop(source: MapProfile, output_root: Path, crop: CropDefinition) -> tuple[int, list[str]]:
    """Copy crop assets into the target map using their original relative paths.

    Preserving the original relative layout avoids needing to rewrite every internal
    XML/i3d/texture reference. Example: maps/foliage/blackbean/blackbean.xml stays
    at maps/foliage/blackbean/blackbean.xml in the patched target map.

    Runtime dependency closure is recalculated here from the actual adapted source
    files so package apply cannot silently omit sibling/shared foliage assets.
    """
    source_root = Path(source.root)
    copied = 0
    warnings: list[str] = []

    runtime_assets = set(crop.asset_paths)
    runtime_assets.update(collect_runtime_asset_dependencies(source_root, crop))
    crop.asset_paths.update(runtime_assets)

    for asset in sorted(runtime_assets):
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

        # Primary map i3d files are source templates only. They must never be
        # copied wholesale into the target by the generic asset copier; the
        # dedicated foliage-layer transplant reads them and allocates fresh IDs.
        rel_path = Path(rel)
        if rel_path.suffix.lower() == ".i3d" and rel_path.stem.lower().startswith("map"):
            parent_l = rel_path.parent.name.lower()
            if parent_l.startswith("map") or len(rel_path.parts) <= 2:
                continue

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


def snapshot_all_i3d_file_tables(target_root: Path) -> dict:
    """Capture the *actual* target i3d <Files> tables before crop integration.

    The snapshot is authoritative: CropPorter preserves only entries that really
    exist in the copied vanilla/target map. It never invents or assumes a fileId.
    Imported crop files may append new File entries, but no snapshotted File entry
    may disappear or be repointed.
    """
    tables: list[dict] = []
    for target_i3d in sorted(target_root.rglob("*.i3d")):
        tree = parse_xml_safely(target_i3d)
        if not tree:
            continue
        files_container = find_files_container(tree.getroot())
        if files_container is None:
            continue

        entries: list[dict] = []
        for index, elem in enumerate(list(files_container)):
            if local_name(elem.tag).lower() != "file":
                continue
            file_id = get_i3d_file_id(elem)
            if not file_id:
                continue
            entries.append({
                "index": index,
                "fileId": file_id,
                "attrs": dict(elem.attrib),
                "xml": ET.tostring(elem, encoding="unicode"),
            })

        # Only retain tables that actually contain File entries.
        if entries:
            tables.append({
                "relative_i3d": rel_to_root(target_i3d, target_root).replace(chr(92), "/"),
                "entries": entries,
            })
    return {"tables": tables}


def restore_all_i3d_file_tables(target_root: Path, snapshot: Optional[dict]) -> tuple[int, list[str]]:
    """Enforce append-only semantics for every snapshotted target i3d <Files> table.

    Missing original entries are restored. If an original fileId still exists but
    points to a different file, abort rather than silently corrupt the target.
    Newly appended crop File entries are retained.
    """
    if not snapshot:
        return 0, []

    restored_total = 0
    actions: list[str] = []

    for table in snapshot.get("tables", []):
        rel = str(table.get("relative_i3d") or "")
        if not rel:
            continue
        target_i3d = target_root / rel
        if not target_i3d.exists():
            raise CropPorterError(
                f"Refusing unsafe i3d output: pre-existing target i3d disappeared during apply: {rel}"
            )

        tree = parse_xml_safely(target_i3d)
        if not tree:
            raise CropPorterError(f"Could not parse target i3d during final file-table validation: {rel}")
        files_container = find_files_container(tree.getroot())
        if files_container is None:
            raise CropPorterError(f"No <Files> container found during final file-table validation: {rel}")

        current_by_id = {
            get_i3d_file_id(elem): elem
            for elem in list(files_container)
            if local_name(elem.tag).lower() == "file" and get_i3d_file_id(elem)
        }

        missing: list[tuple[int, ET.Element]] = []
        restored_here = 0

        for record in table.get("entries", []):
            file_id = str(record.get("fileId") or "")
            if not file_id:
                continue

            current = current_by_id.get(file_id)
            original_attrs = dict(record.get("attrs") or {})
            original_name = original_attrs.get("filename") or original_attrs.get("file") or ""

            if current is None:
                original = ET.fromstring(str(record["xml"]))
                missing.append((int(record.get("index", len(list(files_container)))), original))
                restored_here += 1
                actions.append(
                    f"Restored pre-existing i3d File entry in {rel}: "
                    f"{file_id} -> {original_name}."
                )
                continue

            if dict(current.attrib) != original_attrs:
                new_name = current.attrib.get("filename") or current.attrib.get("file") or ""
                raise CropPorterError(
                    f"Refusing unsafe i3d output: pre-existing fileId {file_id} in {rel} changed "
                    f"from '{original_name}' to '{new_name}'. Existing target File entries are append-only."
                )

        # Reinsert missing originals at their previous approximate positions while
        # retaining newly appended crop entries.
        for index, elem in sorted(missing, key=lambda x: x[0]):
            files_container.insert(min(index, len(list(files_container))), elem)

        if restored_here:
            indent_xml(tree)
            tree.write(target_i3d, encoding="utf-8", xml_declaration=True)
            restored_total += restored_here

    return restored_total, actions



def canonical_source_foliage_xml(crop: CropDefinition) -> Optional[str]:
    """Return the source-relative foliage XML that actually defines ``crop``.

    The crop name and the foliage *folder* are deliberately treated as separate
    concepts. For example WINTERBARLEY may be defined by
    ``foliage/barley/winterBarley.xml`` rather than a winterBarley directory.
    """
    crop_l = crop.fruit_name.lower()
    candidates: list[tuple[int, int, str]] = []

    for node in crop.fruit_nodes:
        rel = node.relative_file.replace(chr(92), "/").strip("/")
        rel_l = rel.lower()
        parts_l = [part.lower() for part in Path(rel).parts]
        if "foliage" not in parts_l:
            continue

        score = 0
        base_l = Path(rel).name.lower()

        if base_l == f"{crop_l}.xml":
            score += 200
        if f"/foliage/{crop_l}/" in f"/{rel_l}":
            score += 80
        if crop_l in Path(rel).stem.lower():
            score += 40

        if "/config/" in f"/{rel_l}" or base_l in {
            "map.xml", "mapeu.xml", "mapus.xml", "mapas.xml",
            "maps_fruittypes.xml", "map_fruittypes.xml",
        }:
            score -= 500

        candidates.append((score, -len(rel), rel))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    best_score, _neg_len, best_rel = candidates[0]
    return best_rel if best_score > 0 else None


def collect_source_foliage_layout(
    source: MapProfile,
    crops: list[str],
    source_foliage_root: Optional[str],
    target_foliage_root: Optional[str],
) -> dict[str, str]:
    """Map each fruitType in the selected source family to its target foliage XML.

    This preserves the source directory relationship. A crop family is allowed to
    have multiple fruitTypes in one asset directory.
    """
    layout: dict[str, str] = {}

    for selected_name in [c.lower() for c in crops]:
        selected = source.crop_defs.get(selected_name)
        if selected is None:
            continue

        family = set(source_crop_family(source, selected))
        family.add(selected_name)

        for family_name in sorted(family):
            family_crop = source.crop_defs.get(family_name.lower())
            if family_crop is None:
                continue
            source_rel = canonical_source_foliage_xml(family_crop)
            if not source_rel:
                continue
            target_rel = remap_foliage_asset_path(
                source_rel,
                source_foliage_root,
                target_foliage_root,
            )
            layout[family_name.lower()] = target_rel.replace(chr(92), "/").strip("/")

    return layout


def enforce_source_foliage_layout(
    source: MapProfile,
    output_root: Path,
    crops: list[str],
    source_foliage_root: Optional[str],
    target_foliage_root: Optional[str],
) -> tuple[int, list[str], list[str]]:
    """Make the target foliage XML layout match the source family's layout.

    This is a final reconciliation pass. Earlier integration stages may discover a
    fruitType independently and accidentally place its XML under a directory named
    after the fruitType. That is invalid for shared families such as
    BARLEY/WINTERBARLEY and WHEAT/WINTERWHEAT.

    Only exact same-basename foliage XML candidates are moved, and byte-different
    duplicates are never deleted automatically.
    """
    layout = collect_source_foliage_layout(
        source, crops, source_foliage_root, target_foliage_root
    )
    if not layout:
        return 0, [], []

    actions: list[str] = []
    warnings: list[str] = []
    changes = 0

    foliage_root_rel = (target_foliage_root or "").replace(chr(92), "/").strip("/")
    foliage_root_abs = output_root / foliage_root_rel if foliage_root_rel else output_root

    # bad root-relative path (lower-case key) -> correct root-relative path
    relocated_paths: dict[str, str] = {}

    for fruit_name, expected_rel in sorted(layout.items()):
        expected = output_root / expected_rel
        expected_name_l = expected.name.lower()

        candidates: list[Path] = []
        if foliage_root_abs.exists():
            for path in foliage_root_abs.rglob("*.xml"):
                if path.is_file() and path.name.lower() == expected_name_l:
                    candidates.append(path)

        expected_resolved = expected.resolve()
        misplaced = [p for p in candidates if p.resolve() != expected_resolved]

        if not expected.exists() and misplaced:
            preferred = [
                p for p in misplaced
                if p.parent.name.lower() == fruit_name.lower()
            ]
            source_path = preferred[0] if len(preferred) == 1 else (
                misplaced[0] if len(misplaced) == 1 else None
            )

            if source_path is None:
                warnings.append(
                    f"Could not safely choose a misplaced '{expected.name}' for "
                    f"'{fruit_name}'; expected source layout is '{expected_rel}'."
                )
            else:
                old_rel = source_path.relative_to(output_root).as_posix()
                expected.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_path), str(expected))
                relocated_paths[old_rel.lower()] = expected_rel
                changes += 1
                actions.append(
                    f"Restored shared foliage layout for '{fruit_name}': "
                    f"{old_rel} -> {expected_rel}"
                )

                parent = source_path.parent
                while parent != foliage_root_abs and parent.exists():
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

        elif expected.exists():            for duplicate in misplaced:
                try:
                    same = files_are_same(expected, duplicate)
                except OSError:
                    same = False
                if not same:
                    warnings.append(
                        f"Found a byte-different duplicate for '{fruit_name}': "
                        f"{duplicate.relative_to(output_root).as_posix()}; kept both."
                    )
                    continue

                old_rel = duplicate.relative_to(output_root).as_posix()
                duplicate.unlink()
                relocated_paths[old_rel.lower()] = expected_rel
                changes += 1
                actions.append(
                    f"Removed duplicate shared foliage XML for '{fruit_name}': "
                    f"{old_rel}; canonical path is {expected_rel}"
                )
                parent = duplicate.parent
                while parent != foliage_root_abs and parent.exists():
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent

    if not relocated_paths:
        return changes, actions, warnings

    textual_exts = {".xml", ".i3d"}
    for config_path in output_root.rglob("*"):
        if not config_path.is_file() or config_path.suffix.lower() not in textual_exts:
            continue

        tree = parse_xml_safely(config_path)
        if not tree:
            continue

        dirty = False
        for elem in tree.getroot().iter():
            for key, value in list(elem.attrib.items()):
                if not isinstance(value, str) or not value or value.startswith("$"):
                    continue
                norm = value.replace(chr(92), "/").strip()

                for old_rel_l, new_rel in relocated_paths.items():
                    old_abs = output_root / old_rel_l
                    new_abs = output_root / new_rel

                    if norm.strip("/").lower() == old_rel_l:
                        elem.set(key, new_rel)
                        dirty = True
                        break

                    try:
                        old_local = Path(
                            os.path.relpath(old_abs, config_path.parent)
                        ).as_posix()
                        new_local = Path(
                            os.path.relpath(new_abs, config_path.parent)
                        ).as_posix()
                    except ValueError:
                        old_local = ""
                        new_local = ""

                    if old_local and norm.lower() == old_local.lower():
                        elem.set(key, new_local)
                        dirty = True
                        break

        if dirty:
            indent_xml(tree)
            tree.write(config_path, encoding="utf-8", xml_declaration=True)
            changes += 1
            actions.append(
                f"Rewrote shared foliage reference(s) in "
                f"{config_path.relative_to(output_root).as_posix()}."
            )

    return changes, actions, warnings

def apply_patch(source: MapProfile, target: MapProfile, crops: list[str], output: Path, grle_converter: Optional[Path] = None) -> dict:
    output_root = copy_target_to_output(target, output)
    patched_target = MapProfile(target.source_path, str(output_root), False, str(output_root))
    scan_profile(patched_target)
    original_i3d_file_tables = snapshot_all_i3d_file_tables(output_root)

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

    fruit_target_file, registry_actions, registry_warnings = ensure_active_map_registry(
        output_root, "fruit_types", fruit_target_file
    )
    apply_report["actions"].extend(registry_actions)
    apply_report["warnings"].extend(registry_warnings)
    fill_target_file, registry_actions, registry_warnings = ensure_active_map_registry(
        output_root, "fill_types", fill_target_file
    )
    apply_report["actions"].extend(registry_actions)
    apply_report["warnings"].extend(registry_warnings)
    height_target_file, registry_actions, registry_warnings = ensure_active_map_registry(
        output_root, "height_types", height_target_file
    )
    apply_report["actions"].extend(registry_actions)
    apply_report["warnings"].extend(registry_warnings)

    for rel_file, role in [
        (fruit_target_file, "fruit_types"),
        (fill_target_file, "fill_types"),
        (height_target_file, "height_types"),
        (growth_target_file, "growth"),
    ]:
        if not rel_file:
            apply_report["warnings"].append(f"No target {role} XML file detected. Related nodes cannot be inserted automatically.")

    capacity_changes, capacity_actions, capacity_warnings = ensure_density_fruits_capacity_from_source(source, output_root, crops, grle_converter)
    apply_report["summary"]["inserted_nodes"] += capacity_changes
    apply_report["actions"].extend(capacity_actions)
    apply_report["warnings"].extend(capacity_warnings)

    height_capacity_changes, height_capacity_actions, height_capacity_warnings = ensure_density_height_types_capacity_from_source(
        source, output_root, height_target_file, crops, grle_converter
    )
    apply_report["summary"]["inserted_nodes"] += height_capacity_changes
    apply_report["actions"].extend(height_capacity_actions)
    apply_report["warnings"].extend(height_capacity_warnings)

    for crop_name in [c.lower() for c in crops]:
        crop = source.crop_defs.get(crop_name)
        if not crop:
            apply_report["errors"].append(f"Crop not found in source: {crop_name}")
            continue

        # The common engine always carries the complete foliage directory. This keeps
        # direct-source and library-source behaviour identical for crop families.
        crop.asset_paths.update(collect_crop_foliage_tree(Path(source.root), crop))
        for family_name in source_crop_family(source, crop):
            family_crop = source.crop_defs.get(family_name)
            if family_crop is not None:
                crop.asset_paths.update(family_crop.asset_paths)
                crop.asset_paths.update(collect_crop_foliage_tree(Path(source.root), family_crop))
        copied, warnings = copy_assets_for_crop(source, output_root, crop)
        apply_report["summary"]["copied_assets"] += copied
        apply_report["warnings"].extend(warnings)
        apply_report["actions"].append(f"Copied {copied} asset(s) for crop '{crop_name}'.")

        l10n_inserted, l10n_warnings, l10n_keys = patch_l10n_from_source_for_crop_family(source, output_root, crop)
        apply_report["warnings"].extend(l10n_warnings)
        apply_report["actions"].append(
            f"Patched {l10n_inserted} exact source l10n entr(y/ies) for crop family '{crop_name}' "
            f"using {len(l10n_keys)} referenced key(s)."
        )

        patched_layers = 0
        layer_warnings: list[str] = []
        for family_name in source_crop_family(source, crop):
            family_crop = source.crop_defs.get(family_name)
            if family_crop is None:
                continue
            n, w = patch_i3d_foliage_layer_for_crop(source, output_root, family_crop)
            patched_layers += n
            layer_warnings.extend(w)
        apply_report["summary"]["inserted_nodes"] += patched_layers
        apply_report["warnings"].extend(layer_warnings)
        if patched_layers:
            apply_report["actions"].append(f"Patched {patched_layers} i3d foliage layer entr(y/ies) for crop family '{crop_name}'.")

        if fruit_target_file:
            registry_changes, registry_actions, registry_warnings = reconcile_source_fruit_registry(
                source, output_root, fruit_target_file, crop
            )
            apply_report["summary"]["inserted_nodes"] += registry_changes
            apply_report["actions"].extend(registry_actions)
            apply_report["warnings"].extend(registry_warnings)
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
            # Normalize local fillType asset references before the FIRST insertion.
            # Some source maps reference PNGs in XML while only shipping DDS files.
            # The package/source scanner can resolve and copy the DDS, but inserting
            # the raw source node here leaves a dead .png reference in the target.
            # Normalizing here keeps direct-source and package-source applies aligned.
            normalized_fill_nodes = normalise_filltype_asset_references(
                Path(source.root), crop.fill_type_nodes
            )
            inserted, warnings = insert_nodes_into_xml(
                output_root,
                fill_target_file,
                normalized_fill_nodes,
                collect_target_known_fill_type_names(output_root, fill_target_file),
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

        family_changes, family_actions, family_warnings = merge_source_crop_family_semantics(
            source, output_root, crop, fruit_target_file, fill_target_file, height_target_file
        )
        apply_report["summary"]["inserted_nodes"] += family_changes
        apply_report["actions"].extend(family_actions)
        apply_report["warnings"].extend(family_warnings)

        registry_validation = validate_active_crop_registries(
            output_root, fruit_target_file, fill_target_file, crop
        )
        apply_report.setdefault("registry_validation", {})[crop_name] = registry_validation
        apply_report["warnings"].extend(registry_validation.get("warnings", []))
        apply_report["errors"].extend(registry_validation.get("errors", []))

    # Preserve only the File entries captured from the real target baseline.
    # New crop File entries are append-only additions; no assumed/reserved IDs are
    # introduced by this guard.
    # Final source-layout reconciliation. FruitType identity does not imply one
    # foliage directory per fruitType; preserve families that intentionally share
    # an asset directory (e.g. barley + winterBarley, wheat + winterWheat).
    layout_changes, layout_actions, layout_warnings = enforce_source_foliage_layout(
        source,
        output_root,
        [c.lower() for c in crops],
        source_foliage_root,
        target_foliage_root,
    )
    apply_report["summary"]["inserted_nodes"] += layout_changes
    apply_report["actions"].extend(layout_actions)
    apply_report["warnings"].extend(layout_warnings)

    restored_files, restore_actions = restore_all_i3d_file_tables(output_root, original_i3d_file_tables)
    apply_report["actions"].extend(restore_actions)
    if restored_files:
        apply_report["actions"].append(
            f"Restored {restored_files} pre-existing i3d File entr(y/ies) removed by an intermediate integration step."
        )

    # Re-scan output for a basic validation pass.
    validation_profile = MapProfile(str(output_root), str(output_root), False, str(output_root))
    scan_profile(validation_profile)
    validation = validate_output(source, validation_profile, [c.lower() for c in crops])
    apply_report["validation"] = validation
    apply_report["warnings"].extend(validation.get("warnings", []))
    apply_report["errors"].extend(validation.get("errors", []))

    write_reports(apply_report, output_root, prefix="CropPorter_Apply")
    return apply_report



def apply_patch_in_place(
    source: MapProfile,
    output_root: Path,
    crops: list[str],
    grle_converter: Optional[Path] = None,
    report_prefix: str = "CropPorter_Apply",
    target_source_label: Optional[str] = None,
) -> dict:
    """Apply crops to an already-created target working tree.

    Every call rescans the accumulated tree first, so IDs, registries, density
    capacity, and duplicate detection include packages applied earlier in a batch.
    """
    output_root = Path(output_root).resolve()
    if not output_root.exists() or not output_root.is_dir():
        raise CropPorterError(f"In-place target folder does not exist: {output_root}")
    patched_target = MapProfile(target_source_label or str(output_root), str(output_root), False, str(output_root))
    scan_profile(patched_target)
    original_i3d_file_tables = snapshot_all_i3d_file_tables(output_root)

    apply_report = {
        "version": VERSION,
        "source": source.source_path,
        "target": target_source_label or str(output_root),
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

    fruit_target_file, registry_actions, registry_warnings = ensure_active_map_registry(
        output_root, "fruit_types", fruit_target_file
    )
    apply_report["actions"].extend(registry_actions)
    apply_report["warnings"].extend(registry_warnings)
    fill_target_file, registry_actions, registry_warnings = ensure_active_map_registry(
        output_root, "fill_types", fill_target_file
    )
    apply_report["actions"].extend(registry_actions)
    apply_report["warnings"].extend(registry_warnings)
    height_target_file, registry_actions, registry_warnings = ensure_active_map_registry(
        output_root, "height_types", height_target_file
    )
    apply_report["actions"].extend(registry_actions)
    apply_report["warnings"].extend(registry_warnings)

    for rel_file, role in [
        (fruit_target_file, "fruit_types"),
        (fill_target_file, "fill_types"),
        (height_target_file, "height_types"),
        (growth_target_file, "growth"),
    ]:
        if not rel_file:
            apply_report["warnings"].append(f"No target {role} XML file detected. Related nodes cannot be inserted automatically.")

    capacity_changes, capacity_actions, capacity_warnings = ensure_density_fruits_capacity_from_source(source, output_root, crops, grle_converter)
    apply_report["summary"]["inserted_nodes"] += capacity_changes
    apply_report["actions"].extend(capacity_actions)
    apply_report["warnings"].extend(capacity_warnings)

    height_capacity_changes, height_capacity_actions, height_capacity_warnings = ensure_density_height_types_capacity_from_source(
        source, output_root, height_target_file, crops, grle_converter
    )
    apply_report["summary"]["inserted_nodes"] += height_capacity_changes
    apply_report["actions"].extend(height_capacity_actions)
    apply_report["warnings"].extend(height_capacity_warnings)

    for crop_name in [c.lower() for c in crops]:
        crop = source.crop_defs.get(crop_name)
        if not crop:
            apply_report["errors"].append(f"Crop not found in source: {crop_name}")
            continue

        # The common engine always carries the complete foliage directory. This keeps
        # direct-source and library-source behaviour identical for crop families.
        crop.asset_paths.update(collect_crop_foliage_tree(Path(source.root), crop))
        for family_name in source_crop_family(source, crop):
            family_crop = source.crop_defs.get(family_name)
            if family_crop is not None:
                crop.asset_paths.update(family_crop.asset_paths)
                crop.asset_paths.update(collect_crop_foliage_tree(Path(source.root), family_crop))
        copied, warnings = copy_assets_for_crop(source, output_root, crop)
        apply_report["summary"]["copied_assets"] += copied
        apply_report["warnings"].extend(warnings)
        apply_report["actions"].append(f"Copied {copied} asset(s) for crop '{crop_name}'.")

        l10n_inserted, l10n_warnings, l10n_keys = patch_l10n_from_source_for_crop_family(source, output_root, crop)
        apply_report["warnings"].extend(l10n_warnings)
        apply_report["actions"].append(
            f"Patched {l10n_inserted} exact source l10n entr(y/ies) for crop family '{crop_name}' "
            f"using {len(l10n_keys)} referenced key(s)."
        )

        patched_layers = 0
        layer_warnings: list[str] = []
        for family_name in source_crop_family(source, crop):
            family_crop = source.crop_defs.get(family_name)
            if family_crop is None:
                continue
            n, w = patch_i3d_foliage_layer_for_crop(source, output_root, family_crop)
            patched_layers += n
            layer_warnings.extend(w)
        apply_report["summary"]["inserted_nodes"] += patched_layers
        apply_report["warnings"].extend(layer_warnings)
        if patched_layers:
            apply_report["actions"].append(f"Patched {patched_layers} i3d foliage layer entr(y/ies) for crop family '{crop_name}'.")

        if fruit_target_file:
            registry_changes, registry_actions, registry_warnings = reconcile_source_fruit_registry(
                source, output_root, fruit_target_file, crop
            )
            apply_report["summary"]["inserted_nodes"] += registry_changes
            apply_report["actions"].extend(registry_actions)
            apply_report["warnings"].extend(registry_warnings)
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
            # Normalize local fillType asset references before the FIRST insertion.
            # Some source maps reference PNGs in XML while only shipping DDS files.
            # The package/source scanner can resolve and copy the DDS, but inserting
            # the raw source node here leaves a dead .png reference in the target.
            # Normalizing here keeps direct-source and package-source applies aligned.
            normalized_fill_nodes = normalise_filltype_asset_references(
                Path(source.root), crop.fill_type_nodes
            )
            inserted, warnings = insert_nodes_into_xml(
                output_root,
                fill_target_file,
                normalized_fill_nodes,
                collect_target_known_fill_type_names(output_root, fill_target_file),
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

        family_changes, family_actions, family_warnings = merge_source_crop_family_semantics(
            source, output_root, crop, fruit_target_file, fill_target_file, height_target_file
        )
        apply_report["summary"]["inserted_nodes"] += family_changes
        apply_report["actions"].extend(family_actions)
        apply_report["warnings"].extend(family_warnings)

        registry_validation = validate_active_crop_registries(
            output_root, fruit_target_file, fill_target_file, crop
        )
        apply_report.setdefault("registry_validation", {})[crop_name] = registry_validation
        apply_report["warnings"].extend(registry_validation.get("warnings", []))
        apply_report["errors"].extend(registry_validation.get("errors", []))

    # Preserve only the File entries captured from the real target baseline.
    # New crop File entries are append-only additions; no assumed/reserved IDs are
    # introduced by this guard.
    restored_files, restore_actions = restore_all_i3d_file_tables(output_root, original_i3d_file_tables)
    apply_report["actions"].extend(restore_actions)
    if restored_files:
        apply_report["actions"].append(
            f"Restored {restored_files} pre-existing i3d File entr(y/ies) removed by an intermediate integration step."
        )

    # Re-scan output for a basic validation pass.
    validation_profile = MapProfile(str(output_root), str(output_root), False, str(output_root))
    scan_profile(validation_profile)
    validation = validate_output(source, validation_profile, [c.lower() for c in crops])
    apply_report["validation"] = validation
    apply_report["warnings"].extend(validation.get("warnings", []))
    apply_report["errors"].extend(validation.get("errors", []))

    write_reports(apply_report, output_root, prefix=report_prefix)
    return apply_report



def apply_patch(source: MapProfile, target: MapProfile, crops: list[str], output: Path, grle_converter: Optional[Path] = None) -> dict:
    """Create a patched copy of a target, then run the shared in-place engine."""
    output_root = copy_target_to_output(target, output)
    return apply_patch_in_place(
        source, output_root, crops, grle_converter=grle_converter,
        report_prefix="CropPorter_Apply", target_source_label=target.source_path
    )

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

        if registry_style == "fruitTypeFilename":
            # Filename-style registries load complete crop definitions from foliage XML.
            # Named <fruitType> nodes collected from foliageDamage, Precision Farming,
            # map XML, etc. are semantic references, not registry definitions, and must
            # never be appended here. The real foliage reference is reconciled separately.
            if "/foliage/" not in source_rel_norm.lower() or not source_rel_norm.lower().endswith(".xml"):
                continue
            if file_key in current_files:
                warnings.append(f"Skipped duplicate fruit registry file '{source_rel_norm}' in {active_rel_file}")
                continue
            new_ref = ET.Element("fruitType")
            new_ref.set("filename", source_rel_norm)
            fruit_types.append(new_ref)
            current_files.add(file_key)
            if name_key:
                current_names.add(name_key)
            inserted += 1
        elif "/foliage/" in source_rel_norm.lower() and source_rel_norm.lower().endswith(".xml"):
            if file_key in current_files:
                warnings.append(f"Skipped duplicate fruit registry file '{source_rel_norm}' in {active_rel_file}")
                continue
            container = ensure_additional_files_container(root)
            new_ref = ET.Element("additionalFile")
            new_ref.set("filename", source_rel_norm)
            container.append(new_ref)
            current_files.add(file_key)
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
    """Resolve the active fruitTypes XML, including registries embedded in map XML."""
    map_xml = find_primary_map_xml(target_root)
    if map_xml is not None:
        tree = parse_xml_safely(map_xml)
        if tree:
            node = _find_map_registry_node(tree.getroot(), "fruit_types")
            if node is not None:
                local_rel = _resolve_local_map_reference(target_root, map_xml, node.attrib.get("filename", ""))
                if local_rel and (target_root / local_rel).exists():
                    return local_rel
                if _map_registry_is_inline(node, "fruit_types"):
                    return rel_to_root(map_xml, target_root).replace(chr(92), "/")
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
        warnings.append(
            f"No source fillTypeCategory membership found for crop '{crop.fruit_name}'. "
            "No inferred categories were applied; preserving source semantics is safer than guessing."
        )
        return 0, warnings

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
    for ref in crop.fill_type_nodes:
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
        # Only validate fillTypes that the source map actually defines in its canonical
        # local fillTypes registry.  References such as BARLEY_CUT/GRASS_WINDROW can
        # legitimately come from the implicit base-game registry and must not be
        # reported as missing merely because the target supplemental registry omits them.
        canonical_source_fills = {
            (ref.attrs.get("name") or ref.attrs.get("fillType") or ref.attrs.get("fillTypeName") or "").upper()
            for ref in collect_refs_for_fill_types(Path(source.root), set(source_crop.fill_type_names))
        }
        for fill_name in source_crop.fill_type_names:
            if fill_name.upper() not in canonical_source_fills:
                continue
            if fill_name not in output_profile.fill_type_names:
                result["warnings"].append(f"Output map may be missing fillType '{fill_name}' for crop '{crop_name}'.")
    result["warnings"].append("DensityMap_fruits binary compatibility was not independently validated; CropPorter now promotes FoliageMultiLayer channel settings when the source requires more capacity.")
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
        report = apply_patch(source, target, args.crops, Path(args.output), Path(args.grle_converter) if getattr(args, "grle_converter", None) else None)
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
        (element_is_foliage_crop_entry(ET.fromstring('<FoliageType name="RYE"/>'), "rye"), True, "RYE foliage entry should match rye"),
        (element_is_foliage_crop_entry(ET.fromstring('<FoliageType name="GREENRYE"/>'), "rye"), False, "GREENRYE foliage entry must not match rye"),
        (element_is_foliage_crop_entry(ET.fromstring('<FoliageType name="VETCHRYE"/>'), "rye"), False, "VETCHRYE foliage entry must not match rye"),
    ]
    failed = 0
    for actual, expected, label in checks:
        if actual != expected:
            failed += 1
            print(f"FAIL: {label} -> got {actual}, expected {expected}")
        else:
            print(f"PASS: {label}")

    # Dependency-scope regression: a map-level i3d template may reference unrelated
    # files through its global <Files> table, but crop export must not follow them.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_scope_") as td:
        root = Path(td)
        (root / "crop").mkdir(parents=True, exist_ok=True)
        (root / "unrelated").mkdir(parents=True, exist_ok=True)
        (root / "crop" / "crop.xml").write_text(
            '<foliageType><foliageLayer shapeSource="crop.i3d"/></foliageType>', encoding="utf-8"
        )
        (root / "crop" / "crop.i3d").write_text(
            '<i3D><Files><File fileId="1" filename="crop_diffuse.png"/></Files></i3D>', encoding="utf-8"
        )
        (root / "crop" / "crop_diffuse.png").write_bytes(b"crop")
        (root / "unrelated" / "huge.dds").write_bytes(b"unrelated")
        (root / "map.i3d").write_text(
            '<i3D><Files><File fileId="1" filename="unrelated/huge.dds"/></Files>'
            '<Layers><FoliageMultiLayer name="densityMap_fruits"/></Layers></i3D>', encoding="utf-8"
        )
        expanded = expand_asset_references(root, {"crop/crop.xml", "map.i3d"})
        scope_ok = "crop/crop_diffuse.png" in expanded and "unrelated/huge.dds" not in expanded
        if scope_ok:
            print("PASS: map i3d template does not expand unrelated asset graph")
        else:
            failed += 1
            print(f"FAIL: map i3d dependency scope -> {sorted(expanded)}")

    # Append-only densityMap_fruits regression: maps such as Hobo's Hollow may
    # reference an existing compiled GDM directly from map.i3d.  Expanding fruit
    # channels must append a PNG File row and repoint densityMapId, never mutate
    # the original fileId -> filename mapping (which final safety validation rejects).
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_density_append_") as td:
        root_dir = Path(td)
        i3d = root_dir / "map.i3d"
        i3d.write_text(
            '<i3D><Files><File fileId="755" filename="data/densityMap_fruits.gdm"/></Files>'
            '<Layers><FoliageMultiLayer name="densityMap_fruits" densityMapId="755" '
            'numChannels="10" numTypeIndexChannels="5" compressionChannels="5"/></Layers></i3D>',
            encoding="utf-8",
        )
        snap = snapshot_all_i3d_file_tables(root_dir)
        tree = ET.parse(i3d)
        xml_root = tree.getroot()
        layer = find_density_fruits_foliage_multilayer(xml_root)
        old_file = get_i3d_density_fruits_file_entry(xml_root, layer) if layer is not None else None
        if layer is None or old_file is None:
            failed += 1
            print("FAIL: append-only densityMap_fruits regression setup")
        else:
            new_id, appended = append_i3d_replacement_file_reference(
                xml_root, layer, old_file, "data/densityMap_fruits.png"
            )
            indent_xml(tree)
            tree.write(i3d, encoding="utf-8", xml_declaration=True)
            try:
                restored, _ = restore_all_i3d_file_tables(root_dir, snap)
                check_tree = ET.parse(i3d)
                check_root = check_tree.getroot()
                files = collect_file_entries_by_id(check_root)
                check_layer = find_density_fruits_foliage_multilayer(check_root)
                ok = (
                    appended
                    and restored == 0
                    and get_i3d_file_filename(files.get("755")) == "data/densityMap_fruits.gdm"
                    and new_id in files
                    and get_i3d_file_filename(files[new_id]) == "data/densityMap_fruits.png"
                    and check_layer is not None
                    and (check_layer.attrib.get("densityMapId") or check_layer.attrib.get("densityMapID")) == new_id
                )
            except Exception as exc:
                ok = False
                print(f"FAIL: append-only densityMap_fruits safety raised {exc}")
            if ok:
                print("PASS: densityMap_fruits GDM->PNG uses append-only File replacement")
            else:
                failed += 1
                print(
                    "FAIL: densityMap_fruits append-only replacement -> "
                    f"appended={appended}, new_id={new_id}"
                )

    # Inline fruitTypes regression: maps such as Hobo's Hollow keep the active
    # registry directly in map/map.xml as <fruitTypes><fruitType filename=.../>.
    # CropPorter must recognise that file as the active registry without adding a
    # self-referential filename attribute or demanding a separate maps_fruitTypes.xml.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_inline_fruit_registry_") as td:
        root = Path(td)
        map_dir = root / "map"
        map_dir.mkdir(parents=True, exist_ok=True)
        map_xml = map_dir / "map.xml"
        map_xml.write_text(
            '<map><filename>map/map.i3d</filename><fruitTypes>'
            '<fruitType filename="$data/foliage/wheat/wheat.xml"/>'
            '<fruitType filename="$data/foliage/barley/barley.xml"/>'
            '<fruitType filename="$data/foliage/canola/canola.xml"/>'
            '<fruitType filename="$data/foliage/maize/maize.xml"/>'
            '</fruitTypes></map>',
            encoding="utf-8",
        )
        roles = classify_xml_file_by_content(map_xml)
        active_rel, inline_actions, inline_warnings = ensure_active_map_registry(
            root, "fruit_types", None
        )
        check_tree = ET.parse(map_xml)
        check_node = _find_map_registry_node(check_tree.getroot(), "fruit_types")
        resolved = resolve_active_fruit_types_xml(root, "unused.xml")
        ok = (
            "fruit_types" in roles
            and active_rel == "map/map.xml"
            and resolved == "map/map.xml"
            and check_node is not None
            and "filename" not in check_node.attrib
            and any("inline fruitTypes registry" in action for action in inline_actions)
            and not inline_warnings
        )
        if ok:
            print("PASS: inline map.xml fruitTypes registry is detected and preserved")
        else:
            failed += 1
            print(
                "FAIL: inline fruitTypes registry -> "
                f"roles={roles}, active={active_rel}, resolved={resolved}, "
                f"attrs={dict(check_node.attrib) if check_node is not None else None}, "
                f"actions={inline_actions}, warnings={inline_warnings}"
            )

    # Supplemental height-registry regression: a map may have no local
    # densityMapHeightTypes XML at all while terrainDetailHeight still exposes the
    # default 6-bit type / 6-bit physical-height packing.  CropPorter must create
    # an additive map-local registry and activate it without replacing defaults.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_height_registry_") as td:
        root = Path(td)
        map_dir = root / "map"
        data_dir = map_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        map_xml = map_dir / "map.xml"
        map_xml.write_text(
            '<map><filename>map/map.i3d</filename><fruitTypes>'
            '<fruitType filename="$data/foliage/barley/barley.xml"/>'
            '</fruitTypes></map>',
            encoding="utf-8",
        )
        (root / "modDesc.xml").write_text('<modDesc><maps/></modDesc>', encoding="utf-8")
        (map_dir / "map.i3d").write_text(
            '<i3D><Files><File fileId="749" filename="data/densityMap_height.gdm"/></Files>'
            '<Layers><DetailLayer name="terrainDetailHeight" densityMapId="749" '
            'numDensityMapChannels="12" compressionChannels="6" combinedValuesChannels="0 6 0" '
            'heightFirstChannel="6" heightNumChannels="6"/></Layers></i3D>',
            encoding="utf-8",
        )
        active_rel, height_actions, height_warnings = ensure_active_map_registry(
            root, "height_types", None
        )
        active_path = root / active_rel if active_rel else None
        reg_tree = parse_xml_safely(active_path) if active_path else None
        reg = find_child_container(reg_tree.getroot(), "densityMapHeightTypes") if reg_tree else None
        map_tree = ET.parse(map_xml)
        hook = _find_map_registry_node(map_tree.getroot(), "height_types")
        ok = (
            active_rel == "map/config/densityMapHeightTypes.xml"
            and active_path is not None
            and active_path.exists()
            and reg is not None
            and reg.attrib.get("firstChannel") == "0"
            and reg.attrib.get("numChannels") == "6"
            and hook is not None
            and hook.attrib.get("filename") == active_rel
            and any("Created supplemental densityMapHeightTypes registry" in x for x in height_actions)
            and not height_warnings
        )
        if ok:
            print("PASS: missing map height registry gets additive local densityMapHeightTypes hook")
        else:
            failed += 1
            print(
                "FAIL: supplemental height registry -> "
                f"active={active_rel}, reg={dict(reg.attrib) if reg is not None else None}, "
                f"hook={dict(hook.attrib) if hook is not None else None}, "
                f"actions={height_actions}, warnings={height_warnings}"
            )

    # Pipeline wiring regression: multi-package apply uses apply_patch_in_place(),
    # not the older copy-and-apply helper.  v16 implemented supplemental height
    # registries in ensure_active_map_registry() but forgot to invoke that role in
    # the active in-place engine, so Hobo's Hollow still reported no height registry.
    # Keep this explicit guard so future registry work cannot land only in a dead path.
    import inspect
    in_place_source = inspect.getsource(apply_patch_in_place)
    if 'output_root, "height_types", height_target_file' in in_place_source:
        print("PASS: in-place apply pipeline activates height_types registry")
    else:
        failed += 1
        print("FAIL: in-place apply pipeline does not activate height_types registry")

    # Append-only densityMap_height regression: direct .gdm targets must keep the
    # pre-existing I3D File mapping immutable while a new PNG row is appended and
    # terrainDetailHeight is repointed.  The stale physical GDM can then be retired
    # without violating the I3D append-only safety invariant.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_height_append_") as td:
        root = Path(td)
        i3d = root / "map.i3d"
        i3d.write_text(
            '<i3D><Files><File fileId="749" filename="data/densityMap_height.gdm"/></Files>'
            '<Layers><DetailLayer name="terrainDetailHeight" densityMapId="749" '
            'numDensityMapChannels="12" compressionChannels="6" combinedValuesChannels="0 6 0" '
            'heightFirstChannel="6" heightNumChannels="6"/></Layers></i3D>',
            encoding="utf-8",
        )
        snap = snapshot_all_i3d_file_tables(root)
        tree = ET.parse(i3d)
        xml_root = tree.getroot()
        layer = find_terrain_detail_height_layer(xml_root)
        old_file = get_i3d_density_height_file_entry(xml_root, layer) if layer is not None else None
        if layer is None or old_file is None:
            failed += 1
            print("FAIL: append-only densityMap_height regression setup")
        else:
            new_id, appended = append_i3d_replacement_file_reference(
                xml_root, layer, old_file, "data/densityMap_height.png"
            )
            indent_xml(tree)
            tree.write(i3d, encoding="utf-8", xml_declaration=True)
            try:
                restored, _ = restore_all_i3d_file_tables(root, snap)
                check_tree = ET.parse(i3d)
                check_root = check_tree.getroot()
                files = collect_file_entries_by_id(check_root)
                check_layer = find_terrain_detail_height_layer(check_root)
                ok = (
                    appended
                    and restored == 0
                    and get_i3d_file_filename(files.get("749")) == "data/densityMap_height.gdm"
                    and new_id in files
                    and get_i3d_file_filename(files[new_id]) == "data/densityMap_height.png"
                    and check_layer is not None
                    and (check_layer.attrib.get("densityMapId") or check_layer.attrib.get("densityMapID")) == new_id
                )
            except Exception as exc:
                ok = False
                print(f"FAIL: append-only densityMap_height safety raised {exc}")
            if ok:
                print("PASS: densityMap_height GDM->PNG uses append-only File replacement")
            else:
                failed += 1
                print(
                    "FAIL: densityMap_height append-only replacement -> "
                    f"appended={appended}, new_id={new_id}"
                )

    # Height-map channel migration regression: GIANTS grleConverter emits a
    # 12-channel GDM as RGB8 (R=bits 0-7, G=8-15, B=16-23).  Verify that
    # expanding the type index from 6 to 8 bits moves the 6-bit physical-height
    # field from [6-11] to [8-13] without changing either logical value.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_height_rgb_") as td:
        png = Path(td) / "densityMap_height.png"
        samples = [(1, 2), (17, 31), (63, 63)]
        row = bytearray()
        for type_value, height_value in samples:
            value = type_value | (height_value << 6)
            row.extend((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
        ihdr = struct.pack(">IIBBBBB", len(samples), 1, 8, 2, 0, 0, 0)
        _write_png_scanlines(png, len(samples), 1, [row], [(b"IHDR", ihdr), (b"IEND", b"")])
        moved, total = repack_density_height_png_channels(png, 0, 6, 6, 6, 8)
        _, _, rows, _ = _decode_png_scanlines(
            png, expected_bit_depth=8, expected_colour_type=2, bytes_per_pixel=3
        )
        migrated = []
        out_row = rows[0]
        for x in range(0, len(out_row), 3):
            value = out_row[x] | (out_row[x + 1] << 8) | (out_row[x + 2] << 16)
            migrated.append((value & 0xFF, (value >> 8) & 0x3F))
        if migrated == samples and total == 14 and moved == 3:
            print("PASS: RGB8 densityMap_height migration preserves type/height values")
        else:
            failed += 1
            print(
                "FAIL: RGB8 densityMap_height migration -> "
                f"migrated={migrated}, total={total}, moved={moved}"
            )

    # Runtime resolver regression: GIANTS prefers a same-stem compiled .gdm over
    # an I3D-referenced PNG.  Once the PNG has a wider channel layout, the old GDM
    # must not remain under the same stem.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_height_gdm_") as td:
        root = Path(td)
        png = root / "densityMap_height.png"
        gdm = root / "densityMap_height.gdm"
        png.write_bytes(b"expanded-png")
        gdm.write_bytes(b"stale-12-channel-gdm")
        retire_actions = retire_stale_compiled_density_siblings_for_png(png, root)
        backup = root / "densityMap_height.gdm.cropporter.preexpanded.bak"
        if (not gdm.exists()) and backup.exists() and retire_actions:
            print("PASS: stale densityMap_height GDM is retired after PNG expansion")
        else:
            failed += 1
            print(
                "FAIL: stale densityMap_height GDM retirement -> "
                f"gdm_exists={gdm.exists()}, backup_exists={backup.exists()}, actions={retire_actions}"
            )
    # Save-migration encoder regression: encode a 14-channel RGB8 height map with
    # a compression split at channel 6, then decode it internally and verify every
    # logical pixel.  This deliberately exercises type bits 6-7 crossing the
    # compression boundary used by Castlereagh's migrated terrainDetailHeight.
    with tempfile.TemporaryDirectory(prefix="cropporter_selftest_gdm_encode_") as td:
        root = Path(td)
        png = root / "densityMap_height.png"
        gdm = root / "densityMap_height.gdm"
        expected_values = []
        rows = []
        for y in range(32):
            row = bytearray()
            for x in range(32):
                i = y * 32 + x
                type_value = (i * 13) % 200
                height_value = (i * 5) % 64
                value = type_value | (height_value << 8)
                expected_values.append(value)
                row.extend((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
            rows.append(row)
        ihdr = struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0)
        _write_png_scanlines(png, 32, 32, rows, [(b"IHDR", ihdr), (b"IEND", b"")])
        encode_density_png_to_gdm(png, gdm, num_channels=14, compression_channels=6)
        header, decoded_values = decode_gdm_logical_values_for_test(gdm)
        if (
            header.num_channels == 14
            and header.compression_boundaries == [0, 6, 14]
            and decoded_values == expected_values
        ):
            print("PASS: GDM encoder round-trip preserves 14-channel height pixels")
        else:
            failed += 1
            print(
                "FAIL: GDM encoder round-trip -> "
                f"channels={header.num_channels}, boundaries={header.compression_boundaries}, "
                f"pixels_match={decoded_values == expected_values}"
            )

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


def cmd_apply_plantation(args: argparse.Namespace) -> int:
    """Experimental plantation/vine crop porter for v0.2.

    Phase 1 focuses on making row-planted crops such as coffee available as placeable
    plantation rows and getting their core crop/fill/effect assets into the target map.
    Vehicle integration is intentionally conservative and should be tested separately.
    """
    source = prepare_map_input(Path(args.source))
    target = prepare_map_input(Path(args.target))
    output_root = Path(args.output).resolve()
    try:
        scan_profile(source)
        scan_profile(target)

        crop_name = args.crop.lower()
        crop = source.crop_defs.get(crop_name)
        if crop is None:
            raise CropPorterError(f"Crop '{args.crop}' was not detected in source map.")

        probe = build_plantation_crop_probe(source, crop_name)
        if probe.get("mode") != "vine/placeable plantation" and not args.force:
            raise CropPorterError(
                f"Crop '{args.crop}' was not classified as a plantation/vine crop. "
                "Use normal apply for field crops, or pass --force for experimental testing."
            )

        if output_root.exists():
            raise CropPorterError(f"Output already exists. Choose a new folder or delete it first: {output_root}")
        shutil.copytree(Path(target.root), output_root)

        report = {
            "version": VERSION,
            "mode": "apply-plantation",
            "source": source.source_path,
            "target": target.source_path,
            "output": str(output_root),
            "crop": crop_name,
            "summary": {
                "inserted_nodes": 0,
                "copied_assets": 0,
                "patched_store_items": 0,
                "patched_map_hooks": 0,
                "errors": 0,
                "warnings": 0,
            },
            "actions": [],
            "warnings": [],
            "probe": probe,
        }

        # Copy all assets identified by the plantation probe, plus linked fillType textures such as DRYCOFFEE fillPlanes.
        probe_assets = list(probe.get("assets", []))
        probe_assets.extend(collect_linked_filltype_assets(Path(source.root), set(probe.get("linkedFillTypes", []))))

        vehicle_store_items: list[str] = []
        if getattr(args, "include_vehicles", False):
            vehicle_assets, vehicle_store_items = collect_plantation_vehicle_dependencies(Path(source.root), probe)
            probe_assets.extend(vehicle_assets)
            report["actions"].append(f"Included {len(vehicle_assets)} vehicle asset dependency file(s).")

        copied_assets = copy_selected_assets(Path(source.root), output_root, probe_assets)
        report["summary"]["copied_assets"] += copied_assets
        report["actions"].append(f"Copied {copied_assets} plantation asset file(s).")

        # Reuse the field-crop XML patch pieces where possible.
        fruit_target_file = find_primary_xml_file(target, "fruit_types")
        fill_target_file = find_primary_xml_file(target, "fill_types")
        height_target_file = find_primary_xml_file(target, "height_types")

        if fruit_target_file:
            inserted, warnings = insert_fruit_nodes_into_xml(output_root, fruit_target_file, crop.fruit_nodes, set(target.fruit_names))
            report["summary"]["inserted_nodes"] += inserted
            report["warnings"].extend(warnings)
            report["actions"].append(f"Inserted {inserted} fruitType registry entry change(s).")
        else:
            report["warnings"].append("No target fruitTypes XML detected. Coffee fruitType was not inserted automatically.")

        if fill_target_file:
            inserted, warnings = insert_nodes_into_xml(output_root, fill_target_file, crop.fill_type_nodes, set(target.fill_type_names), ["name", "fillType", "fillTypeName"])
            report["summary"]["inserted_nodes"] += inserted
            report["warnings"].extend(warnings)
            report["actions"].append(f"Inserted {inserted} fillType node change(s) from crop probe.")

            linked_inserted, linked_warnings = insert_linked_fill_types_for_plantation(source, output_root, fill_target_file, probe)
            report["summary"]["inserted_nodes"] += linked_inserted
            report["warnings"].extend(linked_warnings)
            report["actions"].append(f"Inserted {linked_inserted} linked fillType node change(s).")

            fill_cat_inserted, fill_cat_warnings = patch_fill_type_categories_for_names(source, output_root, fill_target_file, set(probe.get("linkedFillTypes", [])))
            report["summary"]["inserted_nodes"] += fill_cat_inserted
            report["warnings"].extend(fill_cat_warnings)
            report["actions"].append(f"Patched {fill_cat_inserted} linked fillTypeCategory change(s).")
        else:
            report["warnings"].append("No target fillTypes XML detected. Coffee/DRYCOFFEE fillTypes were not inserted automatically.")

        if height_target_file:
            package_height_path = package_root / str(manifest.get("packageHeightTypeNodes", ""))
            if package_height_path.exists():
                package_height_refs = [ref_from_dict(x) for x in json.loads(package_height_path.read_text(encoding="utf-8"))]
                package_height_refs = [remap_xml_node_ref_paths(x, source_map_data_prefix, target_map_data_prefix) for x in package_height_refs]
            else:
                package_height_refs = crop.height_type_nodes
            inserted, warnings = insert_nodes_into_xml(output_root, height_target_file, package_height_refs, set(target.height_type_names), ["name", "fillType", "fillTypeName"])
            report["summary"]["inserted_nodes"] += inserted
            report["warnings"].extend(warnings)
            linked_height_inserted, linked_height_warnings = insert_linked_height_types_for_plantation(source, output_root, height_target_file, probe)
            report["summary"]["inserted_nodes"] += linked_height_inserted
            report["warnings"].extend(linked_height_warnings)
            report["actions"].append(f"Inserted {inserted + linked_height_inserted} heightType node change(s).")
        else:
            report["warnings"].append("No target densityMapHeightTypes XML detected. Height types were not inserted automatically.")

        # Copy source l10n entries that reference the crop/fillTypes/store items.
        l10n_changes, l10n_warnings = patch_plantation_l10n(source, output_root, probe)
        report["summary"]["inserted_nodes"] += l10n_changes
        report["warnings"].extend(l10n_warnings)
        report["actions"].append(f"Patched {l10n_changes} plantation l10n entry change(s).")

        store_item_list = list(probe.get("storeItems", []))
        store_item_list.extend(vehicle_store_items)
        store_changes, store_warnings = patch_moddesc_store_items(output_root, store_item_list)
        report["summary"]["patched_store_items"] += store_changes
        report["warnings"].extend(store_warnings)
        report["actions"].append(f"Patched {store_changes} modDesc storeItem entry change(s).")

        hook_changes, hook_warnings = patch_plantation_map_hooks(output_root, probe)
        report["summary"]["patched_map_hooks"] += hook_changes
        report["warnings"].extend(hook_warnings)
        report["actions"].append(f"Patched {hook_changes} plantation map hook change(s).")

        # Terrain foliage layer is still useful for map/PDA/painted crop support.
        patched_layers, layer_warnings = patch_i3d_foliage_layer_for_crop(source, output_root, crop)
        report["summary"]["inserted_nodes"] += patched_layers
        report["warnings"].extend(layer_warnings)
        if patched_layers:
            report["actions"].append(f"Patched {patched_layers} i3d foliage layer entry for crop '{crop_name}'.")

        report["summary"]["warnings"] = len(report["warnings"])
        write_reports(report, output_root, prefix="CropPorter_ApplyPlantation")

        print("Apply plantation complete.")
        print(f"Output: {output_root}")
        print(f"Inserted/patched nodes: {report['summary']['inserted_nodes']}")
        print(f"Copied assets: {report['summary']['copied_assets']}")
        print(f"Store items patched: {report['summary']['patched_store_items']}")
        print(f"Map hooks patched: {report['summary']['patched_map_hooks']}")
        if report["warnings"]:
            print("Warnings:")
            for warning in report["warnings"]:
                print(f"- {warning}")
        return 0
    finally:
        source.cleanup()
        target.cleanup()


def copy_selected_assets(source_root: Path, output_root: Path, rel_paths: list[str]) -> int:
    copied = 0
    for rel in unique_preserve_order(rel_paths):
        rel_norm = rel.replace(chr(92), "/")
        source_path = source_root / rel_norm
        target_path = output_root / rel_norm
        if not source_path.exists() or not source_path.is_file():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied += 1
    return copied


def collect_plantation_vehicle_dependencies(source_root: Path, probe: dict) -> tuple[list[str], list[str]]:
    """Collect optional vehicle dependencies for plantation crops.

    This is intentionally opt-in via --include-vehicles because vehicles can introduce
    extra dependencies and category behaviour. For coffee, the source provides a compact
    Braud 9090X COFFEE package under maps/vehicles.
    """
    assets: list[str] = []
    store_items: list[str] = []
    for vehicle_xml in probe.get("vehicles", []) or []:
        rel = str(vehicle_xml).replace(chr(92), "/")
        if not rel.lower().endswith(".xml"):
            continue
        xml_path = source_root / rel
        if not xml_path.exists():
            continue
        assets.append(rel)
        store_items.append(f"modDesc.xml -> {rel}")

        tree = parse_xml_safely(xml_path)
        if tree:
            for elem in tree.getroot().iter():
                for attr_value in elem.attrib.values():
                    value = attr_value.replace(chr(92), "/").strip()
                    if not value or value.startswith("$data/"):
                        continue
                    lower = value.lower()
                    if lower.endswith((".xml", ".i3d", ".i3d.shapes", ".dds", ".png", ".jpg", ".jpeg")):
                        candidate = value
                        if not candidate.startswith("maps/"):
                            candidate = rel_to_root((xml_path.parent / candidate).resolve(), source_root).replace(chr(92), "/")
                        assets.append(candidate)
                        if lower.endswith(".i3d"):
                            shape_candidate = candidate + ".shapes"
                            if (source_root / shape_candidate).exists():
                                assets.append(shape_candidate)

        # Fallback: copy same-stem local vehicle files beside the XML.
        stem = Path(rel).stem.lower()
        vehicle_dir = xml_path.parent
        for path in vehicle_dir.iterdir() if vehicle_dir.exists() else []:
            if not path.is_file():
                continue
            if stem in path.name.lower():
                assets.append(rel_to_root(path, source_root).replace(chr(92), "/"))

    return unique_preserve_order(sorted(assets)), unique_preserve_order(store_items)


def collect_linked_filltype_assets(source_root: Path, linked_fill_types: set[str]) -> list[str]:
    """Find HUD/fillPlane texture assets referenced by linked fillTypes such as DRYCOFFEE.

    Plantation crops often have a primary harvested fillType (COFFEE) and a processed
    linked fillType (DRYCOFFEE). The crop asset probe finds the primary crop textures,
    but not always the processed output textures. This helper searches both XML
    references and predictable fillPlane/HUD filenames.
    """
    wanted = {x.upper() for x in linked_fill_types if x}
    assets: list[str] = []
    if not wanted:
        return assets

    # 1. Read explicit asset references from matching fillType nodes.
    for path in source_root.rglob("*.xml"):
        tree = parse_xml_safely(path)
        if not tree:
            continue
        rel_dir = path.parent
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "filltype":
                continue
            name = elem.attrib.get("name") or elem.attrib.get("fillType")
            if not name or name.upper() not in wanted:
                continue
            for node in elem.iter():
                for attr_value in node.attrib.values():
                    value = attr_value.replace(chr(92), "/").strip()
                    if not value or value.startswith("$data/"):
                        continue
                    lower = value.lower()
                    if lower.endswith((".dds", ".png", ".jpg", ".jpeg", ".i3d", ".i3d.shapes")):
                        candidate = value
                        if not candidate.startswith("maps/"):
                            try:
                                abs_candidate = (rel_dir / candidate).resolve()
                                candidate = rel_to_root(abs_candidate, source_root).replace(chr(92), "/")
                            except Exception:
                                pass
                        assets.append(candidate)

    # 2. Fallback filename pattern search under maps/fillPlanes.
    fillplanes = source_root / "maps" / "fillPlanes"
    if fillplanes.exists():
        for fill_type in wanted:
            token = fill_type.lower()
            patterns = [
                f"{token}*.*",
                f"*{token}*.*",
                f"hud_fill_{token}*.*",
            ]
            for pattern in patterns:
                for path in fillplanes.rglob(pattern):
                    if path.is_file() and path.suffix.lower() in {".dds", ".png", ".jpg", ".jpeg"}:
                        assets.append(rel_to_root(path, source_root).replace(chr(92), "/"))

    return unique_preserve_order(sorted(assets))


def insert_linked_fill_types_for_plantation(source: MapProfile, output_root: Path, fill_target_file: str, probe: dict) -> tuple[int, list[str]]:
    wanted = {x.upper() for x in probe.get("linkedFillTypes", [])}
    if not wanted:
        return 0, []
    refs = find_fill_type_refs_by_names(Path(source.root), wanted)
    refs = normalise_filltype_asset_references(Path(source.root), refs)
    return insert_nodes_into_xml(output_root, fill_target_file, refs, set(), ["name", "fillType", "fillTypeName"]) 


def normalise_filltype_asset_references(source_root: Path, refs: list[XmlNodeRef]) -> list[XmlNodeRef]:
    """Rewrite fillType texture references to files that actually exist in the source.

    Some maps define PNG paths in XML while shipping DDS files only. GIANTS will then
    warn that maps/fillPlanes/foo.png cannot be loaded after porting. Prefer the exact
    XML path if it exists; otherwise try sibling .dds/.png alternatives and update the
    copied XML node accordingly.
    """
    result: list[XmlNodeRef] = []
    for ref in refs:
        try:
            elem = ET.fromstring(ref.xml_text)
        except ET.ParseError:
            result.append(ref)
            continue
        rewrite_asset_paths_to_existing_files(source_root, elem)
        result.append(XmlNodeRef(
            file_role=ref.file_role,
            relative_file=ref.relative_file,
            tag=ref.tag,
            attrs=dict(elem.attrib),
            xml_text=ET.tostring(elem, encoding="unicode"),
        ))
    return result


def rewrite_asset_paths_to_existing_files(source_root: Path, elem: ET.Element) -> None:
    for node in elem.iter():
        for attr_name, attr_value in list(node.attrib.items()):
            value = attr_value.replace(chr(92), "/").strip()
            if not value or value.startswith("$data/"):
                continue
            lower = value.lower()
            if not lower.endswith((".png", ".dds", ".jpg", ".jpeg")):
                continue
            replacement = resolve_existing_asset_variant(source_root, value)
            if replacement and replacement != value:
                node.set(attr_name, replacement)


def resolve_existing_asset_variant(source_root: Path, rel_path: str) -> Optional[str]:
    norm = rel_path.replace(chr(92), "/")
    if (source_root / norm).exists():
        return norm
    p = Path(norm)
    suffixes = [".dds", ".png", ".jpg", ".jpeg"]
    for suffix in suffixes:
        candidate = str(p.with_suffix(suffix)).replace(chr(92), "/")
        if (source_root / candidate).exists():
            return candidate
    return None


def insert_linked_height_types_for_plantation(source: MapProfile, output_root: Path, height_target_file: str, probe: dict) -> tuple[int, list[str]]:
    wanted = {x.upper() for x in probe.get("linkedFillTypes", [])}
    if not wanted:
        return 0, []
    refs = find_height_type_refs_by_names(Path(source.root), wanted)
    return insert_nodes_into_xml(output_root, height_target_file, refs, set(), ["name", "fillType", "fillTypeName"])


def find_fill_type_refs_by_names(source_root: Path, wanted: set[str]) -> list[XmlNodeRef]:
    refs: list[XmlNodeRef] = []
    for path in source_root.rglob("*.xml"):
        if not _looks_like_registry_xml(path, "fill_types"):
            continue
        tree = parse_xml_safely(path)
        if not tree:
            continue
        rel = rel_to_root(path, source_root).replace(chr(92), "/")
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "filltype":
                continue
            name = elem.attrib.get("name") or elem.attrib.get("fillType")
            if name and name.upper() in wanted:
                refs.append(XmlNodeRef(
                    file_role="fill_types",
                    relative_file=rel,
                    tag=local_name(elem.tag),
                    attrs=dict(elem.attrib),
                    xml_text=ET.tostring(elem, encoding="unicode"),
                ))
    return refs


def find_height_type_refs_by_names(source_root: Path, wanted: set[str]) -> list[XmlNodeRef]:
    refs: list[XmlNodeRef] = []
    for path in source_root.rglob("*.xml"):
        tree = parse_xml_safely(path)
        if not tree:
            continue
        rel = rel_to_root(path, source_root).replace(chr(92), "/")
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "densitymapheighttype":
                continue
            name = elem.attrib.get("fillTypeName") or elem.attrib.get("fillType") or elem.attrib.get("name")
            if name and name.upper() in wanted:
                refs.append(XmlNodeRef(
                    file_role="height_types",
                    relative_file=rel,
                    tag=local_name(elem.tag),
                    attrs=dict(elem.attrib),
                    xml_text=ET.tostring(elem, encoding="unicode"),
                ))
    return refs


def patch_fill_type_categories_for_names(source: MapProfile, output_root: Path, fill_target_file: str, fill_type_names: set[str]) -> tuple[int, list[str]]:
    warnings: list[str] = []
    names = {x.upper() for x in fill_type_names if x}
    if not names:
        return 0, []
    path = output_root / fill_target_file
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse fillTypes XML for category patching: {fill_target_file}"]
    root = tree.getroot()
    categories = find_child_container(root, "fillTypeCategories")
    if categories is None:
        categories = ET.Element("fillTypeCategories")
        root.append(categories)
    source_categories = find_source_fill_categories_for_crop(source, names)
    if not source_categories:
        warnings.append(
            f"No source fillTypeCategory membership found for linked fillTypes {', '.join(sorted(names))}; "
            "no inferred categories were applied."
        )
        return 0, warnings
    changed = 0
    for category_name in sorted(source_categories):
        category = find_named_category(categories, "fillTypeCategory", category_name)
        if category is None:
            category = ET.Element("fillTypeCategory")
            category.set("name", category_name)
            categories.append(category)
            changed += 1
        for fill_type_name in sorted(names):
            if add_token_to_category_text(category, fill_type_name):
                changed += 1
    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.plantationfillcategories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, warnings


def patch_plantation_l10n(source: MapProfile, output_root: Path, probe: dict) -> tuple[int, list[str]]:
    wanted_terms = {probe.get("crop", "").lower()}
    for ft in probe.get("linkedFillTypes", []):
        wanted_terms.add(ft.lower())
    wanted_terms.update({"drycoffee", "coffeeplantation", "coffeeorchard"})
    return copy_matching_l10n_entries(source, output_root, wanted_terms)


def copy_matching_l10n_entries(source: MapProfile, output_root: Path, wanted_terms: set[str]) -> tuple[int, list[str]]:
    warnings: list[str] = []
    changes = 0
    source_root = Path(source.root)
    source_l10n_files = [Path(x) for x in probe_l10n_files(source_root)]
    if not source_l10n_files:
        return 0, ["No source l10n files found for plantation crop."]
    target_l10n_dir = find_or_create_target_l10n_dir(output_root)
    for src_rel in source_l10n_files:
        src_path = source_root / src_rel
        lang_suffix = extract_l10n_suffix(src_path.name)
        target_path = target_l10n_dir / f"l10n_{lang_suffix}.xml"
        src_tree = parse_xml_safely(src_path)
        if not src_tree:
            continue
        if not target_path.exists():
            write_empty_l10n_file(target_path)
        target_tree = parse_xml_safely(target_path)
        if not target_tree:
            warnings.append(f"Could not parse target l10n file: {target_path}")
            continue
        src_texts = find_child_container(src_tree.getroot(), "texts") or src_tree.getroot()
        tgt_texts = find_child_container(target_tree.getroot(), "texts") or target_tree.getroot()
        existing_names = {elem.attrib.get("name", "") for elem in tgt_texts.iter() if local_name(elem.tag).lower() == "text"}
        for elem in src_texts.iter():
            if local_name(elem.tag).lower() != "text":
                continue
            name = elem.attrib.get("name", "")
            text = elem.attrib.get("text", "")
            haystack = f"{name} {text}".lower()
            if not any(term and term in haystack for term in wanted_terms):
                continue
            if name in existing_names:
                continue
            tgt_texts.append(ET.fromstring(ET.tostring(elem, encoding="unicode")))
            existing_names.add(name)
            changes += 1
        if changes:
            indent_xml(target_tree)
            target_tree.write(target_path, encoding="utf-8", xml_declaration=True)
    return changes, warnings


def probe_l10n_files(root: Path) -> list[str]:
    rels: list[str] = []
    for path in root.rglob("l10n_*.xml"):
        rels.append(rel_to_root(path, root).replace(chr(92), "/"))
    return sorted(rels)


def write_empty_l10n_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""<?xml version="1.0" encoding="utf-8"?>
<l10n>
    <texts>
    </texts>
</l10n>
""", encoding="utf-8")


def extract_l10n_suffix(filename: str) -> str:
    match = re.search(r"l10n_([A-Za-z0-9]+)\.xml", filename)
    return match.group(1) if match else "en"


def find_or_create_target_l10n_dir(output_root: Path) -> Path:
    for dirname in ("languages", "language", "l10n"):
        candidate = output_root / dirname
        if candidate.exists():
            return candidate
    candidate = output_root / "languages"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def patch_moddesc_store_items(output_root: Path, store_items: list[str]) -> tuple[int, list[str]]:
    moddesc = output_root / "modDesc.xml"
    tree = parse_xml_safely(moddesc)
    if not tree:
        return 0, ["Could not parse target modDesc.xml for plantation storeItem patching."]
    root = tree.getroot()
    store_container = find_child_container(root, "storeItems")
    if store_container is None:
        store_container = ET.Element("storeItems")
        root.append(store_container)
    existing = {(elem.attrib.get("xmlFilename") or elem.attrib.get("filename") or "").replace(chr(92), "/").lower() for elem in store_container.iter() if local_name(elem.tag).lower() == "storeitem"}
    changed = 0
    for item in store_items:
        filename = item.split("->", 1)[1].strip() if "->" in item else item.strip()
        if not filename or filename.lower().startswith("$data/"):
            continue
        key = filename.replace(chr(92), "/").lower()
        if key in existing:
            continue
        elem = ET.Element("storeItem")
        elem.set("xmlFilename", filename)        store_container.append(elem)
        existing.add(key)
        changed += 1
    if changed:
        backup = moddesc.with_suffix(moddesc.suffix + ".cropporter.storeitems.bak")
        if not backup.exists():
            shutil.copy2(moddesc, backup)
        indent_xml(tree)
        tree.write(moddesc, encoding="utf-8", xml_declaration=True)
    return changed, []


def patch_plantation_map_hooks(output_root: Path, probe: dict) -> tuple[int, list[str]]:
    map_xml = find_primary_map_xml(output_root)
    if not map_xml:
        return 0, ["No primary target map XML detected for plantation hook patching."]
    tree = parse_xml_safely(map_xml)
    if not tree:
        return 0, [f"Could not parse target map XML: {rel_to_root(map_xml, output_root)}"]
    root = tree.getroot()
    changed = 0
    if ensure_single_filename_node(root, "motionPathEffects", "maps/effects/motionPathEffects.xml"):
        changed += 1
    if ensure_additional_file(root, "maps/effects/motionPathEffects.xml"):
        changed += 1
    if ensure_fruit_requirement(root, str(probe.get("crop", "")).upper()):
        changed += 1
    if changed:
        backup = map_xml.with_suffix(map_xml.suffix + ".cropporter.plantationhooks.bak")
        if not backup.exists():
            shutil.copy2(map_xml, backup)
        indent_xml(tree)
        tree.write(map_xml, encoding="utf-8", xml_declaration=True)
    return changed, []


def find_primary_map_xml(output_root: Path) -> Optional[Path]:
    """Find the map descriptor XML that wires the map subsystems together.

    Map creators do not consistently use ``maps/map.xml``.  Common layouts include
    ``map/map.xml``, ``mapEU/mapEU.xml``, ``mapUS/mapUS.xml`` and creator-specific
    names.  Identify the descriptor by structure instead of directory convention.
    """
    system_tags = {
        "fruittype", "fruittypes", "filltypes", "densitymapheighttypes",
        "foliagedamage", "bales", "environment", "farmlands", "fields",
        "motionpatheffects", "spraytypes", "groundtypes", "licenseplates",
    }
    candidates: list[tuple[int, int, int, int, Path]] = []
    root_resolved = output_root.resolve()

    for path in output_root.rglob("*.xml"):
        if path.name.lower() == "moddesc.xml":
            continue
        try:
            rel = path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        rel_l = rel.as_posix().lower()
        if any(token in rel_l for token in ("/foliage/", "/placeables/", "/translations/", "/language/")):
            continue
        tree = parse_xml_safely(path)
        if not tree:
            continue
        root = tree.getroot()
        if local_name(root.tag).lower() != "map":
            continue

        direct_filename_systems = 0
        direct_known_systems = 0
        for child in list(root):
            tag = local_name(child.tag).lower()
            if tag in system_tags:
                direct_known_systems += 1
                if child.attrib.get("filename"):
                    direct_filename_systems += 1

        # A central map descriptor normally has several direct subsystem hooks.
        if direct_filename_systems < 2 and direct_known_systems < 4:
            continue

        stem_l = path.stem.lower()
        parent_l = path.parent.name.lower()
        conventional = 0 if stem_l in {"map", "mapeu", "mapus", "mapas"} or stem_l == parent_l else 1
        config_penalty = 1 if "/config/" in rel_l else 0
        candidates.append((-direct_filename_systems, conventional, config_penalty, len(rel.parts), path))

    if candidates:
        return sorted(candidates, key=lambda x: (x[0], x[1], x[2], x[3], str(x[4]).lower()))[0][4]

    # Backwards-compatible fallbacks for older layouts.
    legacy = sorted(output_root.glob("maps/map*.xml"))
    if legacy:
        return legacy[0]
    for pattern in ("map*/map*.xml", "map*/map.xml", "map*.xml"):
        matches = sorted(output_root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _resolve_local_map_reference(output_root: Path, map_xml: Path, filename: str) -> Optional[str]:
    """Resolve a non-$data map XML filename to a mod-root-relative path."""
    value = (filename or "").replace(chr(92), "/").strip()
    if not value or value.startswith("$"):
        return None
    value = value.lstrip("/")
    candidates = [output_root / value, map_xml.parent / value]
    for candidate in candidates:
        if candidate.exists():
            try:
                return rel_to_root(candidate, output_root).replace(chr(92), "/")
            except ValueError:
                pass
    # Most FS map descriptors use mod-root-relative paths. Preserve that form even
    # when a referenced file will be created later in the patch.
    return value


def _find_map_registry_node(map_root: ET.Element, role: str) -> Optional[ET.Element]:
    wanted = {
        "fruit_types": "fruittypes",
        "fill_types": "filltypes",
        "height_types": "densitymapheighttypes",
    }.get(role)
    if not wanted:
        return None
    for child in list(map_root):
        if local_name(child.tag).lower() == wanted:
            return child
    return None


def _map_registry_is_inline(node: Optional[ET.Element], role: str) -> bool:
    """Return True when the primary map XML itself contains active registry rows.

    Some FS25 maps (Hobo's Hollow is one example) do not point ``<fruitTypes>`` at
    a separate maps_fruitTypes.xml.  Instead the primary map XML contains direct
    ``<fruitType filename=.../>`` entries, often initially referencing ``$data``.
    That inline container is already the active registry and is a safe insertion
    target; it must not be replaced with, or made to self-reference, a synthetic
    registry file.
    """
    if node is None:
        return False
    if role == "fruit_types":
        for child in list(node):
            tag = local_name(child.tag).lower()
            if tag == "fruittype" and (
                child.attrib.get("filename")
                or child.attrib.get("file")
                or child.attrib.get("name")
            ):
                return True
            if tag == "additionalfiles":
                for nested in child.iter():
                    if local_name(nested.tag).lower() == "additionalfile" and (
                        nested.attrib.get("filename") or nested.attrib.get("file")
                    ):
                        return True
    return False


def _registry_candidate_looks_complete(path: Path, role: str) -> bool:
    """Guard against activating a tiny supplemental registry as a full replacement."""
    tree = parse_xml_safely(path)
    if not tree:
        return False
    root = tree.getroot()
    if role == "fruit_types":
        container = find_child_container(root, "fruitTypes")
        if container is None:
            return False
        files = []
        for child in list(container):
            if local_name(child.tag).lower() != "fruittype":
                continue
            filename = (child.attrib.get("filename") or "").replace(chr(92), "/").lower()
            if filename:
                files.append(filename)
        stems = {Path(x).stem.lower() for x in files}
        core = {"wheat", "barley", "canola", "maize"}
        return len(files) >= 10 and core.issubset(stems)

    if role == "fill_types":
        container = find_child_container(root, "fillTypes")
        if container is None:
            return False
        names = {
            (child.attrib.get("name") or "").upper()
            for child in list(container)
            if local_name(child.tag).lower() == "filltype" and child.attrib.get("name")
        }
        core = {"WHEAT", "BARLEY", "CANOLA", "MAIZE", "STRAW"}
        return len(names) >= 10 and core.issubset(names)
    return False


def _derive_supplemental_fill_registry_path(output_root: Path, map_xml: Path) -> str:
    """Choose a conventional local fillTypes path for a map that has no custom file.

    Prefer the map-data prefix referenced by the primary map XML's i3d filename
    (for example ``mapEU/mapEU.i3d`` -> ``mapEU/config/maps_fillTypes.xml``).
    This is intentionally independent from foliage-root discovery.
    """
    tree = parse_xml_safely(map_xml)
    if tree:
        root = tree.getroot()
        for child in list(root):
            if local_name(child.tag).lower() != "filename":
                continue
            value = (child.text or "").strip().replace(chr(92), "/")
            if not value or value.startswith("$"):
                continue
            parts = [part for part in value.split("/") if part]
            if len(parts) >= 2:
                return f"{parts[0]}/config/maps_fillTypes.xml"

    # Fallback to the map XML stem when it looks like a map namespace.
    stem = map_xml.stem
    if stem.lower().startswith("map"):
        return f"{stem}/config/maps_fillTypes.xml"
    return "maps/config/maps_fillTypes.xml"


def _create_supplemental_fill_registry(output_root: Path, map_xml: Path) -> tuple[str, bool]:
    """Create an empty map-local *supplemental* fillTypes registry if required.

    FS25's FillTypeManager loads ``data/maps/maps_fillTypes.xml`` first and then
    loads the map XML's optional ``fillTypes`` file with ``isBaseType=false``.
    Therefore a map-local file does not need to duplicate all base-game fillTypes;
    it can contain only imported additions/overrides.
    """
    rel = _derive_supplemental_fill_registry_path(output_root, map_xml)
    path = output_root / rel
    if path.exists():
        return rel, False

    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("map")
    root.append(ET.Element("fillTypes"))
    root.append(ET.Element("fillTypeCategories"))
    tree = ET.ElementTree(root)
    indent_xml(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return rel, True



def _derive_supplemental_height_registry_path(output_root: Path, map_xml: Path) -> str:
    """Choose a conventional map-local densityMapHeightTypes path.

    The primary map XML's I3D filename is the most reliable namespace hint:
    ``map/map.i3d`` -> ``map/config/densityMapHeightTypes.xml`` and
    ``mapEU/mapEU.i3d`` -> ``mapEU/config/densityMapHeightTypes.xml``.
    """
    tree = parse_xml_safely(map_xml)
    if tree:
        root = tree.getroot()
        for child in list(root):
            if local_name(child.tag).lower() != "filename":
                continue
            value = (child.text or "").strip().replace(chr(92), "/")
            if not value or value.startswith("$"):
                continue
            parts = [part for part in value.split("/") if part]
            if len(parts) >= 2:
                return f"{parts[0]}/config/densityMapHeightTypes.xml"

    stem = map_xml.stem
    if stem.lower().startswith("map"):
        return f"{stem}/config/densityMapHeightTypes.xml"
    return "maps/config/densityMapHeightTypes.xml"


def _infer_height_registry_channels_from_i3d(target_root: Path) -> tuple[int, int]:
    """Infer the target's current height-type bit range from terrainDetailHeight.

    This is used only when the target has no local densityMapHeightTypes XML.  The
    I3D remains authoritative for the currently compiled densityMap_height layout.
    """
    i3d = find_primary_target_i3d_for_fruits(target_root)
    if i3d is None:
        return 0, 6
    tree = parse_xml_safely(i3d)
    if not tree:
        return 0, 6
    layer = find_terrain_detail_height_layer(tree.getroot())
    if layer is None:
        return 0, 6

    first_channel = 0
    combined = (layer.attrib.get("combinedValuesChannels") or "").split()
    if combined:
        try:
            first_channel = int(combined[0])
        except ValueError:
            first_channel = 0
    try:
        height_first = int(layer.attrib.get("heightFirstChannel", "0"))
    except ValueError:
        height_first = 0
    type_bits = height_first - first_channel
    if type_bits <= 0:
        # Standard FS25 maps use six height-type bits when no custom registry exists.
        type_bits = 6
    return first_channel, type_bits


def _create_supplemental_height_registry(output_root: Path, map_xml: Path) -> tuple[str, bool]:
    """Create an additive map-local densityMapHeightTypes registry.

    GIANTS loads the default height types before map-specific additions.  Therefore
    this file only needs the imported CropPorter height types; its channel metadata
    is initialized from the target I3D and may then be promoted by the normal
    capacity/layout synchronizer.
    """
    rel = _derive_supplemental_height_registry_path(output_root, map_xml)
    path = output_root / rel
    if path.exists():
        return rel, False

    first_channel, type_bits = _infer_height_registry_channels_from_i3d(output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("map")
    container = ET.SubElement(root, "densityMapHeightTypes")
    container.set("firstChannel", str(first_channel))
    container.set("numChannels", str(type_bits))
    tree = ET.ElementTree(root)
    indent_xml(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return rel, True


def _active_height_registry_from_moddesc(output_root: Path) -> Optional[str]:
    """Return a local densityMapHeightTypes file already activated by modDesc.xml."""
    moddesc = output_root / "modDesc.xml"
    tree = parse_xml_safely(moddesc) if moddesc.exists() else None
    if not tree:
        return None
    for child in list(tree.getroot()):
        if local_name(child.tag).lower() != "densitymapheighttypes":
            continue
        value = (child.attrib.get("filename") or "").strip().replace(chr(92), "/")
        if not value or value.startswith("$"):
            continue
        candidate = (output_root / value).resolve()
        try:
            rel = rel_to_root(candidate, output_root).replace(chr(92), "/")
        except ValueError:
            continue
        if candidate.exists() and _looks_like_registry_xml(candidate, "height_types"):
            return rel
    return None

def _insert_top_level_registry_hook(root: ET.Element, node: ET.Element, role: str) -> None:
    """Insert a map registry hook near related top-level registry declarations."""
    children = list(root)

    if role == "fill_types":
        # Put fillTypes immediately after the last top-level fruitTypes declaration
        # when possible. This mirrors GIANTS map layouts and keeps the map XML tidy.
        fruit_positions = [
            i for i, child in enumerate(children)
            if local_name(child.tag).lower() == "fruittypes"
        ]
        if fruit_positions:
            root.insert(fruit_positions[-1] + 1, node)
            return

    if role == "height_types":
        # Height types are map-level additions. Keep the hook alongside the other
        # type registries, preferably immediately after fillTypes/fruitTypes.
        related_positions = [
            i for i, child in enumerate(children)
            if local_name(child.tag).lower() in {"fruittypes", "filltypes"}
        ]
        if related_positions:
            root.insert(related_positions[-1] + 1, node)
            return

    # Otherwise place it before decoFoliages/additionalFiles when available.
    preferred_before = {"decofoliages", "additionalfiles"}
    for i, child in enumerate(children):
        if local_name(child.tag).lower() in preferred_before:
            root.insert(i, node)
            return
    root.append(node)


def ensure_active_map_registry(output_root: Path, role: str, fallback_rel_file: Optional[str]) -> tuple[Optional[str], list[str], list[str]]:
    """Resolve and activate the map-local fruit/fill registry used by the target.

    FruitTypes and fillTypes need different treatment:

    * fruitTypes may replace/extend the map's fruit registry and therefore an
      existing complete local registry is preferred before changing the hook;
    * fillTypes are different in FS25: FillTypeManager loads the base-game
      ``data/maps/maps_fillTypes.xml`` first, then loads the map-level ``fillTypes``
      file as non-base data. A target with no ``<fillTypes>`` element (such as
      vanilla Castlereagh) is therefore using the implicit base-game defaults.
      CropPorter can safely create a *supplemental* local fillTypes XML containing
      only the imported types/converters/categories and register it in the map XML.
    """
    actions: list[str] = []
    warnings: list[str] = []
    map_xml = find_primary_map_xml(output_root)
    fallback = (fallback_rel_file or "").replace(chr(92), "/")

    if map_xml is None:
        if fallback and (output_root / fallback).exists():
            warnings.append(f"No primary map XML detected; using detected local {role} registry '{fallback}' without activation verification.")
            return fallback, actions, warnings
        return None, actions, [f"No primary map XML or local {role} registry could be resolved."]

    tree = parse_xml_safely(map_xml)
    if not tree:
        return None, actions, [f"Could not parse primary map XML: {rel_to_root(map_xml, output_root)}"]
    root = tree.getroot()
    node = _find_map_registry_node(root, role)

    # Existing explicit local reference is authoritative.
    if node is not None:
        current = (node.attrib.get("filename") or "").strip()
        current_local = _resolve_local_map_reference(output_root, map_xml, current)
        if current and not current.startswith("$") and current_local and (output_root / current_local).exists():
            return current_local, actions, warnings

        # A filename-less (or $data-backed) <fruitTypes> container with direct
        # fruitType rows is itself the active registry.  Use the primary map XML
        # as the insertion/validation target rather than trying to activate a
        # separate complete fruitTypes file.
        if role == "fruit_types" and _map_registry_is_inline(node, role):
            inline_rel = rel_to_root(map_xml, output_root).replace(chr(92), "/")
            actions.append(f"Using inline fruitTypes registry in {inline_rel}.")
            return inline_rel, actions, warnings

    # densityMapHeightTypes are also map-specific additions layered on top of the
    # engine defaults.  Older/custom maps may rely entirely on the implicit base
    # registry and ship no local file.  If CropPorter needs local height types,
    # create a supplemental registry and activate it in the primary map XML.
    if role == "height_types":
        moddesc_active = _active_height_registry_from_moddesc(output_root)
        if moddesc_active:
            actions.append(f"Using densityMapHeightTypes registry activated by modDesc.xml: {moddesc_active}.")
            return moddesc_active, actions, warnings

        local_rel = ""
        if fallback and (output_root / fallback).exists() and _looks_like_registry_xml(output_root / fallback, role):
            local_rel = fallback
        elif fallback and (output_root / fallback).exists():
            warnings.append(
                f"Ignored non-registry densityMapHeightTypes candidate '{fallback}'; creating a supplemental map-level registry instead."
            )
        created = False
        if not local_rel:
            local_rel, created = _create_supplemental_height_registry(output_root, map_xml)
            if created:
                actions.append(f"Created supplemental densityMapHeightTypes registry: {local_rel}")

        if node is None:
            node = ET.Element("densityMapHeightTypes")
            _insert_top_level_registry_hook(root, node, role)
            old_value = "<implicit base-game default>"
        else:
            old_value = node.attrib.get("filename") or "<empty>"
        node.set("filename", local_rel)

        backup = map_xml.with_suffix(map_xml.suffix + ".cropporter.registryhooks.bak")
        if not backup.exists():
            shutil.copy2(map_xml, backup)
        indent_xml(tree)
        tree.write(map_xml, encoding="utf-8", xml_declaration=True)
        actions.append(
            f"Activated supplemental height_types registry in {rel_to_root(map_xml, output_root)}: {old_value} -> {local_rel}"
        )
        return local_rel, actions, warnings

    # fillTypes are additive to FS25's automatically loaded default registry.
    # If the map does not already provide a local fillTypes file, create a tiny
    # supplemental registry and hook it into the map descriptor.
    if role == "fill_types":
        local_rel = ""
        if fallback and (output_root / fallback).exists() and _looks_like_registry_xml(output_root / fallback, role):
            local_rel = fallback
        elif fallback and (output_root / fallback).exists():
            warnings.append(
                f"Ignored non-registry fillTypes candidate '{fallback}'; creating a supplemental map-level registry instead."
            )
        created = False
        if not local_rel:
            local_rel, created = _create_supplemental_fill_registry(output_root, map_xml)
            if created:
                actions.append(f"Created supplemental fillTypes registry: {local_rel}")

        if node is None:
            node = ET.Element("fillTypes")
            _insert_top_level_registry_hook(root, node, role)
            old_value = "<implicit base-game default>"
        else:
            old_value = node.attrib.get("filename") or "<empty>"
        node.set("filename", local_rel)

        backup = map_xml.with_suffix(map_xml.suffix + ".cropporter.registryhooks.bak")
        if not backup.exists():
            shutil.copy2(map_xml, backup)
        indent_xml(tree)
        tree.write(map_xml, encoding="utf-8", xml_declaration=True)
        actions.append(
            f"Activated supplemental fill_types registry in {rel_to_root(map_xml, output_root)}: {old_value} -> {local_rel}"
        )
        return local_rel, actions, warnings

    # fruitTypes retain the conservative activation rule: do not replace a base
    # registry with a partial/dormant local file unless it looks complete.
    if not fallback or not (output_root / fallback).exists():
        current = (node.attrib.get("filename") or "") if node is not None else ""
        if current.startswith("$"):
            warnings.append(
                f"Primary map XML uses base-game {role} registry '{current}', but no local registry was detected to activate."
            )
        else:
            warnings.append(f"No usable local {role} registry detected for activation.")
        return None, actions, warnings

    fallback_path = output_root / fallback
    if not _registry_candidate_looks_complete(fallback_path, role):
        warnings.append(
            f"Refused to activate '{fallback}' as the map's {role} registry because it does not look like a complete base registry."
        )
        return None, actions, warnings

    tag_name = "fruitTypes" if role == "fruit_types" else "fillTypes"
    if node is None:
        node = ET.Element(tag_name)
        _insert_top_level_registry_hook(root, node, role)
        old_value = "<implicit base-game default>"
    else:
        old_value = node.attrib.get("filename") or "<empty>"
    node.set("filename", fallback)

    backup = map_xml.with_suffix(map_xml.suffix + ".cropporter.registryhooks.bak")
    if not backup.exists():
        shutil.copy2(map_xml, backup)
    indent_xml(tree)
    tree.write(map_xml, encoding="utf-8", xml_declaration=True)
    actions.append(
        f"Activated local {role} registry in {rel_to_root(map_xml, output_root)}: {old_value} -> {fallback}"
    )
    return fallback, actions, warnings

def validate_active_crop_registries(output_root: Path, fruit_rel: Optional[str], fill_rel: Optional[str], crop: CropDefinition) -> dict:
    """Validate the exact registries that the target map descriptor will load."""
    result = {"errors": [], "warnings": [], "details": {}}
    crop_name = crop.fruit_name.upper()

    if not fruit_rel:
        result["errors"].append(f"No active local fruitTypes registry is available for {crop_name}.")
    else:
        path = output_root / fruit_rel
        tree = parse_xml_safely(path)
        refs = []
        if tree:
            container = find_child_container(tree.getroot(), "fruitTypes")
            if container is not None:
                refs = [
                    (x.attrib.get("filename") or "").replace(chr(92), "/")
                    for x in list(container)
                    if local_name(x.tag).lower() == "fruittype" and x.attrib.get("filename")
                ]
        wanted = crop.fruit_name.lower()
        matching = [x for x in refs if f"/{wanted}/" in x.lower() or Path(x).stem.lower() == wanted]
        result["details"]["fruitRegistry"] = fruit_rel
        result["details"]["fruitReferences"] = matching
        if not matching:
            result["errors"].append(f"Active fruitTypes registry '{fruit_rel}' does not reference {crop_name} foliage XML.")

    # Only require fillTypes that the source actually DEFINES in its canonical
    # fillTypes registry. crop.fill_type_names also contains inferred references from
    # foliage XML attributes (for example cutFillType="BARLEY_CUT"), and those may
    # legitimately be supplied by the base game rather than a source-local definition.
    wanted_fill_candidates = {crop_name, crop_name + "_CUT"}
    wanted_fills: set[str] = set()
    for ref in crop.fill_type_nodes:
        try:
            fill_elem = ET.fromstring(ref.xml_text)
        except ET.ParseError:
            continue
        fill_name = normalise_name(find_first_attr(fill_elem, ["name", "fillType", "fillTypeName"])).upper()
        if fill_name in wanted_fill_candidates:
            wanted_fills.add(fill_name)

    result["details"]["requiredFillTypes"] = sorted(wanted_fills)
    if wanted_fills and not fill_rel:
        result["errors"].append(f"No active local fillTypes registry is available for {crop_name}.")
    elif fill_rel:
        path = output_root / fill_rel
        tree = parse_xml_safely(path)
        names: set[str] = set()
        if tree:
            container = find_child_container(tree.getroot(), "fillTypes")
            if container is not None:
                names = {
                    (x.attrib.get("name") or "").upper()
                    for x in list(container)
                    if local_name(x.tag).lower() == "filltype" and x.attrib.get("name")
                }
        result["details"]["fillRegistry"] = fill_rel
        missing = sorted(wanted_fills - names)
        if missing:
            result["errors"].append(
                f"Active fillTypes registry '{fill_rel}' is missing: {', '.join(missing)}."
            )

        # Validate local HUD/fillPlane references for the imported fillTypes.  This
        # catches the common FS25 case where source XML names a .png but the map
        # actually ships the same-stem .dds file.  CropPorter must normalize the
        # XML reference, not merely copy the nearby DDS.
        if tree:
            container = find_child_container(tree.getroot(), "fillTypes")
            missing_assets: list[str] = []
            if container is not None:
                asset_exts = (".dds", ".png", ".jpg", ".jpeg", ".i3d", ".i3d.shapes")
                for fill_elem in list(container):
                    if local_name(fill_elem.tag).lower() != "filltype":
                        continue
                    fill_name = (fill_elem.attrib.get("name") or "").upper()
                    if fill_name not in wanted_fills:
                        continue
                    for node in fill_elem.iter():
                        for value in node.attrib.values():
                            rel_asset = value.replace(chr(92), "/").strip()
                            if not rel_asset or rel_asset.startswith("$"):
                                continue
                            if not rel_asset.lower().endswith(asset_exts):
                                continue
                            root_candidate = output_root / rel_asset
                            local_candidate = path.parent / rel_asset
                            if not root_candidate.exists() and not local_candidate.exists():
                                missing_assets.append(f"{fill_name}: {rel_asset}")
            if missing_assets:
                result["details"]["missingFillTypeAssets"] = sorted(set(missing_assets))
                result["errors"].append(
                    "Active fillTypes registry contains missing local asset reference(s): "
                    + "; ".join(sorted(set(missing_assets)))
                )
    return result


def ensure_single_filename_node(root: ET.Element, tag_name: str, filename: str) -> bool:
    for elem in root.iter():
        if local_name(elem.tag).lower() == tag_name.lower() and elem.attrib.get("filename", "").replace(chr(92), "/").lower() == filename.lower():
            return False
    elem = ET.Element(tag_name)
    elem.set("filename", filename)
    root.append(elem)
    return True


def ensure_additional_file(root: ET.Element, filename: str) -> bool:
    container = ensure_additional_files_container(root)
    key = filename.replace(chr(92), "/").lower()
    for elem in container.iter():
        if local_name(elem.tag).lower() == "additionalfile" and elem.attrib.get("filename", "").replace(chr(92), "/").lower() == key:
            return False
    elem = ET.Element("additionalFile")
    elem.set("filename", filename)
    container.append(elem)
    return True


def ensure_fruit_requirement(root: ET.Element, fruit_type_name: str) -> bool:
    if not fruit_type_name:
        return False
    for elem in root.iter():
        if local_name(elem.tag).lower() == "fruitrequirement" and elem.attrib.get("fruitTypeName", "").upper() == fruit_type_name.upper():
            return False
    # Only create inside an existing fruitRequirements container if the map has one.
    container = find_child_container(root, "fruitRequirements")
    if container is None:
        return False
    elem = ET.Element("fruitRequirement")
    elem.set("fruitTypeName", fruit_type_name.upper())
    container.append(elem)
    return True


def cmd_probe_plantation_crop(args: argparse.Namespace) -> int:
    source = prepare_map_input(Path(args.source))
    try:
        scan_profile(source)
        crop_name = args.crop.lower()
        report = build_plantation_crop_probe(source, crop_name)
        print_plantation_crop_probe(report)
        return 0
    finally:
        source.cleanup()


def build_plantation_crop_probe(source: MapProfile, crop_name: str) -> dict:
    root = Path(source.root)
    crop_l = crop_name.lower()
    crop_u = crop_name.upper()

    report = {
        "version": VERSION,
        "source": source.source_path,
        "crop": crop_name,
        "mode": "unknown",
        "fruit": [],
        "fillTypes": [],
        "linkedFillTypes": [],
        "heightTypes": [],
        "treePlaceables": [],
        "productionPlaceables": [],
        "vehicles": [],
        "effects": [],
        "storeItems": [],
        "mapHooks": [],
        "sellingStations": [],
        "l10n": [],
        "assets": [],
        "warnings": [],
    }

    # Reuse the existing crop probe where possible.
    crop_def = source.crop_defs.get(crop_l)
    if crop_def:
        report["fruit"] = sorted({ref.relative_file for ref in crop_def.fruit_nodes})
        report["fillTypes"] = sorted({extract_named_node_label(ref) for ref in crop_def.fill_type_nodes if extract_named_node_label(ref)})
        report["heightTypes"] = sorted({ref.relative_file for ref in crop_def.height_type_nodes})
        report["assets"] = sorted(crop_def.asset_paths)

    # Broad source scan for plantation/vine-specific dependencies.
    for path in sorted(root.rglob("*.xml")):
        rel = rel_to_root(path, root).replace(chr(92), "/")
        text = read_text_safely(path)
        if not text:
            continue
        text_l = text.lower()
        if crop_l not in text_l and crop_u not in text:
            continue

        rel_l = rel.lower()
        tree = parse_xml_safely(path)

        if "/trees/" in rel_l or "singleton" in rel_l or has_xml_token(text_l, "placeable type=\"vine\"") or (has_xml_token(text_l, "<vine") and "/vehicles/" not in rel_l):
            report["treePlaceables"].append(rel)
        if "/placeables/" in rel_l and ("production" in text_l or "dryer" in text_l or "coffee" in text_l):
            report["productionPlaceables"].append(rel)
        if "/vehicles/" in rel_l or "braud" in rel_l or "vineharvester" in text_l:
            report["vehicles"].append(rel)
        if "/effects/" in rel_l or "motionpatheffect" in text_l:
            report["effects"].append(rel)
        if "sellingstation" in rel_l or "sellingstation" in text_l:
            report["sellingStations"].append(rel)
        if "/l10n" in rel_l or "/language" in rel_l or "/languages" in rel_l:
            report["l10n"].append(rel)

        if tree:
            root_elem = tree.getroot()
            for elem in root_elem.iter():
                tag = local_name(elem.tag).lower()
                attrs = {k: v for k, v in elem.attrib.items()}
                joined = " ".join(str(v) for v in attrs.values()).lower()
                if crop_l not in joined and crop_l not in ((elem.text or "").lower()):
                    continue
                if tag == "storeitem":
                    filename = attrs.get("xmlFilename") or attrs.get("filename") or attrs.get("xmlfilename") or ""
                    report["storeItems"].append(f"{rel} -> {filename}" if filename else rel)
                elif tag in {"input", "output", "capacity", "filltype", "typeDefinition".lower()}:
                    for attr_name in ("fillType", "fillTypes", "filltype", "filltypes"):
                        value = attrs.get(attr_name)
                        if value:
                            for token in re.split(r"[^A-Za-z0-9_]+", value):
                                if token and (crop_u in token.upper() or "COFFEE" in token.upper()):
                                    report["linkedFillTypes"].append(token.upper())
                elif tag in {"motionpatheffects", "motionpatheffect", "fruitrequirement", "additionalfile", "vine"}:
                    report["mapHooks"].append(format_xml_probe_hit(rel, elem))

    # Include neighbouring assets from key crop folders.
    dependency_dirs = {
        f"maps/foliage/{crop_l}",
        f"maps/trees/{crop_l}",
        f"maps/effects/{crop_l}",
        f"maps/placeables/{crop_l}",
        f"maps/placeables/{crop_l}dryer",
    }
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_to_root(path, root).replace(chr(92), "/")
        rel_l = rel.lower()
        if any(rel_l.startswith(dep) for dep in dependency_dirs):
            report["assets"].append(rel)

    # Determine mode.
    if report["treePlaceables"] or any("type=\"vine\"" in x.lower() or "<vine" in x.lower() for x in report["mapHooks"]):
        report["mode"] = "vine/placeable plantation"
    elif report["fruit"]:
        report["mode"] = "field crop or foliage-only crop"

    for key in ["treePlaceables", "productionPlaceables", "vehicles", "effects", "storeItems", "mapHooks", "sellingStations", "l10n", "assets", "linkedFillTypes"]:
        report[key] = unique_preserve_order(sorted(report[key]))

    if report["mode"] == "vine/placeable plantation":
        report["warnings"].append("This crop uses vine/placeable systems. Do not use normal field-crop apply unless explicitly testing.")
    if not report["storeItems"]:
        report["warnings"].append("No storeItem entries were detected for the plantation crop. Planting rows may not be available in the shop without modDesc integration.")
    if not report["treePlaceables"]:
        report["warnings"].append("No tree/placeable row definitions detected. This may not be a plantation crop, or dependencies use non-standard paths.")

    return report


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def extract_named_node_label(ref: XmlNodeRef) -> str:
    try:
        elem = ET.fromstring(ref.xml_text)
    except ET.ParseError:
        return ""
    return elem.attrib.get("name") or elem.attrib.get("fillType") or elem.attrib.get("fillTypeName") or ""


def read_text_safely(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def has_xml_token(text_l: str, token_l: str) -> bool:
    return token_l.lower() in text_l


def format_xml_probe_hit(rel: str, elem: ET.Element) -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in elem.attrib.items())
    tag = local_name(elem.tag)
    if attrs:
        return f"{rel} <{tag} {attrs}>"
    return f"{rel} <{tag}>"


def print_plantation_crop_probe(report: dict) -> None:
    print(f"FS25_CropPorter {report.get('version', VERSION)}")
    print(f"Source: {report.get('source')}")
    print(f"Crop: {report.get('crop')}")
    print(f"Mode: {report.get('mode')}")
    print()

    sections = [
        ("Fruit", "fruit"),
        ("FillTypes", "fillTypes"),
        ("Linked fillTypes", "linkedFillTypes"),
        ("HeightTypes", "heightTypes"),
        ("Tree / row placeables", "treePlaceables"),
        ("Production placeables", "productionPlaceables"),
        ("Vehicles", "vehicles"),
        ("Effects", "effects"),
        ("Store items", "storeItems"),
        ("Map hooks", "mapHooks"),
        ("Selling stations", "sellingStations"),
        ("L10n", "l10n"),
        ("Assets", "assets"),
    ]
    for title, key in sections:
        values = report.get(key) or []
        print(f"{title}: {len(values)}")
        for value in values:
            print(f"- {value}")
        print()

    warnings = report.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")


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


def get_density_fruits_layer_settings(root: Path) -> Optional[dict[str, int]]:
    """Return channel settings from the primary densityMap_fruits FoliageMultiLayer."""
    i3d = find_primary_target_i3d_for_fruits(root)
    if i3d is None:
        return None
    tree = parse_xml_safely(i3d)
    if not tree:
        return None
    layer = find_density_fruits_foliage_multilayer(tree.getroot())
    if layer is None:
        return None
    result: dict[str, int] = {}
    for key in ("numChannels", "numTypeIndexChannels", "compressionChannels"):
        try:
            result[key] = int(layer.attrib.get(key, ""))
        except (TypeError, ValueError):
            pass
    return result


def get_i3d_density_fruits_file_entry(root: ET.Element, layer: ET.Element) -> Optional[ET.Element]:
    """Resolve the <File> used by the densityMap_fruits FoliageMultiLayer."""
    density_id = (
        layer.attrib.get("densityMapId")
        or layer.attrib.get("densityMapID")
        or layer.attrib.get("densityMapFileId")
        or layer.attrib.get("densityMapFileID")
    )
    fallback: Optional[ET.Element] = None
    for elem in root.iter():
        if local_name(elem.tag).lower() != "file":
            continue
        filename = get_i3d_file_filename(elem).replace(chr(92), "/")
        file_id = get_i3d_file_id(elem)
        if density_id and file_id == density_id:
            return elem
        if "densitymap_fruits" in filename.lower():
            fallback = elem
    return fallback


def resolve_grle_converter_executable(converter_hint: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Return (exe, cmd) for a converter hint that may be a folder, EXE, or CMD."""
    hint = converter_hint.expanduser()
    if hint.is_dir():
        exe = hint / "grleConverter.exe"
        cmd = hint / "convert.cmd"
        return (exe if exe.exists() else None, cmd if cmd.exists() else None)

    suffix = hint.suffix.lower()
    if suffix == ".exe":
        return (hint if hint.exists() else None, None)
    if suffix in {".cmd", ".bat"}:
        exe = hint.with_name("grleConverter.exe")
        return (exe if exe.exists() else None, hint if hint.exists() else None)

    return (None, None)


def run_grle_converter(gdm_path: Path, png_path: Path, converter_hint: Optional[Path]) -> list[str]:
    """Convert a GIANTS GDM/GRLE file to PNG using the installed GRLE Converter.

    Prefer grleConverter.exe directly so CropPorter can supply an explicit output path
    and avoid the interactive ``pause`` normally present in convert.cmd.  The CMD is
    retained as a fallback for installations that expose only the wrapper.
    """
    actions: list[str] = []
    hint = Path(converter_hint or DEFAULT_GRLE_CONVERTER)
    exe, cmd = resolve_grle_converter_executable(hint)

    if exe is None and cmd is None:
        raise CropPorterError(
            "A density map requires GDM->PNG conversion, but GIANTS grleConverter "
            f"was not found at '{hint}'. Use --grle-converter to provide convert.cmd, "
            "grleConverter.exe, or its containing folder."
        )

    png_path.parent.mkdir(parents=True, exist_ok=True)
    if png_path.exists():
        png_path.unlink()

    if exe is not None:
        proc = subprocess.run(
            [str(exe), str(gdm_path), "-out", str(png_path)],
            cwd=str(gdm_path.parent),
            capture_output=True,
            text=True,
            errors="replace",
        )
        tool_used = str(exe)
    else:
        if os.name != "nt":
            raise CropPorterError(
                f"Cannot execute Windows converter wrapper '{cmd}' on this platform."
            )
        # convert.cmd normally writes %~n1.png in the current working directory;
        # use the GDM directory as cwd and feed a newline for wrappers containing pause.
        command_line = f'call "{cmd}" "{gdm_path}"'
        proc = subprocess.run(
            ["cmd.exe", "/d", "/s", "/c", command_line],
            cwd=str(gdm_path.parent),
            input="\n",
            capture_output=True,
            text=True,
            errors="replace",
        )
        tool_used = str(cmd)

    if proc.returncode != 0 or not png_path.exists():
        output = "\n".join(x for x in [proc.stdout.strip(), proc.stderr.strip()] if x).strip()
        if len(output) > 2000:
            output = output[-2000:]
        raise CropPorterError(
            "grleConverter failed to create the requested density-map PNG "
            f"(exit {proc.returncode}) using '{tool_used}'."
            + (f" Converter output: {output}" if output else "")
        )

    actions.append(
        f"Converted density map '{gdm_path.name}' -> '{png_path.name}' with GIANTS grleConverter."
    )
    return actions


def set_i3d_density_map_file_id(layer: ET.Element, value: str) -> None:
    """Set the density-map file reference while preserving the target's attribute spelling."""
    for key in ("densityMapId", "densityMapID", "densityMapFileId", "densityMapFileID"):
        if key in layer.attrib:
            layer.set(key, value)
            return
    layer.set("densityMapId", value)


def append_i3d_replacement_file_reference(
    root: ET.Element,
    layer: ET.Element,
    source_file_elem: ET.Element,
    replacement_filename: str,
    preferred_start_id: int = 5000,
) -> tuple[str, bool]:
    """Point a density layer at a replacement file without mutating existing <File> rows.

    CropPorter's safety model treats the target i3d <Files> table as append-only.
    If a compiled density map must be replaced by a PNG, preserve the original
    fileId->filename mapping, append a new <File> under a fresh ID (or reuse an
    already-existing exact filename), then repoint only the layer's densityMapId.

    Returns ``(file_id, appended)``.
    """
    files_container = find_files_container(root)
    if files_container is None:
        raise CropPorterError("No <Files> container found while adding replacement density-map reference.")

    replacement_filename = replacement_filename.replace(chr(92), "/")
    existing_id = find_i3d_file_id_by_filename(files_container, replacement_filename)
    if existing_id:
        set_i3d_density_map_file_id(layer, existing_id)
        return existing_id, False

    used_ids = collect_i3d_foliage_pair_ids(root)
    fresh = next_unused_id(used_ids, preferred_start_id)
    new_file = ET.fromstring(ET.tostring(source_file_elem, encoding="unicode"))
    set_i3d_file_id(new_file, str(fresh))
    set_i3d_file_filename(new_file, replacement_filename)
    files_container.append(new_file)
    set_i3d_density_map_file_id(layer, str(fresh))
    return str(fresh), True


def prepare_density_fruits_png_for_expansion(
    target_root: Path,
    target_i3d: Path,
    tree: ET.ElementTree,
    layer: ET.Element,
    converter_hint: Optional[Path],
) -> tuple[Path, list[str]]:
    """Ensure densityMap_fruits is a PNG before increasing type-index channels.

    This reproduces the proven Brazil/Estancia workflow: when the i3d points at a
    .gdm, convert it with GIANTS grleConverter and change only that <File> reference
    to the generated .png.  If the target already references a valid PNG, leave it.
    """
    root = tree.getroot()
    file_elem = get_i3d_density_fruits_file_entry(root, layer)
    if file_elem is None:
        raise CropPorterError(
            f"Could not resolve the densityMap_fruits <File> entry in {rel_to_root(target_i3d, target_root)}."
        )

    filename = get_i3d_file_filename(file_elem).replace(chr(92), "/")
    if not filename:
        raise CropPorterError("densityMap_fruits <File> entry has no filename.")

    density_path = (target_i3d.parent / filename).resolve()
    suffix = density_path.suffix.lower()
    actions: list[str] = []

    if suffix == ".png":
        if density_path.exists():
            actions.append(
                f"densityMap_fruits already references PNG '{rel_to_root(density_path, target_root)}'; no GRLE conversion required."
            )
            return density_path, actions

        gdm_path = density_path.with_suffix(".gdm")
        if not gdm_path.exists():
            raise CropPorterError(
                f"densityMap_fruits references missing PNG '{rel_to_root(density_path, target_root)}' "
                "and no sibling .gdm was found to convert."
            )
        actions.extend(run_grle_converter(gdm_path, density_path, converter_hint))
        return density_path, actions

    if suffix not in {".gdm", ".grle"}:
        raise CropPorterError(
            f"Unsupported densityMap_fruits file type '{suffix}' in {rel_to_root(target_i3d, target_root)}."
        )

    if not density_path.exists():
        raise CropPorterError(
            f"densityMap_fruits source file does not exist: {rel_to_root(density_path, target_root)}"
        )

    png_path = density_path.with_suffix(".png")
    actions.extend(run_grle_converter(density_path, png_path, converter_hint))

    # Existing target <File> rows are immutable.  Preserve the original compiled
    # GDM/GRLE entry, append a new PNG <File> under a fresh ID, and repoint only
    # this FoliageMultiLayer's densityMapId.  This keeps final append-only safety
    # validation intact on maps whose active densityMap_fruits starts as a GDM.
    png_ref = str(Path(filename).with_suffix(".png")).replace(chr(92), "/")
    old_id = get_i3d_file_id(file_elem) or "?"
    new_id, appended = append_i3d_replacement_file_reference(
        root, layer, file_elem, png_ref
    )
    if appended:
        actions.append(
            "Preserved existing densityMap_fruits File entry "
            f"{old_id} -> '{filename}', appended {new_id} -> '{png_ref}', "
            f"and repointed the foliage layer densityMapId to {new_id}."
        )
    else:
        actions.append(
            f"Reused existing densityMap_fruits PNG File entry {new_id} -> '{png_ref}' "
            f"and repointed the foliage layer densityMapId from {old_id} to {new_id}."
        )
    return png_path, actions


def ensure_density_fruits_capacity_from_source(
    source: MapProfile,
    target_root: Path,
    crops: Optional[list[str]] = None,
    grle_converter: Optional[Path] = None,
) -> tuple[int, list[str], list[str]]:
    """Ensure densityMap_fruits has safe headroom using the proven Brazil workflow.

    The logical layout is ``numTypeIndexChannels + compressionChannels``.  When the
    current type-index width is full after the selected crop family is added, expand
    only the type-index side by the minimum number of bits and keep compressionChannels
    unchanged.  Thus Castlereagh 10/5/5 becomes 11/6/5, and a future 11/6/5 map would
    become 12/7/5 if that capacity were eventually exhausted.

    Before changing the channel declaration, ensure the density map is PNG.  If the
    i3d points at a GDM/GRLE, convert it with GIANTS grleConverter and repoint the i3d
    to the generated PNG, matching the already-proven Brazil/Estancia procedure.
    """
    actions: list[str] = []
    warnings: list[str] = []

    target_i3d = find_primary_target_i3d_for_fruits(target_root)
    if target_i3d is None:
        return 0, actions, ["Could not determine target densityMap_fruits channel settings."]

    tree = parse_xml_safely(target_i3d)
    if not tree:
        return 0, actions, [
            f"Could not parse target i3d for density capacity: {rel_to_root(target_i3d, target_root)}"
        ]

    layer = find_density_fruits_foliage_multilayer(tree.getroot())
    if layer is None:
        return 0, actions, [
            f"No densityMap_fruits FoliageMultiLayer found in {rel_to_root(target_i3d, target_root)}"
        ]

    try:
        target_type_bits = int(layer.attrib.get("numTypeIndexChannels", "0"))
        compression_bits = int(layer.attrib.get("compressionChannels", "0"))
        num_channels = int(layer.attrib.get("numChannels", "0"))
    except ValueError:
        return 0, actions, ["Target densityMap_fruits has invalid channel metadata."]
    if target_type_bits <= 0 or compression_bits < 0:
        return 0, actions, ["Target densityMap_fruits has invalid or missing channel metadata."]

    existing_names: set[str] = set()
    existing_count = 0
    for elem in list(layer):
        if local_name(elem.tag).lower() != "foliagetype":
            continue
        existing_count += 1
        name = (elem.attrib.get("name") or "").strip().lower()
        if name:
            existing_names.add(name)

    planned_names: set[str] = set()
    selected = [c.lower() for c in (crops or [])]
    for crop_name in selected:
        crop = source.crop_defs.get(crop_name)
        if crop is None:
            continue
        family = source_crop_family(source, crop) or [crop_name]
        for family_name in family:
            family_l = family_name.lower()
            if family_l in existing_names:
                continue
            family_crop = source.crop_defs.get(family_l)
            if family_crop is None:
                continue
            if find_source_foliage_layer_bundle(Path(source.root), family_l) is not None:
                planned_names.add(family_l)

    final_count = existing_count + len(planned_names)
    usable_capacity = (2 ** target_type_bits) - 1  # reserve zero/empty index
    layout = f"{num_channels}/{target_type_bits}/{compression_bits}"

    # Keep the current map untouched while at least one usable type index remains.
    # If this import fills/exceeds the usable range, expand now so future crop imports
    # have headroom instead of leaving the map exactly at its limit.
    if final_count < usable_capacity:
        actions.append(
            "densityMap_fruits capacity is sufficient; retained existing channel layout "
            f"{layout} ({existing_count} existing + {len(planned_names)} planned = "
            f"{final_count}, usable capacity {usable_capacity})."
        )
        return 0, actions, warnings

    new_type_bits = target_type_bits
    while final_count >= (2 ** new_type_bits) - 1:
        new_type_bits += 1
    new_num_channels = new_type_bits + compression_bits

    _png_path, png_actions = prepare_density_fruits_png_for_expansion(
        target_root, target_i3d, tree, layer, grle_converter
    )
    actions.extend(png_actions)

    before = f"{num_channels}/{target_type_bits}/{compression_bits}"
    layer.set("numChannels", str(new_num_channels))
    layer.set("numTypeIndexChannels", str(new_type_bits))
    layer.set("compressionChannels", str(compression_bits))
    after = f"{new_num_channels}/{new_type_bits}/{compression_bits}"

    backup = target_i3d.with_suffix(target_i3d.suffix + ".cropporter.density.bak")
    if not backup.exists():
        shutil.copy2(target_i3d, backup)
    indent_xml(tree)
    tree.write(target_i3d, encoding="utf-8", xml_declaration=True)

    actions.append(
        "Expanded densityMap_fruits using Brazil/Estancia strategy: "
        f"{before} -> {after}; kept compressionChannels={compression_bits} and increased "
        f"only numTypeIndexChannels ({existing_count} existing + {len(planned_names)} planned)."
    )
    return 1, actions, warnings

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

    files_container = find_files_container(target_root_elem)
    if files_container is None:
        warnings.append(f"No <Files> container found in target i3d: {rel_to_root(target_i3d, target_root)}")
        return 0, warnings

    # Preserve the target i3d file table exactly. Imported foliage files may only
    # be APPENDED under fresh IDs; existing fileId -> filename mappings are immutable.
    # This guards against accidentally turning an existing entry such as
    # config/foliageDamage.xml into the imported crop XML.
    original_file_table: dict[str, str] = {}
    for _file in list(files_container):
        if local_name(_file.tag).lower() != "file":
            continue
        _fid = get_i3d_file_id(_file)
        if _fid:
            original_file_table[_fid] = get_i3d_file_filename(_file)

    # Do not treat a matching FoliageType name as proof that the crop is already
    # integrated. Some maps contain placeholder/stale foliage entries whose
    # foliageXmlId points at an unrelated <File>. Validate the referenced filename
    # before deciding to skip. If it is wrong, replace/repair the existing entry.
    existing_crop_entry = None
    for child in list(target_layer):
        if element_is_foliage_crop_entry(child, crop_l):
            existing_crop_entry = child
            break

    canonical_foliage_xml = get_canonical_crop_foliage_xml_for_i3d(crop, target_i3d, target_root)
    if existing_crop_entry is not None:
        target_file_lookup = collect_file_entries_by_id(target_root_elem)
        existing_refs = collect_file_ids_referenced_by_element(existing_crop_entry)
        existing_filenames = {
            get_i3d_file_filename(target_file_lookup[ref]).lower()
            for ref in existing_refs if ref in target_file_lookup
        }
        wanted = (canonical_foliage_xml or "").replace(chr(92), "/").lower()
        if wanted and wanted in existing_filenames:
            return 0, warnings
        warnings.append(
            f"Existing foliage entry for crop '{crop.fruit_name}' does not reference its foliage XML; repairing the i3d linkage."
        )

    # Allocate only in the paired File fileId / FoliageType foliageXmlId namespace.
    # Start at the caller's requested value (5000 by default) and choose the first
    # value unused by BOTH sides of that pair. Unrelated i3d IDs are intentionally
    # ignored because they live in different namespaces.
    used_ids = collect_i3d_foliage_pair_ids(target_root_elem)
    id_map: dict[str, str] = {}

    # File IDs are always remapped to a fresh target ID.  Source IDs are only
    # references into the source map and must never be retained or reused in the
    # target. Allocation begins at preferred_start_id (default 5000) and walks
    # upward until the paired fileId/foliageXmlId value is free.
    for file_entry in source_file_entries:
        source_file_id = get_i3d_file_id(file_entry)
        desired_filename = canonical_foliage_xml or get_i3d_file_filename(file_entry)

        new_file = ET.fromstring(ET.tostring(file_entry, encoding="unicode"))
        if desired_filename:
            set_i3d_file_filename(new_file, desired_filename)

        if source_file_id:
            fresh = next_unused_id(used_ids, preferred_start_id)
            used_ids.add(fresh)
            id_map[source_file_id] = str(fresh)
            set_i3d_file_id(new_file, str(fresh))

        # Remap any other references in the copied File node after assigning its
        # own new fileId.
        remap_i3d_ids_and_refs(new_file, id_map)
        files_container.append(new_file)

    # Allocate distinct fresh IDs for any remaining IDs owned/referenced by the
    # foliage entry itself.
    for old_id in sorted(collect_source_ids_to_remap(source_entry, source_file_entries), key=lambda x: int(x) if x.isdigit() else x):
        if old_id in id_map:
            continue
        fresh = next_unused_id(used_ids, preferred_start_id)
        used_ids.add(fresh)
        id_map[old_id] = str(fresh)

    new_entry = ET.fromstring(ET.tostring(source_entry, encoding="unicode"))
    remap_i3d_ids_and_refs(new_entry, id_map)

    # Validate that every numeric XML/file reference on the copied foliage entry
    # resolves to the newly allocated target mapping rather than leaking a source
    # ID through attributes such as foliageXmlId.
    for node in new_entry.iter():
        for key, value in node.attrib.items():
            key_l = key.lower()
            if value.isdigit() and (key_l.endswith("xmlid") or key_l in {"fileid", "fileidref"}):
                if value not in set(id_map.values()):
                    warnings.append(
                        f"Copied foliage reference {key}={value} was not remapped to a fresh target ID for crop '{crop.fruit_name}'."
                    )

    if existing_crop_entry is not None:
        children = list(target_layer)
        try:
            idx = children.index(existing_crop_entry)
            target_layer.remove(existing_crop_entry)
            target_layer.insert(idx, new_entry)
        except ValueError:
            target_layer.append(new_entry)
    else:
        target_layer.append(new_entry)

    # Hard validation: no file mapping that existed before this crop patch may
    # have changed. A crop import is append-only with respect to the <Files> table.
    final_file_lookup = collect_file_entries_by_id(target_root_elem)
    for _fid, _filename in original_file_table.items():
        _current = final_file_lookup.get(_fid)
        _current_filename = get_i3d_file_filename(_current) if _current is not None else None
        if _current_filename != _filename:
            raise CropPorterError(
                f"Refusing unsafe i3d patch for crop '{crop.fruit_name}': existing fileId {_fid} "
                f"changed from '{_filename}' to '{_current_filename}'. Existing <File> entries must never be overwritten."
            )

    # Validate the crop's foliage reference resolves to its canonical foliage XML.
    final_crop_entry = None
    for _child in list(target_layer):
        if element_is_foliage_crop_entry(_child, crop_l):
            final_crop_entry = _child
            break
    if final_crop_entry is None:
        raise CropPorterError(f"i3d patch validation failed: no foliage entry exists for crop '{crop.fruit_name}'.")
    _refs = collect_file_ids_referenced_by_element(final_crop_entry)
    _resolved = {
        get_i3d_file_filename(final_file_lookup[_ref]).replace(chr(92), "/").lower()
        for _ref in _refs if _ref in final_file_lookup
    }
    _wanted = (canonical_foliage_xml or "").replace(chr(92), "/").lower()
    if _wanted and _wanted not in _resolved:
        raise CropPorterError(
            f"i3d patch validation failed for crop '{crop.fruit_name}': foliage entry does not resolve to '{canonical_foliage_xml}'."
        )

    # Hard validation of the imported pair. Every source foliage XML file ID that
    # was remapped must exist as a new <File> entry and be referenced by the copied
    # foliage node using the same numeric value.
    current_file_lookup = collect_file_entries_by_id(target_root_elem)
    copied_refs = collect_file_ids_referenced_by_element(new_entry)
    for source_file_id, target_file_id in id_map.items():
        if source_file_id not in {get_i3d_file_id(x) for x in source_file_entries}:
            continue
        if target_file_id not in current_file_lookup:
            raise CropPorterError(
                f"i3d foliage allocation failed for '{crop.fruit_name}': allocated fileId {target_file_id} was not written."
            )
        if target_file_id not in copied_refs:
            raise CropPorterError(
                f"i3d foliage allocation failed for '{crop.fruit_name}': fileId {target_file_id} is not paired with foliageXmlId {target_file_id}."
            )

    backup = target_i3d.with_suffix(target_i3d.suffix + ".cropporter.bak")
    if not backup.exists():
        shutil.copy2(target_i3d, backup)
    indent_xml(tree)
    tree.write(target_i3d, encoding="utf-8", xml_declaration=True)
    return 1, warnings


def find_source_foliage_layer_bundle(root: Path, crop_l: str) -> Optional[tuple[ET.Element, list[ET.Element]]]:
    """Find the source densityMap_fruits entry for a crop.

    Do not require the crop name to be written directly on the top-level foliage
    node. GIANTS/map authors may identify the foliage via a referenced fileId, with
    the crop name only appearing in the corresponding <File filename=...> entry,
    or on a descendant node.  Package export must preserve those layouts too.
    """
    crop_l = crop_l.lower()

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
            referenced_ids = collect_file_ids_referenced_by_element(elem)
            file_entries = [file_lookup[x] for x in referenced_ids if x in file_lookup]

            direct_match = element_is_foliage_crop_entry(elem, crop_l)
            descendant_match = any(
                child is not elem and element_is_foliage_crop_entry(child, crop_l)
                for child in elem.iter()
            )
            file_match = any(
                value_references_name(
                    (f.attrib.get("filename") or f.attrib.get("file") or ""),
                    crop_l,
                )
                for f in file_entries
            )

            # Some i3d foliage nodes do not expose the referenced fileId in a form
            # our generic reference collector recognises.  If exactly one/more
            # source <File> entries name the crop, use those as an additional signal.
            named_files = [
                f for f in file_lookup.values()
                if value_references_name(
                    (f.attrib.get("filename") or f.attrib.get("file") or ""),
                    crop_l,
                )
            ]

            if direct_match or descendant_match or file_match:
                if not file_entries:
                    file_entries = named_files
                return elem, unique_xml_elements(file_entries)

        # Last-resort association: when the FoliageMultiLayer contains a single
        # top-level node that references one of the crop-named files indirectly.
        named_files = [
            f for f in file_lookup.values()
            if value_references_name(
                (f.attrib.get("filename") or f.attrib.get("file") or ""),
                crop_l,
            )
        ]
        if named_files:
            named_ids = {
                (f.attrib.get("fileId") or f.attrib.get("fileID") or f.attrib.get("id"))
                for f in named_files
            }
            named_ids.discard(None)
            for elem in list(layer):
                refs = collect_file_ids_referenced_by_element(elem)
                if named_ids.intersection(refs):
                    return elem, unique_xml_elements(named_files)

    return None


def unique_xml_elements(elements: Iterable[ET.Element]) -> list[ET.Element]:
    """De-duplicate Element references without relying on Element truthiness."""
    result: list[ET.Element] = []
    seen: set[int] = set()
    for elem in elements:
        marker = id(elem)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(elem)
    return result


def get_canonical_crop_foliage_xml_for_i3d(crop: CropDefinition, target_i3d: Path, target_root: Path) -> Optional[str]:
    """Return the crop definition XML path relative to the target i3d folder.

    A crop may have several ``fruitType`` nodes spread across unrelated files, for
    example ``config/foliageDamage.xml``, ``map.xml`` and the actual
    ``foliage/<crop>/<crop>.xml`` definition.  The first discovered node is therefore
    not a safe canonical choice.  Prefer the real foliage definition and explicitly
    de-prioritise config/registry files.
    """
    if not crop.fruit_nodes:
        return None

    crop_l = crop.fruit_name.lower()
    candidates: list[tuple[int, str]] = []
    for node in crop.fruit_nodes:
        rel = node.relative_file.replace(chr(92), "/")
        rel_l = rel.lower()
        base_l = Path(rel_l).name
        score = 0

        # The strongest signal is the normal GIANTS crop definition layout.
        if "/foliage/" in f"/{rel_l}":
            score += 100
        if base_l == f"{crop_l}.xml":
            score += 80
        if f"/foliage/{crop_l}/" in f"/{rel_l}":
            score += 60
        if value_references_name(base_l, crop_l):
            score += 20

        # These files can legitimately contain fruitType nodes, but they are not
        # foliage-definition XMLs and must never become foliageXmlId targets.
        if any(token in rel_l for token in (
            "/config/foliagedamage.xml",
            "/config/maps_fruittypes.xml",
        )):
            score -= 500
        if base_l in {"map.xml", "mapeu.xml", "mapus.xml", "mapas.xml"}:
            score -= 500

        candidates.append((score, rel))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -len(item[1])), reverse=True)
    best_score, rel = candidates[0]
    if best_score <= 0:
        return None

    source_abs = (target_root / rel).resolve()
    try:
        return os.path.relpath(source_abs, target_i3d.parent.resolve()).replace(chr(92), "/")
    except ValueError:
        if rel.startswith("maps/"):
            return rel[len("maps/"):]
        return rel


def get_i3d_file_id(file_elem: ET.Element) -> Optional[str]:
    return file_elem.attrib.get("fileId") or file_elem.attrib.get("fileID") or file_elem.attrib.get("id")


def set_i3d_file_id(file_elem: ET.Element, value: str) -> None:
    if "fileId" in file_elem.attrib:
        file_elem.set("fileId", value)
    elif "fileID" in file_elem.attrib:
        file_elem.set("fileID", value)
    elif "id" in file_elem.attrib:
        file_elem.set("id", value)
    else:
        file_elem.set("fileId", value)


def get_i3d_file_filename(file_elem: ET.Element) -> str:
    return (file_elem.attrib.get("filename") or file_elem.attrib.get("file") or "").replace(chr(92), "/")


def find_i3d_file_id_by_filename(files_container: ET.Element, filename: str) -> Optional[str]:
    wanted = filename.replace(chr(92), "/").lower()
    if not wanted:
        return None
    for elem in list(files_container):
        if local_name(elem.tag).lower() != "file":
            continue
        current = get_i3d_file_filename(elem).lower()        if current == wanted:
            return get_i3d_file_id(elem)
    return None


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
            if (
                "fileid" in key_l
                or key_l in {"file", "filenameid", "foliagexmlid"}
                or key_l.endswith("xmlid")
            ) and value.isdigit():
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
        if key_l in {"name", "filename", "file", "xmlfilename"} and value_references_name(value, crop_l):
            return True
    return False


def foliage_layer_has_crop(layer: ET.Element, crop_l: str) -> bool:
    for elem in list(layer):
        if element_is_foliage_crop_entry(elem, crop_l):
            return True
    return False


def collect_i3d_numeric_ids(root: ET.Element) -> set[int]:
    """Legacy broad ID collector retained for diagnostics/other callers.

    Do NOT use this to allocate foliage XML file IDs. GIANTS i3d files contain many
    independent numeric ID namespaces (node IDs, shape IDs, materials, transforms,
    file IDs, etc.), and those unrelated namespaces must not block a fileId/
    foliageXmlId pair.
    """
    used: set[int] = set()
    for elem in root.iter():
        for key, value in elem.attrib.items():
            key_l = key.lower()
            if not value.isdigit():
                continue
            if key_l == "id" or key_l.endswith("id") or "id" in key_l:
                used.add(int(value))
    return used


def collect_i3d_foliage_pair_ids(root: ET.Element) -> set[int]:
    """Collect only IDs in the paired foliage XML namespace.

    For a foliage definition, GIANTS links:
      <File fileId="N" filename="...crop.xml"/>
      <FoliageType ... foliageXmlId="N"/>

    Allocation for imported crops must therefore consider only existing File fileId
    values and existing foliageXmlId references. Unrelated node/material/shape IDs
    are separate namespaces and must not force the allocator above 5000.
    """
    used: set[int] = set()
    for elem in root.iter():
        tag_l = local_name(elem.tag).lower()
        if tag_l == "file":
            value = elem.attrib.get("fileId") or elem.attrib.get("fileID")
            if value and value.isdigit():
                used.add(int(value))
        for key, value in elem.attrib.items():
            if key.lower() == "foliagexmlid" and value.isdigit():
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



# ---------------------------------------------------------------------------
# v0.3 Crop package / library mode
# ---------------------------------------------------------------------------

PACKAGE_MANIFEST = "cropporter_manifest.json"
PACKAGE_ASSETS_DIR = "assets"
PACKAGE_XML_DIR = "xml"
PACKAGE_SOURCE_DIR = "source"


def ref_to_dict(ref: XmlNodeRef) -> dict:
    return {
        "file_role": ref.file_role,
        "relative_file": ref.relative_file,
        "tag": ref.tag,
        "attrs": ref.attrs,
        "xml_text": ref.xml_text,
    }


def ref_from_dict(data: dict) -> XmlNodeRef:
    return XmlNodeRef(
        file_role=str(data.get("file_role", "")),
        relative_file=str(data.get("relative_file", "")),
        tag=str(data.get("tag", "")),
        attrs=dict(data.get("attrs", {})),
        xml_text=str(data.get("xml_text", "")),
    )


def crop_to_package_dict(crop: CropDefinition) -> dict:
    return {
        "fruit_name": crop.fruit_name,
        "fruit_nodes": [ref_to_dict(x) for x in crop.fruit_nodes],
        "fill_type_names": sorted(crop.fill_type_names),
        "fill_type_nodes": [ref_to_dict(x) for x in crop.fill_type_nodes],
        "height_type_nodes": [ref_to_dict(x) for x in crop.height_type_nodes],
        "growth_nodes": [ref_to_dict(x) for x in crop.growth_nodes],
        "other_nodes": [ref_to_dict(x) for x in crop.other_nodes],
        "asset_paths": sorted(crop.asset_paths),
        "warnings": list(crop.warnings),
    }


def crop_from_package_dict(data: dict) -> CropDefinition:
    crop = CropDefinition(fruit_name=str(data.get("fruit_name", "")).lower())
    crop.fruit_nodes = [ref_from_dict(x) for x in data.get("fruit_nodes", [])]
    crop.fill_type_names = {str(x).upper() for x in data.get("fill_type_names", [])}
    crop.fill_type_nodes = [ref_from_dict(x) for x in data.get("fill_type_nodes", [])]
    crop.height_type_nodes = [ref_from_dict(x) for x in data.get("height_type_nodes", [])]
    crop.growth_nodes = [ref_from_dict(x) for x in data.get("growth_nodes", [])]
    crop.other_nodes = [ref_from_dict(x) for x in data.get("other_nodes", [])]
    crop.asset_paths = {str(x).replace(chr(92), "/") for x in data.get("asset_paths", [])}
    crop.warnings = list(data.get("warnings", []))
    return crop


def source_label(path: str) -> str:
    p = Path(path)
    return safe_name(p.stem or p.name or "source")


def make_package_filename(crop: str, source_name: str) -> str:
    return f"{safe_name(crop.lower())}_{safe_name(source_name.lower())}.cropporter.zip"


def copy_asset_to_package(source_root: Path, package_root: Path, rel: str) -> bool:
    rel_norm = rel.replace(chr(92), "/").lstrip("/")
    src = source_root / rel_norm
    if not src.exists() or not src.is_file():
        return False
    dst = package_root / PACKAGE_ASSETS_DIR / rel_norm
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def collect_crop_i3d_template_assets(source_root: Path, crop_name: str) -> list[str]:
    result: list[str] = []
    bundle = find_source_foliage_layer_bundle(source_root, crop_name.lower())
    if not bundle:
        return result
    entry, file_entries = bundle
    for i3d_path in sorted(source_root.rglob("*.i3d")):
        tree = parse_xml_safely(i3d_path)
        if not tree:
            continue
        layer = find_density_fruits_foliage_multilayer(tree.getroot())
        if layer is None:
            continue
        for child in list(layer):
            if element_is_foliage_crop_entry(child, crop_name.lower()):
                result.append(rel_to_root(i3d_path, source_root).replace(chr(92), "/"))
                break
    for file_entry in file_entries:
        filename = (file_entry.attrib.get("filename") or file_entry.attrib.get("file") or "").replace(chr(92), "/")
        if not filename or filename.startswith("$"):
            continue
        candidates = [source_root / filename, source_root / "maps" / filename]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                result.append(rel_to_root(candidate, source_root).replace(chr(92), "/"))
                shape = Path(str(candidate) + ".shapes")
                if shape.exists():
                    result.append(rel_to_root(shape, source_root).replace(chr(92), "/"))
                break
    return unique_preserve_order(sorted(result))



def infer_crop_map_data_prefix(crop: CropDefinition) -> str:
    """Infer the source map-data prefix from crop foliage paths.

    Examples:
      maps/map/foliage/barley/barley.xml -> maps/map
      maps/mapEU/foliage/wheat/wheat.xml -> maps/mapEU
      maps/foliage/blackbean/blackbean.xml -> maps
    """
    candidates: list[str] = []
    for ref in crop.fruit_nodes:
        rel = ref.relative_file.replace(chr(92), "/").strip("/")
        lower = rel.lower()
        token = "/foliage/"
        if token in lower:
            idx = lower.index(token)
            candidates.append(rel[:idx])
    for rel in crop.asset_paths:
        norm = str(rel).replace(chr(92), "/").strip("/")
        lower = norm.lower()
        token = "/foliage/"
        if token in lower and crop.fruit_name.lower() in lower:
            idx = lower.index(token)
            candidates.append(norm[:idx])
    if not candidates:
        return "maps"
    # Prefer the most frequent, then shortest stable prefix.
    counts: dict[str, int] = {}
    for value in candidates:
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts, key=lambda x: (-counts[x], len(x), x.lower()))[0]


def detect_foliage_root_from_i3d(map_root: Path) -> Optional[str]:
    """Detect a map's local foliage root from its active densityMap_fruits layer.

    This deliberately follows the target map's own FoliageType -> foliageXmlId ->
    <File filename=...> relationships instead of assuming ``foliage`` lives directly
    below map/mapEU/mapUS.  Examples returned are::

        mapEU/foliage
        mapEU/textures/foliage
        maps/mapAS/foliage

    Paths are always relative to ``map_root``.
    """
    candidates: list[str] = []

    for i3d_path in sorted(map_root.rglob("*.i3d")):
        tree = parse_xml_safely(i3d_path)
        if not tree:
            continue
        root_elem = tree.getroot()
        layer = find_density_fruits_foliage_multilayer(root_elem)
        if layer is None:
            continue
        file_lookup = collect_file_entries_by_id(root_elem)

        for foliage_entry in list(layer):
            for file_id in collect_file_ids_referenced_by_element(foliage_entry):
                file_elem = file_lookup.get(file_id)
                if file_elem is None:
                    continue
                filename = get_i3d_file_filename(file_elem).strip()
                if not filename or filename.startswith("$"):
                    continue

                # Resolve the i3d-local file path to a map-root-relative path, then
                # retain everything through the actual ``foliage`` directory.
                absolute = (i3d_path.parent / filename).resolve()
                try:
                    rel = rel_to_root(absolute, map_root).replace(chr(92), "/")
                except ValueError:
                    continue
                parts = rel.split("/")
                foliage_indexes = [i for i, part in enumerate(parts) if part.lower() == "foliage"]
                if not foliage_indexes:
                    continue
                idx = foliage_indexes[-1]
                candidates.append("/".join(parts[:idx + 1]))

    if candidates:
        counts: dict[str, int] = {}
        for value in candidates:
            counts[value] = counts.get(value, 0) + 1
        return sorted(counts, key=lambda x: (-counts[x], len(x), x.lower()))[0]

    # Conservative filesystem fallback for maps whose i3d linkage is unusual.
    fs_candidates: list[tuple[int, int, str]] = []
    common_crop_names = {"wheat", "barley", "canola", "oat", "maize", "sorghum"}
    for directory in map_root.rglob("foliage"):
        if not directory.is_dir():
            continue
        child_names = {x.name.lower() for x in directory.iterdir() if x.is_dir()}
        score = len(common_crop_names.intersection(child_names))
        try:
            rel = rel_to_root(directory, map_root).replace(chr(92), "/")
        except ValueError:
            continue
        fs_candidates.append((-score, len(rel), rel))
    if fs_candidates:
        return sorted(fs_candidates)[0][2]
    return None


def remap_reconstructed_source_foliage_root(snapshot_root: Path, source_foliage_root: str,
                                             target_foliage_root: str) -> list[str]:
    """Project a reconstructed package source onto the target's foliage layout.

    A package may have been exported from ``map/foliage`` while the destination map
    stores crops under ``mapEU/textures/foliage``.  Package mode is an adapter, so the
    temporary reconstructed source is reshaped to match the target before the common
    CropPorter engine scans it.  The package itself is never modified.
    """
    actions: list[str] = []
    old = str(source_foliage_root or "").replace(chr(92), "/").strip("/")
    new = str(target_foliage_root or "").replace(chr(92), "/").strip("/")
    if not old or not new or old.lower() == new.lower():
        return actions

    old_dir = snapshot_root / old
    new_dir = snapshot_root / new
    if old_dir.exists() and old_dir.is_dir():
        new_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(old_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(old_dir)
            dst = new_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or not files_are_same(src, dst):
                shutil.copy2(src, dst)
        shutil.rmtree(old_dir, ignore_errors=True)
        actions.append(f"Remapped reconstructed foliage tree: {old} -> {new}")

    # Registry/config files commonly use root-relative paths such as
    # mapEU/foliage/triticale/triticale.xml.  Rewrite that complete path prefix only;
    # never perform a generic 'foliage' or 'map' replacement.
    textual_exts = {".xml", ".i3d", ".json", ".txt"}
    root_pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(old)}/",
        re.IGNORECASE,
    )
    for path in snapshot_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in textual_exts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        replaced = root_pattern.sub(new + "/", content)
        if replaced != content:
            path.write_text(replaced, encoding="utf-8")

    return actions


def detect_target_map_data_prefix(target_root: Path, profile: Optional[MapProfile] = None) -> str:
    """Detect the target map's own local data prefix without assuming mapEU/mapUS/map.

    First inspect XML files directly at the map-mod root.  A map's primary XML is
    commonly named ``map.xml``, ``mapEU.xml``, ``mapUS.xml``, ``mapAS.xml`` or a
    creator-specific variant containing ``map``.  The filename stem tells us the
    local data prefix used by supporting XML references.

    Example::

        mapEU.xml  -> mapEU
        map.xml    -> map
        mapUS.xml  -> mapUS

    If no usable top-level map XML is present, fall back conservatively to the
    central fruit/fill/height config paths already detected by the scanner.
    """
    root_candidates: list[tuple[int, int, str]] = []

    # Primary rule: direct children of the extracted map-mod root only.
    for path in target_root.glob("*.xml"):
        if not path.is_file():
            continue
        stem = path.stem
        stem_l = stem.lower()
        if "map" not in stem_l or stem_l == "moddesc":
            continue

        # Prefer conventional names beginning with map, then shorter names.
        priority = 0 if stem_l.startswith("map") else 1
        # A sibling folder with the same name is very strong corroborating evidence.
        sibling_bonus = 0 if (target_root / stem).is_dir() else 1
        root_candidates.append((priority, sibling_bonus, len(stem), stem))

    if root_candidates:
        return sorted(root_candidates, key=lambda x: (x[0], x[1], x[2], x[3].lower()))[0][3]

    # Fallback: infer only from central config files, never from foliage/textures/etc.
    config_candidates: list[tuple[int, int, str]] = []
    if profile is not None:
        for role in ("fruit_types", "fill_types", "height_types"):
            rel = find_primary_xml_file(profile, role)
            if not rel:
                continue
            norm = rel.replace(chr(92), "/").strip("/")
            lower = norm.lower()
            marker = "/config/"
            if marker not in lower:
                continue
            prefix = norm[:lower.index(marker)]
            if prefix:
                config_candidates.append((0, len(prefix), prefix))

    if config_candidates:
        return sorted(config_candidates, key=lambda x: (x[0], x[1], x[2].lower()))[0][2]

    # Final conservative fallback.  This avoids inventing mapEU/mapUS.
    return "map"


def collect_crop_foliage_tree(source_root: Path, crop: CropDefinition) -> list[str]:
    """Collect the complete local foliage directory for the crop.

    Dependency scanning is useful, but packaging should be self-contained. Once the
    crop's foliage XML is known, package every file alongside it and below it.
    """
    dirs: set[Path] = set()
    for ref in crop.fruit_nodes:
        rel = ref.relative_file.replace(chr(92), "/").strip("/")
        if "/foliage/" in rel.lower():
            path = source_root / rel
            if path.exists():
                dirs.add(path.parent)

    # Fallback for maps whose registry references are unusual.
    fruit_l = crop.fruit_name.lower()
    for foliage_dir in source_root.rglob("foliage"):
        if not foliage_dir.is_dir():
            continue
        for child in foliage_dir.iterdir():
            if child.is_dir() and child.name.lower() == fruit_l:
                dirs.add(child)

    result: list[str] = []
    for directory in sorted(dirs, key=lambda x: str(x).lower()):
        for path in directory.rglob("*"):
            if path.is_file():
                try:
                    result.append(rel_to_root(path, source_root).replace(chr(92), "/"))
                except ValueError:
                    continue
    return unique_preserve_order(sorted(result))


def remap_map_data_path(value: str, source_prefix: str, target_prefix: str) -> str:
    norm = str(value).replace(chr(92), "/")
    source = source_prefix.replace(chr(92), "/").rstrip("/")
    target = target_prefix.replace(chr(92), "/").rstrip("/")
    if not source or source == target:
        return norm
    if norm.lower() == source.lower():
        return target
    prefix = source + "/"
    if norm.lower().startswith(prefix.lower()):
        return target + norm[len(source):]
    return norm


def remap_xml_node_ref_paths(ref: XmlNodeRef, source_prefix: str, target_prefix: str) -> XmlNodeRef:
    xml_text = ref.xml_text
    # Preserve case outside the source prefix while remapping common slash styles.
    old = source_prefix.replace(chr(92), "/").rstrip("/")
    new = target_prefix.replace(chr(92), "/").rstrip("/")
    if old and old.lower() != new.lower():
        xml_text = re.sub(re.escape(old), lambda _m: new, xml_text, flags=re.IGNORECASE)
    attrs = {
        k: remap_map_data_path(v, source_prefix, target_prefix) if isinstance(v, str) else v
        for k, v in ref.attrs.items()
    }
    return XmlNodeRef(
        file_role=ref.file_role,
        relative_file=remap_map_data_path(ref.relative_file, source_prefix, target_prefix),
        tag=ref.tag,
        attrs=attrs,
        xml_text=xml_text,
    )


def remap_crop_definition_paths(crop: CropDefinition, source_prefix: str, target_prefix: str) -> CropDefinition:
    if not source_prefix or source_prefix.lower() == target_prefix.lower():
        return crop
    remapped = CropDefinition(fruit_name=crop.fruit_name)
    remapped.fruit_nodes = [remap_xml_node_ref_paths(x, source_prefix, target_prefix) for x in crop.fruit_nodes]
    remapped.fill_type_names = set(crop.fill_type_names)
    remapped.fill_type_nodes = [remap_xml_node_ref_paths(x, source_prefix, target_prefix) for x in crop.fill_type_nodes]
    remapped.height_type_nodes = [remap_xml_node_ref_paths(x, source_prefix, target_prefix) for x in crop.height_type_nodes]
    remapped.growth_nodes = [remap_xml_node_ref_paths(x, source_prefix, target_prefix) for x in crop.growth_nodes]
    remapped.other_nodes = [remap_xml_node_ref_paths(x, source_prefix, target_prefix) for x in crop.other_nodes]
    remapped.asset_paths = {remap_map_data_path(x, source_prefix, target_prefix) for x in crop.asset_paths}
    remapped.warnings = list(crop.warnings)
    return remapped


def collect_packaged_crop_registry_xmls(package_root: Path, manifest: dict, crop_name: str, source_prefix: str, target_prefix: str) -> list[tuple[str, str]]:
    """Return (source_rel, target_rel) foliage XMLs that should be registered.

    A crop folder can contain more than one fruit XML.  BallySpring barley is a good
    example: the local barley folder can contain the base barley XML plus additional
    cereal variants.  Package apply therefore reconciles every fruit XML in the crop's
    packaged foliage directory, rather than assuming one XML per crop name.
    """
    assets_root = package_root / PACKAGE_ASSETS_DIR
    crop_l = crop_name.lower()
    results: list[tuple[str, str]] = []

    for raw in manifest.get("assets", []) or []:
        source_rel = str(raw).replace(chr(92), "/").strip("/")
        lower = source_rel.lower()
        if not lower.endswith(".xml") or "/foliage/" not in lower:
            continue

        # Require the XML to live in this crop's foliage folder, but permit mixed
        # case and map/mapEU/mapAS source prefixes.
        parts = source_rel.split("/")
        try:
            foliage_idx = [x.lower() for x in parts].index("foliage")
        except ValueError:
            continue
        if foliage_idx + 1 >= len(parts) or parts[foliage_idx + 1].lower() != crop_l:
            continue

        packaged_xml = assets_root / source_rel
        if not packaged_xml.exists():
            continue
        tree = parse_xml_safely(packaged_xml)
        if not tree:
            continue
        # Only register XMLs that actually contain a fruitType definition/reference.
        if not any(local_name(elem.tag).lower() == "fruittype" for elem in tree.getroot().iter()):
            continue

        target_rel = remap_map_data_path(source_rel, source_prefix, target_prefix)
        results.append((source_rel, target_rel))

    return sorted(set(results), key=lambda x: x[1].lower())


def find_target_fruit_registry_files(target_root: Path, fallback_rel_file: Optional[str]) -> list[str]:
    """Find local map fruitTypes registry/config files worth reconciling.

    Prefer map??/config/*fruitTypes.xml, but also include the resolved active registry
    when a map keeps fruitTypes inline (as seen during the Brazil map work).
    """
    candidates: list[str] = []
    for path in target_root.rglob("*.xml"):
        rel = rel_to_root(path, target_root).replace(chr(92), "/")
        lower = rel.lower()
        name = path.name.lower().replace("_", "")
        if "fruittypes" not in name:
            continue
        if "/config/" in lower or lower.startswith("config/"):
            candidates.append(rel)

    if fallback_rel_file:
        fallback = fallback_rel_file.replace(chr(92), "/")
        if (target_root / fallback).exists():
            candidates.append(fallback)
        active = resolve_active_fruit_types_xml(target_root, fallback)
        if (target_root / active).exists():
            candidates.append(active)

    return unique_preserve_order(candidates)


def registry_reference_matches_crop_xml(filename: str, crop_name: str, xml_basename: str) -> bool:
    norm = filename.replace(chr(92), "/").lower()
    basename = Path(norm).name.lower()
    if basename != xml_basename.lower():
        return False
    # Match $data/foliage/barley/barley.xml, mapEU/foliage/barley/barley.xml,
    # maps/map/foliage/barley/barley.xml, etc.
    return f"/foliage/{crop_name.lower()}/" in "/" + norm.lstrip("/")


def reconcile_packaged_fruit_registry(
    package_root: Path,
    manifest: dict,
    target_root: Path,
    fallback_rel_file: Optional[str],
    crop_name: str,
    source_prefix: str,
    target_prefix: str,
) -> tuple[int, list[str], list[str]]:
    """Redirect existing basegame fruit XML refs to local packaged XMLs and add missing refs.

    For every packaged fruit XML in foliage/<crop>/:
      * if the target registry references that XML via $data, redirect only that
        external/basegame reference to the target map-local path;
      * if the target already has a local reference, leave it untouched;
      * if the XML is absent, add a new local reference using the detected target
        map identifier.
    """
    packaged_xmls = collect_packaged_crop_registry_xmls(
        package_root, manifest, crop_name, source_prefix, target_prefix
    )
    if not packaged_xmls:
        return 0, [], [f"No packaged fruit XMLs found under foliage/{crop_name}/ for registry reconciliation."]

    registry_files = find_target_fruit_registry_files(target_root, fallback_rel_file)
    if not registry_files:
        return 0, [], ["No target fruitTypes registry/config XML found for packaged foliage reconciliation."]

    changed_total = 0
    actions: list[str] = []
    warnings: list[str] = []

    for rel_file in registry_files:
        path = target_root / rel_file
        tree = parse_xml_safely(path)
        if not tree:
            warnings.append(f"Could not parse target fruitTypes registry: {rel_file}")
            continue
        root = tree.getroot()
        fruit_types = find_child_container(root, "fruitTypes")
        if fruit_types is None:
            # A dedicated maps_fruitTypes.xml can itself be the fruitTypes root.
            if local_name(root.tag).lower() == "fruittypes":
                fruit_types = root
            else:
                continue

        registry_style = detect_fruit_registry_style(fruit_types)
        file_changed = 0

        for _source_rel, target_rel in packaged_xmls:
            basename = Path(target_rel).name
            matched = False

            for elem in root.iter():
                tag = local_name(elem.tag).lower()
                if tag not in {"fruittype", "additionalfile"}:
                    continue
                attr_name = "filename" if "filename" in elem.attrib else ("file" if "file" in elem.attrib else None)
                if not attr_name:
                    continue
                current = elem.attrib.get(attr_name, "")
                if not registry_reference_matches_crop_xml(current, crop_name, basename):
                    continue
                matched = True
                current_norm = current.replace(chr(92), "/")
                # Only $data explicitly points outside the map to basegame data.
                # Existing local references belong to the target map and must not be
                # rewritten merely because the package came from a differently named map.
                if current_norm.lower().startswith("$data/"):
                    old_value = current
                    elem.set(attr_name, target_rel)
                    file_changed += 1
                    actions.append(f"{rel_file}: redirected basegame '{old_value}' -> '{target_rel}'.")
                else:
                    actions.append(f"{rel_file}: kept existing local fruit XML reference '{current}'.")

            if matched:
                continue

            # The XML is part of the packaged crop folder but is not currently loaded
            # by the map. Add it using the registry's existing style.
            if registry_style == "additionalFile":
                container = ensure_additional_files_container(root)
                new_ref = ET.Element("additionalFile")
                new_ref.set("filename", target_rel)
                container.append(new_ref)
            else:
                new_ref = ET.Element("fruitType")
                new_ref.set("filename", target_rel)
                fruit_types.append(new_ref)
            file_changed += 1
            actions.append(f"{rel_file}: added missing fruit XML '{target_rel}'.")

        if file_changed:
            backup = path.with_suffix(path.suffix + ".cropporter.registry.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
            indent_xml(tree)
            tree.write(path, encoding="utf-8", xml_declaration=True)
            changed_total += file_changed

    return changed_total, actions, warnings


def open_package(package_path: Path) -> tuple[Path, bool]:
    package_path = package_path.resolve()
    if package_path.is_dir():
        if not (package_path / PACKAGE_MANIFEST).exists():
            raise CropPorterError(f"Package folder is missing {PACKAGE_MANIFEST}: {package_path}")
        return package_path, False
    if package_path.is_file() and package_path.suffix.lower() == ".zip":
        temp_dir = Path(tempfile.mkdtemp(prefix="cropporter_pkg_"))
        try:
            with zipfile.ZipFile(package_path, "r") as zf:
                zf.extractall(temp_dir)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise CropPorterError(f"Not a valid package ZIP: {package_path}") from exc
        if not (temp_dir / PACKAGE_MANIFEST).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise CropPorterError(f"Package ZIP is missing {PACKAGE_MANIFEST}: {package_path}")
        return temp_dir, True
    raise CropPorterError(f"Unsupported package path: {package_path}")


def load_manifest(package_root: Path) -> dict:
    return json.loads((package_root / PACKAGE_MANIFEST).read_text(encoding="utf-8"))


def load_package_crop(package_root: Path, manifest: dict) -> CropDefinition:
    rel = manifest.get("cropDefinition", f"{PACKAGE_XML_DIR}/crop_definition.json")
    path = package_root / rel
    if not path.exists():
        raise CropPorterError(f"Package crop definition is missing: {path}")
    return crop_from_package_dict(json.loads(path.read_text(encoding="utf-8")))


def copy_package_assets(package_root: Path, output_root: Path, source_prefix: str = "", target_prefix: str = "") -> tuple[int, list[str]]:
    assets_root = package_root / PACKAGE_ASSETS_DIR
    if not assets_root.exists():
        return 0, []
    copied = 0
    remapped_paths: list[str] = []
    for src in sorted(assets_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(assets_root).as_posix()
        # Source map i3d files are packaged only as foliage-layer templates. Never
        # install them into the target map; patch_i3d_foliage_layer_for_crop uses
        # the packaged copy internally to transplant just the required crop layer.
        rel_path = Path(rel)
        if (
            rel_path.suffix.lower() == ".i3d"
            and len(rel_path.parts) == 2
            and rel_path.parts[0].lower() == "maps"
            and rel_path.stem.lower().startswith("map")
        ):
            continue

        mapped_rel = remap_map_data_path(rel, source_prefix, target_prefix)
        dst = output_root / mapped_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and files_are_same(src, dst):
            continue
        shutil.copy2(src, dst)
        copied += 1
        if mapped_rel != rel:
            remapped_paths.append(f"{rel} -> {mapped_rel}")
    return copied, remapped_paths


def add_fruit_categories_by_name(output_root: Path, fallback_rel_file: Optional[str], crop_name: str, categories: list[str]) -> tuple[int, list[str]]:
    if not fallback_rel_file:
        return 0, [f"No target fruitTypes XML found for crop '{crop_name}' category patching."]
    if not categories:
        return 0, [f"No fruitTypeCategory memberships were stored in the package for '{crop_name}'."]
    active_rel = resolve_active_fruit_types_xml(output_root, fallback_rel_file)
    path = output_root / active_rel
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fruitTypes XML for package category patching: {active_rel}"]
    root = tree.getroot()
    changed = 0
    for category_name in sorted(set(categories)):
        category = find_fruit_type_category(root, category_name)
        if category is None:
            category = ensure_fruit_type_category(root, category_name)
            changed += 1
        if add_token_to_category_text(category, crop_name.upper()):
            changed += 1
    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.packagecategories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, []


def add_fill_categories_by_name(output_root: Path, fallback_rel_file: Optional[str], fill_type_names: set[str], categories: list[str]) -> tuple[int, list[str]]:
    names = {x.upper() for x in fill_type_names if x}
    if not names:
        return 0, []
    rel = resolve_target_fill_types_xml(output_root, fallback_rel_file)
    if not rel:
        return 0, ["No target fillTypes XML found for package category patching."]
    path = output_root / rel
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fillTypes XML for package category patching: {rel}"]
    root = tree.getroot()
    container = find_child_container(root, "fillTypeCategories")
    if container is None:
        container = ET.Element("fillTypeCategories")
        root.append(container)
    warnings: list[str] = []
    category_names = set(categories or [])
    if not category_names:
        warnings.append(
            f"No stored fillTypeCategory memberships for {', '.join(sorted(names))}; no inferred categories were applied."
        )
        return 0, warnings
    changed = 0
    for category_name in sorted(category_names):
        category = find_named_category(container, "fillTypeCategory", category_name)
        if category is None:
            category = ET.Element("fillTypeCategory")
            category.set("name", category_name)
            container.append(category)
            changed += 1
        for fill_name in sorted(names):
            if add_token_to_category_text(category, fill_name):
                changed += 1
    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.packagefillcategories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, warnings


def package_summary_text(manifest: dict) -> str:
    lines = [
        f"FS25_CropPorter package",
        f"Package version: {manifest.get('packageVersion', 'unknown')}",
        f"Created by: {manifest.get('cropPorterVersion', 'unknown')}",
        f"Crop: {manifest.get('crop', 'unknown')}",
        f"Crop type: {manifest.get('cropType', 'unknown')}",
        f"Source map: {manifest.get('sourceMap', 'unknown')}",
        f"Source map-data path: {manifest.get('sourceMapDataPrefix', 'auto/legacy')}",
        f"Exported at: {manifest.get('exportedAt', 'unknown')}",
        f"Redistribution: {manifest.get('redistribution', 'unknown')}",
        "",
        f"FruitTypes: {', '.join(manifest.get('fruitTypes', [])) or 'none'}",
        f"FillTypes: {', '.join(manifest.get('fillTypes', [])) or 'none'}",
        f"Linked fillTypes: {', '.join(manifest.get('linkedFillTypes', [])) or 'none'}",
        f"Assets: {len(manifest.get('assets', []))}",
        f"Store items: {len(manifest.get('storeItems', []))}",
        f"Converter rules: {len(manifest.get('converterNodes', [])) if isinstance(manifest.get('converterNodes'), list) else ('packaged' if manifest.get('converterNodes') else 0)}",
    ]
    return chr(10).join(lines)



def collect_packaged_fruit_type_names(source_root: Path, crop: CropDefinition) -> set[str]:
    """Collect every fruitType defined by XMLs in the packaged crop foliage folder.

    This is essential for crop families such as BARLEY + WINTERBARLEY, where the
    source registry loads multiple XMLs from one foliage directory.
    """
    names: set[str] = {crop.fruit_name.upper()}
    foliage_files = collect_crop_foliage_tree(source_root, crop)
    for rel in foliage_files:
        if not rel.lower().endswith(".xml"):
            continue
        tree = parse_xml_safely(source_root / rel)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "fruittype":
                continue
            name = elem.attrib.get("name") or elem.attrib.get("fruitType") or elem.attrib.get("fruitTypeName")
            if name:
                names.add(name.upper())
    return names


def collect_package_fill_type_names(source_root: Path, fruit_type_names: set[str], crop: CropDefinition) -> set[str]:
    """Collect fillTypes belonging to every fruitType in the packaged crop family."""
    names = {x.upper() for x in crop.fill_type_names}
    fruit_tokens = {x.upper() for x in fruit_type_names}
    for path in source_root.rglob("*.xml"):
        if not _looks_like_registry_xml(path, "fill_types"):
            continue
        tree = parse_xml_safely(path)
        if not tree:
            continue
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "filltype":
                continue
            name = elem.attrib.get("name") or elem.attrib.get("fillType") or elem.attrib.get("fillTypeName")
            if not name:
                continue
            name_u = name.upper()
            if any(
                name_u == fruit or
                name_u.startswith(fruit + "_") or
                fruit.startswith(name_u + "_")
                for fruit in fruit_tokens
            ):
                names.add(name_u)
    return names


def collect_refs_for_fill_types(source_root: Path, wanted: set[str]) -> list[XmlNodeRef]:
    wanted_u = {x.upper() for x in wanted}
    refs: list[XmlNodeRef] = []
    seen: set[tuple[str, str]] = set()
    for path in source_root.rglob("*.xml"):
        if not _looks_like_registry_xml(path, "fill_types"):
            continue
        tree = parse_xml_safely(path)
        if not tree:
            continue
        rel = rel_to_root(path, source_root).replace(chr(92), "/")
        for elem in tree.getroot().iter():
            if local_name(elem.tag).lower() != "filltype":
                continue
            name = elem.attrib.get("name") or elem.attrib.get("fillType") or elem.attrib.get("fillTypeName")
            if not name or name.upper() not in wanted_u:
                continue
            xml_text = ET.tostring(elem, encoding="unicode")
            key = (rel, xml_text)
            if key in seen:
                continue
            seen.add(key)
            refs.append(XmlNodeRef("fill_types", rel, local_name(elem.tag), dict(elem.attrib), xml_text))
    return refs


def collect_refs_for_height_types(source_root: Path, wanted: set[str]) -> list[XmlNodeRef]:
    wanted_u = {x.upper() for x in wanted}
    refs: list[XmlNodeRef] = []
    seen: set[tuple[str, str]] = set()
    for path in source_root.rglob("*.xml"):
        tree = parse_xml_safely(path)
        if not tree:
            continue
        rel = rel_to_root(path, source_root).replace(chr(92), "/")
        for elem in tree.getroot().iter():
            if "heighttype" not in local_name(elem.tag).lower():
                continue
            name = elem.attrib.get("fillTypeName") or elem.attrib.get("fillType") or elem.attrib.get("name")
            if not name or name.upper() not in wanted_u:
                continue
            xml_text = ET.tostring(elem, encoding="unicode")
            key = (rel, xml_text)
            if key in seen:
                continue
            seen.add(key)
            refs.append(XmlNodeRef("height_types", rel, local_name(elem.tag), dict(elem.attrib), xml_text))
    return refs


def collect_category_memberships(source: MapProfile, fruit_names: set[str], fill_names: set[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    fruit_memberships: dict[str, list[str]] = {}
    for fruit in sorted({x.upper() for x in fruit_names}):
        fruit_memberships[fruit] = sorted(find_source_fruit_categories_for_crop(source, fruit))

    fill_memberships: dict[str, list[str]] = {}
    for fill_name in sorted({x.upper() for x in fill_names}):
        fill_memberships[fill_name] = sorted(find_source_fill_categories_for_crop(source, {fill_name}))
    return fruit_memberships, fill_memberships


def _walk_with_parent(parent: ET.Element):
    for child in list(parent):
        yield parent, child
        yield from _walk_with_parent(child)


def collect_converter_nodes(source_root: Path, relevant_fill_types: set[str], relevant_fruit_types: set[str]) -> list[dict]:
    """Collect crop-specific converter rules such as BLACKBEAN_CUT -> BLACKBEAN/CHAFF."""
    fill_names = {x.upper() for x in relevant_fill_types}
    fruit_names = {x.upper() for x in relevant_fruit_types}
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def token_is_relevant(token: str) -> bool:
        token_u = token.upper()
        if token_u in fill_names or token_u in fruit_names:
            return True
        return any(token_u.startswith(fruit + "_") for fruit in fruit_names)

    for path in source_root.rglob("*.xml"):
        tree = parse_xml_safely(path)
        if not tree:
            continue
        rel = rel_to_root(path, source_root).replace(chr(92), "/")
        # FillType converters belong in fillTypes XML. Fruit-type converter rules
        # live in fruitTypes configuration and must not be transplanted into the
        # fillTypes registry merely because they mention the same crop.
        if "filltype" not in rel.lower():
            continue
        root = tree.getroot()
        for parent, elem in _walk_with_parent(root):
            tag = local_name(elem.tag).lower()
            if tag not in {"converter", "fruittypeconverter", "filltypeconverter"}:
                continue
            tokens: list[str] = []
            for key in ("from", "to", "input", "output", "fruitType", "fillType"):
                value = elem.attrib.get(key)
                if value:
                    tokens.extend(t for t in re.split(r"[^A-Za-z0-9_]+", value) if t)
            if not any(token_is_relevant(token) for token in tokens):
                continue
            xml_text = ET.tostring(elem, encoding="unicode")
            parent_tag = local_name(parent.tag)
            key = (rel, parent_tag, xml_text)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "relative_file": rel,
                "container": parent_tag,
                "tag": local_name(elem.tag),
                "attrs": dict(elem.attrib),
                "xml_text": xml_text,
            })
    return result


def converter_key(elem: ET.Element) -> tuple[str, ...]:
    return (
        local_name(elem.tag).lower(),
        elem.attrib.get("from", "").upper(),
        elem.attrib.get("to", "").upper(),
        elem.attrib.get("input", "").upper(),
        elem.attrib.get("output", "").upper(),
        elem.attrib.get("fruitType", "").upper(),
        elem.attrib.get("fillType", "").upper(),
        elem.attrib.get("factor", ""),
    )


def insert_package_converters(output_root: Path, fill_target_file: str, converter_nodes: list[dict]) -> tuple[int, list[str]]:
    if not converter_nodes:
        return 0, []
    path = output_root / fill_target_file
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fillTypes XML for converter patching: {fill_target_file}"]
    root = tree.getroot()
    warnings: list[str] = []
    changed = 0

    existing = {converter_key(elem) for elem in root.iter() if local_name(elem.tag).lower() in {"converter", "fruittypeconverter", "filltypeconverter"}}

    for item in converter_nodes:
        try:
            elem = ET.fromstring(item.get("xml_text", ""))
        except ET.ParseError:
            warnings.append(f"Could not parse packaged converter node: {item.get('tag', 'converter')}")
            continue
        key = converter_key(elem)
        if key in existing:
            continue

        container_name = str(item.get("container") or "").strip()
        container = find_child_container(root, container_name) if container_name else None
        if container is None:
            # Preserve source structure where possible. Converter containers in GIANTS
            # fillTypes XMLs are normally direct children of the root.
            if container_name and container_name.lower() not in {"map", local_name(root.tag).lower()}:
                container = ET.Element(container_name)
                root.append(container)
            else:
                container = root

        container.append(elem)
        existing.add(key)
        changed += 1

    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.converters.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, warnings


def add_fruit_category_memberships(output_root: Path, fallback_rel_file: Optional[str], memberships: dict[str, list[str]]) -> tuple[int, list[str]]:
    if not fallback_rel_file:
        return 0, ["No target fruitTypes XML found for packaged fruitTypeCategory patching."]
    active_rel = resolve_active_fruit_types_xml(output_root, fallback_rel_file)
    path = output_root / active_rel
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fruitTypes XML for category patching: {active_rel}"]
    root = tree.getroot()
    changed = 0
    warnings: list[str] = []
    for fruit_name, categories in sorted(memberships.items()):
        if not categories:
            warnings.append(f"No stored fruitTypeCategory memberships for '{fruit_name}'.")
            continue
        for category_name in sorted(set(categories)):
            category = find_fruit_type_category(root, category_name)
            if category is None:
                category = ensure_fruit_type_category(root, category_name)
                changed += 1
            if add_token_to_category_text(category, fruit_name.upper()):
                changed += 1
    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.packagecategories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, warnings


def add_fill_category_memberships(output_root: Path, fallback_rel_file: Optional[str], memberships: dict[str, list[str]]) -> tuple[int, list[str]]:
    rel = resolve_target_fill_types_xml(output_root, fallback_rel_file)
    if not rel:
        return 0, ["No target fillTypes XML found for packaged fillTypeCategory patching."]
    path = output_root / rel
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [f"Could not parse target fillTypes XML for category patching: {rel}"]
    root = tree.getroot()
    container = find_child_container(root, "fillTypeCategories")
    if container is None:
        container = ET.Element("fillTypeCategories")
        root.append(container)

    changed = 0
    warnings: list[str] = []
    for fill_name, categories in sorted(memberships.items()):
        category_names = set(categories or [])
        if not category_names:
            # Only infer for the primary crop products; by-products should not be
            # sprayed into every bulk category automatically.
            warnings.append(f"No stored fillTypeCategory memberships for '{fill_name}'.")
            continue
        for category_name in sorted(category_names):
            category = find_named_category(container, "fillTypeCategory", category_name)
            if category is None:
                category = ET.Element("fillTypeCategory")
                category.set("name", category_name)
                container.append(category)
                changed += 1
            if add_token_to_category_text(category, fill_name.upper()):
                changed += 1
    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.packagefillcategories.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, warnings



def copy_source_snapshot_file(source_root: Path, snapshot_root: Path, rel: str) -> bool:
    rel_norm = str(rel).replace(chr(92), "/").lstrip("/")
    if not rel_norm or rel_norm.startswith("$"):
        return False
    src = source_root / rel_norm
    if not src.exists() or not src.is_file():
        return False
    dst = snapshot_root / rel_norm
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def build_package_source_snapshot(source: MapProfile, crop: CropDefinition, snapshot_root: Path, extra_assets: Iterable[str]) -> list[str]:
    """Build a compact source-map snapshot consumed by the normal CropPorter engine.

    The library is deliberately a source cache, not a second porter.  We retain the
    original relative layout for crop assets plus the source configuration files the
    proven apply engine needs to recover categories, converters, fill/height types,
    growth data and i3d foliage-layer templates.
    """
    source_root = Path(source.root)
    wanted: set[str] = {str(x).replace(chr(92), "/") for x in extra_assets if x}
    wanted.update(collect_crop_foliage_tree(source_root, crop))
    wanted.update(collect_crop_i3d_template_assets(source_root, crop.fruit_name))

    for role in ("fruit_types", "fill_types", "height_types", "growth"):
        wanted.update(source.xml_files.get(role, []))

    # Primary map XML/modDesc are cheap and often point at the active config files.
    for path in source_root.glob("*.xml"):
        if path.is_file() and ("map" in path.stem.lower() or path.name.lower() == "moddesc.xml"):
            wanted.add(rel_to_root(path, source_root).replace(chr(92), "/"))
    for path in source_root.glob("maps/map*.xml"):
        if path.is_file():
            wanted.add(rel_to_root(path, source_root).replace(chr(92), "/"))
    wanted.update(probe_l10n_files(source_root))

    copied: list[str] = []
    for rel in sorted(wanted):
        if copy_source_snapshot_file(source_root, snapshot_root, rel):
            copied.append(rel)
    return copied


def remap_snapshot_namespace(snapshot_root: Path, source_prefix: str, target_prefix: str) -> None:
    """Retarget a cached source snapshot to the target map's local namespace.

    This transformation happens only inside the temporary reconstructed source. It
    does not rewrite arbitrary target references. Existing target local references
    remain untouched; registry reconciliation only replaces target $data references.
    """
    old = str(source_prefix or "").replace(chr(92), "/").strip("/")
    new = str(target_prefix or "").replace(chr(92), "/").strip("/")
    if not old or not new or old.lower() == new.lower():
        return

    old_dir = snapshot_root / old
    new_dir = snapshot_root / new
    if old_dir.exists() and old_dir.is_dir():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        if new_dir.exists():
            for src in old_dir.rglob("*"):
                if src.is_file():
                    rel = src.relative_to(old_dir)
                    dst = new_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(src, dst)
            shutil.rmtree(old_dir, ignore_errors=True)
        else:
            shutil.move(str(old_dir), str(new_dir))

    textual_exts = {".xml", ".i3d", ".json", ".txt"}

    # Namespace remapping must only rewrite path prefixes.  A global replacement
    # of a short namespace such as ``map`` corrupts unrelated GIANTS identifiers
    # like densityMap_fruits, densityMap_ground and map-specific attribute names.
    # Match the namespace only when it is a complete path prefix followed by '/'.
    # The negative look-behind prevents changing the tail of another path token.
    path_prefix_pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(old)}/",
        re.IGNORECASE,
    )
    for path in snapshot_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in textual_exts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        replaced = path_prefix_pattern.sub(new + "/", content)
        if replaced != content:
            path.write_text(replaced, encoding="utf-8")


def source_crop_family(source: MapProfile, crop: CropDefinition) -> list[str]:
    names = collect_packaged_fruit_type_names(Path(source.root), crop)
    return sorted({x.lower() for x in names})


def reconcile_source_fruit_registry(source: MapProfile, output_root: Path, fallback_rel_file: str, crop: CropDefinition) -> tuple[int, list[str], list[str]]:
    """Make the target registry load every local foliage XML in the source crop family.

    Existing local target references are preserved. A matching $data reference is
    redirected to the source-local XML, and missing family XMLs are appended.
    """
    source_root = Path(source.root)
    foliage_xmls: list[str] = []
    for rel in collect_crop_foliage_tree(source_root, crop):
        if not rel.lower().endswith(".xml"):
            continue
        tree = parse_xml_safely(source_root / rel)
        if tree and any(local_name(e.tag).lower() == "fruittype" for e in tree.getroot().iter()):
            foliage_xmls.append(rel.replace(chr(92), "/"))
    foliage_xmls = unique_preserve_order(sorted(foliage_xmls))
    if not foliage_xmls:
        return 0, [], [f"No local foliage XMLs found for crop family '{crop.fruit_name}'."]

    active_rel = resolve_active_fruit_types_xml(output_root, fallback_rel_file)
    path = output_root / active_rel
    tree = parse_xml_safely(path)
    if not tree:
        return 0, [], [f"Could not parse active target fruitTypes XML: {active_rel}"]
    root = tree.getroot()
    fruit_types = find_child_container(root, "fruitTypes")
    if fruit_types is None:
        fruit_types = root
    style = detect_fruit_registry_style(fruit_types)
    changed = 0
    actions: list[str] = []

    entries = [e for e in root.iter() if local_name(e.tag).lower() in {"fruittype", "additionalfile"} and e.attrib.get("filename")]
    for local_rel in foliage_xmls:
        base = Path(local_rel).name.lower()
        local_key = local_rel.lower()
        if any(e.attrib.get("filename", "").replace(chr(92), "/").lower() == local_key for e in entries):
            continue

        redirected = False
        for e in entries:
            filename = e.attrib.get("filename", "").replace(chr(92), "/")
            if not filename.lower().startswith("$data"):
                continue
            if Path(filename).name.lower() == base:
                e.set("filename", local_rel)
                changed += 1
                redirected = True
                actions.append(f"Redirected {filename} -> {local_rel}")
                break
        if redirected:
            continue

        if style == "additionalFile":
            wrapper = find_child_container(fruit_types, "additionalFiles")
            if wrapper is None:
                wrapper = ET.Element("additionalFiles")
                fruit_types.append(wrapper)
            elem = ET.Element("additionalFile")
            elem.set("filename", local_rel)
            wrapper.append(elem)
        else:
            elem = ET.Element("fruitType")
            elem.set("filename", local_rel)
            fruit_types.append(elem)
        entries.append(elem)
        changed += 1
        actions.append(f"Added local fruit registry reference: {local_rel}")

    if changed:
        backup = path.with_suffix(path.suffix + ".cropporter.registry.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        indent_xml(tree)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return changed, actions, []


def merge_source_crop_family_semantics(source: MapProfile, output_root: Path, crop: CropDefinition,
                                       fruit_target_file: Optional[str], fill_target_file: Optional[str],
                                       height_target_file: Optional[str]) -> tuple[int, list[str], list[str]]:
    """Merge categories, fill/height types and converters for the complete crop family."""
    changed = 0
    actions: list[str] = []
    warnings: list[str] = []
    source_root = Path(source.root)
    family = {x.upper() for x in source_crop_family(source, crop)}
    fills = collect_package_fill_type_names(source_root, family, crop)

    if fruit_target_file:
        fruit_memberships, fill_memberships = collect_category_memberships(source, family, fills)
        n, w = add_fruit_category_memberships(output_root, fruit_target_file, fruit_memberships)
        changed += n; warnings.extend(w); actions.append(f"Patched {n} crop-family fruitTypeCategory change(s).")
    else:
        fill_memberships = {}

    if fill_target_file:
        refs = normalise_filltype_asset_references(source_root, collect_refs_for_fill_types(source_root, fills))

        # A source-defined fillType belonging directly to the imported fruit family
        # (BARLEY, WINTERBARLEY, WINTERBARLEY_CUT, etc.) is an intentional semantic
        # override. Do not suppress it merely because the target can obtain a same-name
        # base-game fillType implicitly. Only an already-present LOCAL definition should
        # block insertion. Shared/by-product fillTypes such as STRAW continue to use the
        # broader known-fillType de-duplication rules.
        family_owned_names: set[str] = set()
        for member in family:
            family_owned_names.add(member)
            family_owned_names.add(member + "_CUT")

        owned_refs: list[XmlNodeRef] = []
        shared_refs: list[XmlNodeRef] = []
        for ref in refs:
            try:
                fill_elem = ET.fromstring(ref.xml_text)
            except ET.ParseError:
                shared_refs.append(ref)
                continue
            fill_name = normalise_name(find_first_attr(fill_elem, ["name", "fillType", "fillTypeName"])).upper()
            if fill_name in family_owned_names:
                owned_refs.append(ref)
            else:
                shared_refs.append(ref)

        local_fill_names: set[str] = set()
        local_tree = parse_xml_safely(output_root / fill_target_file)
        if local_tree:
            local_container = find_child_container(local_tree.getroot(), "fillTypes")
            if local_container is not None:
                local_fill_names = {
                    normalise_name(x.attrib.get("name")).upper()
                    for x in list(local_container)
                    if local_name(x.tag).lower() == "filltype" and x.attrib.get("name")
                }

        inserted_owned, w = insert_nodes_into_xml(
            output_root, fill_target_file, owned_refs, local_fill_names,
            ["name", "fillType", "fillTypeName"]
        )
        changed += inserted_owned; warnings.extend(w)

        existing_fill_names = collect_target_known_fill_type_names(output_root, fill_target_file)
        inserted_shared, w = insert_nodes_into_xml(
            output_root, fill_target_file, shared_refs, existing_fill_names,
            ["name", "fillType", "fillTypeName"]
        )
        changed += inserted_shared; warnings.extend(w)
        actions.append(
            f"Inserted {inserted_owned + inserted_shared} crop-family fillType node(s) "
            f"({inserted_owned} family-owned override(s), {inserted_shared} shared/by-product)."
        )
        if not fill_memberships:
            _, fill_memberships = collect_category_memberships(source, family, fills)
        n, w = add_fill_category_memberships(output_root, fill_target_file, fill_memberships)
        changed += n; warnings.extend(w); actions.append(f"Patched {n} crop-family fillTypeCategory change(s).")
        converters = collect_converter_nodes(source_root, fills, family)
        n, w = insert_package_converters(output_root, fill_target_file, converters)
        changed += n; warnings.extend(w); actions.append(f"Inserted {n} crop-family converter rule(s).")

    if height_target_file:
        refs = collect_refs_for_height_types(source_root, fills)
        existing_height_names = collect_target_existing_height_type_names(output_root, height_target_file)
        n, w = insert_nodes_into_xml(output_root, height_target_file, refs, existing_height_names, ["name", "fillType", "fillTypeName"])
        changed += n; warnings.extend(w); actions.append(f"Inserted {n} crop-family heightType node(s).")

    return changed, actions, warnings


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def find_terrain_detail_height_layer(root: ET.Element) -> Optional[ET.Element]:
    """Locate the map's tip-to-ground density height DetailLayer."""
    fallback: Optional[ET.Element] = None
    for elem in root.iter():
        if local_name(elem.tag).lower() != "detaillayer":
            continue
        name = (elem.attrib.get("name") or "").strip().lower()
        if name == "terraindetailheight":
            return elem
        if "heightfirstchannel" in {k.lower() for k in elem.attrib}:
            fallback = elem
    return fallback


def get_i3d_density_height_file_entry(root: ET.Element, layer: ET.Element) -> Optional[ET.Element]:
    density_id = (
        layer.attrib.get("densityMapId")
        or layer.attrib.get("densityMapID")
        or layer.attrib.get("densityMapFileId")
        or layer.attrib.get("densityMapFileID")
    )
    fallback: Optional[ET.Element] = None
    for elem in root.iter():
        if local_name(elem.tag).lower() != "file":
            continue
        filename = get_i3d_file_filename(elem).replace(chr(92), "/")
        file_id = get_i3d_file_id(elem)
        if density_id and file_id == density_id:
            return elem
        if "densitymap_height" in filename.lower():
            fallback = elem
    return fallback


def _png_chunks(path: Path) -> list[tuple[bytes, bytes]]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise CropPorterError(f"Not a PNG file: {path}")
    pos = len(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        cdata = data[pos + 8:pos + 8 + length]
        if len(cdata) != length:
            raise CropPorterError(f"Truncated PNG chunk in {path}")
        chunks.append((ctype, cdata))
        pos += 12 + length
        if ctype == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR":
        raise CropPorterError(f"PNG has no IHDR: {path}")
    return chunks


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_scanlines(
    path: Path,
    *,
    expected_bit_depth: int,
    expected_colour_type: int,
    bytes_per_pixel: int,
) -> tuple[int, int, list[bytearray], list[tuple[bytes, bytes]]]:
    """Decode non-interlaced PNG scanlines while preserving the original PNG format.

    GIANTS' grleConverter emits GDM files with more than eight logical channels as
    ordinary RGB8 PNGs: logical bits 0-7 are stored in R, 8-15 in G and 16-23 in B.
    Some older tooling can also expose a 16-bit grayscale density map, so that format
    remains accepted for backwards compatibility.
    """
    chunks = _png_chunks(path)
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise CropPorterError(f"Invalid PNG IHDR in {path}")
    width, height, bit_depth, colour_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", ihdr)
    if (
        bit_depth != expected_bit_depth
        or colour_type != expected_colour_type
        or compression != 0
        or filter_method != 0
        or interlace != 0
    ):
        raise CropPorterError(
            f"Unexpected densityMap_height PNG format in {path}: "
            f"bitDepth={bit_depth}, colourType={colour_type}, interlace={interlace}."
        )

    compressed = b"".join(cdata for ctype, cdata in chunks if ctype == b"IDAT")
    raw = zlib.decompress(compressed)
    row_bytes = width * bytes_per_pixel
    expected = height * (row_bytes + 1)
    if len(raw) != expected:
        raise CropPorterError(
            f"Unexpected densityMap_height PNG payload size in {path}: {len(raw)} != {expected}."
        )

    rows: list[bytearray] = []
    prev = bytearray(row_bytes)
    pos = 0
    for _ in range(height):
        filter_type = raw[pos]
        pos += 1
        scan = raw[pos:pos + row_bytes]
        pos += row_bytes
        recon = bytearray(row_bytes)
        for x, value in enumerate(scan):
            left = recon[x - bytes_per_pixel] if x >= bytes_per_pixel else 0
            up = prev[x]
            up_left = prev[x - bytes_per_pixel] if x >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                predictor = _paeth_predictor(left, up, up_left)
            else:
                raise CropPorterError(f"Unsupported PNG filter {filter_type} in {path}")
            recon[x] = (value + predictor) & 0xFF
        rows.append(recon)
        prev = recon
    return width, height, rows, chunks


def _write_png_scanlines(
    path: Path,
    width: int,
    height: int,
    rows: list[bytearray],
    chunks: list[tuple[bytes, bytes]],
) -> None:
    """Rewrite PNG pixel scanlines without changing IHDR/colour model."""
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter None; deterministic for density-map data
        raw.extend(row)
    new_idat = zlib.compress(bytes(raw), level=6)

    def encode_chunk(ctype: bytes, cdata: bytes) -> bytes:
        crc = binascii.crc32(ctype)
        crc = binascii.crc32(cdata, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(cdata)) + ctype + cdata + struct.pack(">I", crc)

    out = bytearray(PNG_SIGNATURE)
    wrote_idat = False
    for ctype, cdata in chunks:
        if ctype == b"IDAT":
            if not wrote_idat:
                out.extend(encode_chunk(b"IDAT", new_idat))
                wrote_idat = True
            continue
        if ctype == b"IEND" and not wrote_idat:
            out.extend(encode_chunk(b"IDAT", new_idat))
            wrote_idat = True
        out.extend(encode_chunk(ctype, cdata))
    path.write_bytes(out)


def _repack_density_height_value(
    value: int,
    *,
    storage_bits: int,
    old_type_first: int,
    old_type_bits: int,
    old_height_first: int,
    height_bits: int,
    new_type_bits: int,
) -> tuple[int, bool]:
    """Return (new_value, conflict) for one logical densityMap_height pixel."""
    new_height_first = old_type_first + new_type_bits
    storage_mask = (1 << storage_bits) - 1
    old_type_mask = ((1 << old_type_bits) - 1) << old_type_first
    old_height_mask = ((1 << height_bits) - 1) << old_height_first
    old_used_mask = old_type_mask | old_height_mask
    new_type_mask = ((1 << new_type_bits) - 1) << old_type_first
    new_height_mask = ((1 << height_bits) - 1) << new_height_first
    new_used_mask = new_type_mask | new_height_mask

    type_value = (value & old_type_mask) >> old_type_first
    height_value = (value & old_height_mask) >> old_height_first
    passthrough = value & ~old_used_mask & storage_mask
    if passthrough & new_used_mask:
        return value, True
    new_value = passthrough | (type_value << old_type_first) | (height_value << new_height_first)
    return new_value & storage_mask, False


def repack_density_height_png_channels(
    png_path: Path,
    old_type_first: int,
    old_type_bits: int,
    old_height_first: int,
    height_bits: int,
    new_type_bits: int,
) -> tuple[int, int]:
    """Insert extra height-type index bits while preserving existing heap heights.

    GIANTS GDM -> PNG conversion uses RGB8 whenever a density map has more than eight
    logical channels.  The mapping is little-endian across colour components:
    R=bits 0-7, G=bits 8-15, B=bits 16-23.  Castlereagh's 12-channel height map is
    therefore RGB8, not 16-bit grayscale.

    Expanding the type index from 6 to 8 bits must move the existing physical-height
    field from [6-11] to [8-13].  Changing only XML/I3D metadata would reinterpret old
    heap-height bits as height-type bits.
    """
    if old_type_first < 0 or old_type_bits <= 0 or height_bits <= 0:
        raise CropPorterError("Invalid densityMap_height channel layout for migration.")
    new_height_first = old_type_first + new_type_bits

    chunks = _png_chunks(png_path)
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise CropPorterError(f"Invalid PNG IHDR in {png_path}")
    width, height, bit_depth, colour_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", ihdr)

    if bit_depth == 8 and colour_type == 2 and compression == 0 and filter_method == 0 and interlace == 0:
        storage_bits = 24
        bytes_per_pixel = 3
        width, height, rows, chunks = _decode_png_scanlines(
            png_path,
            expected_bit_depth=8,
            expected_colour_type=2,
            bytes_per_pixel=3,
        )
        pixel_format = "RGB8"
    elif bit_depth == 16 and colour_type == 0 and compression == 0 and filter_method == 0 and interlace == 0:
        storage_bits = 16
        bytes_per_pixel = 2
        width, height, rows, chunks = _decode_png_scanlines(
            png_path,
            expected_bit_depth=16,
            expected_colour_type=0,
            bytes_per_pixel=2,
        )
        pixel_format = "GRAY16"
    else:
        raise CropPorterError(
            "densityMap_height PNG must be a GIANTS-compatible non-interlaced RGB8 "
            "(for >8 logical channels) or 16-bit grayscale density image; "
            f"got bitDepth={bit_depth}, colourType={colour_type}, interlace={interlace} in {png_path}."
        )

    new_total = new_height_first + height_bits
    if new_total > storage_bits:
        raise CropPorterError(
            f"densityMap_height requires {new_total} logical channels after expansion, "
            f"exceeding {storage_bits}-bit {pixel_format} storage capacity."
        )
    if new_type_bits <= old_type_bits and new_height_first == old_height_first:
        return 0, new_total

    moved = 0
    nonzero_unused = 0
    for row in rows:
        for x in range(0, len(row), bytes_per_pixel):
            if pixel_format == "RGB8":
                # GIANTS channel packing: R=bits 0-7, G=8-15, B=16-23.
                value = row[x] | (row[x + 1] << 8) | (row[x + 2] << 16)
            else:
                # Standard PNG 16-bit grayscale samples are big-endian.
                value = (row[x] << 8) | row[x + 1]

            new_value, conflict = _repack_density_height_value(
                value,
                storage_bits=storage_bits,
                old_type_first=old_type_first,
                old_type_bits=old_type_bits,
                old_height_first=old_height_first,
                height_bits=height_bits,
                new_type_bits=new_type_bits,
            )
            if conflict:
                nonzero_unused += 1
                continue
            if new_value != value:
                moved += 1

            if pixel_format == "RGB8":
                row[x] = new_value & 0xFF
                row[x + 1] = (new_value >> 8) & 0xFF
                row[x + 2] = (new_value >> 16) & 0xFF
            else:
                row[x] = (new_value >> 8) & 0xFF
                row[x + 1] = new_value & 0xFF

    if nonzero_unused:
        raise CropPorterError(
            "densityMap_height contains non-zero data in channels that the expanded layout needs; "
            f"refusing lossy migration ({nonzero_unused} pixel(s) conflict)."
        )

    backup = png_path.with_suffix(png_path.suffix + ".cropporter.heightlayout.bak")
    if not backup.exists():
        shutil.copy2(png_path, backup)
    _write_png_scanlines(png_path, width, height, rows, chunks)    return moved, new_total

def prepare_density_height_png_for_layout(
    target_root: Path,
    target_i3d: Path,
    tree: ET.ElementTree,
    layer: ET.Element,
    converter_hint: Optional[Path],
) -> tuple[Path, list[str]]:
    root = tree.getroot()
    file_elem = get_i3d_density_height_file_entry(root, layer)
    if file_elem is None:
        raise CropPorterError(
            f"Could not resolve densityMap_height <File> entry in {rel_to_root(target_i3d, target_root)}."
        )
    filename = get_i3d_file_filename(file_elem).replace(chr(92), "/")
    if not filename:
        raise CropPorterError("densityMap_height <File> entry has no filename.")
    density_path = (target_i3d.parent / filename).resolve()
    suffix = density_path.suffix.lower()
    actions: list[str] = []
    if suffix == ".png":
        if density_path.exists():
            actions.append(
                f"densityMap_height already references PNG '{rel_to_root(density_path, target_root)}'; no GRLE conversion required."
            )
            return density_path, actions

        # GIANTS maps commonly keep the I3D reference as .png while shipping only
        # the compiled sibling .gdm.  This is the same layout already handled for
        # densityMap_fruits, so resolve the compiled sibling before declaring the
        # map broken.
        compiled_path = density_path.with_suffix(".gdm")
        if not compiled_path.exists():
            grle_path = density_path.with_suffix(".grle")
            if grle_path.exists():
                compiled_path = grle_path
            else:
                raise CropPorterError(
                    f"densityMap_height references missing PNG '{rel_to_root(density_path, target_root)}' "
                    "and no sibling .gdm/.grle was found to convert."
                )
        actions.extend(run_grle_converter(compiled_path, density_path, converter_hint))
        return density_path, actions
    if suffix not in {".gdm", ".grle"}:
        raise CropPorterError(f"Unsupported densityMap_height file type '{suffix}'.")
    if not density_path.exists():
        raise CropPorterError(f"densityMap_height file does not exist: {rel_to_root(density_path, target_root)}")
    png_path = density_path.with_suffix(".png")
    actions.extend(run_grle_converter(density_path, png_path, converter_hint))
    png_ref = str(Path(filename).with_suffix(".png")).replace(chr(92), "/")
    new_id, appended = append_i3d_replacement_file_reference(root, layer, file_elem, png_ref)
    old_id = get_i3d_file_id(file_elem) or "<unknown>"
    if appended:
        actions.append(
            f"Preserved existing densityMap_height File entry {old_id} -> '{filename}', "
            f"appended {new_id} -> '{png_ref}', and repointed terrainDetailHeight densityMapId to {new_id}."
        )
    else:
        actions.append(
            f"Reused existing densityMap_height PNG File entry {new_id} -> '{png_ref}' and repointed "
            f"terrainDetailHeight densityMapId from {old_id} to {new_id}."
        )
    return png_path, actions


def retire_stale_compiled_density_siblings_for_png(
    png_path: Path,
    target_root: Path,
) -> list[str]:
    """Remove compiled siblings that no longer match an expanded PNG layout.

    GIANTS will prefer a same-stem .gdm/.grle even when the I3D filename ends in
    .png.  After CropPorter changes the logical channel count in the PNG, leaving
    the original compiled 12-channel file beside it causes the engine to load that
    stale file and reject it against the new I3D metadata.

    Preserve a CropPorter backup, then remove only the exact same-stem compiled
    sibling so the engine is forced to consume the migrated PNG.
    """
    actions: list[str] = []
    for suffix in (".gdm", ".grle"):
        compiled = png_path.with_suffix(suffix)
        if not compiled.exists():
            continue
        backup = compiled.with_suffix(compiled.suffix + ".cropporter.preexpanded.bak")
        if not backup.exists():
            shutil.copy2(compiled, backup)
        compiled.unlink()
        actions.append(
            "Retired stale compiled density-map sibling "
            f"'{rel_to_root(compiled, target_root)}' after PNG channel migration; "
            "GIANTS otherwise prefers the old compiled file over the expanded PNG."
        )
    return actions


def synchronize_density_height_layer_layout(
    target_root: Path,
    first_channel: int,
    desired_type_bits: int,
    converter_hint: Optional[Path] = None,
) -> tuple[int, list[str], list[str]]:
    """Keep terrainDetailHeight channel layout consistent with densityMapHeightTypes."""
    actions: list[str] = []
    warnings: list[str] = []
    target_i3d = find_primary_target_i3d_for_fruits(target_root)
    if target_i3d is None:
        return 0, actions, ["Could not locate target map i3d to synchronize terrainDetailHeight channels."]
    tree = parse_xml_safely(target_i3d)
    if not tree:
        return 0, actions, [f"Could not parse target i3d: {rel_to_root(target_i3d, target_root)}"]
    layer = find_terrain_detail_height_layer(tree.getroot())
    if layer is None:
        return 0, actions, [f"No terrainDetailHeight layer found in {rel_to_root(target_i3d, target_root)}"]
    try:
        old_height_first = int(layer.attrib.get("heightFirstChannel", "0"))
        height_bits = int(layer.attrib.get("heightNumChannels", "0"))
        old_total = int(layer.attrib.get("numDensityMapChannels", "0"))
    except ValueError:
        return 0, actions, ["terrainDetailHeight has invalid channel metadata."]
    old_type_bits = old_height_first - first_channel
    if old_type_bits <= 0 or height_bits <= 0:
        return 0, actions, [
            f"Unsupported terrainDetailHeight layout: firstChannel={first_channel}, "
            f"heightFirstChannel={old_height_first}, heightNumChannels={height_bits}."
        ]
    new_height_first = first_channel + desired_type_bits
    new_total = new_height_first + height_bits
    if new_total > 24:
        raise CropPorterError(
            f"Cannot expand densityMapHeightTypes to {desired_type_bits} bits while preserving "
            f"{height_bits} height bits: terrainDetailHeight would require {new_total} channels (>24)."
        )
    if old_type_bits == desired_type_bits and old_height_first == new_height_first and old_total >= new_total:
        actions.append(
            f"terrainDetailHeight layout already matches height-type width {desired_type_bits}: "
            f"type [{first_channel}-{first_channel + desired_type_bits - 1}], "
            f"height [{old_height_first}-{old_height_first + height_bits - 1}]."
        )
        return 0, actions, warnings

    png_path, prep_actions = prepare_density_height_png_for_layout(
        target_root, target_i3d, tree, layer, converter_hint
    )
    actions.extend(prep_actions)
    moved, _ = repack_density_height_png_channels(
        png_path, first_channel, old_type_bits, old_height_first, height_bits, desired_type_bits
    )
    actions.extend(retire_stale_compiled_density_siblings_for_png(png_path, target_root))
    layer.set("numDensityMapChannels", str(new_total))
    layer.set("combinedValuesChannels", f"{first_channel} {desired_type_bits} 0")
    layer.set("heightFirstChannel", str(new_height_first))
    # Preserve the target's physical-height precision and compression width.
    layer.set("heightNumChannels", str(height_bits))
    if "compressionChannels" in layer.attrib:
        layer.set("compressionChannels", str(height_bits))

    backup = target_i3d.with_suffix(target_i3d.suffix + ".cropporter.heightlayout.bak")
    if not backup.exists():
        shutil.copy2(target_i3d, backup)
    indent_xml(tree)
    tree.write(target_i3d, encoding="utf-8", xml_declaration=True)
    actions.append(
        "Expanded terrainDetailHeight type-index layout without overlap: "
        f"type [{first_channel}-{first_channel + old_type_bits - 1}] + "
        f"height [{old_height_first}-{old_height_first + height_bits - 1}] -> "
        f"type [{first_channel}-{first_channel + desired_type_bits - 1}] + "
        f"height [{new_height_first}-{new_height_first + height_bits - 1}]; "
        f"numDensityMapChannels {old_total} -> {new_total}; repacked {moved} non-zero/changed pixel sample(s)."
    )
    return 1, actions, warnings



@dataclass
class GdmHeaderInfo:
    magic: str
    dimension: int
    chunk_size: int
    max_bpp: int
    num_channels: int
    num_compression_ranges: int
    compression_boundaries: list[int]
    type_index_channels: int = 0
    header_size: int = 0


def parse_gdm_header(path: Path) -> GdmHeaderInfo:
    """Read enough of a GIANTS GDM header to validate a save migration."""
    try:
        data = path.read_bytes()[:64]
    except OSError as exc:
        raise CropPorterError(f"Could not read GDM file '{path}': {exc}") from exc
    if len(data) < 9:
        raise CropPorterError(f"GDM file is too small: {path}")

    magic = data[:4]
    if magic == b'"MDF':
        if len(data) < 16:
            raise CropPorterError(f"Extended GDM header is truncated: {path}")
        version = struct.unpack_from("<I", data, 4)[0]
        if version != 0:
            raise CropPorterError(f"Unsupported GDM version {version} in {path}")
        dim_log2 = data[8]
        chunk_log2 = data[9]
        max_bpp = data[10]
        num_channels = data[11]
        num_ranges = data[12]
        type_index_channels = data[13]
        header_size = 16
        magic_label = '"MDF'
    elif magic == b"!MDF":
        dim_log2 = data[4]
        chunk_log2 = data[5]
        max_bpp = data[6]
        num_channels = data[7]
        num_ranges = data[8]
        type_index_channels = 0
        header_size = 9
        magic_label = "!MDF"
    else:
        raise CropPorterError(f"Not a recognised GDM file: {path}")

    if num_ranges < 1:
        raise CropPorterError(f"GDM reports no compression ranges: {path}")
    boundary_count = num_ranges - 1
    if len(data) < header_size + boundary_count:
        raise CropPorterError(f"GDM compression-boundary header is truncated: {path}")
    boundaries = [0]
    boundaries.extend(int(v) for v in data[header_size:header_size + boundary_count])
    boundaries.append(int(num_channels))
    if boundaries != sorted(boundaries) or boundaries[0] != 0 or boundaries[-1] != num_channels:
        raise CropPorterError(f"Invalid GDM compression boundaries {boundaries} in {path}")

    return GdmHeaderInfo(
        magic=magic_label,
        dimension=1 << (dim_log2 + 5),
        chunk_size=1 << chunk_log2,
        max_bpp=max_bpp,
        num_channels=num_channels,
        num_compression_ranges=num_ranges,
        compression_boundaries=boundaries,
        type_index_channels=type_index_channels,
        header_size=header_size,
    )


def _encode_gdm_block(pixels: list[int]) -> bytes:
    """Encode one 32x32 GDM compression-range block.

    Encoding layout follows the documented FS25 GDM block structure verified by
    Paint-a-Farm/grleconvert (MIT): palette blocks for <=4 unique values and
    direct packed values otherwise.

    The encoding mirrors the documented FS25 GDM palette/raw-bit packing used by
    GIANTS density maps.  Small palettes use 1-2 bit indices; larger value sets are
    stored directly at the minimum bit depth required by the maximum value.
    """
    if not pixels:
        return b"\x00\x01\x00\x00"

    unique_values = sorted(set(int(v) for v in pixels))
    out = bytearray()
    if len(unique_values) == 1:
        value = unique_values[0]
        if not 0 <= value <= 0xFFFF:
            raise CropPorterError(f"GDM block value out of range: {value}")
        out.extend((0, 1))
        out.extend(struct.pack("<H", value))
        return bytes(out)

    palette: Optional[list[int]] = None
    if len(unique_values) <= 4:
        bit_depth = 1 if len(unique_values) <= 2 else 2
        palette = unique_values
        out.extend((bit_depth, len(palette)))
        for value in palette:
            if not 0 <= value <= 0xFFFF:
                raise CropPorterError(f"GDM palette value out of range: {value}")
            out.extend(struct.pack("<H", value))
        value_to_index = {value: idx for idx, value in enumerate(palette)}
        values_to_pack = [value_to_index[int(v)] for v in pixels]
    else:
        max_val = unique_values[-1]
        if max_val > 0xFFFF:
            raise CropPorterError(f"GDM raw value out of range: {max_val}")
        bit_depth = max(1, int(max_val).bit_length())
        out.extend((bit_depth, 0))
        values_to_pack = [int(v) for v in pixels]

    bitmap_size = (len(values_to_pack) * bit_depth + 7) // 8
    bitmap = bytearray(bitmap_size)
    mask = (1 << bit_depth) - 1
    for pixel_index, value in enumerate(values_to_pack):
        value &= mask
        bit_pos = pixel_index * bit_depth
        byte_idx = bit_pos // 8
        bit_offset = bit_pos % 8
        bits_left = bit_depth
        shift = 0
        while bits_left:
            take = min(8 - bit_offset, bits_left)
            part_mask = (1 << take) - 1
            bitmap[byte_idx] |= ((value >> shift) & part_mask) << bit_offset
            bits_left -= take
            shift += take
            byte_idx += 1
            bit_offset = 0
    out.extend(bitmap)
    return bytes(out)


def _decode_gdm_block_values(data: bytes, pos: int, chunk_size: int) -> tuple[list[int], int]:
    """Decode one GDM block; used for deterministic encoder regression tests."""
    if pos + 2 > len(data):
        raise CropPorterError("Truncated GDM block header.")
    bit_depth = data[pos]
    palette_count = data[pos + 1]
    palette_size = 2 * palette_count
    total_pixels = chunk_size * chunk_size
    bitmap_size = 0 if bit_depth == 0 else (total_pixels * bit_depth + 7) // 8
    block_size = 2 + palette_size + bitmap_size
    if pos + block_size > len(data):
        raise CropPorterError("Truncated GDM block payload.")
    palette = [
        struct.unpack_from("<H", data, pos + 2 + i * 2)[0]
        for i in range(palette_count)
    ]
    if bit_depth == 0:
        value = palette[0] if palette else 0
        return [value] * total_pixels, block_size

    bitmap = data[pos + 2 + palette_size:pos + block_size]
    mask = (1 << bit_depth) - 1
    pixels: list[int] = []
    for pixel_index in range(total_pixels):
        bit_pos = pixel_index * bit_depth
        byte_idx = bit_pos // 8
        bit_offset = bit_pos % 8
        bits_left = bit_depth
        shift = 0
        packed = 0
        while bits_left:
            take = min(8 - bit_offset, bits_left)
            part_mask = (1 << take) - 1
            packed |= ((bitmap[byte_idx] >> bit_offset) & part_mask) << shift
            bits_left -= take
            shift += take
            byte_idx += 1
            bit_offset = 0
        if bit_depth <= 2 and palette:
            pixels.append(palette[packed] if packed < len(palette) else 0)
        else:
            pixels.append(packed & mask)
    return pixels, block_size


def decode_gdm_logical_values_for_test(path: Path) -> tuple[GdmHeaderInfo, list[int]]:
    """Small internal decoder used by selftest; not part of the save migration path."""
    data = path.read_bytes()
    header = parse_gdm_header(path)
    pos = header.header_size + max(0, header.num_compression_ranges - 1)
    chunks_per_dim = header.dimension // header.chunk_size
    out = [0] * (header.dimension * header.dimension)
    bits_per_range = [
        header.compression_boundaries[i + 1] - header.compression_boundaries[i]
        for i in range(header.num_compression_ranges)
    ]
    for chunk_index in range(chunks_per_dim * chunks_per_dim):
        ranges: list[list[int]] = []
        for _ in range(header.num_compression_ranges):
            values, block_size = _decode_gdm_block_values(data, pos, header.chunk_size)
            ranges.append(values)
            pos += block_size
        chunk_row = chunk_index // chunks_per_dim
        chunk_col = chunk_index % chunks_per_dim
        base_y = chunk_row * header.chunk_size
        base_x = chunk_col * header.chunk_size
        for pixel_index in range(header.chunk_size * header.chunk_size):
            combined = 0
            shift = 0
            for range_index, values in enumerate(ranges):
                combined |= values[pixel_index] << shift
                shift += bits_per_range[range_index]
            py = pixel_index // header.chunk_size
            px = pixel_index % header.chunk_size
            out[(base_y + py) * header.dimension + base_x + px] = combined
    return header, out


def _density_png_format(path: Path) -> tuple[int, int, int, int]:
    chunks = _png_chunks(path)
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise CropPorterError(f"Invalid PNG IHDR in {path}")
    width, height, bit_depth, colour_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if compression != 0 or filter_method != 0 or interlace != 0:
        raise CropPorterError(
            f"Unsupported density PNG format in {path}: compression={compression}, "
            f"filter={filter_method}, interlace={interlace}."
        )
    return width, height, bit_depth, colour_type


def density_png_pixel_fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    """Return dimensions/format plus CRC32 of reconstructed pixel bytes."""
    width, height, bit_depth, colour_type = _density_png_format(path)
    if bit_depth == 8 and colour_type == 2:
        _, _, rows, _ = _decode_png_scanlines(
            path, expected_bit_depth=8, expected_colour_type=2, bytes_per_pixel=3
        )
    elif bit_depth == 8 and colour_type == 0:
        _, _, rows, _ = _decode_png_scanlines(
            path, expected_bit_depth=8, expected_colour_type=0, bytes_per_pixel=1
        )
    elif bit_depth == 16 and colour_type == 0:
        _, _, rows, _ = _decode_png_scanlines(
            path, expected_bit_depth=16, expected_colour_type=0, bytes_per_pixel=2
        )
    else:
        raise CropPorterError(
            f"Unsupported density PNG for fingerprint: bitDepth={bit_depth}, colourType={colour_type} in {path}."
        )
    crc = 0
    for row in rows:
        crc = binascii.crc32(row, crc)
    return width, height, bit_depth, colour_type, crc & 0xFFFFFFFF


def encode_density_png_to_gdm(
    png_path: Path,
    gdm_path: Path,
    *,
    num_channels: int,
    compression_channels: Optional[int],
) -> dict:
    """Encode a GIANTS-compatible density PNG to a modern 0x22 GDM file.

    This is intentionally narrow: square power-of-two density maps, 32x32 chunks,
    RGB8 for >8 logical channels or grayscale8 for <=8.  It is sufficient for
    densityMap_height save migration and avoids requiring Giants Editor merely to
    recompile the migrated save density map.
    """
    if not 1 <= num_channels <= 24:
        raise CropPorterError(f"Unsupported GDM channel count: {num_channels}")

    width, height, bit_depth, colour_type = _density_png_format(png_path)
    if width != height or width < 32 or (width & (width - 1)) != 0:
        raise CropPorterError(f"GDM density image must be square power-of-two >=32; got {width}x{height}.")
    if num_channels > 8:
        if bit_depth != 8 or colour_type != 2:
            raise CropPorterError(
                f"{num_channels}-channel GDM encoding requires RGB8 PNG; got bitDepth={bit_depth}, colourType={colour_type}."
            )
        _, _, rows, _ = _decode_png_scanlines(
            png_path, expected_bit_depth=8, expected_colour_type=2, bytes_per_pixel=3
        )
        bytes_per_pixel = 3
    else:
        if bit_depth != 8 or colour_type != 0:
            raise CropPorterError(
                f"{num_channels}-channel GDM encoding requires grayscale8 PNG; got bitDepth={bit_depth}, colourType={colour_type}."
            )
        _, _, rows, _ = _decode_png_scanlines(
            png_path, expected_bit_depth=8, expected_colour_type=0, bytes_per_pixel=1
        )
        bytes_per_pixel = 1

    dim_log2 = width.bit_length() - 1 - 5
    chunk_size = 32
    chunk_log2 = 5
    if width % chunk_size:
        raise CropPorterError(f"Density image dimension {width} is not divisible by {chunk_size}.")

    if compression_channels is not None and 0 < compression_channels < num_channels:
        boundaries = [0, int(compression_channels), int(num_channels)]
    else:
        boundaries = [0, int(num_channels)]
    bits_per_range = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]
    if any(bits <= 0 or bits > 16 for bits in bits_per_range):
        raise CropPorterError(f"Unsupported GDM compression ranges: {boundaries}")

    output = bytearray()
    output.extend(b'"MDF')
    output.extend(struct.pack("<I", 0))
    output.extend((dim_log2, chunk_log2, 2, num_channels, len(bits_per_range), 0, 0, 0))
    if len(bits_per_range) > 1:
        for boundary in boundaries[1:-1]:
            output.append(boundary)

    chunks_per_dim = width // chunk_size
    logical_mask = (1 << num_channels) - 1
    max_logical_value = 0
    for chunk_row in range(chunks_per_dim):
        base_y = chunk_row * chunk_size
        for chunk_col in range(chunks_per_dim):
            base_x = chunk_col * chunk_size
            chunk_values: list[int] = []
            append_value = chunk_values.append
            for py in range(chunk_size):
                row = rows[base_y + py]
                if bytes_per_pixel == 3:
                    start = base_x * 3
                    for px in range(chunk_size):
                        x = start + px * 3
                        value = row[x] | (row[x + 1] << 8) | (row[x + 2] << 16)
                        if value & ~logical_mask:
                            raise CropPorterError(
                                f"PNG contains non-zero bits above configured {num_channels} channels at "
                                f"pixel ({base_x + px},{base_y + py})."
                            )
                        if value > max_logical_value:
                            max_logical_value = value
                        append_value(value)
                else:
                    for px in range(chunk_size):
                        value = row[base_x + px]
                        if value & ~logical_mask:
                            raise CropPorterError(
                                f"PNG contains non-zero bits above configured {num_channels} channels at "
                                f"pixel ({base_x + px},{base_y + py})."
                            )
                        if value > max_logical_value:
                            max_logical_value = value
                        append_value(value)

            shift = 0
            for range_bits in bits_per_range:
                mask = (1 << range_bits) - 1
                range_pixels = [((value >> shift) & mask) for value in chunk_values]
                output.extend(_encode_gdm_block(range_pixels))
                shift += range_bits

    gdm_path.parent.mkdir(parents=True, exist_ok=True)
    gdm_path.write_bytes(output)
    return {
        "dimension": width,
        "numChannels": num_channels,
        "compressionBoundaries": boundaries,
        "sizeBytes": len(output),
        "maxLogicalValue": max_logical_value,
    }


def inspect_map_density_height_layout(map_root: Path) -> dict:
    """Return the target map's densityMapHeightTypes + terrainDetailHeight layout."""
    profile = MapProfile(str(map_root), str(map_root), False, str(map_root))
    scan_profile(profile)
    height_rel = find_primary_xml_file(profile, "height_types")
    if not height_rel:
        raise CropPorterError("Could not locate the map densityMapHeightTypes XML.")
    height_tree = parse_xml_safely(map_root / height_rel)
    if not height_tree:
        raise CropPorterError(f"Could not parse map height-type registry: {height_rel}")
    height_container = find_child_container(height_tree.getroot(), "densityMapHeightTypes")
    if height_container is None:
        raise CropPorterError(f"No <densityMapHeightTypes> container in {height_rel}")
    try:
        first_channel = int(height_container.attrib.get("firstChannel", "0"))
        type_bits = int(height_container.attrib["numChannels"])
    except (KeyError, ValueError) as exc:
        raise CropPorterError(f"Invalid densityMapHeightTypes channel metadata in {height_rel}") from exc

    i3d = find_primary_target_i3d_for_fruits(map_root)
    if i3d is None:
        raise CropPorterError("Could not locate the primary map I3D containing density layers.")
    i3d_tree = parse_xml_safely(i3d)
    if not i3d_tree:
        raise CropPorterError(f"Could not parse map I3D: {rel_to_root(i3d, map_root)}")
    layer = find_terrain_detail_height_layer(i3d_tree.getroot())
    if layer is None:
        raise CropPorterError(f"No terrainDetailHeight layer in {rel_to_root(i3d, map_root)}")
    try:
        total_channels = int(layer.attrib["numDensityMapChannels"])
        height_first = int(layer.attrib["heightFirstChannel"])
        height_bits = int(layer.attrib["heightNumChannels"])
        compression_channels = int(layer.attrib.get("compressionChannels", "0")) or None
    except (KeyError, ValueError) as exc:
        raise CropPorterError("Invalid terrainDetailHeight channel metadata in map I3D.") from exc

    expected_height_first = first_channel + type_bits
    if height_first != expected_height_first:
        raise CropPorterError(
            "Map height layout is internally inconsistent: "
            f"densityMapHeightTypes implies heightFirstChannel={expected_height_first}, "
            f"but terrainDetailHeight says {height_first}."
        )
    if total_channels < height_first + height_bits:
        raise CropPorterError(
            f"Map terrainDetailHeight total channels ({total_channels}) do not cover height range "
            f"[{height_first}-{height_first + height_bits - 1}]."
        )
    return {
        "heightTypesFile": height_rel,
        "i3dFile": rel_to_root(i3d, map_root),
        "firstChannel": first_channel,
        "typeBits": type_bits,
        "heightFirstChannel": height_first,
        "heightBits": height_bits,
        "numDensityMapChannels": total_channels,
        "compressionChannels": compression_channels,
    }


def migrate_save_density_height(
    save_root: Path,
    map_root: Path,
    converter_hint: Optional[Path],
    *,
    dry_run: bool = False,
) -> dict:
    """Migrate an established save's densityMap_height.gdm to the patched map layout."""
    save_root = save_root.resolve()
    map_root = map_root.resolve()
    if not save_root.is_dir():
        raise CropPorterError(f"Savegame folder does not exist: {save_root}")
    if not map_root.is_dir():
        raise CropPorterError(f"Map folder does not exist: {map_root}")

    save_gdm = save_root / "densityMap_height.gdm"
    if not save_gdm.exists():
        raise CropPorterError(f"Savegame has no densityMap_height.gdm: {save_gdm}")

    target = inspect_map_density_height_layout(map_root)
    source_header = parse_gdm_header(save_gdm)
    target_total = int(target["numDensityMapChannels"])
    height_bits = int(target["heightBits"])
    first_channel = int(target["firstChannel"])
    new_type_bits = int(target["typeBits"])
    target_height_first = int(target["heightFirstChannel"])

    if source_header.dimension < 32 or source_header.chunk_size != 32:
        raise CropPorterError(
            f"Unsupported save densityMap_height geometry: dimension={source_header.dimension}, "
            f"chunkSize={source_header.chunk_size}; v13 requires 32x32 GDM chunks."
        )
    if source_header.num_channels == target_total:
        return {
            "status": "already-compatible",
            "savegame": str(save_root),
            "map": str(map_root),
            "sourceChannels": source_header.num_channels,
            "targetChannels": target_total,
            "message": "Save densityMap_height already matches the map channel count; no migration performed.",
        }
    if source_header.num_channels > target_total:
        raise CropPorterError(
            f"Save densityMap_height has {source_header.num_channels} channels but target map has only {target_total}; refusing downgrade."
        )

    old_height_first = source_header.num_channels - height_bits
    old_type_bits = old_height_first - first_channel
    if old_type_bits <= 0:
        raise CropPorterError(
            f"Cannot infer the old type-index width from save channels={source_header.num_channels}, "
            f"firstChannel={first_channel}, heightBits={height_bits}."
        )
    if target_height_first != first_channel + new_type_bits:
        raise CropPorterError("Target map heightFirstChannel does not match its height-type registry width.")
    if old_height_first >= target_height_first:
        raise CropPorterError(
            f"Save layout does not require an upward type-index migration: oldHeightFirst={old_height_first}, "
            f"targetHeightFirst={target_height_first}."
        )

    plan = {
        "status": "dry-run" if dry_run else "migrated",
        "savegame": str(save_root),
        "map": str(map_root),
        "sourceGdm": str(save_gdm),
        "sourceChannels": source_header.num_channels,
        "sourceCompressionBoundaries": source_header.compression_boundaries,
        "oldTypeFirst": first_channel,
        "oldTypeBits": old_type_bits,
        "oldHeightFirst": old_height_first,
        "heightBits": height_bits,
        "newTypeBits": new_type_bits,
        "newHeightFirst": target_height_first,
        "targetChannels": target_total,
        "targetCompressionChannels": target.get("compressionChannels"),
        "backup": None,
        "pixelChanges": None,
        "validation": None,
    }
    if dry_run:
        return plan

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = save_gdm.with_name(save_gdm.name + f".cropporter.pre-v13.{stamp}.bak")
    shutil.copy2(save_gdm, backup)
    plan["backup"] = str(backup)

    work_dir = Path(tempfile.mkdtemp(prefix=".cropporter_save_migrate_", dir=str(save_root)))
    try:
        decoded_png = work_dir / "densityMap_height.decoded.png"
        migrated_png = work_dir / "densityMap_height.migrated.png"
        new_gdm = work_dir / "densityMap_height.gdm"
        roundtrip_png = work_dir / "densityMap_height.roundtrip.png"

        # Decode the untouched backed-up source, then operate only on the work copy.
        run_grle_converter(save_gdm, decoded_png, converter_hint)
        shutil.copy2(decoded_png, migrated_png)
        moved, logical_total = repack_density_height_png_channels(
            migrated_png,
            first_channel,
            old_type_bits,
            old_height_first,
            height_bits,
            new_type_bits,
        )
        if logical_total != target_total:
            raise CropPorterError(
                f"Migrated PNG requires {logical_total} channels but target map expects {target_total}."
            )
        plan["pixelChanges"] = moved

        encode_info = encode_density_png_to_gdm(
            migrated_png,
            new_gdm,
            num_channels=target_total,
            compression_channels=target.get("compressionChannels"),
        )
        encoded_header = parse_gdm_header(new_gdm)
        if encoded_header.num_channels != target_total:
            raise CropPorterError(
                f"Encoded save GDM has {encoded_header.num_channels} channels; expected {target_total}."
            )
        if encoded_header.dimension != source_header.dimension:
            raise CropPorterError(
                f"Encoded save GDM dimension changed {source_header.dimension} -> {encoded_header.dimension}."
            )

        # Validate the new binary with GIANTS' own decoder before touching the save.
        run_grle_converter(new_gdm, roundtrip_png, converter_hint)
        migrated_fp = density_png_pixel_fingerprint(migrated_png)
        roundtrip_fp = density_png_pixel_fingerprint(roundtrip_png)
        if migrated_fp != roundtrip_fp:
            raise CropPorterError(
                "Encoded GDM failed GIANTS round-trip validation: decoded pixels differ from the migrated PNG."
            )
        plan["validation"] = {
            "giantsRoundTrip": "PASS",
            "pixelFingerprint": f"{migrated_fp[-1]:08x}",
            "encodedHeader": asdict(encoded_header),
            "encoded": encode_info,
        }

        staged = save_gdm.with_name(save_gdm.name + ".cropporter.new")
        if staged.exists():
            staged.unlink()
        shutil.copy2(new_gdm, staged)
        os.replace(staged, save_gdm)

        final_header = parse_gdm_header(save_gdm)
        if final_header.num_channels != target_total:
            raise CropPorterError(
                f"Post-replacement validation failed: save GDM has {final_header.num_channels} channels."
            )

        report_path = save_root / "CropPorter_SaveMigration_v13.json"
        plan["report"] = str(report_path)
        report_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        return plan
    except Exception:
        # If replacement occurred and a later validation failed, restore the backed-up source.
        try:
            current = parse_gdm_header(save_gdm) if save_gdm.exists() else None
            if current is None or current.num_channels != source_header.num_channels:
                shutil.copy2(backup, save_gdm)
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def cmd_migrate_save(args: argparse.Namespace) -> int:
    save_root = Path(args.savegame)
    map_profile = prepare_map_input(Path(args.map))
    try:
        if map_profile.is_temp:
            map_root = Path(map_profile.root)
        else:
            map_root = Path(map_profile.root)
        result = migrate_save_density_height(
            save_root,
            map_root,
            Path(args.grle_converter) if getattr(args, "grle_converter", None) else None,
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        print(json.dumps(result, indent=2))
        return 0
    finally:
        map_profile.cleanup()


def ensure_density_height_types_capacity_from_source(
    source: MapProfile,
    target_root: Path,
    height_target_file: Optional[str],
    crops: Optional[list[str]] = None,
    grle_converter: Optional[Path] = None,
) -> tuple[int, list[str], list[str]]:
    """Preserve/expand densityMapHeightTypes ID capacity for imported crop families.

    ``densityMapHeightTypes`` has its own ``numChannels`` namespace and is separate
    from the ``densityMap_fruits`` foliage type-index channels.  Importing local
    height types without carrying the source channel width can leave a target at the
    common 6-bit/63-type ceiling.  This is especially visible when gameplay mods
    register additional tip-to-ground height types later in the load order.

    The target is promoted to the greatest of:
      * its existing numChannels,
      * the source map's densityMapHeightTypes numChannels, and
      * the minimum width needed for the projected local height-type count.

    The source width is intentionally authoritative when larger.  A crop package
    extracted from a map configured for 8 channels must not silently collapse that
    registry back to a 6-channel target.
    """
    actions: list[str] = []
    warnings: list[str] = []
    if not height_target_file:
        return 0, actions, ["No target densityMapHeightTypes XML is available for capacity promotion."]

    target_path = target_root / height_target_file
    target_tree = parse_xml_safely(target_path)
    if not target_tree:
        return 0, actions, [f"Could not parse target densityMapHeightTypes XML: {height_target_file}"]
    target_container = find_child_container(target_tree.getroot(), "densityMapHeightTypes")
    if target_container is None:
        return 0, actions, [f"Target height-types XML has no <densityMapHeightTypes> container: {height_target_file}"]

    try:
        target_bits = int(target_container.attrib.get("numChannels", "0"))
    except ValueError:
        target_bits = 0

    source_bits = 0
    source_height_files = list(source.xml_files.get("height_types", []))
    if not source_height_files:
        for path in Path(source.root).rglob("*.xml"):
            if "densitymapheighttypes" in path.name.lower():
                try:
                    source_height_files.append(rel_to_root(path, Path(source.root)).replace(chr(92), "/"))
                except ValueError:
                    pass
    for rel in source_height_files:
        tree = parse_xml_safely(Path(source.root) / rel)
        if not tree:
            continue
        container = find_child_container(tree.getroot(), "densityMapHeightTypes")
        if container is None:
            continue
        try:
            source_bits = max(source_bits, int(container.attrib.get("numChannels", "0")))
        except ValueError:
            continue

    existing_names = collect_target_existing_height_type_names(target_root, height_target_file)
    planned_names: set[str] = set()
    source_root = Path(source.root)
    for crop_name in [c.lower() for c in (crops or [])]:
        crop = source.crop_defs.get(crop_name)
        if crop is None:
            continue
        family = {x.upper() for x in source_crop_family(source, crop)} or {crop_name.upper()}
        fills = collect_package_fill_type_names(source_root, family, crop)
        for ref in collect_refs_for_height_types(source_root, fills):
            name = (ref.attrs.get("fillTypeName") or ref.attrs.get("fillType") or ref.attrs.get("name") or "").upper()
            if name and name not in existing_names:
                planned_names.add(name)

    final_count = len(existing_names | planned_names)
    min_bits = max(target_bits, 1)
    # Reserve the all-zero index and avoid leaving a freshly-built map exactly at
    # the representable limit; match the headroom policy used for densityMap_fruits.
    while final_count >= (2 ** min_bits) - 1:
        min_bits += 1

    desired_bits = max(target_bits, source_bits, min_bits)
    try:
        first_channel = int(target_container.attrib.get("firstChannel", "0"))
    except ValueError:
        first_channel = 0

    layout_changed, layout_actions, layout_warnings = synchronize_density_height_layer_layout(
        target_root, first_channel, desired_bits, grle_converter
    )
    actions.extend(layout_actions)
    warnings.extend(layout_warnings)

    if desired_bits <= target_bits:
        actions.append(
            f"densityMapHeightTypes capacity is sufficient; retained numChannels={target_bits} "
            f"({len(existing_names)} existing + {len(planned_names)} planned = {final_count})."
        )
        return layout_changed, actions, warnings

    before = target_bits
    target_container.set("numChannels", str(desired_bits))
    backup = target_path.with_suffix(target_path.suffix + ".cropporter.heightchannels.bak")
    if not backup.exists():
        shutil.copy2(target_path, backup)
    indent_xml(target_tree)
    target_tree.write(target_path, encoding="utf-8", xml_declaration=True)
    source_note = f"; source requires {source_bits}" if source_bits else ""
    actions.append(
        f"Expanded densityMapHeightTypes numChannels {before} -> {desired_bits}"
        f"{source_note}; projected local height types={final_count}, usable capacity={(2 ** desired_bits) - 1}."
    )
    return 1 + layout_changed, actions, warnings


def cmd_export_crop(args: argparse.Namespace) -> int:
    source = prepare_map_input(Path(args.source))
    try:
        scan_profile(source)
        crop_name = args.crop.lower()
        crop = source.crop_defs.get(crop_name)
        if crop is None:
            raise CropPorterError(f"Crop '{args.crop}' was not detected in source map.")
        source_root = Path(source.root)
        library = Path(args.library).resolve()
        library.mkdir(parents=True, exist_ok=True)
        source_name = args.source_name or source_label(source.source_path)
        package_name = args.package_name or make_package_filename(crop_name, source_name)
        if not package_name.lower().endswith(".zip"):
            package_name += ".cropporter.zip"
        zip_path = library / package_name

        temp_package = Path(tempfile.mkdtemp(prefix="cropporter_export_"))
        try:
            (temp_package / PACKAGE_ASSETS_DIR).mkdir(parents=True, exist_ok=True)
            (temp_package / PACKAGE_XML_DIR).mkdir(parents=True, exist_ok=True)

            probe = None
            linked_fill_refs: list[XmlNodeRef] = []
            linked_height_refs: list[XmlNodeRef] = []
            vehicle_store_items: list[str] = []

            # Resolve the complete crop family before copying assets.  BARLEY packages,
            # for example, also carry WINTERBARLEY; WHEAT carries WINTERWHEAT.  Family
            # fillTypes can reference HUD/fill-plane assets outside the foliage folder,
            # so calculating these semantics after the copy pass creates incomplete
            # packages.
            package_fruit_types = collect_packaged_fruit_type_names(source_root, crop)
            package_fill_types = collect_package_fill_type_names(source_root, package_fruit_types, crop)
            package_fill_refs = normalise_filltype_asset_references(
                source_root, collect_refs_for_fill_types(source_root, package_fill_types)
            )
            package_height_refs = collect_refs_for_height_types(source_root, package_fill_types)
            fruit_category_memberships, fill_category_memberships = collect_category_memberships(
                source, package_fruit_types, package_fill_types
            )
            converter_nodes = collect_converter_nodes(source_root, package_fill_types, package_fruit_types)

            asset_paths: set[str] = set()
            for family_name in sorted(package_fruit_types):
                family_crop = source.crop_defs.get(family_name.lower())
                if family_crop is None:
                    continue
                asset_paths.update(family_crop.asset_paths)
                asset_paths.update(collect_crop_foliage_tree(source_root, family_crop))
                # Do NOT add the source map.i3d template to the runtime asset
                # dependency closure.  The main map.i3d contains a <Files> table for
                # the entire source map, so recursively traversing it packages nearly
                # every map asset.  build_package_source_snapshot() adds the map.i3d
                # separately as a semantic template after the crop-local closure has
                # been calculated.  Crop-local shapeSource I3Ds are discovered from
                # the foliage XML itself by expand_asset_references().

            # Canonical family fill/height nodes may live outside the crop foliage tree.
            # Capture every local asset they reference before recursive expansion.
            for ref in [*package_fill_refs, *package_height_refs]:
                try:
                    elem = ET.fromstring(ref.xml_text)
                except ET.ParseError:
                    continue
                asset_paths.update(collect_path_attrs_deep(elem))

            # Traverse XML and I3D dependencies to a fixed point.  This captures sibling
            # references such as mustard/../canola/distance and
            # silageMaize/../maize/distance as well as shared shape-source I3Ds.
            asset_paths.update(expand_asset_references(source_root, asset_paths))
            asset_paths.update(probe_l10n_files(source_root))
            source_map_data_prefix = infer_crop_map_data_prefix(crop)

            if args.plantation:
                probe = build_plantation_crop_probe(source, crop_name)
                asset_paths.update(str(x).replace(chr(92), "/") for x in probe.get("assets", []) or [])
                asset_paths.update(str(x).replace(chr(92), "/") for x in probe.get("l10n", []) or [])
                asset_paths.update(collect_linked_filltype_assets(source_root, set(probe.get("linkedFillTypes", []))))
                linked_wanted = {x.upper() for x in probe.get("linkedFillTypes", [])}
                linked_fill_refs = normalise_filltype_asset_references(source_root, find_fill_type_refs_by_names(source_root, linked_wanted))
                linked_height_refs = find_height_type_refs_by_names(source_root, linked_wanted)
                if args.include_vehicles:                    vehicle_assets, vehicle_store_items = collect_plantation_vehicle_dependencies(source_root, probe)
                    asset_paths.update(vehicle_assets)

            # Plantation/linked additions can introduce further local references.
            asset_paths.update(expand_asset_references(source_root, asset_paths))

            unresolved_dependencies: list[str] = []
            if not args.plantation:
                # A library package must be self-contained for every local reference the
                # crop family requires.  Validate against an intentionally empty target
                # so missing sibling foliage assets are caught during export, not in-game.
                empty_target = temp_package / "_dependency_probe_target"
                empty_target.mkdir(parents=True, exist_ok=True)
                unresolved_dependencies = collect_missing_crop_asset_dependencies(
                    source, empty_target, [crop_name]
                )
                shutil.rmtree(empty_target, ignore_errors=True)
                if unresolved_dependencies:
                    preview = "\n  - ".join(unresolved_dependencies[:20])
                    extra = "" if len(unresolved_dependencies) <= 20 else f"\n  ... and {len(unresolved_dependencies) - 20} more"
                    raise CropPorterError(
                        f"Cannot export '{crop_name}': unresolved local crop-family dependencies were found. "
                        "The source map/package is incomplete for this crop.\n  - " + preview + extra
                    )

            # Collapse source XML references onto the asset variant that actually exists
            # (for example .png -> .dds).  Keep unresolved paths only so the manifest's
            # missingAssets list represents genuine missing dependencies rather than
            # harmless extension aliases that were already normalised in the XML nodes.
            normalized_asset_paths: set[str] = set()
            for rel in asset_paths:
                hit = resolve_asset_path(source_root, rel)
                if hit and hit.exists() and hit.is_file():
                    try:
                        normalized_asset_paths.add(rel_to_root(hit, source_root).replace(chr(92), "/"))
                    except ValueError:
                        normalized_asset_paths.add(rel)
                else:
                    normalized_asset_paths.add(rel)
            asset_paths = normalized_asset_paths

            copied: list[str] = []
            missing: list[str] = []
            for rel in unique_preserve_order(sorted(asset_paths)):
                if copy_asset_to_package(source_root, temp_package, rel):
                    copied.append(rel)
                else:
                    missing.append(rel)

            crop_def_rel = f"{PACKAGE_XML_DIR}/crop_definition.json"
            (temp_package / crop_def_rel).write_text(json.dumps(crop_to_package_dict(crop), indent=2), encoding="utf-8")
            linked_fill_rel = f"{PACKAGE_XML_DIR}/linked_fill_types.json"
            linked_height_rel = f"{PACKAGE_XML_DIR}/linked_height_types.json"
            (temp_package / linked_fill_rel).write_text(json.dumps([ref_to_dict(x) for x in linked_fill_refs], indent=2), encoding="utf-8")
            (temp_package / linked_height_rel).write_text(json.dumps([ref_to_dict(x) for x in linked_height_refs], indent=2), encoding="utf-8")
            plantation_probe_rel = ""
            if probe is not None:
                plantation_probe_rel = f"{PACKAGE_XML_DIR}/plantation_probe.json"
                (temp_package / plantation_probe_rel).write_text(json.dumps(probe, indent=2), encoding="utf-8")

            package_fill_rel = f"{PACKAGE_XML_DIR}/package_fill_types.json"
            package_height_rel = f"{PACKAGE_XML_DIR}/package_height_types.json"
            converter_rel = f"{PACKAGE_XML_DIR}/converters.json"
            (temp_package / package_fill_rel).write_text(
                json.dumps([ref_to_dict(x) for x in package_fill_refs], indent=2), encoding="utf-8"
            )
            (temp_package / package_height_rel).write_text(
                json.dumps([ref_to_dict(x) for x in package_height_refs], indent=2), encoding="utf-8"
            )
            (temp_package / converter_rel).write_text(
                json.dumps(converter_nodes, indent=2), encoding="utf-8"
            )

            source_snapshot_rel = PACKAGE_SOURCE_DIR
            source_snapshot_root = temp_package / source_snapshot_rel
            source_snapshot_root.mkdir(parents=True, exist_ok=True)
            snapshot_files = build_package_source_snapshot(source, crop, source_snapshot_root, copied)

            primary_fill_types = collect_crop_primary_fill_types(crop)
            linked_fill_types = {x.upper() for x in (probe or {}).get("linkedFillTypes", [])}
            manifest = {
                "packageVersion": "1.0",
                "cropPorterVersion": VERSION,
                "crop": crop_name,
                "cropType": "plantation" if args.plantation else "field",
                "sourceMap": source_name,
                "sourcePath": source.source_path,
                "sourceMapDataPrefix": source_map_data_prefix,
                "exportedAt": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
                "redistribution": args.redistribution,
                "cropDefinition": crop_def_rel,
                "sourceSnapshot": source_snapshot_rel,
                "sourceSnapshotFiles": sorted(snapshot_files),
                "linkedFillTypeNodes": linked_fill_rel,
                "linkedHeightTypeNodes": linked_height_rel,
                "plantationProbe": plantation_probe_rel,
                "fruitTypes": sorted(package_fruit_types),
                "fillTypes": sorted(package_fill_types),
                "linkedFillTypes": sorted(linked_fill_types),
                "fruitTypeCategories": sorted(find_source_fruit_categories_for_crop(source, crop_name)),
                "fillTypeCategories": sorted(find_source_fill_categories_for_crop(source, primary_fill_types)),
                "fruitTypeCategoryMemberships": fruit_category_memberships,
                "fillTypeCategoryMemberships": fill_category_memberships,
                "packageFillTypeNodes": package_fill_rel,
                "packageHeightTypeNodes": package_height_rel,
                "converterNodes": converter_rel,
                "linkedFillTypeCategories": sorted(find_source_fill_categories_for_crop(source, linked_fill_types)),
                "assets": sorted(copied),
                "missingAssets": sorted(missing),
                "unresolvedLocalDependencies": sorted(unresolved_dependencies),
                "storeItems": sorted(set((probe or {}).get("storeItems", []) + vehicle_store_items)),
                "requires": {
                    "plantationApply": bool(args.plantation),
                    "vehicles": bool(args.include_vehicles),
                    "productions": bool((probe or {}).get("productionPlaceables")),
                },
                "notes": [
                    "Local package generated from a source map. Do not redistribute packages containing third-party assets unless you have permission from the original creator."
                ],
            }
            (temp_package / PACKAGE_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if zip_path.exists():
                zip_path.unlink()
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(temp_package.rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(temp_package))
            print("Export complete.")
            print(f"Package: {zip_path}")
            print(f"Crop: {crop_name}")
            print(f"Type: {manifest['cropType']}")
            print(f"Copied assets: {len(copied)}")
            if missing:
                print("Missing assets:")
                for rel in missing:
                    print(f"- {rel}")
            return 0
        finally:
            shutil.rmtree(temp_package, ignore_errors=True)
    finally:
        source.cleanup()


def cmd_probe_package(args: argparse.Namespace) -> int:
    package_root, cleanup = open_package(Path(args.package))
    try:
        manifest = load_manifest(package_root)
        print(package_summary_text(manifest))
        if args.assets:
            print()
            print("Assets:")
            for asset in manifest.get("assets", []):
                print(f"- {asset}")
        if manifest.get("missingAssets"):
            print()
            print("Missing assets recorded at export:")
            for asset in manifest["missingAssets"]:
                print(f"- {asset}")
        return 0
    finally:
        if cleanup:
            shutil.rmtree(package_root, ignore_errors=True)


def cmd_list_library(args: argparse.Namespace) -> int:
    library = Path(args.library)
    if not library.exists():
        raise CropPorterError(f"Library folder does not exist: {library}")
    packages = sorted(library.glob("*.cropporter.zip"))
    if not packages:
        print("No .cropporter.zip packages found.")
        return 0
    print(f"CropPorter library: {library.resolve()}")
    print()
    for package in packages:
        package_root, cleanup = open_package(package)
        try:
            manifest = load_manifest(package_root)
            print(f"- {package.name}")
            print(f"  crop: {manifest.get('crop', 'unknown')}")
            print(f"  type: {manifest.get('cropType', 'unknown')}")
            print(f"  source: {manifest.get('sourceMap', 'unknown')}")
            print(f"  assets: {len(manifest.get('assets', []))}")
        finally:
            if cleanup:
                shutil.rmtree(package_root, ignore_errors=True)
    return 0


def cmd_apply_package(args: argparse.Namespace) -> int:
    """Apply a library package through the same engine used for direct source maps."""
    package_root, package_cleanup = open_package(Path(args.package))
    target_probe = prepare_map_input(Path(args.target))
    prepared_source_root: Optional[Path] = None
    try:
        manifest = load_manifest(package_root)
        snapshot_rel = manifest.get("sourceSnapshot")
        if not snapshot_rel:
            raise CropPorterError(
                "This package predates source-snapshot packages. Re-export it with the current v0.3 build."
            )
        snapshot_root = package_root / str(snapshot_rel)
        if not snapshot_root.exists():
            raise CropPorterError(f"Package source snapshot is missing: {snapshot_rel}")

        scan_profile(target_probe)
        target_prefix = detect_target_map_data_prefix(Path(target_probe.root), target_probe)
        source_prefix = str(manifest.get("sourceMapDataPrefix") or "")

        prepared_source_root = Path(tempfile.mkdtemp(prefix="cropporter_package_source_"))
        shutil.copytree(snapshot_root, prepared_source_root, dirs_exist_ok=True)

        # The package's assets/ tree is the authoritative runtime payload.  Merge it
        # into the reconstructed source as well as the compact semantic snapshot so
        # sibling dependencies (for example clover -> alfalfa textures, mustard ->
        # canola distance maps, silageMaize -> maize distance maps) are available to
        # the common apply engine.  Older packages may have an intentionally compact
        # snapshot even when the required runtime asset is present under assets/.
        package_assets_root = package_root / PACKAGE_ASSETS_DIR
        if package_assets_root.exists() and package_assets_root.is_dir():
            shutil.copytree(package_assets_root, prepared_source_root, dirs_exist_ok=True)

        remap_snapshot_namespace(prepared_source_root, source_prefix, target_prefix)

        # Namespace (map -> mapEU etc.) and foliage storage location are separate
        # concerns.  Discover where each map actually stores foliage and project the
        # temporary package source onto the target layout before the common engine
        # scans it.  Example: mapEU/foliage -> mapEU/textures/foliage.
        source_foliage_root = detect_foliage_root_from_i3d(prepared_source_root)
        target_foliage_root = detect_foliage_root_from_i3d(Path(target_probe.root))
        foliage_adapter_actions: list[str] = []
        if source_foliage_root and target_foliage_root:
            foliage_adapter_actions = remap_reconstructed_source_foliage_root(
                prepared_source_root, source_foliage_root, target_foliage_root
            )

        crop_name = str(manifest.get("crop") or "").strip().lower()
        if not crop_name:
            raise CropPorterError("Package manifest does not contain a crop name.")

        # Plantation packages use the proven v0.2 plantation engine. Field packages
        # use the proven apply_patch engine. The package layer only reconstructs source.
        if manifest.get("cropType") == "plantation" or manifest.get("requires", {}).get("plantationApply"):
            ns = argparse.Namespace(
                source=str(prepared_source_root),
                target=args.target,
                crop=crop_name,
                output=args.output,
                force=True,
                include_vehicles=bool(manifest.get("requires", {}).get("vehicles")),
            )
            return cmd_apply_plantation(ns)

        source = MapProfile(str(args.package), str(prepared_source_root), False, str(prepared_source_root))
        scan_profile(source)
        if crop_name not in source.crop_defs:
            raise CropPorterError(
                f"Reconstructed package source did not detect crop '{crop_name}'. Re-export the package and inspect it with probe-package."
            )
        report = apply_patch(source, target_probe, [crop_name], Path(args.output), Path(args.grle_converter) if getattr(args, "grle_converter", None) else None)
        # Keep the package identity in a small companion report while the main report
        # remains exactly the normal CropPorter_Apply report from the common engine.
        package_report = {
            "version": VERSION,
            "mode": "apply-package-adapter",
            "package": str(Path(args.package)),
            "sourceSnapshot": str(snapshot_rel),
            "sourceMapDataPrefix": source_prefix,
            "targetMapDataPrefix": target_prefix,
            "sourceFoliageRoot": source_foliage_root,
            "targetFoliageRoot": target_foliage_root,
            "foliageAdapterActions": foliage_adapter_actions,
            "commonEngineReport": "CropPorter_Apply.json",
            "crop": crop_name,
        }
        out = Path(args.output).resolve()
        (out / "CropPorter_PackageAdapter.json").write_text(json.dumps(package_report, indent=2), encoding="utf-8")
        print("Apply package complete via common CropPorter engine.")
        print(f"Output: {report['output']}")
        print(f"Inserted/patched nodes: {report['summary']['inserted_nodes']}")
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
        target_probe.cleanup()
        if prepared_source_root is not None:
            shutil.rmtree(prepared_source_root, ignore_errors=True)
        if package_cleanup:
            shutil.rmtree(package_root, ignore_errors=True)



def safe_report_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return token.strip("._") or "crop"


def package_sha256(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def should_exclude_from_release_zip(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    name = path.name
    if any(part == "__pycache__" for part in rel.parts):
        return True
    if name.startswith("CropPorter_"):
        return True
    lower = name.lower()
    if ".cropporter." in lower and lower.endswith(".bak"):
        return True
    if lower.endswith(".cropporter.bak"):
        return True
    return False


def create_release_zip(source_root: Path, zip_output: Path) -> tuple[Path, int]:
    """Create an FS25-ready ZIP with map contents at the archive root."""
    source_root = Path(source_root).resolve()
    zip_output = Path(zip_output).resolve()
    if zip_output.suffix.lower() != ".zip":
        zip_output = zip_output.with_suffix(".zip")
    if zip_output.exists():
        raise CropPorterError(f"ZIP output already exists. Delete it or choose another path: {zip_output}")
    zip_output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or should_exclude_from_release_zip(path, source_root):
                continue
            zf.write(path, path.relative_to(source_root).as_posix())
            count += 1
    return zip_output, count



def _defined_fruit_names_in_xml(path: Path) -> set[str]:
    tree = parse_xml_safely(path)
    if not tree:
        return set()
    names: set[str] = set()
    for elem in tree.getroot().iter():
        if local_name(elem.tag).lower() != "fruittype":
            continue
        name = elem.attrib.get("name") or elem.attrib.get("fruitType") or elem.attrib.get("fruitTypeName")
        if name:
            names.add(name.lower())
    return names


def _rewrite_exact_family_member_path(snapshot_root: Path, old_tail: str, new_tail: str) -> int:
    """Rewrite one foliage-family member path without renaming its sibling crop."""
    old_tail = old_tail.replace(chr(92), "/").strip("/")
    new_tail = new_tail.replace(chr(92), "/").strip("/")
    if not old_tail or old_tail.lower() == new_tail.lower():
        return 0
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old_tail)}", re.IGNORECASE)
    changed = 0
    for path in snapshot_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".xml", ".i3d", ".json", ".txt"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        replaced = pattern.sub(new_tail, content)
        if replaced != content:
            path.write_text(replaced, encoding="utf-8")
            changed += 1
    return changed


def collect_active_target_fruit_registry_refs(target_profile: MapProfile) -> Optional[dict[str, str]]:
    """Return active fruitType registry references keyed by fruitType name.

    ``None`` means the active registry could not be resolved safely. Filename-style
    entries are keyed from the XML filename stem (e.g. ``barley.xml`` -> ``barley``).
    Inline named fruitTypes are recorded as ``<inline>``.

    This deliberately models what the game actually loads, rather than every foliage
    XML merely present somewhere in the map folder. A dormant local XML must not make a
    fruitType appear active, while a ``$data`` reference means the target is still using
    the base-game definition and can be intentionally overridden by an imported package.
    """
    target_root = Path(target_profile.root)
    fallback = find_primary_xml_file(target_profile, "fruit_types")
    if not fallback:
        return None

    active_rel = resolve_active_fruit_types_xml(target_root, fallback)
    path = target_root / active_rel
    tree = parse_xml_safely(path)
    if not tree:
        return None

    root = tree.getroot()
    fruit_types = find_child_container(root, "fruitTypes")
    if fruit_types is None:
        fruit_types = root

    refs: dict[str, str] = {}
    for elem in list(fruit_types):
        if local_name(elem.tag).lower() != "fruittype":
            continue
        filename = (elem.attrib.get("filename") or "").replace(chr(92), "/").strip()
        if filename:
            stem = Path(filename).stem.lower()
            if stem:
                refs[stem] = filename
            continue
        name = lower_name(find_first_attr(elem, ["name", "fruitType", "fruitTypeName"]))
        if name:
            refs[name] = "<inline>"
    return refs


def isolate_missing_family_members(source_root: Path, source: MapProfile, primary_crop: str,
                                   target_profile: MapProfile) -> tuple[MapProfile, list[str], list[str], list[str]]:
    """Prepare a shared-family package against the target's *active* fruit registry.

    BARLEY/WINTERBARLEY and WHEAT/WINTERWHEAT commonly share one foliage directory
    and one shape-source I3D. The adapter must therefore keep that shared directory
    intact, but it also needs to distinguish three target states for each family member:

    * locally registered -> preserve the target member;
    * registered through ``$data`` -> deliberately replace it with the package-local XML;
    * not registered -> import it as a missing family member.

    This is important on maps such as Castlereagh, which already contain local
    ``barley``/``wheat`` foliage assets in the I3D but still register the actual
    fruitTypes from ``$data``. Merely scanning every XML in the map would incorrectly
    treat those dormant/local assets as authoritative crop definitions.
    """
    crop = source.crop_defs.get(primary_crop)
    if crop is None:
        return source, [primary_crop], [primary_crop], []

    family = sorted(set(source_crop_family(source, crop)))
    actions: list[str] = []

    registry_refs = collect_active_target_fruit_registry_refs(target_profile)
    if registry_refs is None:
        # Conservative fallback for unusual targets where the active registry cannot be
        # resolved: retain the previous scanner-based behaviour rather than guessing.
        target_names = {x.lower() for x in target_profile.fruit_names}
        locally_registered = sorted(x for x in family if x in target_names)
        data_backed: list[str] = []
        missing = sorted(x for x in family if x not in target_names)
        actions.append(
            "Active target fruit registry could not be resolved; shared-family adaptation "
            "fell back to detected fruitType names."
        )
    else:
        locally_registered = []
        data_backed = []
        missing = []
        for member in family:
            ref = registry_refs.get(member.lower())
            if ref is None:
                missing.append(member)
            elif ref.lower().startswith("$data/"):
                data_backed.append(member)
                actions.append(
                    f"Target fruit registry uses base-game $data definition for '{member}': {ref}; "
                    "package-local definition will replace it."
                )
            else:
                locally_registered.append(member)

    apply_members = sorted(set(missing).union(data_backed))
    if not apply_members:
        actions.append(f"Crop family already locally registered in target; no import required: {', '.join(family)}")
        return source, [], family, actions

    target_root = Path(target_profile.root).resolve()

    # Resolve canonical foliage XMLs for every member that will actually be applied.
    apply_xmls: dict[str, Path] = {}
    for member in apply_members:
        member_crop = source.crop_defs.get(member)
        if member_crop is None:
            raise CropPorterError(f"Package family member '{member}' was detected but has no crop definition.")

        candidates: list[Path] = []
        for ref in member_crop.fruit_nodes:
            rel = ref.relative_file.replace(chr(92), "/").strip("/")
            if "/foliage/" not in ("/" + rel.lower()):
                continue
            path = source_root / rel
            if path.exists() and path.is_file() and member in _defined_fruit_names_in_xml(path):
                candidates.append(path)

        if not candidates:
            raise CropPorterError(
                f"Cannot prepare crop family member '{member}': "
                "no local foliage XML was found in the package snapshot."
            )
        apply_xmls[member] = sorted(candidates, key=lambda x: (len(str(x)), str(x).lower()))[0]

    by_directory: dict[Path, set[Path]] = {}
    for member, xml_path in apply_xmls.items():
        by_directory.setdefault(xml_path.parent, set()).add(xml_path)

    for shared_dir, keep_member_xmls in sorted(by_directory.items(), key=lambda x: str(x[0]).lower()):
        removed_sibling_xmls = 0
        preserved_target_assets = 0
        overriding_xmls = {
            apply_xmls[m].resolve() for m in data_backed
            if m in apply_xmls and apply_xmls[m].parent.resolve() == shared_dir.resolve()
        }
        keep_resolved = {p.resolve() for p in keep_member_xmls}

        # Remove fruit-definition XMLs for family members that the target already loads
        # locally. Keep missing members and $data-backed members at their canonical source
        # paths so registry reconciliation can append/redirect them correctly.
        for xml_path in list(shared_dir.rglob("*.xml")):
            names = _defined_fruit_names_in_xml(xml_path)
            if not names:
                continue
            if xml_path.resolve() in keep_resolved or names.intersection(apply_members):
                continue
            if names.intersection(locally_registered) or names.issubset(set(family)):
                xml_path.unlink()
                removed_sibling_xmls += 1

        # Preserve target-owned shared visual assets (I3D, shapes, distance textures, etc.)
        # but intentionally keep a source XML when that member is currently registered
        # through $data. Applying the package then replaces the target-local XML contents
        # and reconcile_source_fruit_registry() redirects the active $data registry entry
        # to that local path.
        for source_path in sorted(
            [p for p in shared_dir.rglob("*") if p.is_file()],
            key=lambda p: str(p).lower(),
        ):
            rel = source_path.resolve().relative_to(source_root.resolve())
            target_path = target_root / rel
            if not target_path.exists() or not target_path.is_file():
                continue

            source_resolved = source_path.resolve()
            if source_resolved in overriding_xmls:
                # Explicit package override of a base-game $data fruitType. The local XML
                # path already belongs to the target foliage group, so replacing this XML
                # is intentional while the group's shared visual assets remain untouched.
                continue

            if source_resolved in keep_resolved:
                # A genuinely missing member should not silently overwrite an unrelated
                # dormant target definition. Identical files need not be copied again.
                if files_are_same(source_path, target_path):
                    source_path.unlink()
                    preserved_target_assets += 1
                    continue
                raise CropPorterError(
                    "Refusing to overwrite an existing target foliage definition for "
                    f"a newly registered crop-family member: {rel.as_posix()}"
                )

            source_path.unlink()
            preserved_target_assets += 1

        rel_dir = rel_to_root(shared_dir, source_root).replace(chr(92), "/")
        members_here = sorted(
            member for member, xml_path in apply_xmls.items()
            if xml_path.parent.resolve() == shared_dir.resolve()
        )
        actions.append(
            f"Prepared shared foliage family directory '{rel_dir}' for {', '.join(members_here)}; "
            f"removed {removed_sibling_xmls} already-local sibling fruit XML(s) from the temporary source "
            f"and preserved {preserved_target_assets} target-owned shared asset file(s)."
        )

    for directory in sorted(
        [p for p in source_root.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    rescanned = MapProfile(source.source_path, str(source_root), False, str(source_root))
    scan_profile(rescanned)

    for member in apply_members:
        if member not in rescanned.crop_defs:
            raise CropPorterError(
                f"Shared-family adapter lost crop '{member}' while preserving the source foliage layout."
            )

    # If the package's primary crop is one of the members being applied (notably a
    # $data-backed BARLEY/WHEAT override), run the common engine once from that primary
    # definition; source_crop_family() will carry the retained family members with it.
    # Otherwise apply only the genuinely missing family member(s).
    apply_crops = [primary_crop] if primary_crop in apply_members else apply_members
    return rescanned, apply_crops, family, actions


def collect_missing_crop_asset_dependencies(source: MapProfile, target_root: Path, crop_names: Iterable[str]) -> list[str]:
    """Find local asset references required by selected crops that exist in neither source cache nor target.

    This lets old/incomplete packages fail before FS25 loads them.  Existing target
    sibling assets are accepted, which preserves the proven Triticale -> Wheat shared
    distance-texture case when the target already supplies those files.
    """
    source_root = Path(source.root)
    missing: set[str] = set()
    refs: list[XmlNodeRef] = []

    for crop_name in crop_names:
        crop = source.crop_defs.get(crop_name.lower())
        if crop is None:
            continue
        family = {x.upper() for x in source_crop_family(source, crop)} or {crop_name.upper()}
        fills = collect_package_fill_type_names(source_root, family, crop)
        refs.extend(crop.fruit_nodes)
        refs.extend(crop.growth_nodes)
        refs.extend(crop.other_nodes)
        refs.extend(collect_refs_for_fill_types(source_root, fills))
        refs.extend(collect_refs_for_height_types(source_root, fills))

        # fruitType nodes do not contain the sibling <foliageLayer> definitions where
        # distanceMap/shapeSource references live.  Validate the complete crop foliage
        # XML/I3D files as well, otherwise stale packages can miss dependencies such as
        # mustard/../canola/distance and silageMaize/../maize/distance.
        for rel in collect_crop_foliage_tree(source_root, crop):
            path = source_root / rel
            if not path.is_file() or path.suffix.lower() not in {".xml", ".i3d"}:
                continue
            tree = parse_xml_safely(path)
            if tree:
                refs.append(XmlNodeRef(
                    "asset_dependency", rel.replace(chr(92), "/"),
                    local_name(tree.getroot().tag), dict(tree.getroot().attrib),
                    ET.tostring(tree.getroot(), encoding="unicode")
                ))

    seen_refs: set[tuple[str, str]] = set()
    for ref in refs:
        try:
            elem = ET.fromstring(ref.xml_text)
        except ET.ParseError:
            continue
        source_xml_path = source_root / ref.relative_file
        target_xml_path = target_root / ref.relative_file
        for node in elem.iter():
            for path_ref in collect_path_attrs(node):
                norm = path_ref.replace(chr(92), "/").strip()
                if not norm or norm.startswith("$"):
                    continue
                key = (ref.relative_file, norm)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                source_hit = resolve_asset_path(source_xml_path.parent, norm) or resolve_asset_path(source_root, norm)
                if source_hit and source_hit.exists():
                    continue
                target_hit = resolve_asset_path(target_xml_path.parent, norm) or resolve_asset_path(target_root, norm)
                if target_hit and target_hit.exists():
                    continue
                missing.add(f"{ref.relative_file} -> {norm}")
    return sorted(missing)


def reconstruct_package_source_for_target(package_path: Path, target_profile: MapProfile) -> dict:
    """Reconstruct one package source and project it onto the current target layout."""
    package_root, package_cleanup = open_package(package_path)
    prepared_source_root: Optional[Path] = None
    try:
        manifest = load_manifest(package_root)
        snapshot_rel = manifest.get("sourceSnapshot")
        if not snapshot_rel:
            raise CropPorterError(f"Package '{package_path}' predates source-snapshot packages. Re-export it with current v0.3.")
        snapshot_root = package_root / str(snapshot_rel)
        if not snapshot_root.exists():
            raise CropPorterError(f"Package source snapshot is missing: {snapshot_rel}")

        target_prefix = detect_target_map_data_prefix(Path(target_profile.root), target_profile)
        source_prefix = str(manifest.get("sourceMapDataPrefix") or "")
        prepared_source_root = Path(tempfile.mkdtemp(prefix="cropporter_package_source_"))
        shutil.copytree(snapshot_root, prepared_source_root, dirs_exist_ok=True)
        remap_snapshot_namespace(prepared_source_root, source_prefix, target_prefix)

        source_foliage_root = detect_foliage_root_from_i3d(prepared_source_root)
        target_foliage_root = detect_foliage_root_from_i3d(Path(target_profile.root))
        foliage_adapter_actions: list[str] = []
        if source_foliage_root and target_foliage_root:
            foliage_adapter_actions = remap_reconstructed_source_foliage_root(
                prepared_source_root, source_foliage_root, target_foliage_root
            )

        crop_name = str(manifest.get("crop") or "").strip().lower()
        if not crop_name:
            raise CropPorterError(f"Package manifest has no crop name: {package_path}")
        source = MapProfile(str(package_path), str(prepared_source_root), False, str(prepared_source_root))
        scan_profile(source)
        if crop_name not in source.crop_defs:
            raise CropPorterError(
                f"Reconstructed package source did not detect crop '{crop_name}' from '{package_path}'. "
                "Re-export the package and inspect it with probe-package."
            )

        source, apply_crops, expected_family, family_adapter_actions = isolate_missing_family_members(
            prepared_source_root,
            source,
            crop_name,
            target_profile,
        )
        dependency_errors = collect_missing_crop_asset_dependencies(
            source, Path(target_profile.root), apply_crops
        ) if apply_crops else []
        if dependency_errors:
            preview = "\n  - ".join(dependency_errors[:20])
            extra = "" if len(dependency_errors) <= 20 else f"\n  ... and {len(dependency_errors) - 20} more"
            raise CropPorterError(
                f"Package '{package_path.name}' has unresolved local asset dependencies after target adaptation. "
                "Re-export this crop package with the current CropPorter build.\n  - " + preview + extra
            )

        return {
            "package_root": package_root, "package_cleanup": package_cleanup,
            "prepared_source_root": prepared_source_root, "manifest": manifest,
            "source": source, "crop": crop_name, "applyCrops": apply_crops,
            "expectedFruitTypes": expected_family, "sourceSnapshot": str(snapshot_rel),
            "sourceMapDataPrefix": source_prefix, "targetMapDataPrefix": target_prefix,
            "sourceFoliageRoot": source_foliage_root, "targetFoliageRoot": target_foliage_root,
            "foliageAdapterActions": foliage_adapter_actions,
            "familyAdapterActions": family_adapter_actions,
        }
    except Exception:
        if prepared_source_root is not None:
            shutil.rmtree(prepared_source_root, ignore_errors=True)
        if package_cleanup:
            shutil.rmtree(package_root, ignore_errors=True)
        raise


def cleanup_reconstructed_package(ctx: dict) -> None:
    if ctx.get("prepared_source_root"):
        shutil.rmtree(Path(ctx["prepared_source_root"]), ignore_errors=True)
    if ctx.get("package_cleanup") and ctx.get("package_root"):
        shutil.rmtree(Path(ctx["package_root"]), ignore_errors=True)


def promote_completed_work_tree(source: Path, destination: Path, attempts: int = 10, delay_seconds: float = 0.5) -> tuple[str, list[str]]:
    """Promote a completed batch work tree robustly on Windows.

    Directory rename can transiently fail with WinError 5 when Explorer, Defender,
    indexing, or another process briefly holds a directory handle. Retry first to keep
    the promotion atomic. If rename remains blocked, fall back to a verified copy so a
    fully successful crop build is not discarded merely because the final rename was
    denied.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.exists():
        raise CropPorterError(f"Completed work tree no longer exists: {source}")
    if destination.exists():
        raise CropPorterError(f"Final output unexpectedly already exists: {destination}")

    warnings: list[str] = []
    last_error: Optional[BaseException] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            source.rename(destination)
            return "rename", warnings
        except PermissionError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)
        except OSError as exc:
            # Retry other transient Windows filesystem failures too.
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)

    # A directory handle can block rename while ordinary file reads/copies are still
    # allowed. Copy the completed tree as a recovery path, then verify every file by
    # relative path and size before accepting the destination.
    try:
        shutil.copytree(source, destination)
        source_files = {
            p.relative_to(source).as_posix(): p.stat().st_size
            for p in source.rglob("*") if p.is_file()
        }
        destination_files = {
            p.relative_to(destination).as_posix(): p.stat().st_size
            for p in destination.rglob("*") if p.is_file()
        }
        if source_files != destination_files:
            shutil.rmtree(destination, ignore_errors=True)
            raise CropPorterError(
                "Fallback copy verification failed while promoting the completed batch build. "
                f"Original work tree has been preserved at: {source}"
            )
        warnings.append(
            "Final directory rename remained blocked after retries; used a verified "
            f"copy fallback instead. Last rename error: {last_error}"
        )
        try:
            shutil.rmtree(source)
        except OSError as exc:
            warnings.append(f"Completed temporary work tree could not be removed after copy fallback: {source} ({exc})")
        return "verified-copy", warnings
    except Exception as exc:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, CropPorterError):
            raise
        raise CropPorterError(
            "Could not promote the completed batch build after repeated rename attempts "
            f"or by verified copy. Completed work tree has been preserved at: {source}. "
            f"Last rename error: {last_error}; copy error: {exc}"
        ) from exc


def cmd_apply_packages(args: argparse.Namespace) -> int:
    """Build one target from multiple field-crop packages without rebuilding between crops."""
    package_paths = [Path(p).resolve() for p in args.package]
    if not package_paths:
        raise CropPorterError("At least one --package is required.")

    package_meta: list[dict] = []
    seen_crops: set[str] = set()
    for package_path in package_paths:
        package_root, cleanup = open_package(package_path)
        try:
            manifest = load_manifest(package_root)
            crop = str(manifest.get("crop") or "").strip().lower()
            if not crop:
                raise CropPorterError(f"Package manifest has no crop name: {package_path}")
            if crop in seen_crops:
                raise CropPorterError(f"Duplicate crop package selected for '{crop}'. Apply each crop once in a batch build.")
            seen_crops.add(crop)
            is_plantation = bool(manifest.get("cropType") == "plantation" or manifest.get("requires", {}).get("plantationApply"))
            if is_plantation:
                raise CropPorterError(
                    f"Multi-package batch currently supports field-crop packages only; '{crop}' is a plantation package. "
                    "Apply plantation packages separately until the plantation engine is in-place safe."
                )
            snapshot_rel = manifest.get("sourceSnapshot")
            if not snapshot_rel or not (package_root / str(snapshot_rel)).exists():
                raise CropPorterError(f"Package '{package_path}' has no usable sourceSnapshot. Re-export it with current v0.3.")
            package_meta.append({
                "path": str(package_path), "crop": crop, "sourceMap": manifest.get("sourceMap"),
                "packageVersion": manifest.get("packageVersion"), "sha256": package_sha256(package_path),
            })
        finally:
            if cleanup:
                shutil.rmtree(package_root, ignore_errors=True)

    target = prepare_map_input(Path(args.target))
    output_final = Path(args.output).resolve()
    work_root: Optional[Path] = None
    try:
        scan_profile(target)
        if output_final.exists():
            raise CropPorterError(f"Output already exists. Choose a new folder or delete it first: {output_final}")
        output_final.parent.mkdir(parents=True, exist_ok=True)
        work_root = Path(tempfile.mkdtemp(prefix=f".{output_final.name}.cropporter_work_", dir=str(output_final.parent)))
        work_root.rmdir()
        copy_target_to_output(target, work_root)

        multi_report = {
            "version": VERSION, "mode": "apply-packages", "target": target.source_path,
            "output": str(output_final), "selected_crops": [m["crop"] for m in package_meta],
            "packages": package_meta,
            "actions": [f"Copied pristine target once for {len(package_meta)} package(s)."],
            "warnings": [], "errors": [], "package_results": [],
            "summary": {"packages_requested": len(package_meta), "packages_applied": 0, "inserted_nodes": 0, "copied_assets": 0},
        }
        converter = Path(args.grle_converter) if getattr(args, "grle_converter", None) else None
        expected_fruits: set[str] = set()

        for index, package_path in enumerate(package_paths, start=1):
            current_target = MapProfile(str(work_root), str(work_root), False, str(work_root))
            scan_profile(current_target)
            ctx: Optional[dict] = None
            try:
                ctx = reconstruct_package_source_for_target(package_path, current_target)
                crop = ctx["crop"]
                apply_crops = list(ctx.get("applyCrops", [crop]))
                expected_fruits.update(x.lower() for x in ctx.get("expectedFruitTypes", [crop]))
                report_prefix = f"CropPorter_Apply_{index:02d}_{safe_report_token(crop)}"
                if apply_crops:
                    report = apply_patch_in_place(
                        ctx["source"], work_root, apply_crops, grle_converter=converter,
                        report_prefix=report_prefix, target_source_label=target.source_path
                    )
                else:
                    report = {
                        "version": VERSION, "mode": "apply-packages-noop",
                        "source": str(package_path), "target": target.source_path,
                        "crops": [], "actions": ["Crop family already present; package required no changes."],
                        "warnings": [], "errors": [],
                        "summary": {"inserted_nodes": 0, "copied_assets": 0},
                    }
                    write_reports(report, work_root, prefix=report_prefix)
                adapter_report = {
                    "version": VERSION, "mode": "apply-packages-adapter", "sequence": index,
                    "package": str(package_path), "crop": crop, "applyCrops": apply_crops,
                    "expectedFruitTypes": ctx.get("expectedFruitTypes", []),
                    "sourceSnapshot": ctx["sourceSnapshot"],
                    "sourceMapDataPrefix": ctx["sourceMapDataPrefix"], "targetMapDataPrefix": ctx["targetMapDataPrefix"],
                    "sourceFoliageRoot": ctx["sourceFoliageRoot"], "targetFoliageRoot": ctx["targetFoliageRoot"],
                    "foliageAdapterActions": ctx["foliageAdapterActions"],
                    "familyAdapterActions": ctx.get("familyAdapterActions", []),
                    "commonEngineReport": f"{report_prefix}.json",
                }
                adapter_name = f"CropPorter_PackageAdapter_{index:02d}_{safe_report_token(crop)}.json"
                (work_root / adapter_name).write_text(json.dumps(adapter_report, indent=2), encoding="utf-8")
                multi_report["package_results"].append({
                    "sequence": index, "package": str(package_path), "crop": crop,
                    "applyCrops": apply_crops, "expectedFruitTypes": ctx.get("expectedFruitTypes", []),
                    "report": f"{report_prefix}.json", "adapterReport": adapter_name,
                    "summary": dict(report.get("summary", {})),
                    "warnings": list(report.get("warnings", [])), "errors": list(report.get("errors", [])),
                })
                multi_report["summary"]["inserted_nodes"] += int(report.get("summary", {}).get("inserted_nodes", 0))
                multi_report["summary"]["copied_assets"] += int(report.get("summary", {}).get("copied_assets", 0))
                multi_report["warnings"].extend([f"{crop}: {w}" for w in report.get("warnings", [])])
                if report.get("errors"):
                    multi_report["errors"].extend([f"{crop}: {e}" for e in report["errors"]])
                    write_reports(multi_report, work_root, prefix="CropPorter_MultiApply")
                    failed = output_final.with_name(output_final.name + "_FAILED")
                    if failed.exists():
                        shutil.rmtree(failed, ignore_errors=True)
                    work_root.rename(failed)
                    work_root = None
                    print(f"Multi-package build stopped on '{crop}'. Diagnostic output preserved: {failed}")
                    for err in report["errors"]:
                        print(f"ERROR: {err}")
                    return 1
                multi_report["summary"]["packages_applied"] += 1
                applied_desc = ", ".join(apply_crops) if apply_crops else "no changes (family already present)"
                multi_report["actions"].append(
                    f"Processed package {index}/{len(package_paths)}: {crop}; applied: {applied_desc}; "
                    "rescanned accumulated target before next package."
                )
                multi_report["actions"].extend(
                    [f"{crop}: {action}" for action in ctx.get("familyAdapterActions", [])]
                )
            finally:
                if ctx is not None:
                    cleanup_reconstructed_package(ctx)

        final_profile = MapProfile(str(work_root), str(work_root), False, str(work_root))
        scan_profile(final_profile)
        final_fruits = {x.lower() for x in final_profile.fruit_names}
        expected_fruits.update(seen_crops)
        missing_final = sorted(crop for crop in expected_fruits if crop not in final_fruits)
        if missing_final:
            multi_report["errors"].append("Final accumulated target scan is missing imported fruitType(s): " + ", ".join(missing_final))

        density = get_density_fruits_layer_settings(work_root)
        state = {
            "version": VERSION, "baseTarget": target.source_path, "output": str(output_final),
            "applied": package_meta, "densityMapFruits": density,
        }
        (work_root / "CropPorter_State.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        multi_report["stateFile"] = "CropPorter_State.json"
        multi_report["densityMapFruits"] = density
        write_reports(multi_report, work_root, prefix="CropPorter_MultiApply")

        if multi_report["errors"]:
            failed = output_final.with_name(output_final.name + "_FAILED")
            if failed.exists():
                shutil.rmtree(failed, ignore_errors=True)
            work_root.rename(failed)
            work_root = None
            print(f"Multi-package final validation failed. Diagnostic output preserved: {failed}")
            for err in multi_report["errors"]:
                print(f"ERROR: {err}")
            return 1

        try:
            promotion_mode, promotion_warnings = promote_completed_work_tree(work_root, output_final)
        except Exception:
            # The crop build itself completed successfully. Preserve the work tree for
            # manual recovery/diagnosis instead of deleting it in the outer finally.
            work_root = None
            raise
        work_root = None
        multi_report["actions"].append(f"Promoted completed work tree using: {promotion_mode}.")        multi_report["warnings"].extend(promotion_warnings)
        # Refresh the final report in the promoted output so the promotion method and
        # any Windows filesystem warning are captured in the build audit.
        write_reports(multi_report, output_final, prefix="CropPorter_MultiApply")
        zip_path = None
        zip_count = 0
        if getattr(args, "zip_output", None):
            zip_path, zip_count = create_release_zip(output_final, Path(args.zip_output))

        print("Multi-package build complete via common CropPorter engine.")
        print(f"Output: {output_final}")
        print(f"Packages applied: {multi_report['summary']['packages_applied']}/{multi_report['summary']['packages_requested']}")
        print(f"Inserted/patched nodes: {multi_report['summary']['inserted_nodes']}")
        print(f"Copied assets: {multi_report['summary']['copied_assets']}")
        if zip_path is not None:
            print(f"Release ZIP: {zip_path} ({zip_count} file(s); CropPorter reports/backups excluded)")
        if multi_report["warnings"]:
            print("Warnings:")
            for warning in multi_report["warnings"]:
                print(f"- {warning}")
        return 0
    finally:
        target.cleanup()
        if work_root is not None and work_root.exists():
            shutil.rmtree(work_root, ignore_errors=True)

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

    probe_plantation = sub.add_parser("probe-plantation-crop", help="Probe vine/orchard/row-crop dependencies such as coffee plantations.")
    probe_plantation.add_argument("--source", required=True, help="Source map folder or ZIP containing the plantation crop.")
    probe_plantation.add_argument("--crop", required=True, help="Crop fruitType name, e.g. coffee.")
    probe_plantation.set_defaults(func=cmd_probe_plantation_crop)

    apply_plantation = sub.add_parser("apply-plantation", help="Experimental: port vine/orchard/row crop systems such as coffee plantations.")
    apply_plantation.add_argument("--source", required=True, help="Source map folder or ZIP containing the plantation crop.")
    apply_plantation.add_argument("--target", required=True, help="Target map folder or ZIP to patch.")
    apply_plantation.add_argument("--crop", required=True, help="Crop fruitType name, e.g. coffee.")
    apply_plantation.add_argument("--output", required=True, help="Output folder for the patched target map.")
    apply_plantation.add_argument("--force", action="store_true", help="Allow experimental apply even if the crop is not classified as plantation/vine.")
    apply_plantation.add_argument("--include-vehicles", action="store_true", help="Also copy optional plantation vehicle dependencies and add their storeItems, e.g. Braud 9090X COFFEE.")
    apply_plantation.set_defaults(func=cmd_apply_plantation)

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
    apply.add_argument("--grle-converter", default=DEFAULT_GRLE_CONVERTER, help=f"GIANTS grleConverter convert.cmd/exe/folder used for density-map expansion. Default: {DEFAULT_GRLE_CONVERTER}")
    apply.set_defaults(func=cmd_apply)


    export_crop = sub.add_parser("export-crop", help="v0.3: export a crop from a source map into a reusable CropPorter package.")
    export_crop.add_argument("--source", required=True, help="Source map folder or ZIP containing the crop.")
    export_crop.add_argument("--crop", required=True, help="Crop fruitType name, e.g. blackbean or coffee.")
    export_crop.add_argument("--library", required=True, help="Folder where the .cropporter.zip package will be written.")
    export_crop.add_argument("--source-name", help="Optional source map label stored in the package manifest.")
    export_crop.add_argument("--package-name", help="Optional output package filename.")
    export_crop.add_argument("--plantation", action="store_true", help="Export as a plantation/vine package using the v0.2 plantation probe.")
    export_crop.add_argument("--include-vehicles", action="store_true", help="Include optional plantation vehicle dependencies in the package.")
    export_crop.add_argument("--redistribution", default="unknown", choices=["unknown", "private", "permitted", "open"], help="Redistribution status recorded in the package manifest.")
    export_crop.set_defaults(func=cmd_export_crop)

    probe_package = sub.add_parser("probe-package", help="v0.3: inspect a CropPorter package manifest.")
    probe_package.add_argument("package", help=".cropporter.zip file or extracted package folder.")
    probe_package.add_argument("--assets", action="store_true", help="List packaged assets.")
    probe_package.set_defaults(func=cmd_probe_package)

    list_library = sub.add_parser("list-library", help="v0.3: list packages in a CropPorter library folder.")
    list_library.add_argument("library", help="Folder containing .cropporter.zip packages.")
    list_library.set_defaults(func=cmd_list_library)

    apply_package = sub.add_parser("apply-package", help="v0.3: apply a CropPorter package to a target map without the original source map.")
    apply_package.add_argument("--package", required=True, help=".cropporter.zip file or extracted package folder.")
    apply_package.add_argument("--target", required=True, help="Target map folder or ZIP to patch.")
    apply_package.add_argument("--output", required=True, help="Output folder for the patched target map.")
    apply_package.add_argument("--grle-converter", default=DEFAULT_GRLE_CONVERTER, help=f"GIANTS grleConverter convert.cmd/exe/folder used for density-map expansion. Default: {DEFAULT_GRLE_CONVERTER}")
    apply_package.set_defaults(func=cmd_apply_package)

    apply_packages = sub.add_parser(
        "apply-packages",
        help="v0.3: build one target map from multiple CropPorter field-crop packages in one working tree.",
    )
    apply_packages.add_argument(
        "--package", action="append", required=True,
        help=".cropporter.zip file or extracted package folder. Repeat --package for each crop, in apply order.",
    )
    apply_packages.add_argument("--target", required=True, help="Pristine target map folder or ZIP. Copied only once.")
    apply_packages.add_argument("--output", required=True, help="Final combined patched target map folder.")
    apply_packages.add_argument(
        "--zip-output",
        help="Optional final FS25 ZIP. Includes the complete map (including language/) and excludes CropPorter reports/backups.",
    )
    apply_packages.add_argument(
        "--grle-converter", default=DEFAULT_GRLE_CONVERTER,
        help=f"GIANTS grleConverter convert.cmd/exe/folder used for density-map expansion. Default: {DEFAULT_GRLE_CONVERTER}",
    )
    apply_packages.set_defaults(func=cmd_apply_packages)

    migrate_save = sub.add_parser(
        "migrate-save",
        help="v0.3: migrate an established savegame densityMap_height.gdm to the patched map terrainDetailHeight layout.",
    )
    migrate_save.add_argument("--savegame", required=True, help="Existing FS25 savegame folder containing densityMap_height.gdm.")
    migrate_save.add_argument("--map", required=True, help="Patched map folder or ZIP whose terrainDetailHeight layout the save must match.")
    migrate_save.add_argument(
        "--grle-converter", default=DEFAULT_GRLE_CONVERTER,
        help=f"GIANTS grleConverter convert.cmd/exe/folder used to decode and validate the save GDM. Default: {DEFAULT_GRLE_CONVERTER}",
    )
    migrate_save.add_argument("--dry-run", action="store_true", help="Validate and show the inferred migration plan without modifying the savegame.")
    migrate_save.set_defaults(func=cmd_migrate_save)


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