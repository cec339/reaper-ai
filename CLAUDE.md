# REAPER AI - Claude Instructions

## Track Index Convention
REAPER uses 0-based track indices internally, but users typically refer to tracks using 1-based numbering. When a user says "track 31", they mean the 31st track which is **index 30** in the 0-based `get_context` output. Always subtract 1 from user-provided track numbers before looking them up in context output.

The `get_context` command prints tracks as `  30. shurevocaltest` — the number shown IS the 0-based index. So "track 31" from a user = index 30 in this listing.

## ReaEQ Parameter Names
ReaEQ parameter names follow the pattern `Freq-<BandName>`, `Gain-<BandName>`, `BW-<BandName>`. Band names are: `Low Shelf`, `Band 2`, `Band 3`, `High Shelf 4`, `High Pass 5`. They are NOT `Band 1 Freq`, `Band 1 Gain`, etc.

## ReaEQ High Pass Band 5 Gotcha
When using the High Pass 5 band, **only set the frequency**. Always leave `Gain-High Pass 5` at 0.5 (0.0 dB). The gain param does not control slope — setting it to any other value adds an unwanted boost or cut. The frequency alone controls the HPF cutoff.

## ReaEQ Band Enable/Disable
ReaEQ band enable state is NOT exposed as a standard param. Use the `enable_reaeq_band` daemon command:
```
send_command('enable_reaeq_band', track='...', fx_index=0, band=4, enabled=True)
```
Band indices: 0=Low Shelf, 1=Band 2, 2=Band 3, 3=High Shelf 4, 4=High Pass 5.
Internally uses `TrackFX_SetNamedConfigParm(tr, fx, "BANDENABLED<N>", "1"/"0")`. Note: REAPER returns `false` from this call even though it works — ignore the return value.

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

## Creating Bus/Folder Tracks
`set_track_folder depth=1` converts an EXISTING track into a folder parent — its audio content becomes the bus, not a child. To create a proper empty bus above existing tracks:
1. `create_track` with the bus name
2. `reorder_track` to move the new track above the intended children
3. `set_track_folder depth=1` on the new bus track
4. `set_track_folder depth=-1` on the last child track

Never use `set_track_folder depth=1` on a track that has audio you want to keep as a child.

## Daemon Sync (Dev Workflow)
When `reaper_daemon.lua` is edited in this repo, sync it to REAPER's live Scripts folder before asking for a restart:
```
powershell -ExecutionPolicy Bypass -File .\sync-daemon.ps1
```
Then ask the user to re-run `reaper_daemon.lua` in REAPER Actions.
