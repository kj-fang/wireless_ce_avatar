"""
>>> WPP parser from ETL file
>>> DDD parser from binaries
how to use?
simple way is to run this script with ETL/DDD file as output
also created BAT file so you can trigger it as right-click on the specific ETL/DDD file
need to take py, bat, tracefmt.exe (from latest wdk), TextAnalysisTool (from google)
and your favorite TAT filers file for TextAnalysisTool.
place all files in remote folder, see INF_SHARE below, which points to my folder
once running, ETL/DDD will be parsed to TXT and automatically opened with TextAnalysisTool
and your desired filters
"""

import re
import argparse
import logging
import os
import sys
import datetime
import shutil
from pathlib import Path
import subprocess
import json
from typing import Tuple
import requests
import urllib3

from configs.global_configs import app_config



def emit_and_log(msg):
    log.info(msg)
    app_config.socketio.emit('wpp_log', {'data': msg}, namespace='/progress') 

# disable some warning of unverified HTTP get requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# pylint: disable=logging-fstring-interpolation
# sorry, but I like this way

logging.basicConfig(format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("WPP_DDD_parser")
log.setLevel(logging.INFO)

# defines
ARTIFACTS_SHARE = "//infs089.iil.intel.com/HOME/rf/filters"
WPP_LOG_FIX_PL = "J:/TicTac3/source/wppLogFix.pl"
LOCAL_ETL_NAME = "WifiDriverIHVSession.etl"
LOCAL_DDD_NAME = "dddLog_0.bin"
OUTPUT_TXT_NAME = "ParsedDriverLog.tmp"
OUTPUT_LOG_NAME = "ParsedDriverLog.log"
DDD_PLAYER_NAME = "DDDPlayer.exe"
LOCAL_TAT_PATH = [
    "c:/logs/my_filter.tat",  # for IT machine use
    "j:/logs/my_filter.tat",  # for server use
    "j:/TicTac3/config/my_filter.tat",  # for TicTac3 use
]

# artifacts to copy from ARTIFACTS_SHARE to local WS
FILES_TO_COPY_ETL_LIST = ["tracefmt.exe", "TextAnalysisTool.NET.exe", "roi.tat"]

FILES_TO_COPY_DDD_LIST = ["TextAnalysisTool.NET.exe", "roi.tat"]

# possible driver paths in PF
DRIVER_PATH_LIST = ["zip_listener_path", "jer_server_path", "dfs_path"]

# PF URLs
POTATO_FARM_SITE_LIST = ["potatofarm.intel.com", "potatofarm-pre.intel.com"]


class CacheManager:
    """
    class responsible for caching artifacts, such as PDB, DDDPlayer locally,
    so the fetching could be from local drive rather than from the network
    """

    def __init__(self, jenkins_build_id: int, os_type: str, file_name: str):
        """
        ctor for the cache manager class
        """
        # cast to str, since appears in dictionaries, better use strings
        self.__jenkins_build_id = str(jenkins_build_id)
        self.__os_type = os_type
        self.__file_name = file_name
        self.__cache_file_name = os.path.join(os.environ["tmp"], "wpp_ddd_parser_cache.json")

    def store_binary_path(self, binary_path: str) -> None:
        """
        stores the binary path in the cache DB (local json file)
        """
        orig_path = self.get_binary_path()
        if orig_path and orig_path == binary_path:
            # binary already exists
            return

        cache_file = {}

        # check cache file exists first
        if os.path.isfile(self.__cache_file_name):
            # open file
            with open(self.__cache_file_name, "r", encoding="utf-8") as f:
                # get data from file
                cache_file = json.load(f)

        # add new entry
        if self.__jenkins_build_id not in cache_file:
            cache_file[self.__jenkins_build_id] = {}

        if self.__os_type not in cache_file[self.__jenkins_build_id]:
            cache_file[self.__jenkins_build_id][self.__os_type] = {}

        # no need to check if exists, since cannot be
        cache_file[self.__jenkins_build_id][self.__os_type][self.__file_name] = binary_path

        with open(self.__cache_file_name, "w", encoding="utf-8") as f:
            # write back
            json.dump(cache_file, f, indent=4)

    def get_binary_path(self) -> str:
        """
        returns the binary path from cache is exists
        if doesn't exist or not reachable, 'None' is returned
        """
        # check cache file exists first
        if os.path.isfile(self.__cache_file_name):
            with open(self.__cache_file_name, "r", encoding="utf-8") as f:
                # read the file
                cache_file = json.load(f)

                try:
                    # check file exists
                    binary_path = cache_file[self.__jenkins_build_id][self.__os_type][self.__file_name]
                    # found in cache - check if really exists in the system
                    if os.path.isfile(binary_path):
                        emit_and_log(f"binary is found in cache {binary_path}")
                        return binary_path
                    # some old traces exist in the cache, however actual binary doesn't exist
                    # from the cache manager perspective, the file doesn't exist
                except KeyError:
                    # not found
                    pass

        # file not found
        return None


class Parser:
    """
    base class responsible for common parsing capabilities
    """

    def __init__(self, binary_path, local_log_file_name, files_to_copy, is_use_custom_filter, is_add_tracefmt_format):
        # create temp workspace
        self.workspace = self.__create_workspace()
        self.local_log_file_name = local_log_file_name
        self.local_log_file_name_log = f"{Path(binary_path).name}.log"
        self.binary_path = binary_path
        self.files_to_copy = files_to_copy
        self.is_use_custom_filter = is_use_custom_filter
        self.is_add_tracefmt_format = is_add_tracefmt_format
        self.pdb_name_list = []
        self.wifi_driver_job_name = "WIFI_DRV"

        # choose relevant file, based on additional formatter
        if self.is_add_tracefmt_format:
            self.parsed_txt_name = OUTPUT_LOG_NAME
        else:
            self.parsed_txt_name = OUTPUT_TXT_NAME

        emit_and_log(f"Base parser is called for {self.binary_path}")
        emit_and_log(f"workspace: {self.workspace}")
        emit_and_log(f"file_name: {self.local_log_file_name}")

    @staticmethod
    def __create_workspace() -> str:
        """
        function creates a new local temp folder to serve as workspace for this parse
        all the artifacts will be placed in this folder
        """
        # generate random folder inside %tmp% - we should have access to write there
        workspace = os.path.join(
            os.environ["tmp"], "log_parser_" + datetime.datetime.now().strftime("%y%m%d_%H%M%S_%f")
        )
        os.mkdir(workspace)
        emit_and_log(f"created new workspace: {workspace}")

        # change dir to WS
        os.chdir(workspace)

        return workspace

    def copy_artifacts_to_local_ws(self) -> None:
        """
        function copies some artifacts listed in FILES_TO_COPY lists to local workspace
        """
        emit_and_log(f"--> copying files from {ARTIFACTS_SHARE}")
        emit_and_log(f"<-- to {self.workspace}")

        for file_to_copy in self.files_to_copy:
            shutil.copyfile(os.path.join(ARTIFACTS_SHARE, file_to_copy), file_to_copy)
            emit_and_log(f"successfully copied {file_to_copy}")

        # copy log file
        shutil.copyfile(self.binary_path, os.path.join(self.workspace, self.local_log_file_name))

    def copy_parsed_log_to_orig_path(self) -> None:
        """
        function tries to copy the parsed log (TXT file) into the original path binary was taken from
        caution: this will not always work due to potential missing write permissions
        """
        # get the original directory
        orig_binary_directory = os.path.dirname(os.path.realpath(self.binary_path))

        # create source file
        src_file = os.path.join(self.workspace, self.parsed_txt_name)

        # create destination file
        dst_file = os.path.join(orig_binary_directory, self.local_log_file_name_log)

        # copy file to original directory
        try:
            emit_and_log("copying parsed log file to original path ..")
            shutil.copyfile(src_file, dst_file)
            # if no exception, so all is good
            emit_and_log(f"{self.local_log_file_name_log} was successfully copied to original path")
        except PermissionError:
            log.warning("there are no permissions to copy parsed file to original path")

    def __send_rest_call_to_pf(self, api_name: str) -> dict:
        """
        function sends REST call to PF web with given API
        function returns the JSON dict provided by PF
        assumption: PF API returns JSON
        """
        # add the token of the sys_wirelessce user
        headers = {"X-Api-Token": "ggmuaUOTsdF7Vjm7Mp4SoKmfG3NV41P8IL0zYqAOghM"}

        # for robustness, try several sites
        for pf_site in POTATO_FARM_SITE_LIST:
            try:
                url = f"https://{pf_site}/{api_name}"
                #print("debuggg!", url)
                res = requests.get(url, proxies={"http": None, "https": None}, verify=False, headers=headers, timeout=60) #proxies={"http": None, "https": None},
                if res:
                    # reply can come in several forms dict or list (of dicts)
                    data = res.json()
                    if "builds" in data:
                        # we have already dict, return it
                        # data has all builds corresponding to build ID (WAPI, MSI, DRIVER, etc..)
                        # need to filter only driver builds
                        wifi_build = [x for x in data["builds"] if x["jenkins_job"] == self.wifi_driver_job_name]
                        # sort by submission date, take the newest
                        wifi_build = sorted(wifi_build, key=lambda d: d["submission_date"], reverse=True)
                        return wifi_build[0]
                    else:
                        return data
                else:
                    log.error("failed to get response from PF - it is possible that API doesn't exist")
                    log.error(url)

                # pylint: disable=bare-except
            except:
                # pylint: enable=bare-except
                # there could be many exceptions, mainly due to temporary networking issues
                log.error(f"Failed to reach PF: {pf_site}")

        # if we got here, we didn't manage to reach PF site to get PDB from
        sys.exit(0)

    def __get_build_details(self, jenkins_build_id: str) -> dict:
        """
        function retrieves build details from PF based on jenkins build ID
        """
        # query PF API to get more details about build ID
        return self.__send_rest_call_to_pf(f"api/external/get_build?jenkins_build_id={jenkins_build_id}&strict")

    def get_latest_nightly(self) -> dict:
        """
        function retrieves latest master nightly details from PF
        """
        return self.__send_rest_call_to_pf("api/external/get_latest_nightly")

    @staticmethod
    def __get_tat_file() -> str:
        """
        return the TAT file to use
        in case tat is found in LOCAL_TAT_PATH use it, if not, use roi.tat
        """
        # check if private file found in one of the paths
        for tat_file in LOCAL_TAT_PATH:
            if os.path.exists(tat_file):
                emit_and_log(f"private TAT file found in {LOCAL_TAT_PATH} - use it")
                return LOCAL_TAT_PATH

        # use default
        emit_and_log(f"no private TAT is found in {LOCAL_TAT_PATH}, use default roi.tat")
        return "roi.tat"

    def open_log_in_text_analysis(self) -> None:
        """
        function opens parsed text in text analysis with specific tat filter
        """
        # get the TAT file to use
        tat_file = ""
        if not self.is_use_custom_filter:
            tat_file = self.__get_tat_file()

        # prepare the command
        cmd = f"TextAnalysisTool.NET.exe {self.parsed_txt_name} /Filters:{tat_file}"

        # run the process async
        emit_and_log("open text analysis")
        subprocess.Popen(cmd)

    def copy_pdb_to_ws(self, jenkins_build_id: int, os_type: str, pdb_name: str) -> None:
        """
        function copies FRE PDB file to workspace
        """
        self.__copy_driver_file_to_ws(jenkins_build_id, os_type, pdb_name, "fre")

    def copy_ddd_player_to_ws(self, jenkins_build_id: int, os_type: str) -> None:
        """
        function copies DDD player file to workspace
        """
        self.__copy_driver_file_to_ws(jenkins_build_id, os_type, DDD_PLAYER_NAME, "ddd_free_logs")

    @staticmethod
    def __get_build_days_ago(build_details: dict) -> int:
        """
        returns the number of days build was compiled
        e.g. 4 days ago (from now)
        assumption: 'submission_date' key exists in build_details
        """
        # format: 2023-01-05T20:00:56+00:00'
        submission_date = build_details["submission_date"]
        # need to remove everything after "T" since we care of date only
        submission_date = re.sub("T.*", "", submission_date)
        datetime_format = "%Y-%m-%d"
        datetime_object = datetime.datetime.strptime(submission_date, datetime_format)

        # calculate the number of days in time delta
        return (datetime.datetime.now() - datetime_object).days

    def __copy_driver_file_to_ws(self, jenkins_build_id: int, os_type: str, file_name: str, comp_type: str) -> None:
        """
        function find the relevant driver file and copies to workspace
        flow:
        >>> query PF with this ID to get the build paths
        >>> copy the relevant file to local WS
        """
        # get build details from PF server
        res = self.__get_build_details(jenkins_build_id)

        # print the build date
        built_days_ago = self.__get_build_days_ago(res)
        drv_branch = res["drv_branch"]
        emit_and_log(f"drv_branch: {drv_branch} [{built_days_ago} days ago]")

        # try to get the file from cache
        file_path = None
        cache_manager = CacheManager(jenkins_build_id, os_type, file_name)
        file_path = cache_manager.get_binary_path()

        # get the file, in case not found in cache
        if not file_path:
            # try to get the driver file from any of the paths below
            file_dir_path = ""

            for driver_path in DRIVER_PATH_LIST:
                if driver_path in res:
                    try:
                        if os.path.isdir(res[driver_path]):
                            file_dir_path = res[driver_path]
                            break
                    except TypeError:
                        # path not found
                        pass

            if file_dir_path:
                # driver and file path found
                emit_and_log(f"driver path found: {file_dir_path}")

                # possible path list in glob format
                glob_path_list = [
                    f"Driver/{comp_type}/{os_type.upper()}/**/{file_name}",  # standard
                    f"PDB_PRV/**/{file_name}",  # attestation
                ]

                # look for the file in the path
                for file_path_glob in glob_path_list:
                    for file_file in Path(file_dir_path).rglob(file_path_glob):
                        if os.path.isfile(file_file):
                            file_path = file_file
                            # exit inner loop
                            break
                    # exit outer loop
                    if file_path:
                        break
            else:
                log.warning(f"failed to find {file_name} inside PF folders - probably was purged")
                # try to see if orig folder has file
                local_file_name = os.path.join(os.path.dirname(self.binary_path), file_name)
                if os.path.isfile(local_file_name):
                    # found local file --> good, use it
                    emit_and_log(f"found local file to use: {local_file_name}")
                    # override file path dir
                    file_path = local_file_name
                else:
                    log.error("failed to find build path - file could not be extracted")
                    sys.exit(0)

            if not file_path:
                log.error(f"failed to find file in {file_path_glob}")
                sys.exit(0)

        # directory cannot have multiple PDBs with same name, therefore, add _build suffix
        local_pdb_name = file_name.replace(".pdb", f"_{jenkins_build_id}.pdb")

        # copy file to local workspace
        emit_and_log(f"{file_name} was found. Copying .. this might take several seconds")

        shutil.copyfile(str(file_path), local_pdb_name)
        emit_and_log("file was successfully copied to local workspace")

        # store in cache
        self.pdb_name_list.append(local_pdb_name)
        cache_manager.store_binary_path(os.path.join(self.workspace, local_pdb_name))

        del cache_manager

    def open_workspace_window(self) -> None:
        """
        function opens workspace folder
        """
        subprocess.Popen(f'explorer "{self.workspace}"')

    def run_external_formatter(self) -> None:
        """
        Stas will never get rid of perl
        """
        if self.is_add_tracefmt_format:
            os.system(f"perl {WPP_LOG_FIX_PL} {OUTPUT_TXT_NAME}")


class WppParser(Parser):
    """
    WPP parser
    """

    def __init__(self, binary_path, is_use_custom_filter, is_add_tracefmt_format):
        super().__init__(
            binary_path, LOCAL_ETL_NAME, FILES_TO_COPY_ETL_LIST, is_use_custom_filter, is_add_tracefmt_format
        )

        # local PDB if exists (needed for cases PDB is not found)
        self.__local_pdb_path = self.__get_local_pdb_path()

        emit_and_log("WPP parser is called")

    def __get_local_pdb_path(self) -> str:
        """
        returns the path for local PDB if exists
        if no, "" is returned
        """
        # pick the first one if exists (non recursive search - PDB must be in the root)
        for pdb_file in Path(self.binary_path).parent.rglob("*.pdb"):
            return str(pdb_file)

        return ""

    def __get_tracefmt_format(self) -> str:
        """
        returns the tracefmt format
        """
        if self.is_add_tracefmt_format:
            return "%4!s! [%1!s!] [%!FLAGS!] [%!LEVEL!] [%9!d!]%8!04x! [%!FUNC!] %2!s!:"
        else:
            return "%4!s! [%9!d!] [%!FLAGS!] [%!LEVEL!] [%!FUNC!]:"

    def parse_log_file(self) -> None:
        """
        function is responsible to parse the ETL file into TXT
        this is done using tracefmt.exe, part of WDK
        """
        # set ENV variable for tracefmt formatting
        os.environ["TRACE_FORMAT_PREFIX"] = self.__get_tracefmt_format()

        # generate local PDB list with ;
        pdbs_str = ";".join(self.pdb_name_list)

        # prepare the command
        cmd = f"tracefmt.exe {self.local_log_file_name} -o {OUTPUT_TXT_NAME} -nosummary -pdb {pdbs_str}"

        # run the process sync
        emit_and_log("run tracefmt to parse the ETL")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL)
        proc.wait()

        # verify output
        if os.path.isfile(OUTPUT_TXT_NAME):
            emit_and_log(f"ETL was successfully parsed into: {OUTPUT_TXT_NAME}")
        else:
            log.Error("failed to parse ETL file")
            sys.exit(0)

    def __get_build_id_and_os_type(self) -> list[tuple[int, str, str]]:
        """
        function parses ETL file and extracts some parameters from it
        returns jenkins build ID, OS type, PDB name
        """
        # list of tuples (jenkins_build_id, os_name, pdb_name)
        builds_db = []

        with open(self.local_log_file_name, "rb") as etl_file:
            # convert binary to string
            file_str = str(etl_file.read())

            # get first occurrence
            builds = [i.start() for i in re.finditer("Jenkins", file_str)]
            # walk over all indices (there can be only one though) and update the variables with highest build ID
            for start_index in builds:
                # init vars
                jenkins_build_id = 0
                os_type = ""
                pdb_name = ""

                # find some substring stating with Jenkins
                substring = file_str[start_index : (start_index + 200)]

                # C:\Jenkins\workspace\windows-wifi-driver\WIFI_DRV\104479\Source_Full\drv\
                # win_driver\Win_Driver\Miniport\WinT\obj_rel_winTx64\Netwtw10.pdb
                if match := re.findall(r"Jenkins.*\\(\d+).*(Win\w+).*\\(Netw\ww\d{2}\.pdb)", substring):
                    jenkins_build_id = int(match[0][0])
                    os_type = match[0][1]
                    pdb_name = match[0][2]
                    builds_db.append((jenkins_build_id, os_type, pdb_name))
                else:
                    log.error(f"build regex doesn't match: {substring}")

        # either one of them should exists (local PDB or build which is found)
        assert builds_db or self.__local_pdb_path, "failed to find PDBs in ETL"
        emit_and_log(f"found {len(builds_db)} PDBs: {builds_db}, local PDB path: {self.__local_pdb_path}")

        return builds_db

    def copy_parsing_artifacts(self) -> None:
        """
        function copies PDB file to local workspace
        """
        # extract the build ID and OS type from ETL
        builds_db = self.__get_build_id_and_os_type()

        if builds_db:
            # copy PDBs from all local builds
            for build in builds_db:
                jenkins_build_id, os_type, pdb_name = build
                super().copy_pdb_to_ws(jenkins_build_id, os_type, pdb_name)
        elif self.__local_pdb_path:
            local_pdb_name = Path(self.__local_pdb_path).name
            shutil.copyfile(str(self.__local_pdb_path), local_pdb_name)
            emit_and_log("local PDB was successfully copied to local workspace")

            # store in cache
            self.pdb_name_list.append(local_pdb_name)


