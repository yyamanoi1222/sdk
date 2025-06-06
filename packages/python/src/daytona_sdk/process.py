import asyncio
import base64
import json
import warnings
from typing import Callable, Dict, List, Optional

import httpx
from daytona_api_client import Command, CreateSessionRequest, ExecuteRequest, Session
from daytona_api_client import SessionExecuteRequest as ApiSessionExecuteRequest
from daytona_api_client import SessionExecuteResponse, ToolboxApi
from daytona_sdk._utils.errors import intercept_errors
from pydantic import model_validator

from .charts import parse_chart
from .code_toolbox.sandbox_python_code_toolbox import SandboxPythonCodeToolbox
from .common.code_run_params import CodeRunParams
from .common.execute_response import ExecuteResponse, ExecutionArtifacts
from .protocols import SandboxInstance


class SessionExecuteRequest(ApiSessionExecuteRequest):
    """Contains the request for executing a command in a session.

    Attributes:
        command (str): The command to execute.
        run_async (Optional[bool]): Whether to execute the command asynchronously.
        var_async (Optional[bool]): Deprecated. Use `run_async` instead.
    """

    @model_validator(mode="before")
    @classmethod
    def __handle_deprecated_var_async(
        cls, values
    ):  # pylint: disable=unused-private-member
        if "var_async" in values and values.get("var_async"):
            warnings.warn(
                "'var_async' is deprecated and will be removed in a future version. Use 'run_async' instead.",
                DeprecationWarning,
                stacklevel=3,
            )
            if "run_async" not in values or not values["run_async"]:
                values["run_async"] = values.pop("var_async")
        return values


