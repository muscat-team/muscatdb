`prose_340_telescopes.yaml` is the unmodified production telescope snapshot used
with `prose-prod-3daed56b` (prose2 3.4.0), copied from production checkout
`adc94ba67b1eae6447cc6bf103906e4792f67b9f` on 2026-10-29.

SHA-256: `39503fce597e2d5fd349fa8009ed5068222fc6e31f098cf50e62f2a17f314a08`.
Its embedded `engine_version: 3.3.4` describes the original capture, not the
installed production interpreter. Keep this fixture fixed when advancing the
candidate pin: the slow migration check compares production 3.4.0 plus this
configuration against the new engine plus its packaged configuration.