class DddParser(Parser):
    """
    DDD parser
    """

    def __init__(self, binary_path, is_use_custom_filter, is_add_tracefmt_format):
        super().__init__(
            binary_path, LOCAL_DDD_NAME, FILES_TO_COPY_DDD_LIST, is_use_custom_filter, is_add_tracefmt_format
        )

        self.__build_id_offset = 222
        self.__build_id_length = 10
        self.__is_net_adapter_offset = 232
        self.__is_net_adapter_length = 1
        emit_and_log("DDD parser is called")

    def __get_build_details_from_ddd_bin_file(self) -> Tuple[int, str]:
        """
        read the DDD binary as raw buffer and extract the build ID and OS data
        caution: in case build ID ends with zero, function will omit it
        """
        # generate local path
        local_binary_path = os.path.join(self.workspace, self.local_log_file_name)

        # read the binary and extract build ID and OS type
        with open(local_binary_path, "rb") as f:
            f.seek(self.__build_id_offset)
            build_id_bin = f.read(self.__build_id_length)
            f.seek(self.__is_net_adapter_offset)
            is_net_adapter_bin = f.read(self.__is_net_adapter_length)

        # need to convert the binaries
        # b'18751488\x00\x00'
        if match := re.findall(r"\d+", build_id_bin.decode("ascii")):
            build_id = int(match[0])
        else:
            log.error("failed to find build ID")
            # cannot proceed
            sys.exit(0)

        # b'\x01' or b'\x00'
        is_net_adapter = bool(int.from_bytes(is_net_adapter_bin, "big"))

        os_type = "WinA" if is_net_adapter else "WinT"

        return (build_id, os_type)

    def __get_build_details_using_dddplayer(self) -> Tuple[int, str]:
        """
        function parses DDD bin file and extracts the jenkins build ID and OS type
        for this, need to run latest DDD player with -info flag
        flow:
        >>> get latest nightly build, extract build ID
        >>> fetch latest DDD player, to extract the exact sha1
        >>> get the right DDD player and parse the binary
        """
        # get latest nightly build details from PF server
        res = self.get_latest_nightly()

        # copy latest DDD player to workspace
        self.copy_ddd_player_to_ws(res["jenkins_build_id"], "WinT")

        # run the DDD player on the binary to extract the actual sha1 and OS type
        # it is OK to run the 'info' command on winA using winT DDD player
        # id is 0, since when binary is copied, it is renamed to be 0
        cmd = f"{DDD_PLAYER_NAME} -bin . -id 0 -info"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        res = proc.stdout.read().decode("utf-8")

        build_id = 0
        pattern = r"Build ID: (\d+)"
        if match := re.findall(pattern, res):
            build_id = int(match[0])
        else:
            log.error(f"failed to find pattern '{pattern}' in output")
            log.error(f"DDD player command: {cmd}")
            log.error(res)
            return (0, "")

        os_type = "WinT"
        pattern = r"Is NetAdapter: (\d)"
        if match := re.findall(pattern, res):
            if bool(int(match[0])):
                os_type = "WinA"
        else:
            log.error(f"failed to find pattern '{pattern}' in output")
            log.error(f"DDD player command: {cmd}")
            log.error(res)
            return (0, "")

        # all good
        emit_and_log(f"build ID: {build_id}")
        emit_and_log(f"OS type: {os_type}")
        return (build_id, os_type)

    def __get_build_id_and_os_type(self):
        """
        get the build ID and OS type from the DDD binary
        two different approaches:
        >>> [1] try to extract using latest master DDD player
        >>> [2] (if #1 doesn't work) read the DDD binary and extract from there
        """
        # try to extract using latest master DDD player
        emit_and_log("get the build ID and OS type using latest master DDD player")
        build_id, os_type = self.__get_build_details_using_dddplayer()

        # sanity check
        if build_id and os_type:
            return (build_id, os_type)

        # didn't work - try open the DDD binary and look there
        # (might not work for old binaries having different meta)
        emit_and_log("get the build ID and OS type directly from the binary")
        return self.__get_build_details_from_ddd_bin_file()

    def copy_parsing_artifacts(self) -> None:
        """
        function copies DDD player to local workspace
        """
        # extract the build ID and OS type from DDD binary
        emit_and_log("get latest master DDD player to extract sha1 from binary")
        jenkins_build_id, os_type = self.__get_build_id_and_os_type()

        # copy DDD player
        emit_and_log("copy the real DDD player now and parse the binary")
        super().copy_ddd_player_to_ws(jenkins_build_id, os_type)

    def parse_log_file(self) -> None:
        """
        function parses DDD binary to TXT by running DDDPlayer.exe
        """
        cmd = f"{DDD_PLAYER_NAME} -bin . -id 0 -l FFFF07 -o ."
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        res = proc.stdout.read().decode("utf-8")

        # verify DDD was played successfully
        if "exit with success" in res:
            emit_and_log("DDD was played successfully")
        elif "DDD logs didn't record the halt flow" in res:
            emit_and_log("halt flow was not recorded, DDD was cut in the middle")
        else:
            log.error("failed to play DDD")
            sys.exit(0)

        # rename file to output, since DDD player adds some suffix to file name
        res = Path(self.workspace).glob("*.LOG")
        # take the first file - there should be only one
        log_file = str(list(res)[0])
        os.rename(log_file, OUTPUT_TXT_NAME)

        emit_and_log(f"successfully created log file {OUTPUT_TXT_NAME}")


