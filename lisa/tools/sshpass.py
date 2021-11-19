# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from pathlib import PurePosixPath
from typing import List, Type

from assertpy.assertpy import assert_that

from lisa.base_tools.wget import Wget
from lisa.executable import Tool
from lisa.operating_system import Posix
from lisa.tools.make import Make
from lisa.tools.tar import Tar


class Sshpass(Tool):
    @property
    def command(self) -> str:
        return "sshpass"

    @property
    def can_install(self) -> bool:
        return True

    @property
    def dependencies(self) -> List[Type[Tool]]:
        return [Make]

    def _install(self) -> bool:
        assert isinstance(self.node.os, Posix)

        # download sshpass 1.06 source code
        download_file_path = self.node.tools[Wget].get(  # noqa: E501
            url="https://sourceforge.net/projects/sshpass/files/sshpass/1.06/sshpass-1.06.tar.gz",  # noqa: E501
            file_path="~/",
            filename="sshpass.tar.gz",
        )
        download_folder_path = PurePosixPath(download_file_path).parent

        # extract sshpass source code
        self.node.tools[Tar].extract(download_file_path, str(download_folder_path))
        source_code_folder_path = download_folder_path.joinpath("sshpass-1.06")

        # build sshpass
        self.node.execute(
            "./configure --prefix=/usr/",
            cwd=source_code_folder_path,
            shell=True,
        )
        self.node.execute("make install", cwd=source_code_folder_path, sudo=True)
        return self._check_exists()

    def copy(
        self,
        source_path: str,
        target_path: str,
        target_ip: str,
        target_username: str,
        target_password: str,
        target_port: int = 22,
    ) -> None:
        # copy file to a network location
        result = self.run(
            f"-p {target_password} scp  "
            "-o 'StrictHostKeyChecking no' "
            f"-P{target_port} {source_path} "
            f"{target_username}@{target_ip}:{target_path}",
            shell=True,
        )
        assert_that(
            result.exit_code,
            "Unable to copy file to target location "
            f"{target_ip}/{target_port}:{target_path} {result}",
        ).is_equal_to(0)
