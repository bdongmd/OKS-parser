import os
import sys
import json
import csv
import argparse
from pathlib import Path
from collections import defaultdict

try:
    from lxml import etree
except ImportError:
    raise SystemExit("lxml is required: pip install lxml")

def parse_args():
    p = argparse.ArgumentParser(description="Parse OKS XML files into a graph.")
    p.add_argument("root", type=Path, help="Root directory of the OKS repo")
    p.add_argument("--output-folder", type=Path, default="output/", help="Output directory")
    p.add_argument("--mode", choices=["files", "objects", "both"], default="files",
                   help="What to graph: file-level includes, object-level rels, or both")
    p.add_argument("--format", choices=["json", "graphml", "csv", "all"], default="all",
                   help="Output format(s)")
    p.add_argument("--filter-class", nargs="*", metavar="CLASS",
                   help="(objects mode) Only include objects of these classes")
    p.add_argument("--filter-subsystem", nargs="*", metavar="SUBSYSTEM",
                   help="(files mode) Only include files under these subdirs")
    return p.parse_args()

PARSER = etree.XMLParser(load_dtd=True, no_network=True, recover=True)

def parse_all(root: Path):
    """
    Walk all .xml files under root and return:
        file_includes : dict[str, list[str]]
            file_includes[rel_path] = [included_rel_path, ...]
        obj_defs : dict[(cls, id), str]
            obj_defs[(class, id)] = defining_rel_path
        obj_attrs : dict[(cls, id), dict[str, any]]
            obj_attrs[(class, id)] = {attr_name: value_or_list}
        obj_rels : dict[(cls, id), list[(target_cls, target_id, rel_name)]]
            obj_rels[(class, id)] = [(target_class, target_id, relationship_name), ...]
    """
    file_includes = defaultdict(list)
    obj_defs      = {}   # (cls, id) -> file
    obj_attrs     = {}   # (cls, id) -> {name: val}
    obj_rels      = defaultdict(list)

    all_xml = sorted(root.rglob("*.xml"))
    print(f"Found {len(all_xml)} XML files under {root}")

    skipped = 0
    for xml_path in all_xml:
        rel = str(xml_path.relative_to(root))
        try:
            tree = etree.parse(str(xml_path), PARSER)
        except Exception as e:
            print(f"  SKIP {rel}: {e}")
            skipped += 1
            continue

        oks = tree.getroot()
        if oks is None or oks.tag != "oks-data":
            skipped += 1
            continue

        # <include><file path="..."/></include>
        for f in oks.findall(".//include/file"):
            p = f.get("path", "").strip()
            if p:
                file_includes[rel].append(p)

        # <obj class="..." id="...">
        for obj in oks.findall(".//obj"):
            cls = obj.get("class", "").strip()
            oid = obj.get("id", "").strip()
            if not cls or not oid:
                continue
            key = (cls, oid)
            obj_defs[key] = rel

            # Attributes
            attrs = {}
            for attr in obj.findall("attr"):
                name = attr.get("name", "")
                val  = attr.get("val", None)
                data_vals = [d.get("val") for d in attr.findall("data")]
                if data_vals:
                    attrs[name] = data_vals if len(data_vals) > 1 else data_vals[0]
                elif val is not None:
                    attrs[name] = val
            obj_attrs[key] = attrs

            # Relationships
            for rel_el in obj.findall("rel"):
                rel_name = rel_el.get("name", "")
                # single-value: <rel name="X" class="C" id="I"/>
                rcls = rel_el.get("class", "")
                rid  = rel_el.get("id", "")
                if rcls and rid:
                    obj_rels[key].append((rcls, rid, rel_name))
                # multi-value: <rel name="X"><ref class="C" id="I"/></rel>
                for ref in rel_el.findall("ref"):
                    rcls2 = ref.get("class", "")
                    rid2  = ref.get("id", "")
                    if rcls2 and rid2:
                        obj_rels[key].append((rcls2, rid2, rel_name))

    print(f"  Parsed: {len(all_xml) - skipped} files  |  skipped: {skipped}")
    print(f"  Objects: {len(obj_defs)}")
    print(f"  Object relationships: {sum(len(v) for v in obj_rels.values())}")
    print(f"  File includes: {sum(len(v) for v in file_includes.values())}")
    return file_includes, obj_defs, obj_attrs, obj_rels


# ── Graph construction ────────────────────────────────────────────────────────

SUBSYSTEMS = ["daq","combined","ctp","l1calo","lar","muons","pixel",
              "sct","tile","trt","afp","bcm","lucid","lumi","zdc","dcs"]

def subsystem_of(path_str):
    for s in SUBSYSTEMS:
        if path_str.startswith(s + "/") or path_str == s:
            return s
    return "other"

def build_file_graph(file_includes, filter_subsystems=None):
    """
    Returns nodes and edges for the file-include graph.
    nodes: list of dicts with keys id, label, group (subsystem), file_type
    edges: list of dicts with keys source, target, type, label
    """
    all_files = set(file_includes.keys())
    for incs in file_includes.values():
        all_files.update(incs)

    nodes, edges = [], []

    for f in sorted(all_files):
        sub = subsystem_of(f)
        if filter_subsystems and sub not in filter_subsystems:
            continue
        nodes.append({
            "id":        f,
            "label":     Path(f).name,
            "group":     sub,
            "file_type": "schema" if "schema" in f else "data",
        })

    node_ids = {n["id"] for n in nodes}
    for src, incs in file_includes.items():
        if src not in node_ids:
            continue
        for dst in incs:
            if dst in node_ids:
                edges.append({
                    "source": src,
                    "target": dst,
                    "type":   "include",
                    "label":  "includes",
                })

    return nodes, edges


