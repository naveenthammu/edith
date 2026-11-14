from skills import REGISTRY, skill, tool_schemas


def test_skill_builds_schema_from_hints_and_docstring():
    @skill()
    def demo_weather(city: str, days: int = 1) -> str:
        """Get the weather for a city."""
        return city

    entry = REGISTRY["demo_weather"]
    fn = entry.schema["function"]
    assert entry.schema["type"] == "function"
    assert fn["name"] == "demo_weather"
    assert fn["description"] == "Get the weather for a city."
    assert fn["parameters"] == {
        "type": "object",
        "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["city"],
    }
    assert entry.confirm is False
    assert entry.func("Pune") == "Pune"
    assert entry.schema in tool_schemas()


def test_skill_stores_confirm_flag():
    @skill(confirm=True)
    def demo_shutdown() -> str:
        """Shut down."""
        return "ok"

    assert REGISTRY["demo_shutdown"].confirm is True
    assert REGISTRY["demo_shutdown"].schema["function"]["parameters"]["properties"] == {}
