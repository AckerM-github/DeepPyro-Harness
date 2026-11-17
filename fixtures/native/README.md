# Native replay fixtures (M0 baseline)

This directory is a data-only capture of real DeepSeek provider traffic for the native schema 1 work. Do not edit the recordings: their sha256 and byte counts are frozen in `manifest.json` and validated by `validation.json`.

- Provenance, case matrix, format, validation and limits: [../../docs/native/README.md](../../docs/native/README.md)
- Contract: [../../docs/native/schema.md](../../docs/native/schema.md)
- Decision record: [../../docs/adr/0001-three-track-native-agent.md](../../docs/adr/0001-three-track-native-agent.md)

Contents: `recordings/01..10.jsonl`, `data/hello.txt`, `data/notes.txt`, `manifest.json`, `validation.json`.

Key facts: captured 2026-11-17, provider `deepseek`, model `deepseek-flash`, `temperature=0`, `thinking: disabled`, 10 cases / 17 requests / 7 tool calls / 298,506 bytes, 18/18 required validation checks passed. No API key, `Authorization` header or `sk-` string is present.