def build_object_graph(obj_defs, obj_attrs, obj_rels, filter_classes=None):
    """
    Returns nodes and edges for the object-relationship graph.
    nodes: list of dicts with keys id, label, group (class), file
    edges: list of dicts with keys source, target, type (rel_name), label
    """
    # Determine which objects to include
    if filter_classes:
        cls_set = set(filter_classes)
        active = {k for k in obj_defs if k[0] in cls_set}
        # Also include objects they point to (one hop)
        for key in list(active):
            for rcls, rid, _ in obj_rels.get(key, []):
                active.add((rcls, rid))
    else:
        # Only objects that participate in at least one relationship
        active = set(obj_rels.keys())
        for refs in obj_rels.values():
            for rcls, rid, _ in refs:
                active.add((rcls, rid))

    nodes, edges = [], []
    for (cls, oid) in sorted(active):
        nodes.append({
            "id":    f"{cls}::{oid}",
            "label": oid,
            "group": cls,
            "file":  obj_defs.get((cls, oid), ""),
            "attrs": obj_attrs.get((cls, oid), {}),
        })

    node_ids = {n["id"] for n in nodes}
    for (cls, oid), refs in obj_rels.items():
        src = f"{cls}::{oid}"
        if src not in node_ids:
            continue
        for rcls, rid, rel_name in refs:
            dst = f"{rcls}::{rid}"
            if dst in node_ids:
                edges.append({
                    "source": src,
                    "target": dst,
                    "type":   rel_name,
                    "label":  rel_name,
                })

    return nodes, edges


# ── Exporters ─────────────────────────────────────────────────────────────────

def export_json(nodes, edges, path: Path):
    data = {"nodes": nodes, "links": edges}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  → JSON:    {path}")


def export_csv(nodes, edges, node_path: Path, edge_path: Path):
    # Nodes
    node_fields = ["id", "label", "group", "file", "file_type"]
    with open(node_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=node_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(nodes)
    print(f"  → Nodes CSV: {node_path}")

    # Edges
    edge_fields = ["source", "target", "type", "label"]
    with open(edge_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=edge_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(edges)
    print(f"  → Edges CSV: {edge_path}")


def export_graphml(nodes, edges, path: Path):
    """GraphML export — readable by Gephi, Cytoscape, yEd, NetworkX."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/graphml"',
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '         xsi:schemaLocation="http://graphml.graphdrawing.org/graphml',
        '           http://graphml.graphdrawing.org/graphml/1.0rc/graphml.xsd">',
        '  <key id="label"  for="node" attr.name="label"     attr.type="string"/>',
        '  <key id="group"  for="node" attr.name="group"     attr.type="string"/>',
        '  <key id="file"   for="node" attr.name="file"      attr.type="string"/>',
        '  <key id="elabel" for="edge" attr.name="label"     attr.type="string"/>',
        '  <key id="etype"  for="edge" attr.name="type"      attr.type="string"/>',
        '  <graph id="G" edgedefault="directed">',
    ]

    def esc(s):
        return (str(s)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    for n in nodes:
        lines.append(f'    <node id="{esc(n["id"])}">')
        lines.append(f'      <data key="label">{esc(n.get("label",""))}</data>')
        lines.append(f'      <data key="group">{esc(n.get("group",""))}</data>')
        lines.append(f'      <data key="file">{esc(n.get("file",""))}</data>')
        lines.append( '    </node>')

    for i, e in enumerate(edges):
        lines.append(f'    <edge id="e{i}" source="{esc(e["source"])}" target="{esc(e["target"])}">')
        lines.append(f'      <data key="elabel">{esc(e.get("label",""))}</data>')
        lines.append(f'      <data key="etype">{esc(e.get("type",""))}</data>')
        lines.append( '    </edge>')

    lines += ['  </graph>', '</graphml>']
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  → GraphML: {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")

    # Parse
    file_includes, obj_defs, obj_attrs, obj_rels = parse_all(root)

    out_base = args.output_folder 

    if args.mode in ("files", "both"):
        print("\nBuilding file-include graph...")
        nodes, edges = build_file_graph(file_includes, args.filter_subsystem)
        print(f"  {len(nodes)} nodes, {len(edges)} edges")
        suffix = "_files"
        if args.format in ("json", "all"):
            export_json(nodes, edges, out_base / f"oks_graph{suffix}.json")
        if args.format in ("graphml", "all"):
            export_graphml(nodes, edges, out_base / f"oks_graph{suffix}.graphml")
        if args.format in ("csv", "all"):
            export_csv(nodes, edges,
                       out_base / f"oks_nodes{suffix}.csv",
                       out_base / f"oks_edges{suffix}.csv")

    if args.mode in ("objects", "both"):
        print("\nBuilding object-relationship graph...")
        nodes, edges = build_object_graph(obj_defs, obj_attrs, obj_rels, args.filter_class)
        print(f"  {len(nodes)} nodes, {len(edges)} edges")
        suffix = "_objects"
        if args.format in ("json", "all"):
            export_json(nodes, edges, out_base / f"oks_graph{suffix}.json")
        if args.format in ("graphml", "all"):
            export_graphml(nodes, edges, out_base / f"oks_graph{suffix}.graphml")
        if args.format in ("csv", "all"):
            export_csv(nodes, edges,
                       out_base / f"oks_nodes{suffix}.csv",
                       out_base / f"oks_edges{suffix}.csv")

    print("\nDone.")


if __name__ == "__main__":
    main()
