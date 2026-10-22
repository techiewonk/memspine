"""Model metadata and planning: named profiles, embedding profiles, tiers and role resolution.

Pure configuration data and planning; no adapter, weight or SDK lives here (RM-113 moved
the ports and adapters to ``memspine.services`` and weight loading to ``clients.weights``).
``encoders/query`` is the rule-based query analysis (no model).
"""
