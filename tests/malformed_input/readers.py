"""Every reader of a file or record in the repository, gathered from the readers_*.py modules by area.

A reader joins its area's table in the pull request that adds it, as "Validation" in docs/adding-a-skill.md says;
reader_table.py describes an entry. PENDING names each reader not yet in a table, with the open issue whose pull
request adds it, so a reader is either held to the corpus or named as owed.
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
PENDING: dict[str, int] = {
    "tools.hub_guard.powershell_events": 2345,
    "tools.hub_guard.guard": 2345,
    "tools.plugin_agents._scalar": 2345,
    "tools.plugin_agents.plugin_agent": 2345,
    "tools.plugin_build.render": 2345,
    "tools.plugin_canary.expected": 2345,
    "tools.plugin_canary.option_values": 2345,
    "tools.plugin_canary.install": 2345,
    "tools.plugin_canary.stream_events": 2345,
    "tools.plugin_canary.checks": 2345,
    "tools.runtime_canary.starts_only_by_user": 2345,
    "tools.runtime_canary.claude_stream": 2345,
    "tools.runtime_canary.claimed": 2345,
    "tools.runtime_changelogs.frontmatter_terms": 2345,
    "tools.runtime_changelogs.changelog_releases": 2345,
    "tools.runtime_changelogs.summary": 2345,
    "tools.runtime_prompts.copilot_entrypoint": 2345,
    "tools.skill_evals_runs.skills_in_stream": 2345,
    "tools.skill_evals_runs.set_reviewer_model": 2345,
    "tools.skill_evals_runs._nested": 2345,
    "tools.skill_evals_runs.reviewer_agents": 2345,
    "tools.skill_evals_runs.stream_models": 2345,
    "deployer.discovery.parse_codex_catalog": 2345,
    "deployer.render._substitute_markdown": 2345,
    "deployer.render.render_file": 2345,
    "deployer.render._adapter_frontmatter": 2345,
    "deployer.state_files.parse_json": 2345,
    "deployer.state_files.holds_lone_surrogate": 2345,
    "audit_ai_config._entry_value": 2345,
    "audit_ai_config._block_scalar": 2345,
    "audit_ai_config._skill_body": 2345,
    "audit_ai_config._manifest_signal": 2345,
    "audit_ai_config._codex_hooks": 2345,
    "mcp_handshake.Session.response": 2345,
    "skill_inventory.trigger_scenario_lines": 2345,
    "skill_inventory.tools": 2345,
    "skill_inventory.read_agent": 2345,
    "skill_inventory.json_settings": 2345,
    "skill_inventory.markdown_settings": 2345,
    "skill_inventory.own_needs": 2345,
    "memory_audit.manifest_skill_files": 2345,
    "csharp_layout.evaluated_reference": 2345,
    "dotnet_format_targets.pull_request_base": 2345,
    "dotnet_format_targets.is_sdk_style": 2345,
    "review_analyzers._package_json": 2345,
    "review_config._main": 2345,
    "review_dispatch.count_reads": 2345,
    "review_guard._roles": 2345,
    "review_guard._load_claim": 2345,
    "review_guard._prune_claims": 2345,
    "review_guard._restricted": 2345,
    "review_guard.main": 2345,
    "review_io.atomic_write_json": 2345,
    "review_reviewers._tool_grant": 2345,
    "review_reviewers.frontmatter_value": 2345,
    "github_client.body_reasons": 2345,
    "github_client.GitHubClient.partial_graphql": 2345,
    "json_writer.dumps": 2345,
    "review_synthesis.remove_run": 2345,
    "audit_repository.names_directory": 2345,
    "change_skill.frontmatter_block": 2345,
    "change_skill.frontmatter_value": 2345,
    "change_skill.user_only": 2345,
    "conditions.snapshot_exclusions": 2345,
}

__all__ = ["PENDING", "READERS", "NotText", "Reader", "applicable_cases"]
