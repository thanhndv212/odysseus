# Odysseus OpenCode Integration

This directory contains the OpenCode skill and plugin bundle for Odysseus.

## User Flow

1. Open Odysseus Settings > Integrations.
2. Add an OpenCode Agent.
3. Copy the full setup commands shown after the generated token.
4. Toggle the tools OpenCode is allowed to use.
5. Configure the OpenCode session:

### Option A: Skill-only install (recommended for most users)

```bash
export ODYSSEUS_URL=http://your-odysseus-host:7000
export ODYSSEUS_API_TOKEN=ody_generated_token
mkdir -p ~/.config/opencode/skills
curl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/opencode/plugin.zip" -o /tmp/odysseus-opencode-skill.zip
python3 -m zipfile -e /tmp/odysseus-opencode-skill.zip /tmp/odysseus-opencode-skill
cp -r /tmp/odysseus-opencode-skill/skills/odysseus ~/.config/opencode/skills/
```

OpenCode auto-loads skills from `~/.config/opencode/skills/`, so the `odysseus`
skill is available in any session that has `ODYSSEUS_URL` and
`ODYSSEUS_API_TOKEN` in its environment.

### Option B: Skill + Plugin install (for custom tool integration)

```bash
export ODYSSEUS_URL=http://your-odysseus-host:7000
export ODYSSEUS_API_TOKEN=ody_generated_token
mkdir -p ~/.config/opencode/skills ~/.config/opencode/plugins
curl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/opencode/plugin.zip" -o /tmp/odysseus-opencode-skill.zip
python3 -m zipfile -e /tmp/odysseus-opencode-skill.zip /tmp/odysseus-opencode-skill
cp -r /tmp/odysseus-opencode-skill/skills/odysseus ~/.config/opencode/skills/
cp /tmp/odysseus-opencode-skill/plugins/odysseus.js ~/.config/opencode/plugins/
```

Then add `"odysseus"` to the `plugin` array in `~/.config/opencode/opencode.json`:

```json
{
  "plugin": ["odysseus"]
}
```

### Verify

```bash
python3 ~/.config/opencode/skills/odysseus/scripts/odysseus_api.py capabilities
```

## What's in the bundle

- `skills/odysseus/SKILL.md` — the skill definition OpenCode reads.
- `skills/odysseus/scripts/odysseus_api.py` — small helper that calls the scoped
  `/api/codex/*` endpoints (these are the canonical scope-gated agent API; the
  `codex` path is historic and shared by all agent integrations).
- `plugins/odysseus.js` — OpenCode plugin providing custom tools
  (`odysseus_capabilities`, `odysseus_todos`, `odysseus_emails`,
  `odysseus_memory`, `odysseus_calendar`, `odysseus_documents`,
  `odysseus_cookbook`) that call the same scoped API directly from the agent
  loop.

## Scope enforcement

The token is scope-gated. Every tool surface is checked server-side in Odysseus,
so even if OpenCode tries to call a forbidden endpoint, it gets `403` until the
user enables the matching toggle in Settings > Integrations > OpenCode Agent.

## Plugin vs Skill

- **Skill** (SKILL.md): Provides instructions the agent reads to understand
  when and how to use Odysseus. Works with any agent that supports the skill
  format. The agent uses Bash to call the helper script.

- **Plugin** (odysseus.js): Provides native OpenCode tools that appear in the
  agent's tool list. The agent calls them directly without needing Bash. This
  is more ergonomic but requires OpenCode's plugin system.

Both approaches use the same scoped `/api/codex/*` API endpoints and the same
token-based authentication.