from __future__ import annotations

import logging
import os
import shutil
import signal
from contextlib import suppress
from filecmp import dircmp
from pathlib import Path
from typing import Sequence

import yaml
from airflow.compat.functools import cached_property
from airflow.exceptions import AirflowException, AirflowSkipException
from airflow.hooks.subprocess import SubprocessHook, SubprocessResult
from airflow.models.baseoperator import BaseOperator
from airflow.utils.context import Context
from airflow.utils.operator_helpers import context_to_airflow_vars
from filelock import FileLock, Timeout

from cosmos.providers.dbt.constants import DBT_PROFILE_PATH
from cosmos.providers.dbt.core.utils.file_syncing import exclude, has_differences
from cosmos.providers.dbt.core.utils.profiles_generator import (
    create_default_profiles,
    map_profile,
)

logger = logging.getLogger(__name__)


class DbtBaseOperator(BaseOperator):
    """
    Executes a dbt core cli command.

    :param project_dir: Which directory to look in for the dbt_project.yml file. Default is the current working
    directory and its parents.
    :param conn_id: The airflow connection to use as the target
    :param base_cmd: dbt sub-command to run (i.e ls, seed, run, test, etc.)
    :param select: dbt optional argument that specifies which nodes to include.
    :param exclude: dbt optional argument that specifies which models to exclude.
    :param selector: dbt optional argument - the selector name to use, as defined in selectors.yml
    :param vars: dbt optional argument - Supply variables to the project. This argument overrides variables
        defined in your dbt_project.yml file. This argument should be a YAML
        string, eg. '{my_variable: my_value}' (templated)
    :param models: dbt optional argument that specifies which nodes to include.
    :param cache_selected_only:
    :param no_version_check: dbt optional argument - If set, skip ensuring dbt's version matches the one specified in
        the dbt_project.yml file ('require-dbt-version')
    :param fail_fast: dbt optional argument to make dbt exit immediately if a single resource fails to build.
    :param quiet: dbt optional argument to show only error logs in stdout
    :param warn_error: dbt optional argument to convert dbt warnings into errors
    :param db_name: override the target db instead of the one supplied in the airflow connection
    :param schema: override the target schema instead of the one supplied in the airflow connection
    :param env: If env is not None, it must be a dict that defines the
        environment variables for the new process; these are used instead
        of inheriting the current process environment, which is the default
        behavior. (templated)
    :param append_env: If False(default) uses the environment variables passed in env params
        and does not inherit the current process environment. If True, inherits the environment variables
        from current passes and then environment variable passed by the user will either update the existing
        inherited environment variables or the new variables gets appended to it
    :param output_encoding: Output encoding of bash command
    :param skip_exit_code: If task exits with this exit code, leave the task
        in ``skipped`` state (default: 99). If set to ``None``, any non-zero
        exit code will be treated as a failure.
    :param cancel_query_on_kill: If true, then cancel any running queries when the task's on_kill() is executed.
        Otherwise, the query will keep running when the task is killed.
    :param dbt_executable_path: Path to dbt executable can be used with venv
        (i.e. /home/astro/.pyenv/versions/dbt_venv/bin/dbt)
    """

    template_fields: Sequence[str] = ("env", "vars")
    global_flags = (
        "project_dir",
        "select",
        "exclude",
        "selector",
        "vars",
        "models",
    )
    global_boolean_flags = (
        "no_version_check",
        "cache_selected_only",
        "fail_fast",
        "quiet",
        "warn_error",
    )

    def __init__(
        self,
        project_dir: str,
        conn_id: str,
        base_cmd: str | list[str] = None,
        select: str = None,
        exclude: str = None,
        selector: str = None,
        vars: dict = None,
        models: str = None,
        cache_selected_only: bool = False,
        no_version_check: bool = False,
        fail_fast: bool = False,
        quiet: bool = False,
        warn_error: bool = False,
        db_name: str = None,
        schema: str = None,
        env: dict = None,
        append_env: bool = False,
        output_encoding: str = "utf-8",
        skip_exit_code: int = 99,
        cancel_query_on_kill: bool = True,
        dbt_executable_path: str = "dbt",
        **kwargs,
    ) -> None:
        self.project_dir = project_dir
        self.conn_id = conn_id
        self.base_cmd = base_cmd
        self.select = select
        self.exclude = exclude
        self.selector = selector
        self.vars = vars
        self.models = models
        self.cache_selected_only = cache_selected_only
        self.no_version_check = no_version_check
        self.fail_fast = fail_fast
        self.quiet = quiet
        self.warn_error = warn_error
        self.db_name = db_name
        self.schema = schema
        self.env = env
        self.append_env = append_env
        self.output_encoding = output_encoding
        self.skip_exit_code = skip_exit_code
        self.cancel_query_on_kill = cancel_query_on_kill
        # dbt-ol is the OpenLineage wrapper for dbt, which automatically
        # generates and emits lineage data to a specified backend.
        dbt_ol_path = shutil.which("dbt-ol")
        if dbt_executable_path == "dbt" and shutil.which("dbt-ol"):
            self.dbt_executable_path = dbt_ol_path
        else:
            self.dbt_executable_path = dbt_executable_path
        self.tmp_path = Path("/tmp/dbt")
        super().__init__(**kwargs)

    @cached_property
    def subprocess_hook(self):
        """Returns hook for running the bash command."""
        return SubprocessHook()

    def get_env(self, context: Context, profile_vars: dict[str, str]) -> dict[str, str]:
        """
        Builds the set of environment variables to be exposed for the bash command.
        The order of determination is:
            1. Environment variables created for dbt profiles, `profile_vars`.
            2. The Airflow context as environment variables.
            3. System environment variables if dbt_args{"append_env": True}
            4. User specified environment variables, through dbt_args{"vars": {"key": "val"}}
        If a user accidentally uses a key that is found earlier in the determination order then it is overwritten.
        """
        system_env = os.environ.copy()
        env = self.env
        if env is None:
            env = system_env
        elif self.append_env:
            system_env.update(env)
            env = system_env
        airflow_context_vars = context_to_airflow_vars(context, in_env_var_format=True)
        self.log.debug(
            "Exporting the following env vars:\n%s",
            "\n".join(f"{k}={v}" for k, v in airflow_context_vars.items()),
        )
        combined_env = {**env, **airflow_context_vars, **profile_vars}
        # Eventually the keys & values in the env dict get passed through os.fsencode which enforces this.
        accepted_types = (str, bytes, os.PathLike)
        filtered_env = {
            k: v
            for k, v in combined_env.items()
            if all((isinstance(k, accepted_types), isinstance(v, accepted_types)))
        }

        return filtered_env

    def exception_handling(self, result: SubprocessResult):
        if self.skip_exit_code is not None and result.exit_code == self.skip_exit_code:
            raise AirflowSkipException(
                f"dbt command returned exit code {self.skip_exit_code}. Skipping."
            )
        elif result.exit_code != 0:
            raise AirflowException(
                f"dbt command failed. The command returned a non-zero exit code {result.exit_code}."
            )

    def add_global_flags(self) -> list[str]:
        flags = []
        for global_flag in self.global_flags:
            dbt_name = f"--{global_flag.replace('_', '-')}"
            # intercept project directory and route it to r/w tmp
            if global_flag == "project_dir":
                global_flag_value = (
                    f"/tmp/dbt/{os.path.basename(self.__getattribute__(global_flag))}"
                )
            else:
                global_flag_value = self.__getattribute__(global_flag)
            if global_flag_value is not None:
                if isinstance(global_flag_value, dict):
                    yaml_string = yaml.dump(global_flag_value)
                    flags.extend([dbt_name, yaml_string])
                else:
                    flags.extend([dbt_name, str(global_flag_value)])
        for global_boolean_flag in self.global_boolean_flags:
            if self.__getattribute__(global_boolean_flag):
                flags.append(f"--{global_boolean_flag.replace('_', '-')}")
        return flags

    def sync_temp_project(self) -> Path:
        """Keeps a synchronised copy of the dbt project in a temporary location for read/write operations.

        The order of events is:
            1. Check if the dbt project exists and is a directory.
            2. Create the temporary project top level path if it doesn't exist.
            3. Acquire a lock on the top level of the directory.
            4. Compare the contents to where the files are deployed with some omissions.
            5. If there are differences then delete everything out, with some omissions, and create everything.
            6. Release the lock.

        :raises:
            AirflowException: If the dbt project cannot be found or the dbt project is not a directory.
        :return: The temporary dbt project path
        """
        source_path = Path(
            self.project_dir
        )  # Path will throw a TypeError if None is passed in
        if not source_path.exists():
            raise AirflowException(f"Can not find the project_dir: {str(source_path)}")
        if not source_path.is_dir():
            raise AirflowException(
                f"The project_dir {self.project_dir} must be a directory"
            )
        target_path = self.tmp_path.joinpath(os.path.basename(source_path))
        if not target_path.exists():
            target_path.mkdir(parents=True, exist_ok=True)
        lock = FileLock(target_path / ".lock", timeout=10)
        with suppress(Timeout):
            lock.acquire(timeout=15)
            comparison = dircmp(
                source_path, target_path, ignore=["logs", "target", ".lock"]
            )
            if has_differences(comparison):
                logging.info(
                    f"Changes detected - copying {str(source_path)} to {str(target_path)}"
                )
                for dir_object in os.listdir(target_path):
                    dir_object: str
                    child_path = target_path.joinpath(dir_object)
                    if child_path.is_dir():
                        if dir_object not in ["logs", "target"]:
                            shutil.rmtree(child_path)
                    elif dir_object not in [".lock"]:
                        os.remove(child_path)
                shutil.copytree(
                    source_path, target_path, ignore=exclude, dirs_exist_ok=True
                )
            else:
                logging.info(
                    f"No differences detected between {str(source_path)} and {str(target_path)}"
                )
            lock.release(force=True)
        return target_path

    def run_command(
        self,
        cmd: list[str],
        env: dict[str, str],
        dbt_project_path: Path,
    ) -> SubprocessResult:
        # Fix the profile path, so it's not accidentally superseded by the end user.
        env["DBT_PROFILES_DIR"] = DBT_PROFILE_PATH.parent
        # run bash command
        result = self.subprocess_hook.run_command(
            command=cmd,
            env=env,
            output_encoding=self.output_encoding,
            cwd=str(dbt_project_path),
        )
        self.exception_handling(result)
        return result

    def build_and_run_cmd(
        self, context: Context, cmd_flags: list[str] | None = None
    ) -> SubprocessResult:
        create_default_profiles(DBT_PROFILE_PATH)
        profile, profile_vars = map_profile(
            conn_id=self.conn_id, db_override=self.db_name, schema_override=self.schema
        )
        dbt_cmd = [self.dbt_executable_path]
        if isinstance(self.base_cmd, str):
            dbt_cmd.append(self.base_cmd)
        else:
            dbt_cmd.extend(self.base_cmd)
        dbt_cmd.extend(self.add_global_flags())
        # add command specific flags
        if cmd_flags:
            dbt_cmd.extend(cmd_flags)
        # add profile
        dbt_cmd.extend(["--profile", profile])
        # set env vars
        env = self.get_env(context, profile_vars)
        dbt_project_path = self.sync_temp_project()

        return self.run_command(dbt_cmd, env, dbt_project_path)

    def execute(self, context: Context) -> str:
        # TODO is this going to put loads of unnecessary stuff in to xcom?
        return self.build_and_run_cmd(context=context).output

    def on_kill(self) -> None:
        if self.cancel_query_on_kill:
            self.subprocess_hook.log.info("Sending SIGINT signal to process group")
            if self.subprocess_hook.sub_process and hasattr(
                self.subprocess_hook.sub_process, "pid"
            ):
                os.killpg(
                    os.getpgid(self.subprocess_hook.sub_process.pid), signal.SIGINT
                )
        else:
            self.subprocess_hook.send_sigterm()


