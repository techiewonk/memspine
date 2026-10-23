# Provider plugin example

The installed-wheel fixture at
[`tests/fixtures/provider_plugin`](../tests/fixtures/provider_plugin) shows the
provider entry-point contract with a small deterministic embedder. It performs
no model download or remote call.

## Package and descriptor

The plugin distribution declares an entry point in the category group and
exports a `ProviderDescriptor`:

```toml
[project.entry-points."memspine.embedders"]
fixture_hash = "memspine_provider_plugin_fixture:DESCRIPTOR"
```

The entry-point name is the plugin name in the manifest. The descriptor's
`ProviderRef` category and name must match the category and provider slot, and
provider contract version 1 is required. Its settings model should inherit
`StrictSettings` so unknown provider settings fail validation. The builder
returns a `BuildResult` containing the service, the same `ProviderRef`, declared
capabilities and resource references.

Register every resource closer through the supplied lifecycle manager before
opening or using the resource. The Engine owns those registrations and invokes
them during shutdown. The fixture verifies that its encoder closer runs exactly
once.

Projection providers must expose a no-load `target_identity` where preflight
needs it. In particular, an embedding descriptor used by the vector store must
return `embedder_id` and `dimension`; this lets startup validate the vector table
identity before constructing the encoder.

## Pin and enable

Discovery reads the `memspine.<kind>` entry-point group and does not import or
enable the plugin. Enablement comes from an explicit config manifest, for
example:

```yaml
plugins:
  - name: fixture_hash
    version: "1.2.3" # exact installed distribution version; ==1.2.3 is also accepted
    hash: sha256:<64-lowercase-hex-digits> # optional RECORD hash
    slot: embedders:fixture_hash
    lifetime: app
model:
  embedding:
    provider: fixture_hash
    settings:
      dim: 64
```

The optional hash is the SHA-256 of the installed distribution's `RECORD`
text, prefixed by `sha256:`. The plugin must be installed in the same Python
environment as memspine. A wrong version or hash fails before plugin code is
imported. The slot cannot shadow a built-in provider or another enabled plugin.

## Local evidence

The fixture was built as a wheel and installed beside the memspine wheel into a
fresh CPython 3.13.5 Windows environment. A real entry point was discovered,
then enabled through `Engine(user_config=...)` with an exact version and RECORD
hash pin. Engine write and search used the plugin embedder; shutdown invoked its
owned cleanup. Wrong version and hash pins were rejected before import. A second
entry point declaring contract version 2 was rejected with `PluginContractError`
without invoking its builder. An unknown strict setting was rejected. See the
[run record](provider_verification/plugin-wheel-checks.json)
and [durable log](provider_verification/plugin-wheel-checks.log) for exact
versions, commands, hashes and limits. This fixture does not claim compatibility
with every entry-point kind or platform.
