# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalise Discord unicode emojis to the form Fluxer renders.

Discord stores built-in emojis as their bare unicode surrogate (e.g. ``🗣``,
U+1F5E3). Fluxer's markdown parser and emoji table key on the *qualified*
surrogate that carries the U+FE0F variation selector (``🗣️``); a bare surrogate
that Fluxer expects qualified falls through as plain, uncoloured text. The same
canonical surrogate is also what Fluxer's reaction picker stores, so using it
keeps migrated reactions in the same group as any added live later.

This module reads Fluxer's own emoji table
(``fluxer_app/src/media/data/emojis.json``) and rewrites emojis — both in
message content and in reactions — to the exact ``surrogates`` string Fluxer
uses, so migrated text is indistinguishable from natively-entered text.
"""

import json
import os
import re

# U+FE0F (variation selector-16). Fluxer's toCanonicalSurrogate strips it to
# compare emojis; we strip it to build the lookup key.
_VARIATION_SELECTOR = "️"


def _canonical(surrogate):
    return surrogate.replace(_VARIATION_SELECTOR, "")


def default_emoji_data_path(schema_path):
    """Best-effort location of Fluxer's emojis.json relative to the schema file.

    schema_path points at ``<fluxer>/tools/dev/cassandra_target_schema.json``;
    the emoji data lives at ``<fluxer>/fluxer_app/src/media/data/emojis.json``.
    """
    if not schema_path:
        return ""
    fluxer_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(schema_path))))
    return os.path.join(fluxer_root, "fluxer_app", "src", "media", "data", "emojis.json")


class EmojiNormalizer:
    """Rewrites unicode emojis to Fluxer's canonical (FE0F-qualified) surrogate."""

    def __init__(self, canonical_to_full=None):
        # canonical (FE0F-stripped) surrogate -> Fluxer's exact surrogate string
        self._map = canonical_to_full or {}
        # Regex matching any known emoji form (qualified or bare), longest first
        # so multi-codepoint/ZWJ sequences win over their components.
        self._re = None
        if self._map:
            forms = set()
            for canon, full in self._map.items():
                forms.add(full)
                forms.add(canon)
            alternation = sorted(forms, key=len, reverse=True)
            self._re = re.compile("|".join(re.escape(f) for f in alternation))

    def __bool__(self):
        return bool(self._map)

    @classmethod
    def load(cls, path):
        """Build the table from a Fluxer emojis.json. Returns an empty (no-op)
        normaliser if the file is missing or unreadable."""
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()

        mapping = {}
        for emoji in _iter_emojis(data.get("categories")):
            surrogates = emoji.get("surrogates")
            if not surrogates:
                continue
            mapping.setdefault(_canonical(surrogates), surrogates)
        return cls(mapping)

    def surrogate(self, emoji):
        """Return Fluxer's canonical surrogate for a single emoji, else None.

        Used for reactions, where the whole field is one emoji.
        """
        if not emoji:
            return None
        return self._map.get(_canonical(emoji))

    def qualify(self, text):
        """Rewrite every known emoji occurrence in `text` to Fluxer's canonical
        surrogate (adding FE0F where Fluxer expects it). Unknown text is left
        untouched. Used for message content."""
        if not text or not self._re:
            return text
        return self._re.sub(lambda m: self._map.get(_canonical(m.group(0)), m.group(0)), text)


def _iter_emojis(categories):
    if isinstance(categories, dict):
        for value in categories.values():
            yield from _iter_emojis(value)
    elif isinstance(categories, list):
        for entry in categories:
            if isinstance(entry, dict):
                yield entry
