"""Skills are markdown, so nothing else checks them.

A skill that names a stage key the CLI does not have, or a flag that was
renamed, fails at the worst possible moment: the model reads the instruction,
runs the command, gets an error, and improvises. Improvisation over a missing
number is exactly what this pipeline exists to prevent.

These tests treat the skill files as what they are — configuration for a
program — and check them against the code they drive.

Run: python -m tests.test_skill_manifest   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents import stages                                    # noqa: E402
from agents.session import STAGE_KEYS                        # noqa: E402

SKILLS_DIR = os.path.join(ROOT, "skills")
COMMANDS_DIR = os.path.join(ROOT, "commands")

# Every stage needs a skill that can run it on its own.
STAGE_SKILLS = {
    "problem": "fv-questions", "schema": "fv-schema", "clean": "fv-clean",
    "features": "fv-features", "eda": "fv-eda", "analyst": "fv-metrics",
    "predict": "fv-predict", "visualize": "fv-charts", "insights": "fv-insights",
    "recommend": "fv-actions", "monitor": "fv-monitor", "report": "fv-report",
}


def _skill_files() -> dict:
    found = {}
    for entry in sorted(os.listdir(SKILLS_DIR)):
        path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                found[entry] = fh.read()
    return found


def _front_matter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must open with YAML front matter"
    fields = {}
    for line in match.group(1).split("\n"):
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def test_every_stage_has_a_skill_that_can_run_it() -> None:
    """A stage with no skill is unreachable to the operator."""
    skills = _skill_files()
    for key in STAGE_KEYS:
        name = STAGE_SKILLS.get(key)
        assert name, f"stage '{key}' is not mapped to a skill"
        assert name in skills, f"stage '{key}' names a missing skill: {name}"
        assert f"--stage {key}" in skills[name], (
            f"{name} never invokes `--stage {key}`")

    # And the reverse: a skill naming a stage the registry does not have.
    for name, text in skills.items():
        for invoked in re.findall(r"--stage (\w+)", text):
            assert invoked in STAGE_KEYS or invoked in ("next", "all"), (
                f"{name} invokes unknown stage '{invoked}'")


def test_front_matter_is_usable() -> None:
    """The description is what decides whether a skill is ever selected."""
    for directory, text in _skill_files().items():
        fields = _front_matter(text)
        assert fields.get("name") == directory, (
            f"{directory}: front-matter name is {fields.get('name')!r}")
        description = fields.get("description", "")
        assert len(description) > 80, (
            f"{directory}: description too thin to match a request on")
        # A description with no trigger vocabulary will not be selected for the
        # requests it is meant to serve.
        assert re.search(r"\bUse when\b|\bTriggers on\b", description), (
            f"{directory}: description says what it is but not when to use it")


def test_skills_only_use_flags_the_cli_accepts() -> None:
    """A renamed flag turns a skill into a broken instruction."""
    known = _cli_flags()
    # Every CLI a skill is allowed to invoke. A skill that reaches for a flag
    # none of them defines is a broken instruction, so this list has to grow
    # whenever a new entry point does.
    other_cli = set().union(*(
        _cli_flags(script) for script in
        ("run_pipeline.py", "run_stats.py", "run_cross.py")
    ))
    for name, text in _skill_files().items():
        for flag in set(re.findall(r"(--[a-z][a-z-]+)", text)):
            assert flag in known or flag in other_cli, (
                f"{name} uses unknown flag {flag}")


def _cli_flags(script: str = "run_stage.py") -> set:
    """Flags a script actually defines, read from its source."""
    with open(os.path.join(ROOT, "scripts", script), encoding="utf-8") as fh:
        source = fh.read()
    return set(re.findall(r'add_argument\(\s*"(--[a-z][a-z-]+)"', source))


def test_commands_point_at_skills_that_exist() -> None:
    skills = set(_skill_files())
    for entry in sorted(os.listdir(COMMANDS_DIR)):
        if not entry.endswith(".md"):
            continue
        with open(os.path.join(COMMANDS_DIR, entry), encoding="utf-8") as fh:
            text = fh.read()
        for referenced in re.findall(r"`(fv-[a-z-]+)` skill", text):
            assert referenced in skills, (
                f"commands/{entry} defers to missing skill {referenced}")


def test_offers_that_name_an_artifact_use_a_real_one() -> None:
    """A checkpoint must not offer a download for a file nothing writes."""
    written = {stages.CANONICAL_PARQUET, stages.CLEANED_CSV,
               stages.FEATURES_PARQUET, stages.REPORT_HTML}
    for key, offers in stages.CHECKPOINT_OFFERS.items():
        assert key in STAGE_KEYS, f"offers for unknown stage '{key}'"
        for offer in offers:
            name = offer.get("artifact")
            if name:
                assert name in written, f"{key} offers unwritten artifact {name}"


def test_plugin_manifests_agree_and_parse() -> None:
    """A malformed manifest fails at install, where nothing here can catch it.

    The version appears in two files; drift between them means the marketplace
    advertises one build and installs another.
    """
    import json

    plugin_dir = os.path.join(ROOT, ".claude-plugin")
    with open(os.path.join(plugin_dir, "plugin.json"), encoding="utf-8") as fh:
        plugin = json.load(fh)
    with open(os.path.join(plugin_dir, "marketplace.json"), encoding="utf-8") as fh:
        market = json.load(fh)

    for field in ("name", "version", "description"):
        assert plugin.get(field), f"plugin.json is missing {field}"
    assert re.match(r"^\d+\.\d+\.\d+$", plugin["version"]), plugin["version"]

    entries = market.get("plugins") or []
    entry = next((p for p in entries if p.get("name") == plugin["name"]), None)
    assert entry, f"marketplace.json does not list {plugin['name']}"
    assert entry.get("version") == plugin["version"], (
        f"version drift: plugin.json {plugin['version']} vs "
        f"marketplace.json {entry.get('version')}")
    assert entry.get("source"), "marketplace entry has no source"


def test_readme_describes_the_plugin_that_exists() -> None:
    """A README naming skills or commands that are gone misroutes the reader.

    Checked because this one went stale through a whole refactor: it described
    eight agents and two slash commands long after there were twelve stages and
    sixteen of each.
    """
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()

    import json

    plugin_dir = os.path.join(ROOT, ".claude-plugin")
    with open(os.path.join(plugin_dir, "plugin.json"), encoding="utf-8") as fh:
        plugin_name = json.load(fh)["name"]
    with open(os.path.join(plugin_dir, "marketplace.json"), encoding="utf-8") as fh:
        market_name = json.load(fh)["name"]

    skills = set(_skill_files())
    commands = {e[:-3] for e in os.listdir(COMMANDS_DIR) if e.endswith(".md")}
    # The install lines legitimately name the plugin and the marketplace, and
    # neither is a skill.
    known = skills | commands | {plugin_name, market_name}
    for named in set(re.findall(r"[`/](fv-[a-z-]+)", readme)):
        assert named in known, (
            f"README names {named}, which is neither a skill nor a command")

    # Every stage skill should be findable from the README, or an operator has
    # no way to learn the stage exists.
    for skill in STAGE_SKILLS.values():
        assert skill in readme, f"README never mentions {skill}"

    for script in re.findall(r"python scripts/(\w+\.py)", readme):
        assert os.path.exists(os.path.join(ROOT, "scripts", script)), (
            f"README invokes missing script {script}")


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
