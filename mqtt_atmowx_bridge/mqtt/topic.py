"""MQTT topic filter matching, per MQTT 3.1.1 §4.7."""

from __future__ import annotations


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Whether ``topic`` matches ``topic_filter``.

    ``+`` matches exactly one level and ``#`` matches the rest of the topic
    (including none).
    """
    if topic_filter == topic:
        return True

    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")

    for index, level in enumerate(filter_levels):
        if level == "#":
            # `sport/#` matches `sport`, but no wildcard matches a `$SYS` topic.
            return index != 0 or not topic_levels[0].startswith("$")
        if index >= len(topic_levels):
            return False
        if level == "+":
            if index == 0 and topic_levels[0].startswith("$"):
                return False
            continue
        if level != topic_levels[index]:
            return False

    return len(filter_levels) == len(topic_levels)
