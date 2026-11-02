# Third-party source

This offline edition is part of SIWX and distributed under the repository's
AGPL-3.0 license. The following files originate from `abxian/wexport1`, commit
`eb5e9355dc556c87e502433fc7f0399b1ff02a93`, under MIT:

- `siwx_safe/vendor/wexport/model.py`
- `siwx_safe/vendor/wexport/parse/content.py`
- `siwx_safe/vendor/wexport/parse/__init__.py`

The MIT copyright and license are retained in `siwx_safe/vendor/wexport/LICENSE`
and bundled as `licenses/wexport/LICENSE`. Source files are unchanged. Calls to
the parser always pass `media_root=None`; media paths and download URLs from
imported data are never resolved or fetched. No other wexport modules are copied.

SIWX compatibility is implemented by normalizing its `session` / `messages` JSON
export, including `createTime`, `senderDisplayName`, `typeName`, and `content`.
The legacy SIWX runtime is not imported or bundled.

## Live edition (task337)

The separate `live_app.py` / `live.spec` build additionally uses unchanged MIT
wexport modules from the same source commit: `errors.py`, `crypto/{__init__,aes,
sqlcipher,zstd}.py`, `sources/{base,win4}.py`, and
`win/{__init__,memory,processes,wcdb_keys}.py`. The local `sources/__init__.py`
only documents this selected subset. No extraction algorithm was modified.
The adapter restricts the original extraction call to current-user WeChat PIDs.
Read-only process access is intentionally retained and may trigger antivirus.
No other Windows scanning, image-key, CLI, config, background, or upstream Web
modules are bundled. The JSON-only offline spec rejects live modules.

## Retained SNS and original interface (task348)

The live edition additionally includes the original SIWX SNS XML parser subset,
CDN reconstruction/ISAAC64/AES media logic in `siwx_safe/vendor/sns_{parser,cdn,isaac64}.py`,
under this repository's AGPL-3.0 license. Logger/disk-cache and local image-guessing
paths are not enabled. The adapter uses the opened user's existing in-memory SNS
connection, strict bounded media transport, and opaque browser resource references.
The original Vue3.5.13 MIT copyright/license header is retained in `live_ui/vue.js`.
Original SNS Vue templates are compiled before packaging; no runtime eval needed.
Original widgets and page styles are retained locally. Legal attribution stays in
this file and LICENSE; promotional/source links are removed from product pages.
