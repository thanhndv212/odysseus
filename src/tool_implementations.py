"""
tool_implementations.py

Extracted tool implementation functions (do_* and helpers) from agent_tools.py.
These handle the actual execution logic for each tool type.

THIS FILE IS NOW A FACADE — all implementations have moved to src/tools/.
Import from this module continues to work for backwards compatibility.
New code should import from src.tools.* directly.
"""

from src.tools._helpers import (
    _string_arg, _parse_tool_args, _internal_headers,
    _validate_mcp_command, _mcp_allowed_commands, _validate_cookbook_ssh_target,
    _infer_serve_port, _infer_serve_host, _cookbook_register_task,
    _cookbook_apply_retry_suggestion, _scan_running_model_processes,
    _load_vault_config, _skill_dump,
)
from src.tools.email_tools import set_active_email, get_active_email, clear_active_email
from src.tools.search_tools import do_search_chats
from src.tools.skills_tools import do_manage_skills
from src.tools.tasks_tools import do_manage_tasks
from src.tools.endpoint_tools import do_manage_endpoints
from src.tools.mcp_tools import do_manage_mcp
from src.tools.admin_tools import do_manage_webhooks, do_manage_tokens, do_manage_settings, do_api_call
from src.tools.notes_tools import do_manage_notes
from src.tools.calendar_tools import do_manage_calendar
from src.tools.cookbook_tools import (
    do_app_api, do_download_model, do_serve_model, do_list_served_models,
    do_stop_served_model, do_tail_serve_output, do_list_downloads,
    do_cancel_download, do_search_hf_models, do_adopt_served_model,
    do_list_cookbook_servers, do_list_serve_presets, do_serve_preset,
    do_list_cached_models,
)
from src.tools.image_tools import do_edit_image
from src.tools.research_tools import do_manage_research, do_trigger_research
from src.tools.contact_tools import do_resolve_contact, do_manage_contact
from src.tools.vault_tools import do_vault_search, do_vault_get, do_vault_unlock
