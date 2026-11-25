import pytest
import gameplay.autochess_bench as mod

CASES = [
    ("per_player_rng", (1,)),
    ("merge_atomic_move", (1,)),
    ("board_snapshot_barrier", (1,)),
    ("server_round_clock_ac", (1,)),
    ("synergy_recount_idempotent", (1,)),
    ("population_cap_check", (1,)),
    ("load_lobby", (1,)),
    ("roll_shop", (1,)),
    ("resolve_combat", (1,)),
    ("present_board_ac", (1,)),
    ("validate_board_ac", (1,)),
    ("metrics_board_ac", (1,)),
    ("audit_board_ac", (1,)),
    ("fallback_board_ac", (1,)),
    ("determinism_board_ac", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
