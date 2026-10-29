"""Export public configuration contracts; --check rejects stale checked-in schemas."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from avatarweave_agent.avatars import AvatarDocument
from avatarweave_agent.session_options import SessionOptions, SessionMetadata
from avatarweave_agent.protocol import COMMANDS

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
for name, model in [
    ("avatar-profile.schema.json", AvatarDocument),
    ("session-options.schema.json", SessionOptions),
    ("session-metadata.schema.json", SessionMetadata),
    ("avatar-command.schema.json", COMMANDS),
]:
    path = ROOT / "docs" / name
    schema = (
        model.model_json_schema()
        if hasattr(model, "model_json_schema")
        else model.json_schema()
    )
    if name == "avatar-command.schema.json":
        schema.update(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:avatarweave:command:v1",
                "title": "AvatarWeave command v1",
            }
        )
    expected = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not path.exists() or path.read_text() != expected:
            sys.exit(
                f"Stale schema: {path.name}. Run backend/.venv/bin/python scripts/config-schema.py"
            )
    else:
        path.write_text(expected)
print("Configuration schemas are current.")
