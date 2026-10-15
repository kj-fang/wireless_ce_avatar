import os

from flask import session

from configs.global_configs import app_config


_EVENT_LOG_FILENAMES = {"raweventviewersystemlogs.evt", "system.evtx"}


def find_event_log_for_log(log_path: str) -> str:
    """Return the System Event Log associated with the selected capture log."""
    log_dir = os.path.dirname(os.path.abspath(log_path)) if log_path else ""

    def common_path_length(event_path: str) -> int:
        if not log_dir:
            return -1
        try:
            common = os.path.commonpath(
                [log_dir, os.path.dirname(os.path.abspath(event_path))]
            )
            return len(common)
        except ValueError:
            return -1

    case_context = session.get("case_context", {})
    case_number = (
        case_context.get("case_nbr", "") if isinstance(case_context, dict) else ""
    )
    if case_number:
        results = app_config.get_download_results(case_number)
        download_results = results.get("ddd", {})
        candidates = [
            file_path
            for file_list in download_results.values()
            for file_path in file_list
            if os.path.basename(file_path).lower() in _EVENT_LOG_FILENAMES
            and os.path.isfile(file_path)
        ]
        if candidates:
            best = max(candidates, key=common_path_length)
            if log_dir:
                try:
                    best_common = os.path.commonpath(
                        [log_dir, os.path.dirname(os.path.abspath(best))]
                    )
                    drive_root = os.path.splitdrive(log_dir)[0] + os.sep
                    if os.path.normcase(best_common) != os.path.normcase(drive_root):
                        return best
                except ValueError:
                    pass
            return candidates[0]

    if not log_path or not os.path.isfile(log_path):
        return ""

    search_directories = [
        log_dir,
        os.path.dirname(os.path.dirname(log_dir)),
        os.path.join(log_dir, "Event logs"),
    ]
    for directory in search_directories:
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if filename.lower() in _EVENT_LOG_FILENAMES:
                candidate = os.path.join(directory, filename)
                if os.path.isfile(candidate):
                    return candidate

    return ""