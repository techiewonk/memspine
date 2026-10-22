"""Weight connectors (RM-113): load, offline policy and process sharing of local model weights.

Each ``<lib>.py`` owns one library's load (``local_files_only`` enforcement and any
``snapshot_download``), the ADR-079 shared lease and close; adapters receive the connector.
"""
