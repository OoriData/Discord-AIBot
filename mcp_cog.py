# mcp_cog.py
'''
Discord cog for MCP integration using mcp-sse-client. Connects to MCP servers
and provides commands to interact with MCP tools via an LLM.
'''
import os
import json
import asyncio
from typing import Dict, Any, List, Union, Optional, Tuple
import traceback

import discord
from discord.ext import commands
from discord import app_commands
import structlog
from openai import AsyncOpenAI, OpenAIError
# Openai types might be useful for clarity
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall, ChoiceDelta

from mcp_sse_client import MCPClient, ToolDef, ToolInvocationResult, ToolParameter # Added

# Needed for ClosedResourceError/BrokenResourceError handling if not imported via sse/mcp
# MCPClient might raise its own connection errors or underlying httpx/anyio errors
# We'll catch broader exceptions initially and refine if needed.
try:
    # Keep these if MCPClient explicitly raises them or underlying libs do.
    from anyio import ClosedResourceError, BrokenResourceError, WouldBlock
except ImportError:
    class ClosedResourceError(Exception): pass
    class BrokenResourceError(Exception): pass
    class WouldBlock(Exception): pass
# Add httpx errors which MCPClient might expose
try:
    import httpx
    ConnectError = httpx.ConnectError
    ReadTimeout = httpx.ReadTimeout
except ImportError:
    class ConnectError(IOError): pass
    class ReadTimeout(TimeoutError): pass


logger = structlog.get_logger(__name__)

ALLOWED_FOR_LLM_INIT = ['base_url', 'api_key'] # Adjusted for openai > 1.0
ALLOWED_FOR_LLM_CHAT = ['model', 'temperature', 'max_tokens', 'top_p'] # Added common chat params

# API Key Handling - ensure OPENAI_API_KEY is checked/set if needed by the AsyncOpenAI client
# The logic in __init__ already handles this using the config/env var.


# Helper Function for Message Splitting (Unchanged from provided code)
async def send_long_message(sendable: Union[commands.Context, discord.Interaction], content: str, followup: bool = False, ephemeral: bool = False):
    '''
    Sends a message, splitting it into multiple parts if it exceeds Discord's limit.
    Handles both Context and Interaction objects.
    '''
    max_length = 2000
    start = 0
    # Ensure content is a string, even if it's None or another type unexpectedly
    content_str = str(content) if content is not None else ""
    if not content_str.strip(): # Don't try to send empty messages
        logger.debug('Attempted to send empty message.')
        return

    first_chunk = True
    while start < len(content_str):
        end = start + max_length
        chunk = content_str[start:end]
        try:
            if isinstance(sendable, commands.Context):
                await sendable.send(chunk)
            elif isinstance(sendable, discord.Interaction):
                use_followup = followup or start > 0 or sendable.response.is_done()
                can_be_ephemeral = (use_followup and first_chunk) or (not use_followup and len(content_str) <= max_length)
                send_ephemeral = ephemeral and can_be_ephemeral

                if use_followup:
                     if not sendable.response.is_done():
                         # Interaction likely wasn't deferred or followup wasn't requested initially.
                         # Use original response if possible, otherwise log warning.
                         # This path is less common if defer() is used consistently.
                         try:
                            await sendable.response.send_message(chunk, ephemeral=send_ephemeral)
                            logger.debug("Sent initial interaction response (unexpected path after deferral).")
                         except discord.InteractionResponded:
                             logger.warning("Interaction already responded, but followup wasn't triggered correctly. Sending via followup.")
                             await sendable.followup.send(chunk, ephemeral=send_ephemeral) # Fallback to followup
                     else:
                         await sendable.followup.send(chunk, ephemeral=send_ephemeral)
                else:
                     # Initial response for non-deferred interaction or first message.
                     await sendable.response.send_message(chunk, ephemeral=send_ephemeral)

        except discord.HTTPException as e:
             logger.error(f'Failed to send message chunk: {e}', chunk_start=start, sendable_type=type(sendable), ephemeral=ephemeral, status=e.status, code=e.code)
             # Try to inform user ephemerally if possible
             error_msg = f'Error sending part of the message (Code: {e.code}). Please check channel permissions or message content.'
             try:
                 if isinstance(sendable, discord.Interaction):
                     # Use followup if possible, it's more reliable after potential initial errors
                     if sendable.response.is_done(): await sendable.followup.send(error_msg, ephemeral=True)
                     else: await sendable.response.send_message(error_msg, ephemeral=True)
                 # Context sending is less likely to fail ephemerally
                 # else: await sendable.send(error_msg) # Avoid sending public errors if interaction failed
             except Exception: pass # Avoid error loops
             break # Stop sending further chunks on error

        start = end
        first_chunk = False
        if start < len(content_str): # Only sleep if more chunks are coming
             await asyncio.sleep(0.2)


class MCPCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Dict[str, Any]):
        self.bot = bot
        self.config = config
        # Store live MCPClient objects, keyed by server name
        self.mcp_connections: Dict[str, MCPClient] = {}
        # Store ToolDef objects keyed by server name -> tool name for faster lookup?
        # Or keep as list per server? List is simpler for format_tools_for_openai
        self.mcp_tools: Dict[str, List[ToolDef]] = {} # Store ToolDef objects
        self.message_history: Dict[int, List[Dict[str, Any]]] = {}

        self._connection_tasks: Dict[str, asyncio.Task] = {}
        self._shutdown_event = asyncio.Event()

        # LLM Client Initialization
        llm_init_params = {k: v for k, v in config.get('llm_endpoint', {}).items() if k in ALLOWED_FOR_LLM_INIT}
        llm_endpoint_config = config.get('llm_endpoint', {}) # Get the whole section

        if not llm_endpoint_config:
            logger.error('LLM endpoint configuration ("llm_endpoint") missing!')
            raise ValueError('LLM endpoint configuration missing.')

        # Ensure API key is present (from config or env var handled in load_config)
        if 'api_key' not in llm_init_params:
             llm_init_params['api_key'] = llm_endpoint_config.get('api_key', 'missing-key') # Should be set by load_config
             if llm_init_params['api_key'] == 'missing-key':
                  logger.warning("LLM API Key not found in config or OPENAI_API_KEY env var.")

        try:
            self.llm_client = AsyncOpenAI(**llm_init_params)
            logger.info("AsyncOpenAI client initialized.", base_url=llm_init_params.get('base_url'))
        except Exception as e:
            logger.exception('Failed to initialize OpenAI client', endpoint_config=llm_endpoint_config)
            raise e

        # LLM Chat Parameters (defaults + overrides from config)
        self.llm_chat_params = { # Start with reasonable defaults
             'model': 'local-model',
             'temperature': 0.3,
        }
        # Update with allowed chat params from the llm_endpoint config section
        endpoint_chat_params = {k: v for k, v in llm_endpoint_config.items() if k in ALLOWED_FOR_LLM_CHAT}
        self.llm_chat_params.update(endpoint_chat_params)

        # LLM Settings from dedicated section (overrides endpoint params if keys overlap)
        # DEPRECATED: Merged into llm_chat_params for simplicity. Keeping structure in case needed.
        # self.llm_settings = config.get('llm_settings', {})
        # self.llm_settings.setdefault('model', self.llm_chat_params['model'])
        # self.llm_settings.setdefault('temperature', self.llm_chat_params.get('temperature', 0.3))
        # self.llm_chat_params.update(self.llm_settings) # Apply overrides

        logger.info("LLM chat parameters configured", **self.llm_chat_params)


        self.system_message = config.get('system_message', 'You are a helpful AI assistant. You can access tools using MCP servers.')
        logger.info('MCPCog initialized.')

    async def _manage_mcp_connection(self, url: str, name: str):
        '''Persistent task to manage a single MCP connection using MCPClient.'''
        reconnect_delay = 15 # Initial delay seconds
        max_reconnect_delay = 300 # Max delay seconds

        while not self._shutdown_event.is_set():
            logger.info(f'Attempting to connect/verify MCP server: {name}', url=url)
            client: Optional[MCPClient] = None
            try:
                # 1. Create or Get Client Instance
                # MCPClient constructor likely attempts initial connection/handshake.
                # It might raise errors here if the initial connection fails.
                client = MCPClient(url) # Add headers/timeouts from config if needed
                logger.info(f'MCPClient instance created for {name}. Attempting to list tools...')

                # 2. List Tools (Acts as connection check and fetches tool info)
                # This call will likely handle the underlying SSE connection setup and MCP handshake implicitly.
                # Add timeout? MCPClient might have internal timeouts. Check its docs/code.
                # Let's add a reasonable application-level timeout for list_tools.
                tools_list: List[ToolDef] = await asyncio.wait_for(client.list_tools(), timeout=45.0)

                # 3. Store Client and Tools on Success
                self.mcp_connections[name] = client
                self.mcp_tools[name] = tools_list
                logger.info(f'Successfully connected to {name} and listed {len(tools_list)} tools.', server_name=name)
                reconnect_delay = 15 # Reset delay on success

                # 4. Connection Maintenance (MCPClient handles underlying keep-alive/SSE)
                # We just need to wait until shutdown or an error occurs during a tool call.
                # This task can potentially sleep or periodically check status if MCPClient provides a method.
                # For now, rely on execute_tool to detect issues. Wait for shutdown signal.
                await self._shutdown_event.wait()
                logger.info(f'Shutdown signal received for {name}. Ending management task.')
                # Loop will terminate because _shutdown_event is set.

            except (ConnectError, ReadTimeout, ConnectionRefusedError) as conn_err:
                 logger.warning(f'Connection failed for MCP server {name}: {type(conn_err).__name__}. Retrying...', server_name=name, error=str(conn_err))
            except asyncio.TimeoutError:
                logger.error(f'Timeout connecting or listing tools for MCP server {name}. Retrying...', server_name=name)
            except asyncio.CancelledError:
                logger.info(f'Connection task for {name} cancelled.')
                break # Exit loop immediately
            except Exception as e:
                # Catch other errors from MCPClient init or list_tools
                logger.exception(f'Unexpected error managing connection for {name}. Retrying...', server_name=name, url=url)
                # Optionally log traceback: logger.error("Traceback:", exc_info=True)

            # 5. Cleanup before Reconnect/Shutdown
            logger.debug(f'Cleaning up resources for {name} before retry/shutdown.')
            if name in self.mcp_connections:
                # Does MCPClient need explicit closing? Check its implementation.
                # Assuming no explicit close needed for now, just remove reference.
                del self.mcp_connections[name]
            if name in self.mcp_tools:
                del self.mcp_tools[name]
            client = None # Ensure client object is cleared

            # 6. Wait before Retrying (if not shutting down)
            if not self._shutdown_event.is_set():
                logger.info(f'Waiting {reconnect_delay}s before reconnecting to {name}.')
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=reconnect_delay)
                    logger.info(f'Shutdown signaled during reconnect delay for {name}.')
                    break # Exit loop if shutdown signaled
                except asyncio.TimeoutError:
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay) # Exponential backoff
                except asyncio.CancelledError:
                     logger.info(f'Reconnect wait for {name} cancelled.')
                     break # Exit loop

        # Final Task Cleanup (on shutdown or cancellation)
        logger.info(f'Connection management task for {name} finished cleanly.')
        if name in self.mcp_connections: del self.mcp_connections[name]
        if name in self.mcp_tools: del self.mcp_tools[name]
        # Ensure client is None
        client = None

    async def cog_load(self):
        '''
        Set up MCP connections when the cog is loaded by starting persistent tasks.
        '''
        logger.info('Loading MCPCog and starting connection managers...')
        self._shutdown_event.clear() # Ensure shutdown is not set initially
        mcp_servers = self.config.get('mcp', {}).get('server', [])
        if not mcp_servers:
            logger.warning('No MCP servers defined in configuration.')
            return

        active_task_count = 0
        for server in mcp_servers:
            # Basic validation of server config entry
            if isinstance(server, dict) and 'url' in server and 'name' in server:
                name = server['name']
                url = server['url']
                if not isinstance(name, str) or not isinstance(url, str):
                     logger.warning('Invalid MCP server config entry, name/url not strings', entry=server)
                     continue

                # Start task only if it doesn't exist or has finished
                if name not in self._connection_tasks or self._connection_tasks[name].done():
                    logger.info(f'Creating connection task for MCP server: {name}')
                    task = asyncio.create_task(self._manage_mcp_connection(url, name), name=f'mcp_conn_{name}')
                    self._connection_tasks[name] = task
                    active_task_count += 1
                else:
                    logger.info(f'Connection task for {name} already running.')
                    active_task_count += 1 # Count existing running tasks too
            else:
                 logger.warning('Invalid MCP server config entry, missing "url" or "name"', entry=server)

        logger.info(f'MCPCog load processed. {active_task_count} connection tasks are active or starting.')

    async def cog_unload(self):
        '''
        Clean up MCP connections when the cog is unloaded by stopping tasks.
        '''
        logger.info('Unloading MCPCog and stopping connection tasks...')
        self._shutdown_event.set() # Signal all tasks to stop their loops

        tasks_to_wait_for = list(self._connection_tasks.values())

        if tasks_to_wait_for:
            logger.info(f'Waiting for {len(tasks_to_wait_for)} MCP connection tasks to complete shutdown...')
            # Wait for tasks to finish processing the shutdown signal
            # Give them a bit more time as they might be in a wait/retry loop
            done, pending = await asyncio.wait(tasks_to_wait_for, timeout=15.0)

            if pending:
                logger.warning(f'{len(pending)} connection tasks did not shut down cleanly within timeout. Attempting cancellation.')
                for task in pending:
                    task_name = task.get_name() if hasattr(task, 'get_name') else 'unknown task'
                    logger.debug(f'Cancelling task: {task_name}')
                    task.cancel()
                    try:
                        # Allow cancellation to propagate briefly
                        await asyncio.wait_for(task, timeout=1.0)
                    except asyncio.CancelledError:
                        logger.debug(f'Task {task_name} confirmed cancelled.')
                    except asyncio.TimeoutError:
                        logger.warning(f'Task {task_name} did not respond to cancellation quickly.')
                    except Exception as e:
                         logger.error(f'Error during task cancellation for {task_name}', exc_info=e)

            else:
                 logger.info('All connection tasks shut down gracefully.')
        else:
             logger.info('No active connection tasks to stop.')

        # Clear stored data regardless of task shutdown status
        self.mcp_connections.clear()
        self.mcp_tools.clear()
        self._connection_tasks.clear() # Clear the task references
        logger.info('MCPCog unloaded and connection resources cleared.')

    async def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> Union[ToolInvocationResult, Dict[str, str]]:
        '''
        Execute an MCP tool using a managed MCPClient instance.
        Returns ToolInvocationResult on success, or an error dictionary.
        '''
        logger.debug('Executing tool', tool_name=tool_name, tool_input=tool_input)

        mcp_client: Optional[MCPClient] = None
        server_name_found: Optional[str] = None

        # Find the server/client that has the tool listed
        for server_name, client in self.mcp_connections.items():
            # Check if the tool name exists in the list of tools for this server
            if server_name in self.mcp_tools and any(tool.name == tool_name for tool in self.mcp_tools[server_name]):
                mcp_client = client
                server_name_found = server_name
                logger.debug(f"Found active client for tool '{tool_name}' on server '{server_name}'.")
                break

        if not mcp_client or not server_name_found:
            logger.warning(f'Tool "{tool_name}" not found on any *actively connected* MCP server.')
            # Provide more specific error messages
            if not self.mcp_connections:
                return {'error': f'Tool "{tool_name}" cannot be executed: No MCP servers currently connected.'}
            else:
                # Check if the tool *exists* in config but server is down
                 tool_server_origin = None
                 for s_name, tools in self.mcp_tools.items(): # Check loaded tools first
                     if any(t.name == tool_name for t in tools):
                         tool_server_origin = s_name
                         break
                 # If not found in loaded tools, check config (less reliable state)
                 if not tool_server_origin:
                     for server_config in self.config.get('mcp', {}).get('server', []):
                          s_name = server_config.get('name')
                          # This check requires knowing tools without connection - less ideal.
                          # Relying on self.mcp_tools (populated on connection) is better.
                          pass # Can't reliably check config without prior connection

                 if tool_server_origin and tool_server_origin not in self.mcp_connections:
                      return {'error': f'Tool "{tool_name}" exists (on server "{tool_server_origin}"), but the server is currently disconnected or unavailable.'}
                 elif tool_server_origin: # Exists and server *is* in connections, but wasn't selected? Should not happen.
                     return {'error': f'Internal state error: Tool "{tool_name}" found but client mismatch.'}
                 else:
                     return {'error': f'Tool "{tool_name}" not found on any configured and connected MCP server.'}

        logger.info(f'Calling tool "{tool_name}" on MCP server "{server_name_found}"')
        try:
            # Call the tool using MCPClient's invoke_tool method
            # Add a timeout for the specific tool call.
            result: ToolInvocationResult = await asyncio.wait_for(
                mcp_client.invoke_tool(tool_name, tool_input),
                timeout=120.0
            )
            logger.debug(f'Tool "{tool_name}" executed via MCPClient.', result_content=result.content, error_code=result.error_code)

            # Check for tool-specific errors reported by MCPClient/Server
            # Check specifically for non-zero error codes. Treat None and 0 as success.
            if result.error_code is not None and result.error_code != 0:
                logger.warning(f'Tool "{tool_name}" executed but reported an error.', error_code=result.error_code, content=result.content)
                # Format the error content for the user/LLM
                error_message = f"Tool reported error code {result.error_code}"
                if result.content: # Include content if provided with the error
                     error_message += f": {str(result.content)}"
                return {'error': error_message} # Return standard error dict

            # If error_code is None or 0, it's a success
            return result # Return the successful ToolInvocationResult object

        except asyncio.TimeoutError:
             logger.error(f'Timeout calling tool "{tool_name}" on server "{server_name_found}"', tool_input=tool_input)
             # Attempt to trigger reconnect by removing client? Or let manager handle?
             # Let manager handle based on future failures.
             return {'error': f'Tool call "{tool_name}" timed out.'}
        except (ConnectError, ReadTimeout, BrokenResourceError, ClosedResourceError, ConnectionError, WouldBlock) as conn_err:
            # Catch connection-related errors during the tool call
            logger.error(f'Connection error during tool call "{tool_name}" on server "{server_name_found}": {type(conn_err).__name__}', tool_input=tool_input, error=str(conn_err))
            # Remove the client reference; the management task will handle reconnection.
            if server_name_found in self.mcp_connections: del self.mcp_connections[server_name_found]
            if server_name_found in self.mcp_tools: del self.mcp_tools[server_name_found] # Also clear tools
            return {'error': f'Connection error executing tool "{tool_name}". The server may be temporarily unavailable. Please try again.'}
        except Exception as e:
            logger.exception(f'Unexpected error calling tool "{tool_name}" on server "{server_name_found}"', tool_input=tool_input)
            return {'error': f'Error calling tool "{tool_name}": {str(e)}'}

    def format_calltoolresult_content(self, result: Union[ToolInvocationResult, Dict[str, str]]) -> str:
        '''
        Extract content from a ToolInvocationResult object or format error dict.
        Handles common MCP content structures like text content.
        '''
        if isinstance(result, ToolInvocationResult):
            # ToolInvocationResult has .content and .error_code
            # We assume execute_tool already converted errors to the dict format.
            # So, if we receive ToolInvocationResult here, it should be a success.
            content = result.content
            if content is None:
                 # Assume tool_name attribute exists on ToolInvocationResult based on library structure
                 tool_name_attr = getattr(result, 'tool_name', 'unknown_tool')
                 logger.warning('ToolInvocationResult received with None content.', tool_name=tool_name_attr, error_code=result.error_code) # Assuming tool_name exists
                 return f'Tool executed successfully but returned no content.'

            # *** ADDED: Handle specific MCP text content structure ***
            if isinstance(content, dict) and content.get('type') == 'text' and 'text' in content:
                 text_content = content['text']
                 # Return the text directly, converting simple types to string
                 return str(text_content) if text_content is not None else ""

            # Handle other types (simple types, lists, other dicts)
            elif isinstance(content, (str, int, float, bool)):
                return str(content)
            elif isinstance(content, (dict, list)):
                try:
                    # Nicely format other JSON-like structures
                    return json.dumps(content, indent=2)
                except TypeError:
                    logger.warning("Could not JSON serialize tool result content.", content_type=type(content))
                    return str(content) # Fallback
            else:
                logger.warning('Unexpected type for ToolInvocationResult content.', content_type=type(content))
                return str(content)

        elif isinstance(result, dict) and 'error' in result:
            # Handle our standardized error format
            return f'Tool Error: {result["error"]}'
        else:
            # Gracefully handle unexpected types
            logger.warning('Unexpected format received in format_calltoolresult_content', received_result=result)
            return f'Unexpected tool result format: {str(result)[:200]}'

    def get_channel_history(self, channel_id: int) -> List[Dict[str, Any]]:
        '''
        Get message history for a channel, or initialize if not exists.
        Includes basic history length management.
        '''
        max_history_len = self.config.get('max_history_length', 20)
        if channel_id not in self.message_history:
            self.message_history[channel_id] = [
                {'role': 'system', 'content': self.system_message}
            ]
            logger.info(f'Initialized message history for channel {channel_id}')
        else:
             current_len = len(self.message_history[channel_id])
             target_len = max_history_len + 1 # Keep system message + N history items
             if current_len > target_len:
                 amount_to_remove = current_len - target_len
                 # Keep system message ([0]) + last `max_history_len` messages
                 self.message_history[channel_id] = [self.message_history[channel_id][0]] + self.message_history[channel_id][amount_to_remove + 1:]
                 logger.debug(f'Trimmed message history for channel {channel_id}, removed {amount_to_remove} messages. New length: {len(self.message_history[channel_id])}.')

        return self.message_history[channel_id]

    def _map_mcp_type_to_json_type(self, mcp_type: str) -> str:
        """Maps MCP parameter types to JSON Schema types."""
        # Based on mcp-sse-client ToolParameter.parameter_type which seems to be string
        type_map = {
            "string": "string",
            "integer": "integer",
            "number": "number",
            "boolean": "boolean",
            "array": "array", # Assuming direct mapping for array/object might need refinement
            "object": "object",
            # Add other mappings if MCP uses different type names
        }
        json_type = type_map.get(mcp_type.lower(), "string") # Default to string if unknown
        if json_type != mcp_type.lower():
            logger.debug(f"Mapped MCP type '{mcp_type}' to JSON type '{json_type}'.")
        return json_type

    async def format_tools_for_openai(self) -> List[Dict[str, Any]]:
        '''
        Format active MCP tools (from ToolDef) for OpenAI API.
        '''
        openai_tools = []
        active_server_names = list(self.mcp_connections.keys()) # Use connected clients

        for server_name in active_server_names:
            if server_name not in self.mcp_tools:
                logger.warning(f'Server "{server_name}" is connected but has no tools listed. Skipping for OpenAI format.', server_name=server_name)
                continue

            tool_defs: List[ToolDef] = self.mcp_tools[server_name]
            logger.debug(f'Formatting {len(tool_defs)} tools from active server "{server_name}" for OpenAI.')

            for tool in tool_defs:
                # Validate ToolDef structure (basic check)
                if not isinstance(tool, ToolDef) or not tool.name:
                    logger.warning(f'Skipping malformed/nameless ToolDef from server "{server_name}"', tool_data=tool)
                    continue

                # Build JSON schema for parameters
                properties = {}
                required_params = []
                if isinstance(tool.parameters, list):
                    for param in tool.parameters:
                        if not isinstance(param, ToolParameter) or not param.name:
                            logger.warning(f'Skipping malformed parameter in tool "{tool.name}"', param_data=param)
                            continue

                        param_schema = {
                            'type': self._map_mcp_type_to_json_type(param.parameter_type),
                            'description': param.description or f"Parameter {param.name}" # Add default desc
                        }
                        # Add enum if present (assuming param.enum is a list)
                        if hasattr(param, 'enum') and isinstance(param.enum, list) and param.enum:
                           param_schema['enum'] = param.enum

                        # Add default if present (assuming param.default exists)
                        if hasattr(param, 'default') and param.default is not None:
                            param_schema['default'] = param.default

                        # Handle arrays (needs 'items' schema) - Basic handling
                        if param_schema['type'] == 'array':
                            # Default to array of strings if item type not specified
                            # MCP ToolParameter needs more info for complex types
                            param_schema['items'] = {'type': 'string'}
                            logger.debug(f"Using default 'items: string' for array param '{param.name}' in tool '{tool.name}'. Specify item type in MCP if needed.")

                        # Handle objects (needs 'properties' schema) - Basic handling
                        if param_schema['type'] == 'object':
                            # Need more schema info from MCP ToolParameter for object properties
                            param_schema['properties'] = {}
                            logger.debug(f"Using default 'properties: {{}}' for object param '{param.name}' in tool '{tool.name}'. Specify properties in MCP if needed.")


                        properties[param.name] = param_schema

                        # Check for required flag (assuming param.required is a boolean)
                        if hasattr(param, 'required') and param.required:
                            required_params.append(param.name)

                # Final JSON Schema for the tool
                parameters_schema = {
                    'type': 'object',
                    'properties': properties,
                }
                if required_params:
                    parameters_schema['required'] = required_params

                # OpenAI Tool structure
                openai_tool = {
                    'type': 'function',
                    'function': {
                        'name': tool.name,
                        'description': tool.description or f"Executes the {tool.name} tool.", # Default desc
                        'parameters': parameters_schema,
                    },
                }
                openai_tools.append(openai_tool)

        return openai_tools


    async def _handle_chat_logic(
        self,
        sendable: Union[commands.Context, discord.Interaction],
        message: str,
        channel_id: int,
        user_id: int,
        stream: bool
    ):
        '''
        Core logic for handling chat interactions, LLM calls, and tool execution.
        (Largely unchanged, check error handling and result formatting calls)
        '''
        is_interaction = isinstance(sendable, discord.Interaction)
        send_followup = is_interaction and sendable.response.is_done()

        try:
            # Get and update history
            channel_history = self.get_channel_history(channel_id)
            channel_history.append({'role': 'user', 'content': message})
            logger.debug(f'User message added to history for channel {channel_id}', user_id=user_id, stream=stream)

            # Prepare chat parameters (Combine endpoint & explicit settings)
            # Use the consolidated self.llm_chat_params
            chat_params = {**self.llm_chat_params, 'stream': stream} # Pass stream explicitly
            openai_tools = await self.format_tools_for_openai()
            if openai_tools:
                chat_params['tools'] = openai_tools
                chat_params['tool_choice'] = 'auto'
                logger.debug(f'Including {len(openai_tools)} tools in LLM call for channel {channel_id}')
            else:
                 logger.debug(f'No active tools available for LLM call for channel {channel_id}')

            # LLM Call & Response Handling
            initial_response_content = ''
            tool_calls_aggregated: List[Dict[str, Any]] = []
            assistant_message_dict: Dict[str, Any] = {'role': 'assistant', 'content': None}

            logger.debug(f'Initiating LLM call for channel {channel_id}', user_id=user_id, stream=stream, model=chat_params.get('model'))
            try:
                if stream:
                    llm_stream = await self.llm_client.chat.completions.create(messages=channel_history, user=str(user_id), **chat_params)
                    current_tool_calls: List[Dict[str, Any]] = []
                    async for chunk in llm_stream:
                        delta: Optional[ChoiceDelta] = chunk.choices[0].delta if chunk.choices else None
                        if not delta: continue
                        if token := delta.content or '': initial_response_content += token
                        if delta.tool_calls:
                            for tool_call_chunk in delta.tool_calls:
                                idx = tool_call_chunk.index
                                while len(current_tool_calls) <= idx: current_tool_calls.append({'id': None, 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                                tc_ref = current_tool_calls[idx]
                                chunk_func: Optional[ChoiceDeltaToolCall.Function] = tool_call_chunk.function
                                if tool_call_chunk.id: tc_ref['id'] = tool_call_chunk.id
                                if chunk_func:
                                    if chunk_func.name: tc_ref['function']['name'] += chunk_func.name
                                    if chunk_func.arguments: tc_ref['function']['arguments'] += chunk_func.arguments
                    tool_calls_aggregated = [tc for tc in current_tool_calls if tc.get('id') and tc['function'].get('name')]
                else: # Non-Streaming
                    response = await self.llm_client.chat.completions.create(messages=channel_history, user=str(user_id), **chat_params)
                    response_message: Optional[ChatCompletionMessage] = response.choices[0].message if response.choices else None
                    if not response_message:
                         logger.error('LLM response missing message object', response_data=response.model_dump_json(indent=2))
                         await send_long_message(sendable, '⚠️ Received an empty response from AI.', followup=send_followup, ephemeral=is_interaction)
                         return
                    initial_response_content = response_message.content or ''
                    raw_tool_calls: Optional[List[ChatCompletionMessageToolCall]] = response_message.tool_calls
                    if raw_tool_calls:
                         tool_calls_aggregated = [{'id': tc.id, 'type': tc.type, 'function': {'name': tc.function.name, 'arguments': tc.function.arguments}}
                                                  for tc in raw_tool_calls if tc.type == 'function' and tc.function and tc.id and tc.function.name]
                         logger.debug(f'Non-streaming response included {len(tool_calls_aggregated)} raw tool calls.', channel_id=channel_id)

            except OpenAIError as api_exc: # Catch specific OpenAI errors
                logger.exception('Error during LLM API call', channel_id=channel_id, user_id=user_id, error_type=type(api_exc).__name__)
                await send_long_message(sendable, f'⚠️ Error communicating with AI: {str(api_exc)}', followup=send_followup, ephemeral=is_interaction)
                return
            except Exception as e: # Catch other unexpected errors (e.g., network)
                 logger.exception('Unexpected error during LLM communication', channel_id=channel_id, user_id=user_id)
                 await send_long_message(sendable, f'⚠️ Unexpected error communicating with AI: {str(e)}', followup=send_followup, ephemeral=is_interaction)
                 return

            # Send Initial Response & Update History (Unchanged)
            sent_initial_message = False
            if initial_response_content.strip():
                logger.debug(f'Sending initial LLM response to channel {channel_id}', length=len(initial_response_content), stream=stream)
                await send_long_message(sendable, initial_response_content, followup=send_followup)
                assistant_message_dict['content'] = initial_response_content
                sent_initial_message = True
                send_followup = True # Any subsequent messages MUST be followups

            if tool_calls_aggregated:
                 assistant_message_dict['tool_calls'] = tool_calls_aggregated
                 logger.info(f'LLM requested {len(tool_calls_aggregated)} tool call(s) for channel {channel_id}', tool_names=[tc['function']['name'] for tc in tool_calls_aggregated], stream=stream)

            if assistant_message_dict.get('content') or assistant_message_dict.get('tool_calls'):
                channel_history.append(assistant_message_dict)
            elif not sent_initial_message:
                logger.info(f'LLM call finished for channel {channel_id} with no text/tools.', stream=stream)
                await send_long_message(sendable, 'I received your message but didn\'t have anything specific to add or do.', followup=send_followup, ephemeral=is_interaction)
                return

            # Process Tool Calls (Using updated execute_tool and formatters)
            if tool_calls_aggregated:
                tool_results_for_history = []

                for tool_call in tool_calls_aggregated:
                    # Basic validation of structure remains useful
                    if not isinstance(tool_call, dict) or 'function' not in tool_call or 'id' not in tool_call:
                         logger.error('Malformed tool call structure received from LLM', tool_call_data=tool_call); continue
                    if not isinstance(tool_call['function'], dict) or 'name' not in tool_call['function'] or 'arguments' not in tool_call['function']:
                         logger.error('Malformed tool call function structure received from LLM', tool_call_data=tool_call); continue

                    tool_name = tool_call['function']['name']
                    tool_call_id = tool_call['id']
                    arguments_str = tool_call['function']['arguments']
                    tool_result_content_for_llm = f'Error: Tool call processing failed internally before execution for {tool_name}' # Default

                    try:
                        # Ensure arguments are valid JSON
                        try:
                            tool_args = json.loads(arguments_str)
                            if not isinstance(tool_args, dict):
                                 raise TypeError("Arguments must be a JSON object (dict)")
                        except (json.JSONDecodeError, TypeError) as json_err:
                            logger.error(f'Failed to decode JSON args or not a dict for tool `{tool_name}`', args_str=arguments_str, tool_call_id=tool_call_id, error=str(json_err))
                            tool_result_content_for_llm = f'Error: Invalid JSON object arguments provided for tool "{tool_name}". LLM sent: {arguments_str}'
                            await send_long_message(sendable, f'⚠️ Error: Couldn\'t understand arguments for tool `{tool_name}`. LLM provided malformed JSON: `{arguments_str}`', followup=True, ephemeral=is_interaction)
                            send_followup = True
                            # Append failure result to history
                            tool_results_for_history.append({'role': 'tool', 'tool_call_id': tool_call_id, 'name': tool_name, 'content': tool_result_content_for_llm})
                            continue # Skip execution for this tool call

                        logger.info(f'Executing tool `{tool_name}` for channel {channel_id}', args=tool_args, tool_call_id=tool_call_id)
                        tool_result_obj = await self.execute_tool(tool_name, tool_args)

                        tool_result_content_formatted = self.format_calltoolresult_content(tool_result_obj)
                        tool_result_content_for_llm = tool_result_content_formatted # Send formatted result back to LLM

                        # Send result back to user (non-ephemeral)
                        await send_long_message(sendable, f'```Tool Call: {tool_name}\nResult:\n{tool_result_content_formatted}```', followup=True)
                        send_followup = True

                    except Exception as exec_e:
                        # Catch errors from execute_tool or format_calltoolresult_content itself (should be rare now)
                        logger.exception(f'Unexpected error during tool execution/formatting phase for `{tool_name}`', tool_call_id=tool_call_id)
                        tool_result_content_for_llm = f'Error during tool execution phase for "{tool_name}": {str(exec_e)}'
                        await send_long_message(sendable, f'⚠️ Unexpected Error running tool `{tool_name}`: {str(exec_e)}', followup=True, ephemeral=is_interaction)
                        send_followup = True

                    # Append result to history
                    tool_results_for_history.append({
                        'role': 'tool',
                        'tool_call_id': tool_call_id,
                        'name': tool_name,
                        'content': tool_result_content_for_llm
                    })

                # Follow-up LLM Call (Unchanged logic)
                if tool_results_for_history:
                    channel_history.extend(tool_results_for_history)
                    logger.debug(f'Added {len(tool_results_for_history)} tool results to history for channel {channel_id}')

                    logger.debug(f'Initiating follow-up LLM call for channel {channel_id} after tools.', stream=stream)
                    follow_up_params = {**self.llm_chat_params, 'stream': stream}
                    follow_up_params.pop('tools', None) # No tools needed for follow-up
                    follow_up_params.pop('tool_choice', None)

                    follow_up_text = ''
                    try:
                        if stream:
                            follow_up_stream = await self.llm_client.chat.completions.create(messages=channel_history, user=str(user_id), **follow_up_params)
                            async for chunk in follow_up_stream:
                                if token := chunk.choices[0].delta.content or '': follow_up_text += token
                        else:
                            follow_up_response = await self.llm_client.chat.completions.create(messages=channel_history, user=str(user_id), **follow_up_params)
                            follow_up_message = follow_up_response.choices[0].message if follow_up_response.choices else None
                            if follow_up_message: follow_up_text = follow_up_message.content or ''

                    except OpenAIError as followup_exc: # Catch specific OpenAI errors
                         logger.exception('Error during follow-up LLM call', channel_id=channel_id, user_id=user_id, error_type=type(followup_exc).__name__)
                         await send_long_message(sendable, f'⚠️ Error getting follow-up response from AI: {str(followup_exc)}', followup=True, ephemeral=is_interaction)
                         return
                    except Exception as e: # Catch other unexpected errors
                         logger.exception('Unexpected error during follow-up LLM communication', channel_id=channel_id, user_id=user_id)
                         await send_long_message(sendable, f'⚠️ Unexpected error getting follow-up AI response: {str(e)}', followup=True, ephemeral=is_interaction)
                         return

                    # Send Follow-up & Update History
                    if follow_up_text.strip():
                        logger.debug(f'Sending follow-up LLM response to channel {channel_id}', length=len(follow_up_text), stream=stream)
                        await send_long_message(sendable, follow_up_text, followup=True) # Must be followup
                        channel_history.append({'role': 'assistant', 'content': follow_up_text})
                    else:
                         logger.info(f'LLM follow-up call finished for channel {channel_id} with no text content.', stream=stream)


        except Exception as e:
            logger.exception(f'Unhandled error during chat logic execution for channel {channel_id}', user_id=user_id, stream=stream)
            try:
                 error_message = f'An unexpected error occurred while processing your request. Please check the logs.'
                 await send_long_message(sendable, error_message, followup=send_followup, ephemeral=True) # Send ephemeral
            except Exception as send_err:
                 logger.error('Failed to send error message back to user after main chat logic failure', send_error=send_err)


    # Discord Commands (mcp_list, chat_command, chat_slash)

    @commands.Cog.listener()
    async def on_ready(self):
        ''' Cog ready listener. Connection tasks are started in cog_load. '''
        logger.info(f'Cog {self.__class__.__name__} is ready.')


    @app_commands.command(name='mcp_list', description='List connected MCP servers and available tools')
    async def mcp_list_slash(self, interaction: discord.Interaction):
        ''' Command to list connected MCP servers and tools. '''
        logger.info(f'Command "/mcp_list" invoked by {interaction.user} in channel {interaction.channel_id}')
        await interaction.response.defer(thinking=True, ephemeral=False) # Non-ephemeral thinking

        # Check status based on new connection management
        if not self.mcp_connections and not self.mcp_tools:
            if not self._connection_tasks:
                 await interaction.followup.send('No MCP servers are configured.')
            else:
                 # Check if tasks are running but haven't connected yet
                 all_tasks_done = all(t.done() for t in self._connection_tasks.values())
                 if all_tasks_done:
                      await interaction.followup.send('MCP servers are configured, but none are currently connected. Check server status and bot logs.')
                 else:
                      await interaction.followup.send('MCP servers are configured and attempting to connect/reconnect. Please wait a moment and try again.')
            return

        message = '**MCP Server Status & Tools:**\n\n'
        # Get server names from config and current state
        configured_servers = {s['name'] for s in self.config.get('mcp', {}).get('server', []) if 'name' in s}
        connected_servers = set(self.mcp_connections.keys()) # Servers with active MCPClient
        servers_with_tools = set(self.mcp_tools.keys()) # Servers where list_tools succeeded

        # Consider all servers mentioned in config or currently tracked
        all_server_names = configured_servers.union(connected_servers).union(servers_with_tools)

        if not all_server_names:
             await interaction.followup.send('No MCP servers found in configuration or current state.')
             return

        for name in sorted(list(all_server_names)):
             status = ''
             status_icon = '❓' # Unknown/Default

             # Determine status based on connections, tools, and tasks
             if name in connected_servers and name in servers_with_tools:
                 status = 'Connected & Tools Listed'
                 status_icon = '🟢'
             elif name in connected_servers and name not in servers_with_tools:
                 # This state might occur briefly if list_tools failed after MCPClient init
                 status = 'Connected (Listing Tools Failed?)'
                 status_icon = '🟡'
             elif name in self._connection_tasks and not self._connection_tasks[name].done():
                 # Task is running but not successfully connected/listed tools yet
                 status = 'Connecting / Initializing...'
                 status_icon = '🟠'
             elif name in configured_servers:
                  # Configured but no active connection or task running (or task finished with error)
                  status = 'Disconnected / Unavailable'
                  status_icon = '🔴'
             else:
                  # Should not happen if logic is correct (name exists but not in config/tasks)
                  status = 'Unknown State (Stale Data?)'
                  status_icon = '⚪'


             message += f'- **{name}**: {status_icon} {status}\n'

             # List tools only if successfully listed
             if name in servers_with_tools:
                 tools: List[ToolDef] = self.mcp_tools[name]
                 if tools:
                     message += f'  **Tools ({len(tools)}):**\n'
                     tool_limit = 7
                     displayed_count = 0
                     for i, tool in enumerate(tools):
                          tool_name = tool.name or f'Unnamed Tool {i+1}'
                          if displayed_count < tool_limit:
                              desc = tool.description or 'No description'
                              message += f'    - `{tool_name}`: {desc[:100]}{"..." if len(desc)>100 else ""}\n'
                              displayed_count += 1
                          elif displayed_count == tool_limit:
                              remaining = len(tools) - tool_limit
                              if remaining > 0: message += f'    - ... and {remaining} more\n'
                              break # Stop listing tools for this server
                 else:
                     # This case means list_tools succeeded but returned an empty list
                     message += '  *Connected, but server reported no tools.*\n'

             message += '\n' # Space between servers

        await send_long_message(interaction, message.strip(), followup=True)


    # Chat Commands (Prefix and Slash)
    # These commands use _handle_chat_logic, so changes there are inherited.
    # No structural changes needed here unless specific command behavior changes.

    # Prefix form
    @commands.command(name='chat', help='Chat with the AI assistant (Prefix Command)')
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def chat_command(self, ctx: commands.Context, *, message: str):
        ''' Prefix command to chat with the LLM, using MCP tools. '''
        logger.info(f'Prefix command "!chat" invoked by {ctx.author} ({ctx.author.id}) in channel {ctx.channel.id}')
        channel_id = ctx.channel.id
        user_id = ctx.author.id

        # Check if message is empty after stripping whitespace
        if not message.strip():
             await ctx.send("⚠️ Please provide a message to chat about!")
             return

        async with ctx.typing():
            await self._handle_chat_logic(
                sendable=ctx,
                message=message,
                channel_id=channel_id,
                user_id=user_id,
                stream=False # Keep Prefix non-streaming for simplicity
            )

    @chat_command.error
    async def chat_command_error(self, ctx: commands.Context, error: commands.CommandError):
        ''' Error handler for the prefix chat command. '''
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f'⏳ Woah there! This command is on cooldown. Try again in {error.retry_after:.1f} seconds.', delete_after=10)
        elif isinstance(error, commands.MissingRequiredArgument):
             await ctx.send(f'⚠️ You need to provide a message! Usage: `{ctx.prefix}chat <your message>`')
        else:
            logger.error(f'Error in prefix command chat_command dispatch: {error}', exc_info=error)
            # Avoid sending generic error here if _handle_chat_logic sends its own

    # Slash form
    @app_commands.command(name='chat', description='Chat with the AI assistant')
    @app_commands.describe(message='Your message to the AI')
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id) # Cooldown per user
    async def chat_slash(self, interaction: discord.Interaction, message: str):
        ''' Slash command version of the chat command. '''
        logger.info(f'Slash command `/chat` invoked by {interaction.user} ({interaction.user.id}) in channel {interaction.channel_id}')
        channel_id = interaction.channel_id
        user_id = interaction.user.id

        # Check for empty message early
        if not message.strip():
             await interaction.response.send_message("⚠️ Please provide a message to chat about!", ephemeral=True)
             return

        # Defer response
        await interaction.response.defer(thinking=True, ephemeral=False) # Non-ephemeral for public results

        await self._handle_chat_logic(
            sendable=interaction,
            message=message,
            channel_id=channel_id,
            user_id=user_id,
            stream=False # Keep Slash non-streaming for now
        )

    @chat_slash.error
    async def on_chat_slash_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        ''' Error Handler for Slash Command Cooldowns/Checks. '''
        if interaction.is_expired():
             logger.warning(f'Interaction {interaction.id} expired before error handler could process: {error}')
             return

        error_message = 'An unexpected error occurred with this command.'
        ephemeral = True # Most check errors should be ephemeral

        if isinstance(error, app_commands.CommandOnCooldown):
            error_message = f'⏳ Woah there! This command is on cooldown. Try again in {error.retry_after:.1f} seconds.'
        elif isinstance(error, app_commands.CheckFailure):
             error_message = 'You don\'t have the necessary permissions or conditions met to use this command.'
             logger.warning(f"CheckFailure for /chat by {interaction.user}: {error}")
        else:
            # Log other errors (before our main logic runs)
            logger.error(f'Unhandled error in slash command chat_slash check/dispatch: {error}', interaction_id=interaction.id, exc_info=error)
            # Avoid sending generic user message if _handle_chat_logic sends its own

        # Try to send the error message using followup if deferred, or initial response otherwise
        try:
            if interaction.response.is_done():
                 await interaction.followup.send(error_message, ephemeral=ephemeral)
            else:
                 # This case implies defer() failed or the error occurred before defer()
                 await interaction.response.send_message(error_message, ephemeral=ephemeral)
        except discord.HTTPException as e:
            logger.error(f'Failed to send slash command error message for interaction {interaction.id}: {e}')
        except Exception as e:
             logger.error(f'Generic exception sending slash command error for interaction {interaction.id}: {e}')