# dictionary of file names and parser handlers
# the relevant class is called based on regex match on the log file name
PARSERS_DICT = {
    r"dddLog_\d+\.bin": DddParser,  # DDD binaries
    r"WifiDriver.*\.etl": WppParser,  # WPP ETL file
}


def parse_single_binary(parser: object) -> None:
    """
    parse single binary which is DDD or driver WPP (ETL)
    gets parser class
    """
    # copy artifacts from artifacts share to local workspace
    parser.copy_artifacts_to_local_ws()

    # get the parsing artifacts needed to parse the binary
    parser.copy_parsing_artifacts()

    # parse the binary file into TXT
    parser.parse_log_file()

    # run another formatter over the parsed file
    parser.run_external_formatter()

    # open text analysis
    parser.open_log_in_text_analysis()

    # open window with parsed logs inside
    parser.open_workspace_window()

    # copy the parsed file to the original directory (if possible)
    parser.copy_parsed_log_to_orig_path()


def wpp_ddd_parser_run(binary_path: str, is_use_custom_filter=False, is_add_tracefmt_format=False) -> None:
    """
    entry point for main parser script
    """
    binaries_file_list = []

    # create list of all potential files to parse
    if os.path.isfile(binary_path):
        binaries_file_list.append(binary_path)
    elif os.path.isdir(binary_path):
        # in case of a directory, look for all relevant files recursively
        for file in Path(binary_path).rglob("*"):
            binaries_file_list.append(str(file))

    # iterate over the list of files and parse each one of them
    for binary_file in binaries_file_list:
        # classification - find handler class
        for key, handler in PARSERS_DICT.items():
            if bool(re.search(key, binary_file)):
                if os.path.exists(binary_file):
                    # when same ETL file with .log extension might exist in the folder
                    # (due to former execution of this script)
                    # make sure to ignore it
                    if ".log" not in Path(binary_file).suffixes:
                        parser = handler(binary_file, is_use_custom_filter, is_add_tracefmt_format)
                        # handler class is found - run the parser
                        parse_single_binary(parser)
                        # go to next binary, rather to next parser
                        break
    log.info('wpp_complete')
    app_config.socketio.emit('wpp_complete', {'status': 'done'}, namespace='/progress') 

"""if __name__ == "__main__":
    g_parser = argparse.ArgumentParser(description="Log parser")
    g_parser.add_argument("-p", "--binary-path", type=str, required=True, help="path to the log file")
    params = g_parser.parse_args()

    emit_and_log(params)

    main(params.binary_path)

    emit_and_log("parse is over. Yang, Yang11")"""
