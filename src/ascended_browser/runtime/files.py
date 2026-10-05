"""Saving extractions into a chat workspace needs the Ascended app."""


class WriteFileTool:
    async def execute(self, *_args, **_kwargs) -> dict:
        return {"error": "save_to needs the Ascended app's workspace; it is not available here", "exit_code": 1}
