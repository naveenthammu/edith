from skills import skill

# main.py sets this to the running brain.memory.Memory.
memory = None


@skill()
def remember(fact: str) -> str:
    """Remember a lasting fact about the user, e.g. 'My sister is Priya' or 'I prefer Celsius'."""
    if memory is None:
        return "error: memory not available"
    return "I'll remember that." if memory.add(fact) else "I already knew that."


@skill()
def forget(topic: str) -> str:
    """Forget every remembered fact that mentions a word or phrase."""
    if memory is None:
        return "error: memory not available"
    n = memory.forget(topic)
    return f"Forgot {n} thing{'s' if n != 1 else ''}." if n else "I had nothing about that."
