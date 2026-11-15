"""Generate ``deeppyro/core/event_catalog.py`` from the M0 DSH catalog.

The catalog itself is produced by ``scripts/extract_dsh_events.mjs``, which needs
the DSH asar (Node >= 20). This generator only reshapes its JSON dump into a
committed Python table so the runtime never depends on Node or on the asar:

    node scripts/extract_dsh_events.mjs --dump .dsh-events.json
    python scripts/gen_event_catalog.py --json .dsh-events.json

The output is deterministic: names sorted, modes sorted, one site kept.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "deeppyro" / "core" / "event_catalog.py"
MODE_ORDER = ("emit", "serial", "waterfall", "bail", "parallel")

HEADER = '''"""DSH bus events, generated from the M0 catalog. Do not edit by hand.

Source: ``docs/spec/dsh-event-catalog.md`` (see ``scripts/gen_event_catalog.py``).
``BUS_EVENTS`` maps each DSH bus event to its dispatch modes and the first call
site found in the shipped asar, so :mod:`deeppyro.core.events` can tell a real
event from a name that only looks like one.
"""

from __future__ import annotations

__all__ = ["BUS_EVENTS"]

#: name -> (modes, first DSH call site)
BUS_EVENTS: dict[str, tuple[tuple[str, ...], str]] = {
'''


def _dump(path: Path | None) -> dict:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    result = subprocess.run(
        ["node", str(ROOT / "scripts" / "extract_dsh_events.mjs"), "--dump", "-"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _modes(raw: list[str]) -> tuple[str, ...]:
    known = [mode for mode in MODE_ORDER if mode in set(raw)]
    return tuple(known or ("emit",))


def _first_site(sites: list[str]) -> str:
    for site in sites:
        text = str(site).strip()
        if text:
            return text.replace("dsh/node_modules/", "")
    return ""


def _rows(payload: dict) -> dict[str, tuple[tuple[str, ...], str]]:
    rows: dict[str, tuple[tuple[str, ...], str]] = {}
    for item in payload["names"]:
        if item.get("bucket") != "bus":
            continue
        name = str(item["name"])
        if name in rows:
            continue
        rows[name] = (_modes(list(item.get("modes") or [])), _first_site(list(item.get("sites") or [])))
    return dict(sorted(rows.items()))


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render(payload: dict) -> str:
    body = []
    for name, (modes, site) in _rows(payload).items():
        mode_literal = ", ".join(_literal(mode) for mode in modes) + ("," if len(modes) == 1 else "")
        body.append(f'    {_literal(name)}: (({mode_literal}), {_literal(site)}),')
    return HEADER + "\n".join(body) + "\n}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="read a --dump file instead of running node")
    parser.add_argument("--check", action="store_true", help="fail when the committed file is stale")
    args = parser.parse_args(argv)
    payload = _dump(args.json)
    if args.check:
        wanted = _rows(payload)
        if not TARGET.exists():
            print(f"{TARGET.relative_to(ROOT)} is missing")
            return 1
        import importlib
        import sys

        sys.path.insert(0, str(ROOT / "src"))
        module = importlib.import_module("deeppyro.core.event_catalog")
        committed = dict(module.BUS_EVENTS)
        if committed != wanted:
            missing = sorted(set(wanted) - set(committed))
            extra = sorted(set(committed) - set(wanted))
            changed = sorted(
                name for name in set(wanted) & set(committed) if wanted[name] != committed[name]
            )
            print(f"{TARGET.relative_to(ROOT)} is stale: missing={missing} extra={extra} changed={changed}")
            return 1
        print(f"{TARGET.relative_to(ROOT)} up to date ({len(committed)} bus events)")
        return 0
    TARGET.write_text(render(payload), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
