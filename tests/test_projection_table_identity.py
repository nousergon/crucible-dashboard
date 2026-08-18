"""Regression guard: the projection->table registry must not be keyed on
an OBJECT ADDRESS.

WHY THIS FILE EXISTS
---------------------
``loaders/db_schema._PROJECTION_TABLE`` used to be ``{id(projection): table}``
(alpha-engine-config-I7643 — same class as the closes-panel cache defect fixed
in crucible-research PR657: ``scoring/leaderboard_producers._PANEL_CACHE`` was
keyed on ``id(loader)``).

``id()`` is unique only among objects alive AT THE SAME MOMENT. CPython reuses
the address of a freed object, so a projection tuple constructed after an
earlier registered one was collected can receive that earlier one's id — and
if it is then looked up (or a NEW projection is registered under that
recycled address), the registry silently answers for the wrong projection, or
a live registration is silently clobbered.

Every projection registered in this module today is a permanent module-level
global also held forever by the ``PROJECTIONS`` dict, so this was not observed
exploitable in production — the addresses in ``_PROJECTION_TABLE`` never free.
But ``_register``/``projection_table``/``join`` are a general-purpose registry
API, not restricted to permanent globals, so the hazard is structural. The fix
keys the dict on the projection tuple itself: tuples of strings are hashable
and compare by VALUE, so this removes the address dependency entirely rather
than merely pinning it alive (contrast the loader-object fix in PR657, which
needed to hold a reference specifically because a callable has no natural
value equality).
"""

from __future__ import annotations

import gc

import pytest

from loaders import db_schema


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(db_schema._PROJECTION_TABLE)
    yield
    db_schema._PROJECTION_TABLE.clear()
    db_schema._PROJECTION_TABLE.update(saved)


def test_the_registry_key_is_the_projection_value_not_its_address():
    """The structural invariant, stated so it cannot be regressed silently."""
    proj = ("col_x", "col_y")
    db_schema._register(proj, "some_table")

    assert proj in db_schema._PROJECTION_TABLE, (
        "the registry must be queryable by the projection's VALUE"
    )
    assert not any(isinstance(k, int) for k in db_schema._PROJECTION_TABLE), (
        "the registry still carries an int (id()) key; CPython reuses freed "
        "addresses, so this can silently serve or clobber the wrong table"
    )


def test_a_value_equal_but_distinct_projection_object_resolves_correctly():
    """Two different tuple OBJECTS with the same contents must both resolve —
    proves the lookup is value-based, not identity-based."""
    original = ("ticker", "source", "sector")
    db_schema._register(original, "team_inputs")

    # A freshly built tuple, same values, guaranteed distinct object (a
    # freshly allocated tuple is never the *same* object as one already
    # referenced elsewhere).
    rebuilt = tuple(["ticker", "source", "sector"])
    assert rebuilt is not original

    assert db_schema.projection_table(rebuilt) == "team_inputs"


def test_a_freed_registration_does_not_leak_its_table_onto_a_recycled_address():
    """Reproduces the exact pre-fix hazard: register A, drop every reference
    to A, force its address to be recycled by a later, DIFFERENT-VALUED
    registration B — the registry must still answer B's own table, not A's.

    Under the OLD ``id(projection)`` key this is exactly how one registration
    silently clobbered another: ``_PROJECTION_TABLE[id(a)] = "table_a"``, then
    once ``a`` was freed and a distinct ``b`` happened to land on the same
    address, ``_PROJECTION_TABLE[id(b)] = "table_b"`` overwrote table_a's slot
    — and any code still holding a reference to an *equal-valued* copy of `a`
    would have read `b`'s table back.
    """
    proj_a = tuple(f"col_a_{i}" for i in range(5))
    db_schema._register(proj_a, "table_a")
    address = id(proj_a)
    del proj_a
    gc.collect()

    hit = None
    for i in range(2000):
        cand = tuple(f"col_b_{i}_{j}" for j in range(5))
        if id(cand) == address:
            hit = cand
            break
    if hit is None:
        pytest.skip("no address recycled in 2000 attempts on this interpreter")

    db_schema._register(hit, "table_b")

    # The value that used to be proj_a's contents (rebuilt independently) must
    # NOT resolve to table_b just because an unrelated tuple once shared its
    # freed address.
    rebuilt_a = tuple(f"col_a_{i}" for i in range(5))
    assert db_schema.projection_table(rebuilt_a) is None, (
        "an unregistered projection resolved to a table via a stale, "
        "address-based key"
    )
    assert db_schema.projection_table(hit) == "table_b"


def test_the_id_reuse_this_guards_against_is_real_on_this_interpreter():
    """A guard whose premise nobody has checked is not a guard."""
    hits = 0
    for _ in range(20):
        first = tuple(["scratch"])
        address = id(first)
        del first
        gc.collect()
        second = tuple(["scratch"])
        hits += id(second) == address
        del second
    assert hits > 0, (
        "no address was recycled in 20 attempts — if this interpreter no "
        "longer recycles, re-derive whether the identity key is still a "
        "hazard before relaxing anything in this file"
    )
