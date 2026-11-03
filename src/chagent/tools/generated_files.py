"""Generate real workspace files; collisions become durable host confirmations."""
import asyncio
from pathlib import PurePosixPath
from chagent.domain.messages import TextContent
from chagent.tools.base import AgentTool, AgentToolResult
from chagent.outputs import OutputService


class CreateFileTool(AgentTool):
    def __init__(self, database):
        super().__init__(name='create_file', label='Create workspace file', requires_workspace=True,
            mutates_workspace=True, execution_mode='sequential', replay='never',
            description='Create a UTF-8 output file in the workspace (root by default). filename may include an existing subdirectory. '
                        'A name collision creates a host confirmation; never claim it was saved while pending. '
                        'Use edit_file with a read SHA256 to edit an existing file; do not use this tool to bypass output confirmation.',
            parameters={'type':'object','properties':{'filename':{'type':'string','minLength':1,'maxLength':4096},
                        'content':{'type':'string','maxLength':1_048_576}},'required':['filename','content'],'additionalProperties':False})
        self.database = database

    async def execute(self, tool_call_id, params, signal=None, on_update=None, *, context=None):
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError()
        if context is None or context.session_id is None or context.run_id is None:
            raise PermissionError('File creation requires a session and run')
        name = params['filename']
        if PurePosixPath(name).suffix.lower() not in {'.md','.txt','.csv','.json','.py','.js','.ts','.html','.css','.yaml','.yml','.xml','.sql','.sh'}:
            raise ValueError('Output must be a supported text filename')
        async with self.database.connection() as connection:
            cursor = await connection.execute('SELECT workspace_id FROM sessions WHERE id=%s', (context.session_id,))
            row = await cursor.fetchone()
        if row is None:
            raise ValueError('Session does not exist')
        output = await OutputService(self.database).submit(row['workspace_id'], context.workspace, name,
            params['content'].encode('utf-8'), source='agent-output', session_id=context.session_id,
            run_id=context.run_id, tool_call_id=tool_call_id, workspace_identity=context.workspace_identity)
        saved = output['status']=='committed'
        descriptor = {'id':str(output['id']), 'workspace_id':str(output['workspace_id']),
                      'path':output['output_path'] if saved else output['path'], 'name':PurePosixPath(name).name,
                      'size':output['size'], 'status':output['status']}
        return AgentToolResult(content=[TextContent(('Saved workspace file: ' if saved else 'Not saved; awaiting host collision confirmation: ')+descriptor['path'])],
                               details={'workspace_file':descriptor, 'output_id':str(output['id'])})
