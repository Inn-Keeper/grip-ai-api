from app.poe_prompt import POE_PROMPT


def test_prompt_carries_guide_and_rules():
    for area in ["Quest", "Interview Prep", "Arch Board", "Story Bank", "Profile"]:
        assert area in POE_PROMPT
    for rule in [
        "only about using Grip",
        "PROJECT_DATA.locale",
        "plain text",
        "doesn't do that yet",
        "personal details",
    ]:
        assert rule in POE_PROMPT
    assert "—" not in POE_PROMPT and "–" not in POE_PROMPT
