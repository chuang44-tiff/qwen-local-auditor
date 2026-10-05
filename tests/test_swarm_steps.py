from lib import swarm
from lib.swarm_engine import steps


def test_vote_slots_are_claim_major():
    assert steps.vote_slots(["a", "b"], 2) == [("a", 0), ("a", 1), ("b", 0), ("b", 1)]
    assert steps.vote_slots([], 3) == []
    for batch in swarm.deal(steps.vote_slots(list(range(7)), 3), 3):
        ids = [c for c, _ in batch]
        assert len(ids) == len(set(ids))               # no agent votes twice on one claim


def test_steps_reexport_the_swarm_primitives():
    assert steps.extract_json is swarm.extract_json and steps.tally is swarm.tally


def test_public_text_helpers():
    assert steps.clip("  a \n b  ") == "a b" and steps.clip("x" * 400) == "x" * 300
    assert steps.as_text(5) == "" and steps.as_text("s") == "s"
    assert steps.clean_url(" https://a.example/x ") == "https://a.example/x"
    assert steps.clean_url("https://a.example/ x") == "" and steps.clean_url(3) == ""
    assert steps.score("4/5") == 4 and steps.score("high") == 4 and steps.score(True) is None
    assert steps.slug("What is X, really?") == "what-is-x-really" and steps.slug("!!") == "question"
