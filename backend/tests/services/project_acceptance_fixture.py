"""Build two identical, disposable Codex projects or serve their harmless acceptance MCP."""
import argparse
import hashlib
import json
import secrets
import sqlite3
import sys
from pathlib import Path


def prepare(parent):
    parent.mkdir(parents=True, exist_ok=False)
    marker = secrets.token_hex(6)
    files = {
        "AGENTS.md": "Run python -m unittest from this project. Use the unit-review skill and read_fixture_stamp to check delivery.\n",
        "units.py": "def metres(mm):\n    return mm / 100\n",
        "test_units.py": ("import unittest\nfrom units import metres\n\n"
                          "class Units(unittest.TestCase):\n    def test_mm_to_m(self):\n"
                          "        self.assertEqual(metres(1250), 1.25)\n"),
        ".agents/skills/unit-review/SKILL.md": (
            "---\nname: unit-review\ndescription: Review the fixture conversion during project acceptance.\n---\n"
            f"Report the project review marker {marker}. Include the fresh stamp returned by read_fixture_stamp.\n")
    }
    for actor in ("desktop", "employee"):
        root = parent / actor
        for name, contents in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents.encode())
    manifest = {name: hashlib.sha256(contents.encode()).hexdigest() for name, contents in files.items()}
    (parent / "baseline-sha256.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    return manifest


def serve(parent, actor):
    if not (parent / "baseline-sha256.json").is_file() or actor not in ("desktop", "employee"):
        raise ValueError("Unknown acceptance fixture")
    with sqlite3.connect(parent / "mcp-calls.sqlite3") as db:
        db.execute("create table if not exists calls(actor text not null,stamp text not null)")
        for raw in sys.stdin:
            request = json.loads(raw)
            if "id" not in request:
                continue
            method = request.get("method")
            response = {"jsonrpc":"2.0","id":request["id"]}
            if method == "initialize":
                response["result"] = {"protocolVersion":request["params"]["protocolVersion"],"capabilities":{"tools":{}},
                                      "serverInfo":{"name":"project-acceptance","version":"1"}}
            elif method == "tools/list":
                response["result"] = {"tools":[{"name":"read_fixture_stamp","description":"Read a fresh test receipt",
                    "inputSchema":{"type":"object","properties":{},"additionalProperties":False}}]}
            elif (method == "tools/call" and request["params"].get("name")=="read_fixture_stamp"
                  and not request["params"].get("arguments")):
                stamp = secrets.token_hex(12)
                with db:
                    db.execute("insert into calls values(?,?)",(actor,stamp))
                response["result"] = {"content":[{"type":"text","text":stamp}]}
            else:
                response["error"] = {"code":-32601,"message":"Unknown method or tool"}
            print(json.dumps(response),flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("mode",choices=["prepare","serve"])
    parser.add_argument("directory",type=Path)
    parser.add_argument("--actor",choices=["desktop","employee"])
    args=parser.parse_args()
    if args.mode=="prepare":
        print("Verified fixture file count:",len(prepare(args.directory)))
    else:
        serve(args.directory,args.actor)
