strict tier. Each persisted kind carries x-kind and x-schema-version; a change to a schema needs
a schema_version bump (and an upcaster). Value types embedded in kinds (no x-schema-version) are
checked through the kinds that embed them.
