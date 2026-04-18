"""Trigger-based skill resolver — matches user messages against RESOLVER.md.

Parses the trigger→skill routing table from RESOLVER.md at startup and
provides per-message matching so the LLM gets a compact "load these skills"
instruction instead of scanning all 159 entries.

Two data sources (resolver falls back through them):
  1. RESOLVER.md — hand-curated trigger phrases (primary, higher quality)
  2. SKILL.md frontmatter triggers — auto-generated/hybrid (fallback)
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TriggerEntry:
    """A single trigger→skill mapping."""
    trigger: str
    skill_path: str  # e.g. "apple/imessage/SKILL.md"
    source: str = "resolver"  # "resolver" or "frontmatter"


@dataclass
class SkillMatch:
    """A resolved skill match with context."""
    skill_path: str
    matched_trigger: str
    score: int = 0  # trigger length as specificity proxy


class SkillResolver:
    """Parse RESOLVER.md + SKILL.md frontmatter triggers and match against user messages.

    Thread-safe.  Parses once at init; match() is stateless and cheap.
    """

    # How many skills to return per message
    DEFAULT_MAX_MATCHES = 5

    def __init__(self, skills_dir: Optional[Path] = None):
        from hermes_constants import get_skills_dir
        self._skills_dir = skills_dir or get_skills_dir()
        self._entries: list[TriggerEntry] = []
        self._lock = threading.Lock()
        self._loaded = False

    # ── Parsing ────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_resolver()
            self._load_frontmatter_fallback()
            self._loaded = True
            logger.info(
                "SkillResolver loaded: %d trigger entries (%d resolver, %d frontmatter)",
                len(self._entries),
                sum(1 for e in self._entries if e.source == "resolver"),
                sum(1 for e in self._entries if e.source == "frontmatter"),
            )

    def _load_resolver(self) -> None:
        """Parse RESOLVER.md trigger→skill table."""
        resolver_path = self._skills_dir / "RESOLVER.md"
        if not resolver_path.exists():
            logger.debug("RESOLVER.md not found at %s", resolver_path)
            return

        content = resolver_path.read_text(encoding="utf-8")
        # Match rows like: | some trigger phrase | `category/skill/SKILL.md` |
        # Also handle rows without backticks (shouldn't happen but be safe)
        row_pattern = re.compile(
            r'^\|\s*(.+?)\s*\|\s*`?([^`\|]+?SKILL\.md)`?\s*\|$',
            re.MULTILINE,
        )
        seen = set()
        for match in row_pattern.finditer(content):
            trigger = match.group(1).strip()
            skill_path = match.group(2).strip()
            key = (trigger.lower(), skill_path)
            if key in seen:
                continue
            seen.add(key)
            self._entries.append(TriggerEntry(
                trigger=trigger,
                skill_path=skill_path,
                source="resolver",
            ))

        logger.debug("Parsed %d entries from RESOLVER.md", len(seen))

    def _load_frontmatter_fallback(self) -> None:
        """Load triggers from SKILL.md frontmatter for skills missing from RESOLVER.md."""
        # Collect which skill paths are already covered by resolver entries
        resolver_skills = {e.skill_path for e in self._entries if e.source == "resolver"}

        from agent.skill_utils import parse_frontmatter, iter_skill_index_files

        count = 0
        for skill_file in iter_skill_index_files(self._skills_dir, "SKILL.md"):
            rel_path = str(skill_file.relative_to(self._skills_dir))
            if rel_path in resolver_skills:
                continue  # Already covered by RESOLVER.md

            try:
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _ = parse_frontmatter(raw)
                triggers = frontmatter.get("triggers") or []
                if isinstance(triggers, str):
                    triggers = [triggers]
                for trigger in triggers:
                    if trigger and isinstance(trigger, str):
                        self._entries.append(TriggerEntry(
                            trigger=trigger.strip(),
                            skill_path=rel_path,
                            source="frontmatter",
                        ))
                        count += 1
            except Exception:
                continue

        if count:
            logger.debug("Loaded %d frontmatter triggers for skills not in RESOLVER.md", count)

    # ── Matching ───────────────────────────────────────────────────────

    # Stop words that users commonly insert between trigger words
    _STOP_WORDS = frozenset({
        'a', 'an', 'the', 'my', 'your', 'some', 'this', 'that',
        'to', 'for', 'with', 'on', 'in', 'at', 'of', 'from',
        'and', 'or', 'up', 'me',
    })

    def _trigger_matches(self, trigger_lower: str, message_lower: str) -> bool:
        """Check if a trigger phrase matches the user message.

        Strategy:
          - Single-word triggers: word-boundary match only (avoids "pr" matching "project")
          - Multi-word triggers: check that all trigger words appear in the message
            in order, allowing stop-word gaps between them.
            E.g. "send imessage" matches "send an imessage".
        """
        trigger_words = trigger_lower.split()

        if len(trigger_words) == 1:
            # Single word: strict word-boundary match
            word = trigger_words[0]
            if len(word) <= 2:
                return False  # Too short — "pr", "py", etc. are noise
            return bool(re.search(r'\b' + re.escape(word) + r'\b', message_lower))

        # Multi-word: ordered word match allowing stop-word gaps
        msg_words = message_lower.split()
        t_idx = 0
        for m_word in msg_words:
            if t_idx < len(trigger_words) and m_word == trigger_words[t_idx]:
                t_idx += 1
            # Skip stop words (they don't advance the trigger pointer)
            # Non-stop-word mismatches also don't advance — they're just skipped
        return t_idx == len(trigger_words)

    def resolve(self, user_message: str, max_matches: int = DEFAULT_MAX_MATCHES) -> list[SkillMatch]:
        """Match a user message against all triggers and return top skill matches.

        Matching strategy:
          - Case-insensitive substring match
          - Score = trigger length (longer = more specific = higher priority)
          - Deduplicate by skill_path (keep highest-scoring trigger per skill)
          - Return up to max_matches results, sorted by score descending
        """
        self._ensure_loaded()
        if not self._entries:
            return []

        message_lower = user_message.lower()
        matches_by_skill: dict[str, SkillMatch] = {}

        for entry in self._entries:
            trigger_lower = entry.trigger.lower()
            if not trigger_lower:
                continue

            if not self._trigger_matches(trigger_lower, message_lower):
                continue

            score = len(entry.trigger)  # Longer triggers = more specific
            existing = matches_by_skill.get(entry.skill_path)
            if existing is None or score > existing.score:
                matches_by_skill[entry.skill_path] = SkillMatch(
                    skill_path=entry.skill_path,
                    matched_trigger=entry.trigger,
                    score=score,
                )

        ranked = sorted(matches_by_skill.values(), key=lambda m: m.score, reverse=True)
        return ranked[:max_matches]

    # ── Formatting ─────────────────────────────────────────────────────

    @staticmethod
    def format_matches(matches: list[SkillMatch]) -> str:
        """Format resolver matches as a compact instruction for the LLM."""
        if not matches:
            return ""

        lines = ["RELEVANT SKILLS DETECTED — load these with skill_view() before responding:"]
        for m in matches:
            lines.append(f"  - {m.skill_path} (matched: \"{m.matched_trigger}\")")
        lines.append("")
        lines.append("Load each skill listed above and follow its instructions. "
                      "The full skill index is below if none of these match.")
        return "\n".join(lines)

    # ── Utilities ──────────────────────────────────────────────────────

    def reload(self) -> None:
        """Force re-parse on next resolve() call."""
        with self._lock:
            self._loaded = False
            self._entries.clear()

    @property
    def entry_count(self) -> int:
        self._ensure_loaded()
        return len(self._entries)

    def get_coverage_stats(self) -> dict:
        """Return counts for debugging."""
        self._ensure_loaded()
        resolver_count = sum(1 for e in self._entries if e.source == "resolver")
        frontmatter_count = sum(1 for e in self._entries if e.source == "frontmatter")
        unique_skills = len({e.skill_path for e in self._entries})
        return {
            "total_entries": len(self._entries),
            "resolver_entries": resolver_count,
            "frontmatter_entries": frontmatter_count,
            "unique_skills": unique_skills,
        }


# ── Module-level singleton ─────────────────────────────────────────────

_resolver: Optional[SkillResolver] = None
_resolver_lock = threading.Lock()


def get_resolver() -> SkillResolver:
    """Get or create the module-level SkillResolver singleton."""
    global _resolver
    if _resolver is None:
        with _resolver_lock:
            if _resolver is None:
                _resolver = SkillResolver()
    return _resolver


def resolve_skills(user_message: str, max_matches: int = 5) -> str:
    """Convenience function: resolve and format in one call.

    Returns an empty string if no matches found.
    """
    resolver = get_resolver()
    matches = resolver.resolve(user_message, max_matches)
    if not matches:
        return ""
    return resolver.format_matches(matches)
