"""Every reader of a file or record in the repository, gathered from the readers_*.py modules by area.

A reader joins its area's table in the pull request that adds it, as "Validation" in docs/adding-a-skill.md says;
reader_table.py describes an entry. PENDING names each reader not yet in a table, with the open issue whose pull
request adds it, so a reader is either held to the corpus or named as owed. NOT_READERS names each function that calls
a shared reader without being a reader, with why: it reads only what the same run just wrote, the repository's own
committed text, or text a held reader in another module passes it.
"""

from __future__ import annotations

import readers_core_review
import readers_deployer
import readers_repository
import readers_skills
from reader_table import NotText, Reader, applicable_cases

READERS: list[Reader] = sorted(
    [*readers_core_review.READERS, *readers_deployer.READERS, *readers_repository.READERS, *readers_skills.READERS],
    key=lambda reader: reader.name,
)

# Each function that calls a shared reader, as reader_scan.py finds it, that no table holds yet and no held reader
# of its module reaches, by the name its entry will take, mapped to the issue whose pull request adds it or names it
# as no reader.
PENDING: dict[str, int] = {}

# Each function that calls a shared reader, as reader_scan.py finds it, that no table holds because it is no reader,
# with why. Validation's policies read the repository's own committed files, which validation holds to their shape.
COMMITTED_SETTINGS = "reads the committed .claude/settings.json, whose shape this policy and its siblings hold"
DEPLOY_METADATA = "reads the committed deploy-meta/*.json, whose shape the metadata policies hold"
SOURCE_JSON = "reads the committed source.json and deploy-meta/*.json, whose shape the metadata policies hold"
SNAPSHOT = "reads the committed text of a release tag or the working tree, comparing what it cannot read as text"
NOT_READERS: dict[str, str] = {
    "tools.plugin_agents._scalar": "reads one hook value of a committed agent definition under agents/",
    "tools.plugin_agents.plugin_agent": "splits a committed agent definition under agents/ to move its hooks into "
    "the plugin's hooks file",
    "tools.plugin_build.render": "renders a committed skill file into a plugin, splitting a Markdown file's "
    "frontmatter only to choose its substitutions; one it cannot split renders as body",
    "tools.plugin_canary.expected": "reads the hooks file plugin_build wrote for this run's plugin",
    "tools.plugin_canary.option_values": "reads the manifest plugin_build wrote for this run's plugin",
    "tools.plugin_canary.checks": "reads the PATHS line install wrote with json.dumps in this run",
    "tools.runtime_canary.starts_only_by_user": "reads a SKILL.md or runtime adapter this run deployed from the "
    "checkout into its throwaway home",
    "tools.runtime_changelogs.frontmatter_terms": "reads the keys of the committed skills/*/SKILL.md and agents/*.md",
    "tools.runtime_changelogs.summary": "reads the finding lines watch printed in this run, each reason json.dumps "
    "wrote",
    "tools.runtime_prompts.copilot_entrypoint": "reads the review request prepare_fixture wrote in this render's "
    "throwaway folder",
    "tools.skill_evals_runs.set_reviewer_model": "reads back the reviewer agent it has just written",
    "tools.skill_evals_runs._nested": "reads a hook value of the reviewer agent this run deployed from agents/",
    "tools.skill_evals_runs.reviewer_agents": "reads the reviewer agent this run deployed from agents/ into its "
    "throwaway home",
    "deployer.render._substitute_markdown": "splits a template's frontmatter only to quote each line for its "
    "context; an opening rule never closed renders as Markdown",
    "deployer.render.render_file": "re-reads only the JSON it has just rendered, to refuse a substitution that broke "
    "it; a template it cannot decode is copied as it is",
    "deployer.state_files.parse_json": "the deployer's one JSON parse of a state file's text, called by each state "
    "file's reader, which readers_deployer.py holds",
    "deployer.state_files.holds_lone_surrogate": "re-reads text deployer.manifest.load has already refused, to name "
    "an earlier release's file",
    "review_io.atomic_write_json": "reads back the text json_writer.dumps has just written, for the validator",
    "json_writer.dumps": "reads back the text json.dumps has just written, to refuse a value that cannot round-trip",
    (
        "repository_hygiene.RepositoryHygienePolicies."
        "test_tracked_claude_settings_hold_schema_hooks_and_no_attribution_only"
    ): COMMITTED_SETTINGS,
    (
        "repository_hygiene.RepositoryHygienePolicies.test_hub_guard_hook_sees_every_tool_that_edits_files_or_runs_git"
    ): COMMITTED_SETTINGS,
    "repository_hygiene.RepositoryHygienePolicies.test_hub_guard_hook_blocks_a_command_when_it_fails": (
        COMMITTED_SETTINGS
    ),
    "shell_targets.interpreter_problems": "reads the grants of the committed skills/*/SKILL.md",
    "skill_grants.root_option_problems": "reads the grants of the committed skills' SKILL.md files",
    "skill_layout.deploy_variable_problems": SOURCE_JSON,
    "skill_layout.metadata_format_problems": DEPLOY_METADATA,
    "skill_layout.hooked_agents": "reads the frontmatter of the committed agents/*.md",
    "skill_layout.own_needs": "reads a committed skill's SKILL.md and deploy-meta/*.json",
    "skill_layout.runtime_support_problems": DEPLOY_METADATA,
    "skill_layout.runtime_page_problems": DEPLOY_METADATA,
    "skill_layout.capability_guide_problems": DEPLOY_METADATA,
    "skill_layout._path_documents": DEPLOY_METADATA,
    "skill_layout.script_dependency_problems": DEPLOY_METADATA,
    "skill_layout.fixture_source_problems": "reads the committed source.json and the fixture sources' source.json "
    "under tests/fixtures/",
    "skill_layout._hook_problems": "reads the hooks file plugin_build wrote for this policy's plugin",
    "skill_layout._manifest_problems": "reads the committed .claude-plugin/marketplace.json and the manifest "
    "plugin_build wrote for this policy's plugin",
    "skill_layout.marketplace_problems": "reads the committed .claude-plugin/marketplace.json",
    "skill_layout.trigger_scenario_problems": "reads the committed skills' SKILL.md and the scenario.json fixtures "
    "under tests/fixtures/skill-evals/",
    "skill_layout.skill_tree_problems": SOURCE_JSON,
    "skill_layout.repository_skill_name_problems": "reads the committed .claude/skills/*/SKILL.md",
    "skill_layout.shared_guidance_problems": SOURCE_JSON,
    "skill_layout.SkillLayoutPolicies.test_skills_leave_shared_runtime_guidance_to_their_adapters": SOURCE_JSON,
    "skill_scripts.skill_command_problems": DEPLOY_METADATA,
    "skill_scripts.optional_tool_placement_problems": DEPLOY_METADATA,
    "toolchain.powershell_json": "reads the last line validation's own PowerShell script prints, failing the policy "
    "with the output when it is not a JSON list",
    "upgrade_notes._required_variables": SNAPSHOT,
    "upgrade_notes._review_schemas": SNAPSHOT,
    "validation_support.skill_dependency_closure": DEPLOY_METADATA,
}

__all__ = ["NOT_READERS", "PENDING", "READERS", "NotText", "Reader", "applicable_cases"]
