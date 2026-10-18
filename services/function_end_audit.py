"""Read-only audit of open failure outputs ("open ends") in functions.

A failed / not_found output without a wire ends the run as failed. It may be a
deliberate exit or a forgotten route; the editor flags it so the user can wire
it explicitly to an End block. The runtime behaviour is never changed here.
"""

from services.function_block_catalog import (
    BLOCK_CALL_FUNCTION,
    BLOCK_CATALOG,
    BLOCK_FAILURE,
    BLOCK_SPLITTER,
    OUTCOME_FAILED,
    OUTCOME_NOT_FOUND,
)

OPEN_END_OUTCOMES = (OUTCOME_FAILED, OUTCOME_NOT_FOUND)


def block_outcomes(block, game_id="", function_service=None, helper_cache=None) -> list:
    """Outcomes a block exposes, as drawn by the editor."""

    if block.block_type == BLOCK_SPLITTER:
        count = max(2, min(4, int(block.params.get("outputs", 2))))
        return [f"out_{index + 1}" for index in range(count)]
    outcomes = list(BLOCK_CATALOG.get(block.block_type, {}).get("outcomes", []))
    if block.block_type == BLOCK_CALL_FUNCTION and function_service and game_id:
        helper_id = block.params.get("function_id")
        if helper_id:
            cache = helper_cache if helper_cache is not None else {}
            if helper_id not in cache:
                cache[helper_id] = function_service.load(game_id, helper_id)
            helper = cache[helper_id]
            if helper and helper.interface.outcomes:
                outcomes = list(helper.interface.outcomes)
    return outcomes


def _wired(function) -> set:
    wired = {
        (connection.source_block_id, connection.source_outcome)
        for connection in function.connections
    }
    for block in function.walk_blocks():
        for outcome, targets in (block.branches or {}).items():
            if targets:
                wired.add((block.block_id, outcome))
    return wired


def open_failure_ends(function, function_service=None, helper_cache=None) -> list:
    """List unwired failed / not_found outputs of one function."""

    if function is None:
        return []
    wired = _wired(function)
    result = []
    for block in function.walk_blocks():
        # the failure terminal itself is the explicit, deliberate end
        if block.block_type == BLOCK_FAILURE:
            continue
        for outcome in block_outcomes(block, function.game_id, function_service, helper_cache):
            if outcome in OPEN_END_OUTCOMES and (block.block_id, outcome) not in wired:
                result.append({
                    "block_id": block.block_id,
                    "block_name": block.name or block.block_type,
                    "block_type": block.block_type,
                    "outcome": outcome,
                })
    return result


def audit_game(function_service, game_id) -> list:
    """Open ends per function of a game, worst first (helpers included)."""

    helper_cache = {}
    rows = []
    for function in function_service.list_functions(game_id):
        helper_cache.setdefault(function.function_id, function)
        ends = open_failure_ends(function, function_service, helper_cache)
        rows.append({
            "function_id": function.function_id,
            "name": function.name,
            "kind": function.kind,
            "open_ends": len(ends),
            "blocks": len(list(function.walk_blocks())),
            "details": ends,
        })
    rows.sort(key=lambda row: (-row["open_ends"], str(row["name"]).casefold()))
    return rows
