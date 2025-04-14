**Discord-AIBot**

Discord bot for supporting AI/LLM chat applications powered by the Model Context Protocol (MCP), allowing for numerous integrations

# Configuration

```sh
cp example.config.toml myconfig.toml
```

Then edit `myconfig.toml` as needed. It specifies your LLM endpoint and MCP servers.

# Running

```sh
python mcp_discord_bot.py --discord-token $DISCORD_TOKEN --config-path myconfig.toml
```

Structlog/rich tracebacks can be elaborate, so there is a `--classic-tracebacks` option to tame them

Note: you can use the environment rather than `--discord-token` & `--config-path`

```sh
export MCP_DISCORD_DISCORD_TOKEN="YOUR_TOKEN"
export MCP_DISCORD_CONFIG_PATH="./config.toml"
python mcp_discord_bot.py # Reads from env vars
```

There's an `MCP_DEBUG=1` variable from upstream, but I'm not entirely sure what it shows.

# Checking MCP servers

For SSE servers, you can check with curl, e.g.

```sh
curl -N http://localhost:8901/sse
```


# MCP resources

# General resources

* [MCP Reddit](https://www.reddit.com/r/mcp/)

# Issues

See Uche's post on Reddit: https://www.reddit.com/r/mcp/comments/1jrbrfm/musings_on_mcps_architectural_problems_and_the/

# Servers

* [ACI.dev by Aipolabs](https://github.com/aipotheosis-labs/aipolabs-mcp)—basically a meta-MCP server. "Unified MCP server that can access unlimited tools from one MCP server"

* ["10 awesome MCP servers to supercharge your workflows on…" (Twitter thread)](https://x.com/Saboo_Shubham_/status/1905455781761483093)
* [awesome-mcp-servers (github)](https://github.com/punkpeye/awesome-mcp-servers?tab=readme-ov-file)

# Clients

* [awesome-mcp-clients (github)](https://github.com/punkpeye/awesome-mcp-clients?tab=readme-ov-file)

