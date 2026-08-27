"""Tests for a search that admits when it found nothing.

`search_partner` and `search_project` score every candidate with a difflib
ratio, sort descending, and return the top `limit`. With no floor, a query
matching nothing at all still comes back with three results carrying scores --
and an agent, handed titles it asked for, addresses one of them.

The failure is quiet and expensive in exactly the way this system tries to
avoid: nothing errors, a plausible answer is returned, and the work goes to a
Partner nobody meant to involve. An empty list is a real answer here.
"""

from __future__ import annotations

import pytest

from extension.base import StubExtension
from messaging_core.core import MessagingCore
from messaging_core.db import Database


@pytest.fixture
def db():
    database = Database(path=":memory:")
    yield database
    database.close()


@pytest.fixture
def core(db):
    return MessagingCore(db, extension=StubExtension(source_prefix="science_"))


@pytest.fixture
def world(core):
    """One project holding three plainly-named partners."""
    project_id = core.create_project(
        title="photosynthesis-study", source_prefix="science_", project_system_id="psid-1"
    )
    made = {}
    for title in ("research-worker", "the-analyst", "notebook-context"):
        made[title] = core.create_partner(
            project_id=project_id, title=title,
            partner_id_in_remote=f"rem-{title}", descr="d",
        )
    made["requester"] = core.create_partner(
        project_id=project_id, title="orchestrator",
        partner_id_in_remote="rem-orch", descr="d",
    )
    return made


def titles(hits: list[dict]) -> list[str]:
    return [h["title"] for h in hits]


def test_a_query_matching_nothing_returns_nothing(core, world):
    """Not three confident results with scores attached."""
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="zzzzzzzzzzzz"
    )

    assert hits == [], (
        f"a query sharing nothing with any title returned: {titles(hits)}"
    )


def test_an_exact_title_is_still_found(core, world):
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="the-analyst"
    )

    assert "the-analyst" in titles(hits)


def test_a_genuine_abbreviation_is_still_found(core, world):
    """The floor must not be so high that real searching stops working.

    Someone looking for `research-worker` types `research`, and a floor tuned
    only to reject nonsense would reject this too.
    """
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="research"
    )

    assert "research-worker" in titles(hits), (
        f"a plain prefix of a real title was dropped; got {titles(hits)}"
    )


def test_a_partial_word_is_still_found(core, world):
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="analyst"
    )

    assert "the-analyst" in titles(hits), (
        f"a substring of a real title was dropped; got {titles(hits)}"
    )


def test_an_unrelated_partner_is_not_padded_in_beside_a_real_match(core, world):
    """The point is not only the empty case. A query with ONE good match must
    not be topped up to `limit` with rows that matched nothing."""
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="notebook-context"
    )

    assert "notebook-context" in titles(hits)
    assert "the-analyst" not in titles(hits), (
        f"an unrelated partner was padded in beside the real match: {titles(hits)}"
    )


@pytest.mark.parametrize("query,expected", [
    ("research", "research-worker"),
    ("worker", "research-worker"),
    ("analyst", "the-analyst"),
    ("notebook", "notebook-context"),
    ("res", "research-worker"),
])
def test_a_deliberate_substring_of_a_title_is_always_relevant(core, world, query, expected):
    """A fuzzy ratio alone cannot decide this, and the numbers say so.

    difflib penalises length difference, so a short exact query against a long
    title scores low however exact it is: `worker` against `research-worker` is
    0.571, `res` is 0.333. Lowering the floor to keep them does not work either
    -- `xylophone`, which shares nothing with any of these titles, scores 0.345,
    above `res` and level with `orch`. The two are not separable by ratio.

    What separates them is that every real query here is a substring of its
    target and no nonsense query is a substring of anything.
    """
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title=query
    )

    assert expected in titles(hits), (
        f"{query!r} is a literal substring of {expected!r} and must be relevant; "
        f"got {titles(hits)}"
    )


def test_a_word_that_merely_scores_well_is_not_relevant(core, world):
    """The negative half of the case above, and the reason for the substring rule.

    `xylophone` out-scores several legitimate queries on ratio alone. It is a
    substring of nothing, and must not come back.
    """
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="xylophone"
    )

    assert hits == [], (
        f"a word sharing nothing with any title came back anyway: {titles(hits)}"
    )


def test_a_typo_is_still_tolerated(core, world):
    """The fuzzy floor still has a job: catching a misspelling, which is not a
    substring of anything."""
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="reserch-worker"
    )

    assert "research-worker" in titles(hits), (
        f"a plain misspelling was dropped; got {titles(hits)}"
    )


def test_a_query_too_short_to_mean_anything_is_not_a_substring_match(core, world):
    """`a` is a substring of almost every title. Treating that as a deliberate
    search would return everything and mean nothing."""
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="a"
    )

    assert len(hits) <= 1, (
        f"a one-character query matched broadly by substring: {titles(hits)}"
    )


def test_project_search_has_the_same_floor(core, world):
    assert core.search_project(
        requester_uuid=world["requester"]["uuid"], query_title="zzzzzzzzzzzz"
    ) == []
    assert "photosynthesis-study" in titles(core.search_project(
        requester_uuid=world["requester"]["uuid"], query_title="photosynthesis"
    ))


def test_every_returned_hit_carries_its_score(core, world):
    """Whatever survives the floor still has to say how well it matched."""
    hits = core.search_partner(
        requester_uuid=world["requester"]["uuid"], query_title="research"
    )

    assert hits
    for hit in hits:
        assert "score" in hit and isinstance(hit["score"], float)
