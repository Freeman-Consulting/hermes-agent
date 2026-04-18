"""Tests for agent/skill_resolver.py — trigger parsing, matching, formatting."""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.skill_resolver import (
    SkillMatch,
    SkillResolver,
    TriggerEntry,
    resolve_skills,
)


# =========================================================================
# Helpers
# =========================================================================


def _make_resolver(tmp_path: Path, resolver_content: str = "") -> SkillResolver:
    """Create a SkillResolver backed by a temp skills directory."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(exist_ok=True)
    if resolver_content:
        (skills_dir / "RESOLVER.md").write_text(resolver_content, encoding="utf-8")
    return SkillResolver(skills_dir=skills_dir)


def _make_skill(skills_dir: Path, rel_path: str, triggers: list[str], description: str = "test skill"):
    """Write a minimal SKILL.md with triggers in frontmatter."""
    target = skills_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    trigger_yaml = "\n".join(f"  - {t}" for t in triggers)
    content = textwrap.dedent(f"""\
        ---
        name: {rel_path.split("/")[-2]}
        description: {description}
        triggers:
        {trigger_yaml}
        ---

        # {rel_path}
    """)
    target.write_text(content, encoding="utf-8")


# =========================================================================
# RESOLVER.md parsing
# =========================================================================


class TestResolverParsing:
    def test_parses_standard_table(self, tmp_path):
        content = textwrap.dedent("""\
            # Skill Resolver

            ## Apple

            | Trigger | Skill |
            |---------|-------|
            | send imessage | `apple/imessage/SKILL.md` |
            | find my phone | `apple/findmy/SKILL.md` |

            ## GitHub

            | Trigger | Skill |
            |---------|-------|
            | create github issue | `github/github-issues/SKILL.md` |
            | review this PR | `github/github-code-review/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        resolver._ensure_loaded()
        assert resolver.entry_count == 4
        paths = {e.skill_path for e in resolver._entries}
        assert paths == {
            "apple/imessage/SKILL.md",
            "apple/findmy/SKILL.md",
            "github/github-issues/SKILL.md",
            "github/github-code-review/SKILL.md",
        }

    def test_skips_separator_rows(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test

            | Trigger | Skill |
            |---------|-------|
            | send message | `test/msg/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        resolver._ensure_loaded()
        # Should have 1 entry, not 3 (header + separator + data)
        assert resolver.entry_count == 1

    def test_handles_missing_resolver_md(self, tmp_path):
        resolver = _make_resolver(tmp_path, "")
        resolver._ensure_loaded()
        # No crash, just empty
        assert resolver.entry_count == 0

    def test_deduplicates_identical_triggers(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | send message | `test/a/SKILL.md` |
            | send message | `test/a/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        resolver._ensure_loaded()
        assert resolver.entry_count == 1


# =========================================================================
# Frontmatter fallback
# =========================================================================


class TestFrontmatterFallback:
    def test_loads_triggers_from_skill_files_not_in_resolver(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | existing trigger | `test/existing/SKILL.md` |
        """)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "RESOLVER.md").write_text(content)

        # Skill NOT in resolver — should be loaded from frontmatter
        _make_skill(skills_dir, "other/new-skill/SKILL.md", ["do something new"])

        resolver = SkillResolver(skills_dir=skills_dir)
        resolver._ensure_loaded()

        # Should have 2 entries: 1 from resolver + 1 from frontmatter
        assert resolver.entry_count == 2
        sources = {e.source for e in resolver._entries}
        assert sources == {"resolver", "frontmatter"}

    def test_skips_frontmatter_when_resolver_has_entry(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | resolver trigger | `test/skill/SKILL.md` |
        """)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "RESOLVER.md").write_text(content)
        _make_skill(skills_dir, "test/skill/SKILL.md", ["frontmatter trigger"])

        resolver = SkillResolver(skills_dir=skills_dir)
        resolver._ensure_loaded()

        # Only the resolver entry, not the frontmatter one
        assert resolver.entry_count == 1
        assert resolver._entries[0].source == "resolver"


# =========================================================================
# Trigger matching
# =========================================================================


class TestTriggerMatching:
    def test_exact_substring_match(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | find my phone | `apple/findmy/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        matches = resolver.resolve("find my phone")
        assert len(matches) == 1
        assert matches[0].skill_path == "apple/findmy/SKILL.md"

    def test_stop_word_gap_matching(self, tmp_path):
        """'send imessage' should match 'send an imessage'."""
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | send imessage | `apple/imessage/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        matches = resolver.resolve("send an imessage to John")
        assert len(matches) == 1
        assert matches[0].skill_path == "apple/imessage/SKILL.md"

    def test_word_order_preserved(self, tmp_path):
        """Trigger words must appear in order in the message."""
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | create issue | `github/issues/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        # "create a github issue" — "create" before "issue"
        assert len(resolver.resolve("create a github issue")) == 1
        # "the issue I need to create" — "issue" before "create"
        assert len(resolver.resolve("the issue I need to create")) == 0

    def test_single_word_requires_word_boundary(self, tmp_path):
        """Single-word triggers use word-boundary matching."""
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | imessage | `apple/imessage/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        assert len(resolver.resolve("send imessage to John")) == 1
        # "impressions" should not match "imessage"
        assert len(resolver.resolve("make a good impression")) == 0

    def test_single_word_too_short_ignored(self, tmp_path):
        """2-char single-word triggers are too short and ignored."""
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | pr | `github/pr/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        assert len(resolver.resolve("open a pr for this")) == 0

    def test_case_insensitive(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | Create GitHub Issue | `github/issues/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        assert len(resolver.resolve("create a github issue")) == 1
        assert len(resolver.resolve("CREATE A GITHUB ISSUE")) == 1

    def test_no_match_returns_empty(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | send imessage | `apple/imessage/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        assert resolver.resolve("what's the weather today") == []

    def test_max_matches_respected(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | review code | `a/review/SKILL.md` |
            | review PR | `b/pr/SKILL.md` |
            | review this | `c/this/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        matches = resolver.resolve("review code and review PR", max_matches=2)
        assert len(matches) == 2

    def test_deduplicates_by_skill_path(self, tmp_path):
        """If two triggers match the same skill, only one result."""
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | send imessage | `apple/imessage/SKILL.md` |
            | send text message | `apple/imessage/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        matches = resolver.resolve("send imessage")
        assert len(matches) == 1


# =========================================================================
# Scoring and ranking
# =========================================================================


class TestScoring:
    def test_longer_triggers_rank_higher(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | review | `a/short/SKILL.md` |
            | review this PR | `b/long/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        matches = resolver.resolve("review this PR")
        # "review this PR" (14 chars) should rank above "review" (6 chars)
        assert matches[0].skill_path == "b/long/SKILL.md"

    def test_score_is_trigger_length(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | abcdef | `test/one/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        matches = resolver.resolve("abcdef")
        assert matches[0].score == 6


# =========================================================================
# Formatting
# =========================================================================


class TestFormatting:
    def test_format_matches_produces_instruction(self):
        matches = [
            SkillMatch(skill_path="apple/imessage/SKILL.md", matched_trigger="send imessage", score=13),
            SkillMatch(skill_path="apple/findmy/SKILL.md", matched_trigger="find my phone", score=13),
        ]
        result = SkillResolver.format_matches(matches)
        assert "RELEVANT SKILLS DETECTED" in result
        assert "apple/imessage/SKILL.md" in result
        assert "apple/findmy/SKILL.md" in result
        assert "send imessage" in result
        assert "skill_view()" in result

    def test_format_empty_returns_empty_string(self):
        assert SkillResolver.format_matches([]) == ""


# =========================================================================
# Module-level convenience function
# =========================================================================


class TestResolveSkills:
    def test_returns_formatted_string(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | find my phone | `apple/findmy/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        with patch("agent.skill_resolver.get_resolver", return_value=resolver):
            result = resolve_skills("find my phone")
            assert "apple/findmy/SKILL.md" in result
            assert "RELEVANT SKILLS DETECTED" in result

    def test_returns_empty_string_on_no_match(self, tmp_path):
        resolver = _make_resolver(tmp_path, "")
        with patch("agent.skill_resolver.get_resolver", return_value=resolver):
            result = resolve_skills("hello world")
            assert result == ""


# =========================================================================
# Reload and coverage stats
# =========================================================================


class TestUtilities:
    def test_reload_clears_and_reloads(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | test trigger | `test/skill/SKILL.md` |
        """)
        resolver = _make_resolver(tmp_path, content)
        resolver._ensure_loaded()
        assert resolver.entry_count == 1

        resolver.reload()
        # After reload, _loaded is False and entries are cleared
        assert not resolver._loaded
        # But entry_count triggers a re-load, so it's back to 1
        assert resolver.entry_count == 1

    def test_coverage_stats(self, tmp_path):
        content = textwrap.dedent("""\
            ## Test
            | Trigger | Skill |
            |---------|-------|
            | trigger a | `a/skill/SKILL.md` |
            | trigger b | `b/skill/SKILL.md` |
        """)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "RESOLVER.md").write_text(content)
        _make_skill(skills_dir, "c/skill/SKILL.md", ["trigger c"])

        resolver = SkillResolver(skills_dir=skills_dir)
        stats = resolver.get_coverage_stats()
        assert stats["total_entries"] == 3
        assert stats["resolver_entries"] == 2
        assert stats["frontmatter_entries"] == 1
        assert stats["unique_skills"] == 3
