"""Connection clients for every external system (D-24).

Clients own connect/close/health and transport settings; services receive a
connected client from the lifecycle manager and never open connections.
"""
