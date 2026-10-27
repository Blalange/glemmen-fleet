# Glemmen inventory VS Code client

Thin VS Code client for `inventory/scripts/inventory_lsp.py`.

The server provides:

- semantic diagnostics for the manual and generated inventory layers
- rack layout validation, including incomplete and overlapping layouts
- unknown reference warnings
- Ctrl-click/go-to-definition for slugs, device names, VLANs, contacts and
  module types
- hover details
- key and value completion
- workspace-wide rename of definitions and their references

## Install

The server defaults to the repository's `uv` workflow. Make sure `uv` is
available, then build and install the extension:

```sh
cd editors/vscode/inventory-lsp
npm install
npm run package
code --install-extension glemmen-inventory-0.1.0.vsix
```

Reload VS Code after installing. Open a YAML file under `inventory/manual/` or
`inventory/generated/`; the extension starts the server in the workspace root.

For development, open this folder in VS Code and press `F5` to launch an
Extension Development Host, then open the repository in that window.

## Configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `glemmenInventory.server.command` | `uv` | Server command |
| `glemmenInventory.server.args` | `uv run --with pygls... python inventory/scripts/inventory_lsp.py` | Server arguments |
| `glemmenInventory.server.cwd` | workspace root | Server working directory |
| `glemmenInventory.inventory` | `<workspace>/inventory` | Optional inventory directory |

If you prefer a local virtual environment, set `server.command` to the Python
executable and replace `server.args` with:

```json
["inventory/scripts/inventory_lsp.py", "--inventory", "inventory"]
```

The extension does not install or use a JSON Schema. Field and value
completion comes from the language server's Python field catalog.
