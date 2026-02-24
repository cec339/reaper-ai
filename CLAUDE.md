# REAPER AI - Claude Instructions

## Track Index Convention
REAPER uses 0-based track indices internally, but users typically refer to tracks using 1-based numbering. When a user says "track 31", they mean the 31st track which is **index 30** in the 0-based `get_context` output. Always subtract 1 from user-provided track numbers before looking them up in context output.

The `get_context` command prints tracks as `  30. shurevocaltest` — the number shown IS the 0-based index. So "track 31" from a user = index 30 in this listing.

## ReaEQ Parameter Names
ReaEQ parameter names follow the pattern `Freq-<BandName>`, `Gain-<BandName>`, `BW-<BandName>`. Band names are: `Low Shelf`, `Band 2`, `Band 3`, `High Shelf 4`, `High Pass 5`. They are NOT `Band 1 Freq`, `Band 1 Gain`, etc.

## IVGI2 Parameter Names
IVGI2 uses `Asymmetry` (not `Asym`), `FreqResponse` (not `Response`), and has no `Mix` param — use `Wet` (0-1) for parallel blend.

## apply_plan set_param Format
The `set_param` action in `apply_plan` supports two formats:
- **Array format** (preferred): `{"action":"set_param","fx_index":0,"params":[{"name":"Gain","value":0.5}]}`
- **Flat format**: `{"action":"set_param","fx_index":0,"param_name":"Gain","value":0.5}`

## CLI set-param Format
The CLI `set-param` command uses `name=value` pairs, NOT JSON:
```
reaper-ai set-param <track> <fx_index> "Param Name=0.5" "Other Param=0.3"
```
