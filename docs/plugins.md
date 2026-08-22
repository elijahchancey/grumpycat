# Writing a plugin

The contract is `src/grumpycat/plugins/spec.py`. The registry (`registry.py`) enforces it at
startup in every Lambda and in the worker:

1. every plugin named in `grumpycat.yaml` must resolve to an entry point;
2. its config section must validate against `spec.config_schema`;
3. every `required_secrets` name must be in the secrets map;
4. missing `optional_tools` only log a warning;
5. `spec.api_version` must equal `grumpycat.PLUGIN_API_VERSION`.

See `CONTRIBUTING.md` → *Writing a plugin* for the three kinds, registration, the three
distribution tiers and how to test one. `tests/fakes.py` is a complete minimal example of all
three kinds.
