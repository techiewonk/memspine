"""Customizable prompt subsystem (D-43): prompts are data, never inline strings.

Every internal LLM call resolves a named, versioned prompt from the shipped
YAML default pack, overridable through the ordinary config layering (D-11).
"""
