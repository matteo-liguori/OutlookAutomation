# Connecting an agent

Chamberlain gives an agent twelve tools over MCP, or the same tools from a
command line. Both go through the same checks, so use whichever your agent
supports. Your data root is the folder that holds your private mail and
calendar data; it stays outside Git.

## MCP (Claude Code, Codex, and other MCP clients)

The server runs as a local process and talks over stdio. Point it at your data
root with `--data` or the `CHAMBERLAIN_DATA` environment variable. Use the
Python from this project's virtual environment.

Let the command write the entry:

```text
chamberlain --data <data-root> setup agent codex [--apply]
chamberlain --data <data-root> setup agent claude --project <folder> [--apply]
chamberlain --data <data-root> setup agent claude          prints the `claude mcp add` command
```

Without `--apply` it only shows what it would write. With it, the file is saved as
`<name>.bak-<time>` first, only the `chamberlain` entry is replaced (in `~/.codex/config.toml`
or `<folder>/.mcp.json`), and the server is started and asked to `initialize` to check it
answers. The entry uses this checkout's Python and your `--data` root. To do it by hand:

Claude Code, in a project `.mcp.json` (or `claude mcp add`):

```json
{
  "mcpServers": {
    "chamberlain": {
      "command": "C:/dev/chamberlain/.venv/Scripts/python.exe",
      "args": ["-m", "chamberlain.surface.mcp_server", "--data", "C:/path/to/data-root"]
    }
  }
}
```

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.chamberlain]
command = "C:/dev/chamberlain/.venv/Scripts/python.exe"
args = ["-m", "chamberlain.surface.mcp_server", "--data", "C:/path/to/data-root"]
```

On Mac, use `.venv/bin/python` and Mac paths. Other MCP clients take the same
command and arguments. The server hands the agent the standing instructions at
connect time, so the agent needs nothing else to start.

### Staying current

The server checks, between requests, whether the Chamberlain code on disk has changed since it
started. If it has, it starts a fresh server on the same connection and the new code answers the
call, so an update needs no restart and no new `initialize`. If the new code cannot be loaded,
the call fails with a message to restart the MCP server (or the Codex app).

## Command line

For an agent with a shell and no MCP:

```
chamberlain --data <data-root> tools            # list the tools
chamberlain schema <tool>                       # what a tool accepts
chamberlain --data <data-root> <tool> --json '{...}'
chamberlain prompt                              # the standing instructions
```

Every call prints one JSON reply. The exit code is 0 when it worked, 2 for a
bad request, 3 when the answer is uncertain, and 1 otherwise.

## Showing the viewer

Run `chamberlain viewer`. Under Claude Code it prints a local address instead of opening the
system browser: open that address in Claude's built-in browser pane (the viewer runs until
stopped), or give it to the user if you have no browser of your own. Only the user presses
confirm and approve buttons in the viewer.

## Keeping Claude Code out of the data root

`chamberlain guard on` adds deny rules so Claude Code's file tools cannot read or edit the data
root; `chamberlain guard off` removes them. Off by default while the harness is developed. It does not stop a shell, and Codex has no equivalent.
See [review.md](review.md).

## Checking it works

Run `chamberlain --data <data-root> config validate`, then ask the agent to
call `doctor`. Sending mail and calendar writes still need the permissions you
have set; connecting an agent does not widen them.