class Process:
    """Handles process and code execution within a Sandbox.

    Attributes:
        code_toolbox (SandboxPythonCodeToolbox): Language-specific code execution toolbox.
        toolbox_api (ToolboxApi): API client for Sandbox operations.
        instance (SandboxInstance): The Sandbox instance this process belongs to.
    """

    def __init__(
        self,
        code_toolbox: SandboxPythonCodeToolbox,
        toolbox_api: ToolboxApi,
        instance: SandboxInstance,
        get_root_dir: Callable[[], str],
    ):
        """Initialize a new Process instance.

        Args:
            code_toolbox (SandboxPythonCodeToolbox): Language-specific code execution toolbox.
            toolbox_api (ToolboxApi): API client for Sandbox operations.
            instance (SandboxInstance): The Sandbox instance this process belongs to.
            get_root_dir (Callable[[], str]): A function to get the default root directory of the Sandbox.
        """
        self.code_toolbox = code_toolbox
        self.toolbox_api = toolbox_api
        self.instance = instance
        self._get_root_dir = get_root_dir

    @staticmethod
    def _parse_output(lines: List[str]) -> Optional[ExecutionArtifacts]:
        """
        Parse the output of a command to extract ExecutionArtifacts.

        Args:
            lines: A list of lines of output from a command

        Returns:
            ExecutionArtifacts: The artifacts from the command execution
        """
        artifacts = ExecutionArtifacts("", [])
        for line in lines:
            if not line.startswith("dtn_artifact_k39fd2:"):
                artifacts.stdout += line
                artifacts.stdout += "\n"
            else:
                # Remove the prefix and parse JSON
                json_str = line.replace("dtn_artifact_k39fd2:", "", 1).strip()
                data = json.loads(json_str)
                data_type = data.pop("type")

                # Check if this is chart data
                if data_type == "chart":
                    chart_data = data.get("value", {})
                    artifacts.charts.append(parse_chart(**chart_data))

        return artifacts

    @intercept_errors(message_prefix="Failed to execute command: ")
    def exec(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> ExecuteResponse:
        """Execute a shell command in the Sandbox.

        Args:
            command (str): Shell command to execute.
            cwd (Optional[str]): Working directory for command execution. If not
                specified, uses the Sandbox root directory. Default is the user's root directory.
            env (Optional[Dict[str, str]]): Environment variables to set for the command.
            timeout (Optional[int]): Maximum time in seconds to wait for the command
                to complete. 0 means wait indefinitely.

        Returns:
            ExecuteResponse: Command execution results containing:
                - exit_code: The command's exit status
                - result: Standard output from the command
                - artifacts: ExecutionArtifacts object containing `stdout` (same as result)
                and `charts` (matplotlib charts metadata)

        Example:
            ```python
            # Simple command
            response = sandbox.process.exec("echo 'Hello'")
            print(response.artifacts.stdout)  # Prints: Hello

            # Command with working directory
            result = sandbox.process.exec("ls", cwd="workspace/src")

            # Command with timeout
            result = sandbox.process.exec("sleep 10", timeout=5)
            ```
        """
        base64_user_cmd = base64.b64encode(command.encode()).decode()
        command = f"echo '{base64_user_cmd}' | base64 -d | sh"

        if env and len(env.items()) > 0:
            safe_env_exports = (
                ";".join(
                    [
                        f"export {key}=$(echo '{base64.b64encode(value.encode()).decode()}' | base64 -d)"
                        for key, value in env.items()
                    ]
                )
                + ";"
            )
            command = f"{safe_env_exports} {command}"

        command = f'sh -c "{command}"'
        execute_request = ExecuteRequest(
            command=command, cwd=cwd or self._get_root_dir(), timeout=timeout
        )

        response = self.toolbox_api.execute_command(
            workspace_id=self.instance.id, execute_request=execute_request
        )

        # Post-process the output to extract ExecutionArtifacts
        artifacts = Process._parse_output(response.result.splitlines())

        # Create new response with processed output and charts
        # TODO: Remove model_construct once everything is migrated to pydantic # pylint: disable=fixme
        return ExecuteResponse.model_construct(
            exit_code=response.exit_code,
            result=artifacts.stdout,
            artifacts=artifacts,
            additional_properties=response.additional_properties,
        )

    def code_run(
        self,
        code: str,
        params: Optional[CodeRunParams] = None,
        timeout: Optional[int] = None,
    ) -> ExecuteResponse:
        """Executes code in the Sandbox using the appropriate language runtime.

        Args:
            code (str): Code to execute.
            params (Optional[CodeRunParams]): Parameters for code execution.
            timeout (Optional[int]): Maximum time in seconds to wait for the code
                to complete. 0 means wait indefinitely.

        Returns:
            ExecuteResponse: Code execution result containing:
                - exit_code: The execution's exit status
                - result: Standard output from the code
                - artifacts: ExecutionArtifacts object containing `stdout` (same as result)
                and `charts` (matplotlib charts metadata)

        Example:
            ```python
            # Run Python code
            response = sandbox.process.code_run('''
                x = 10
                y = 20
                print(f"Sum: {x + y}")
            ''')
            print(response.artifacts.stdout)  # Prints: Sum: 30
            ```

            Matplotlib charts are automatically detected and returned in the `charts` field
            of the `ExecutionArtifacts` object.
            ```python
            code = '''
            import matplotlib.pyplot as plt
            import numpy as np

            x = np.linspace(0, 10, 30)
            y = np.sin(x)

            plt.figure(figsize=(8, 5))
            plt.plot(x, y, 'b-', linewidth=2)
            plt.title('Line Chart')
            plt.xlabel('X-axis (seconds)')
            plt.ylabel('Y-axis (amplitude)')
            plt.grid(True)
            plt.show()
            '''

            response = sandbox.process.code_run(code)
            chart = response.artifacts.charts[0]

            print(f"Type: {chart.type}")
            print(f"Title: {chart.title}")
            if chart.type == ChartType.LINE and isinstance(chart, LineChart):
                print(f"X Label: {chart.x_label}")
                print(f"Y Label: {chart.y_label}")
                print(f"X Ticks: {chart.x_ticks}")
                print(f"X Tick Labels: {chart.x_tick_labels}")
                print(f"X Scale: {chart.x_scale}")
                print(f"Y Ticks: {chart.y_ticks}")
                print(f"Y Tick Labels: {chart.y_tick_labels}")
                print(f"Y Scale: {chart.y_scale}")
                print("Elements:")
                for element in chart.elements:
                    print(f"\n\tLabel: {element.label}")
                    print(f"\tPoints: {element.points}")
            ```
        """
        command = self.code_toolbox.get_run_command(code, params)
        return self.exec(command, env=params.env if params else None, timeout=timeout)

    @intercept_errors(message_prefix="Failed to create session: ")
    def create_session(self, session_id: str) -> None:
        """Creates a new long-running background session in the Sandbox.

        Sessions are background processes that maintain state between commands, making them ideal for
        scenarios requiring multiple related commands or persistent environment setup. You can run
        long-running commands and monitor process status.

        Args:
            session_id (str): Unique identifier for the new session.

        Example:
            ```python
            # Create a new session
            session_id = "my-session"
            sandbox.process.create_session(session_id)
            session = sandbox.process.get_session(session_id)
            # Do work...
            sandbox.process.delete_session(session_id)
            ```
        """
        request = CreateSessionRequest(sessionId=session_id)
        self.toolbox_api.create_session(
            self.instance.id, create_session_request=request
        )

    @intercept_errors(message_prefix="Failed to get session: ")
    def get_session(self, session_id: str) -> Session:
        """Gets a session in the Sandbox.

        Args:
            session_id (str): Unique identifier of the session to retrieve.

        Returns:
            Session: Session information including:
                - session_id: The session's unique identifier
                - commands: List of commands executed in the session

        Example:
            ```python
            session = sandbox.process.get_session("my-session")
            for cmd in session.commands:
                print(f"Command: {cmd.command}")
            ```
        """
        return self.toolbox_api.get_session(self.instance.id, session_id=session_id)

    @intercept_errors(message_prefix="Failed to get session command: ")
    def get_session_command(self, session_id: str, command_id: str) -> Command:
        """Gets information about a specific command executed in a session.

        Args:
            session_id (str): Unique identifier of the session.
            command_id (str): Unique identifier of the command.

        Returns:
            Command: Command information including:
                - id: The command's unique identifier
                - command: The executed command string
                - exit_code: Command's exit status (if completed)

        Example:
            ```python
            cmd = sandbox.process.get_session_command("my-session", "cmd-123")
            if cmd.exit_code == 0:
                print(f"Command {cmd.command} completed successfully")
            ```
        """
        return self.toolbox_api.get_session_command(
            self.instance.id, session_id=session_id, command_id=command_id
        )

    @intercept_errors(message_prefix="Failed to execute session command: ")
    def execute_session_command(
        self,
        session_id: str,
        req: SessionExecuteRequest,
        timeout: Optional[int] = None,
    ) -> SessionExecuteResponse:
        """Executes a command in the session.

        Args:
            session_id (str): Unique identifier of the session to use.
            req (SessionExecuteRequest): Command execution request containing:
                - command: The command to execute
                - run_async: Whether to execute asynchronously

        Returns:
            SessionExecuteResponse: Command execution results containing:
                - cmd_id: Unique identifier for the executed command
                - output: Command output (if synchronous execution)
                - exit_code: Command exit status (if synchronous execution)

        Example:
            ```python
            # Execute commands in sequence, maintaining state
            session_id = "my-session"

            # Change directory
            req = SessionExecuteRequest(command="cd /workspace")
            sandbox.process.execute_session_command(session_id, req)

            # Create a file
            req = SessionExecuteRequest(command="echo 'Hello' > test.txt")
            sandbox.process.execute_session_command(session_id, req)

            # Read the file
            req = SessionExecuteRequest(command="cat test.txt")
            result = sandbox.process.execute_session_command(session_id, req)
            print(result.output)  # Prints: Hello
            ```
        """
        return self.toolbox_api.execute_session_command(
            self.instance.id,
            session_id=session_id,
            session_execute_request=req,
            _request_timeout=timeout or None,
        )

    @intercept_errors(message_prefix="Failed to get session command logs: ")
    def get_session_command_logs(self, session_id: str, command_id: str) -> str:
        """Get the logs for a command executed in a session. Retrieves the complete output
        (stdout and stderr) from a command executed in a session.

        Args:
            session_id (str): Unique identifier of the session.
            command_id (str): Unique identifier of the command.

        Returns:
            str: Complete command output including both stdout and stderr.

        Example:
            ```python
            logs = sandbox.process.get_session_command_logs(
                "my-session",
                "cmd-123"
            )
            print(f"Command output: {logs}")
            ```
        """
        return self.toolbox_api.get_session_command_logs(
            self.instance.id, session_id=session_id, command_id=command_id
        )

    @intercept_errors(message_prefix="Failed to get session command logs: ")
    async def get_session_command_logs_async(
        self, session_id: str, command_id: str, on_logs: Callable[[str], None]
    ) -> None:
        """Asynchronously retrieves and processes the logs for a command executed in a session as they become available.

        Args:
            session_id (str): Unique identifier of the session.
            command_id (str): Unique identifier of the command.
            on_logs (Callable[[str], None]): Callback function to handle log chunks.

        Example:
            ```python
            await sandbox.process.get_session_command_logs_async(
                "my-session",
                "cmd-123",
                lambda chunk: print(f"Log chunk: {chunk}")
            )
            ```
        """
        url = (
            f"{self.toolbox_api.api_client.configuration.host}/toolbox/{self.instance.id}"
            + f"/toolbox/process/session/{session_id}/command/{command_id}/logs?follow=true"
        )
        headers = self.toolbox_api.api_client.default_headers

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, headers=headers) as response:
                stream = response.aiter_bytes()
                next_chunk = None
                exit_code_seen_count = 0

                while True:
                    if next_chunk is None:
                        next_chunk = asyncio.create_task(anext(stream, None))
                    timeout = asyncio.create_task(asyncio.sleep(2))

                    done, pending = await asyncio.wait(
                        [next_chunk, timeout], return_when=asyncio.FIRST_COMPLETED
                    )

                    if next_chunk in done:
                        timeout.cancel()
                        chunk = next_chunk.result()
                        next_chunk = None

                        if chunk is None:
                            break

                        on_logs(chunk.decode("utf-8"))
                    elif timeout in done:
                        cmd_status = self.get_session_command(session_id, command_id)

                        if cmd_status.exit_code is not None:
                            exit_code_seen_count += 1
                            if exit_code_seen_count > 1:
                                if next_chunk in pending:
                                    next_chunk.cancel()
                                break

    @intercept_errors(message_prefix="Failed to list sessions: ")
    def list_sessions(self) -> List[Session]:
        """Lists all sessions in the Sandbox.

        Returns:
            List[Session]: List of all sessions in the Sandbox.

        Example:
            ```python
            sessions = sandbox.process.list_sessions()
            for session in sessions:
                print(f"Session {session.session_id}:")
                print(f"  Commands: {len(session.commands)}")
            ```
        """
        return self.toolbox_api.list_sessions(self.instance.id)

    @intercept_errors(message_prefix="Failed to delete session: ")
    def delete_session(self, session_id: str) -> None:
        """Terminates and removes a session from the Sandbox, cleaning up any resources
        associated with it.

        Args:
            session_id (str): Unique identifier of the session to delete.

        Example:
            ```python
            # Create and use a session
            sandbox.process.create_session("temp-session")
            # ... use the session ...

            # Clean up when done
            sandbox.process.delete_session("temp-session")
            ```
        """
        self.toolbox_api.delete_session(self.instance.id, session_id=session_id)
