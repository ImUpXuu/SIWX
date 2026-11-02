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