class DbtLSOperator(DbtBaseOperator):
    """
    Executes a dbt core ls command.
    """

    ui_color = "#DBCDF6"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_cmd = "ls"

    def execute(self, context: Context):
        result = self.build_and_run_cmd(context=context)
        return result.output


class DbtSeedOperator(DbtBaseOperator):
    """
    Executes a dbt core seed command.

    :param full_refresh: dbt optional arg - dbt will treat incremental models as table models
    """

    ui_color = "#F58D7E"

    def __init__(self, full_refresh: bool = False, **kwargs) -> None:
        self.full_refresh = full_refresh
        super().__init__(**kwargs)
        self.base_cmd = "seed"

    def add_cmd_flags(self):
        flags = []
        if self.full_refresh is True:
            flags.append("--full-refresh")

        return flags

    def execute(self, context: Context):
        cmd_flags = self.add_cmd_flags()
        result = self.build_and_run_cmd(context=context, cmd_flags=cmd_flags)
        return result.output


class DbtSnapshotOperator(DbtBaseOperator):
    """
    Executes a dbt core snapshot command.

    """

    ui_color = "#964B00"

    def __init__(self, full_refresh: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_cmd = "snapshot"

    def execute(self, context: Context):
        result = self.build_and_run_cmd(context=context)
        return result.output


class DbtRunOperator(DbtBaseOperator):
    """
    Executes a dbt core run command.
    """

    ui_color = "#7352BA"
    ui_fgcolor = "#F4F2FC"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_cmd = "run"

    def execute(self, context: Context):
        result = self.build_and_run_cmd(context=context)
        return result.output


class DbtTestOperator(DbtBaseOperator):
    """
    Executes a dbt core test command.
    """

    ui_color = "#8194E0"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_cmd = "test"

    def execute(self, context: Context):
        result = self.build_and_run_cmd(context=context)
        return result.output


class DbtRunOperationOperator(DbtBaseOperator):
    """
    Executes a dbt core run-operation command.

    :param macro_name: name of macro to execute
    :param args: Supply arguments to the macro. This dictionary will be mapped to the keyword arguments defined in the
        selected macro.
    """

    ui_color = "#8194E0"
    template_fields: Sequence[str] = "args"

    def __init__(self, macro_name: str, args: dict = None, **kwargs) -> None:
        self.macro_name = macro_name
        self.args = args
        super().__init__(**kwargs)
        self.base_cmd = ["run-operation", macro_name]

    def add_cmd_flags(self):
        flags = []
        if self.args is not None:
            flags.append("--args")
            flags.append(yaml.dump(self.args))
        return flags

    def execute(self, context: Context):
        cmd_flags = self.add_cmd_flags()
        result = self.build_and_run_cmd(context=context, cmd_flags=cmd_flags)
        return result.output


class DbtDepsOperator(DbtBaseOperator):
    """
    Executes a dbt core deps command.
    """

    ui_color = "#8194E0"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.base_cmd = "deps"

    def execute(self, context: Context):
        result = self.build_and_run_cmd(context=context)
        return result.output
