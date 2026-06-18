import zipfile
import os
import rarfile
import py7zr
import shutil
import subprocess
import tempfile
import traceback
import threading

from utils.helpers import to_long_path


class ExtractionCancelled(Exception):
    """Raised by extract_archive / unzip_file / process_single_zip when the
    caller-supplied cancel_event is set. Callers should treat it as a
    user-initiated cancellation, not a real error."""

def find_compressed_files(directory):
    """Find all compressed files (.zip, .rar, .7z) in directory recursively."""
    compressed_files = []
    for dirpath, _, filenames in os.walk(directory):
        for filename in filenames:
            if filename.lower().endswith(('.zip', '.rar', '.7z')):
                compressed_files.append(os.path.join(dirpath, filename))
    return compressed_files


def filter_files(type, etl_files):
    filtered_tiles = []

    if type == "wifi":
        for file in etl_files:
            if 'wifi' in os.path.basename(file).lower() and 'history' not in file.lower() and '.etl.' in file.lower():
                filtered_tiles.append(file)

    elif type == "bt":
        for file in etl_files:
            if os.path.basename(file).lower().startswith(('ibtusb', 'ibtpci')) and os.path.basename(file).lower().endswith('.etl'):
                filtered_tiles.append(file)

    elif type == "fw":
        for file in etl_files:
            if os.path.basename(file).lower().startswith('wrt-fw'):
                filtered_tiles.append(file)
    
    return filtered_tiles


def list_etl_files(root_folder):
    etl_files = []
    for dirpath, _, filenames in os.walk(root_folder):
        for filename in filenames:
            if '.etl' in filename.lower():
                etl_files.append(os.path.join(dirpath, filename))
    return etl_files


def dedup_by_capture_signature(file_paths):
    """Drop duplicate files that belong to the SAME autologger capture.

    A redundantly-packaged upload often contains the same autologger run
    twice — e.g. an already-extracted folder AND its own ``.zip`` — and the
    recursive extractor then re-extracts the zip to a flat location, so the
    same ``WifiDriverIHVSession.etl.004`` ends up at two paths:

        .../Could not connect issue/LUS-..._wrt_.../LUS-..._12-26-53_/WifiDriverIHVSession.etl.004
        .../Could_not_connect_issue/LUS-..._12-26-53_/WifiDriverIHVSession.etl.004

    Both are byte-identical — the same capture. We collapse them to ONE so
    the picker shows a single row.

    Signature = (immediate parent folder name, file basename). The parent
    folder carries the autologger's precise timestamp + address
    (``LUS-..._DD-MM-YYYY_HH-MM-SS_<ms>_<addr...>``), so two genuinely
    different captures keep distinct signatures and are NEVER merged.

    Within a duplicate group we prefer the copy that already has a decoded
    ``<file>.log`` sibling (saves the chatbot a re-decode); otherwise the
    first-seen path wins, preserving the original ordering.

    NOTE: this only de-duplicates the RESULT LIST. Every file is still
    extracted to disk, so nothing the customer uploaded is lost — the other
    copy's siblings (evt, system_info, pcapng, …) all remain available.
    """
    chosen = {}        # signature -> chosen path
    order = []         # first-seen signature order
    for p in file_paths:
        parent = os.path.basename(os.path.dirname(p))
        base = os.path.basename(p)
        sig = (parent, base)
        if sig not in chosen:
            chosen[sig] = p
            order.append(sig)
        else:
            # Same capture already recorded — upgrade to this path only when
            # it carries a decoded .log and the incumbent does not.
            incumbent = chosen[sig]
            try:
                if os.path.exists(p + ".log") and not os.path.exists(incumbent + ".log"):
                    chosen[sig] = p
            except Exception:
                pass  # fs hiccup → keep incumbent
    return [chosen[sig] for sig in order]
        

def extract_archive(archive, extract_to, progress_cb=None, cancel_event=None):
    """Extract archive contents to target directory.

    progress_cb: optional callable(pct: int, basename: str) called after each
                 successfully extracted file, where pct is 0-100.
    cancel_event: optional threading.Event; checked between files AND between
                  1 MB chunks of each file, raises ExtractionCancelled when set.
                  Partial output files are removed on cancel.

    Streaming is used instead of archive.extract() so a single large member
    (e.g. a multi-GB MEMORY.DMP inside a .zip) can still be cancelled without
    waiting for zlib to finish it.
    """
    _CHUNK = 1024 * 1024  # 1 MB
    members = []
    for m in archive.infolist():
        is_dir = m.is_dir() if hasattr(m, "is_dir") else (m.isdir() if hasattr(m, "isdir") else False)
        if not is_dir: members.append(m)
    total = max(len(members), 1)
    print(f"Extracting to {extract_to} ({total} items)")

    for i, member in enumerate(members):
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled()

        filename = member.filename.strip().replace('/', os.sep)
        # Prevent zip-slip / absolute-path extraction
        if os.path.isabs(filename) or filename.startswith('..' + os.sep) or ('..' + os.sep) in filename:
            print(f"Skipping suspicious archive member path: {member.filename!r}")
            continue

        dst_path = os.path.normpath(os.path.join(extract_to, filename))
        extract_root = os.path.normcase(os.path.abspath(extract_to))
        dst_abs = os.path.normcase(os.path.abspath(dst_path))
        if not (dst_abs == extract_root or dst_abs.startswith(extract_root + os.sep)):
            print(f"Skipping suspicious archive member path: {member.filename!r}")
            continue
        dst_path = to_long_path(dst_path)

        # Fix a legacy typo in some autologger builds
        if "AutoLoggParser" in dst_path:
            dst_path = dst_path.replace("AutoLoggParser", "AutoLogParser")

        dst_dir = os.path.dirname(dst_path)
        try:
            os.makedirs(dst_dir, exist_ok=True)
            with archive.open(member) as src, open(dst_path, 'wb') as dst:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ExtractionCancelled()
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
        except ExtractionCancelled:
            # open('wb') creates the file before any write, so a cancel between
            # open and the first chunk still leaves a zero-byte file to clean up.
            try:
                if os.path.exists(dst_path):
                    os.remove(dst_path)
            except OSError:
                pass
            raise
        except Exception as e:
            try:
                if os.path.exists(dst_path):
                    os.remove(dst_path)
            except OSError:
                pass
            print(f"Error extracting {filename}: {e}")
            continue

        if progress_cb:
            try:
                progress_cb(int((i + 1) / total * 100), os.path.basename(filename))
            except Exception:
                pass  # progress updates are best-effort; never abort extraction

    return extract_to

def _find_7zip_executable():
    """Locate the 7-Zip CLI executable."""
    candidates = [
        r'C:\Program Files\7-Zip\7z.exe',
        r'C:\Program Files (x86)\7-Zip\7z.exe',
        '7z',
    ]
    for candidate in candidates:
        if shutil.which(candidate) or os.path.isfile(candidate):
            return candidate
    return None


def _extract_rar_with_7zip(file_path, extract_to):
    """Extract a RAR archive using the 7-Zip CLI. Returns True on success."""
    seven_zip = _find_7zip_executable()
    if not seven_zip:
        print('7-Zip not found; cannot extract RAR without unrar or 7-Zip.')
        return False
    try:
        result = subprocess.run(
            [seven_zip, 'x', file_path, f'-o{extract_to}', '-y'],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            print(f'7-Zip extracted RAR successfully: {file_path}')
            return True
        print(f'7-Zip exited with code {result.returncode}: {result.stderr.strip()}')
    except subprocess.TimeoutExpired as e:
        print(f'7-Zip timed out after {e.timeout} s while extracting: {file_path}')
    except Exception as e:
        print(f'7-Zip subprocess failed: {e}')
    return False


def _fake_progress_worker(progress_cb, stop_event):
    """Increment progress 0→99% at ~3% per 0.5 s until stop_event is set.

    Used for formats where real per-file progress is unavailable
    (.7z via py7zr, .rar via 7-Zip CLI).
    """
    pct = 0
    while not stop_event.wait(0.5):
        if pct < 99:
            pct = min(pct + 3, 99)
            try:
                progress_cb(pct, 'Processing…')
            except Exception:
                break


def unzip_file(file_path, extract_to, already_downloaded, progress_cb=None, cancel_event=None):
    """Extract compressed file to destination.

    progress_cb: optional callable(pct: int, basename: str) – forwarded to
                 extract_archive for .zip/.rar; for .7z a single 100% call is
                 made after extractall completes.
    cancel_event: optional threading.Event forwarded to extract_archive for
                  per-file cancellation of .zip/.rar-fallback extraction.
    """
    if already_downloaded and len(os.listdir(extract_to)) > 0:
        return extract_to
    
    lower_path = file_path.lower()
    unrar_tool = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'services', 'UnRAR', 'UnRAR.exe')
    )
    if os.path.isfile(unrar_tool):
        rarfile.UNRAR_TOOL = unrar_tool

    try:
        if lower_path.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as archive:
                return extract_archive(archive, extract_to, progress_cb=progress_cb, cancel_event=cancel_event)
        elif lower_path.endswith('.rar'):
            # Prefer 7-Zip CLI for RAR (rarfile requires unrar binary which is often absent).
            stop_event = threading.Event()
            fake_t = None
            if progress_cb:
                fake_t = threading.Thread(
                    target=_fake_progress_worker, args=(progress_cb, stop_event), daemon=True
                )
                fake_t.start()
            try:
                success = _extract_rar_with_7zip(file_path, extract_to)
            finally:
                stop_event.set()
                if fake_t:
                    fake_t.join(timeout=1)
            if success:
                if progress_cb:
                    try:
                        progress_cb(100, os.path.basename(file_path))
                    except Exception:
                        pass
                return extract_to
            # If 7-Zip was killed by a cancel signal, don't try the rarfile fallback.
            if cancel_event is not None and cancel_event.is_set():
                raise ExtractionCancelled()
            # Fall back to rarfile if 7-Zip is not installed.
            print('7-Zip unavailable, trying rarfile (requires unrar).')
            try:
                with rarfile.RarFile(file_path, 'r') as archive:
                    return extract_archive(archive, extract_to, progress_cb=progress_cb, cancel_event=cancel_event)
            except Exception as rar_err:
                print(f'rarfile also failed: {rar_err}')
        elif lower_path.endswith('.7z'):
            stop_event = threading.Event()
            fake_t = None
            if progress_cb:
                fake_t = threading.Thread(
                    target=_fake_progress_worker, args=(progress_cb, stop_event), daemon=True
                )
                fake_t.start()
            try:
                with py7zr.SevenZipFile(file_path, mode='r') as archive:
                    names = archive.getnames()
                    archive.extractall(path=extract_to)
            finally:
                stop_event.set()
                if fake_t:
                    fake_t.join(timeout=1)
            if progress_cb:
                try:
                    progress_cb(100, f'{len(names)} files extracted')
                except Exception:
                    pass
            return extract_to

    except ExtractionCancelled:
        raise
    except Exception as e:
        print(f"Extraction failed for {file_path}: {e}")
        
    return extract_to

def process_single_zip(zip_path, download_path_tmp, already_downloaded, progress_cb=None, cancel_event=None):
    """Process ZIP file and categorize extracted files.

    progress_cb: optional callable(pct: int, basename: str) – forwarded to
                 unzip_file for the main (first) archive only.  Nested archives
                 discovered inside it do not report per-file progress so the bar
                 never goes backwards.
    cancel_event: optional threading.Event; checked between archives and
                  forwarded down to per-file extraction. Raises
                  ExtractionCancelled when set.
    """
    folder_name = os.path.splitext(os.path.basename(zip_path))[0].replace(" ", "_")
    download_path = os.path.join(download_path_tmp, folder_name)
    os.makedirs(download_path, exist_ok=True)
    
    # Initialize result lists
    wifi_files, ddd_files, evt_files, bt_files, fw_files, wifilog_files = [], [], [], [], [], []
    processed_files = set()
    unzip_pending = [os.path.abspath(zip_path)]
    is_first_archive = True
    
    while unzip_pending:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled()
        file_to_unzip = unzip_pending.pop(0)
        
        if file_to_unzip in processed_files:
            continue

        # Only stream progress for the top-level archive; nested ones are
        # typically small and forwarding cb would reset the bar to 0.
        cb = progress_cb if is_first_archive else None
        is_first_archive = False
            
        try:
            extract_to = unzip_file(file_to_unzip, download_path, already_downloaded, progress_cb=cb, cancel_event=cancel_event)
        except ExtractionCancelled:
            raise
        except Exception:
            continue
            
        # Process ETL files
        etl_files = list_etl_files(extract_to)

        wifi_files.extend(filter_files('wifi', etl_files))
        bt_files.extend(filter_files('bt', etl_files))
        fw_files.extend(filter_files('fw', etl_files))
        
        # Find DDD files (non-compressed files containing 'ddd') and System Event files (.evt)
        compressed_exts = ('.zip', '.rar', '.7z', '.tar', '.gz', '.xz')
        for root, _, files in os.walk(extract_to):
            for fname in files:
                # Include files with 'ddd' in name or .evt files (System Event logs)
                is_ddd_file = 'ddd' in fname.lower() and not fname.lower().endswith(compressed_exts)
                is_evt_file = fname.lower() == "raweventviewersystemlogs.evt" or fname.lower() == 'system.evtx'
                # print(f"[DEBUG] File: {fname} (DDD: {is_ddd_file}, EVT: {is_evt_file})")
                if is_ddd_file:
                    ddd_files.append(os.path.abspath(os.path.join(root, fname)))
                elif is_evt_file:
                    evt_files.append(os.path.abspath(os.path.join(root, fname)))
        
        processed_files.add(file_to_unzip)
        
        # Find nested compressed files
        new_compressed = find_compressed_files(extract_to)
        for new_file in new_compressed:
            new_file = os.path.abspath(new_file)
            if "history" not in new_file.lower() and new_file not in processed_files:
                unzip_pending.append(new_file)
    
    # Remove duplicates. First collapse identical paths (dict.fromkeys), then
    # collapse same-capture-different-path duplicates produced by redundantly
    # packaged uploads (same autologger extracted nested AND flat).
    wifi_files = dedup_by_capture_signature(list(dict.fromkeys(wifi_files)))
    ddd_files = dedup_by_capture_signature(list(dict.fromkeys(ddd_files)))
    evt_files = dedup_by_capture_signature(list(dict.fromkeys(evt_files)))
    bt_files = dedup_by_capture_signature(list(dict.fromkeys(bt_files)))
    fw_files = dedup_by_capture_signature(list(dict.fromkeys(fw_files)))

    print(f"WiFi files: {len(wifi_files)}")
    print(f"DDD files: {len(ddd_files)}")
    print(f"EVT files: {len(evt_files)}")
    print(f"BT files: {len(bt_files)}")
    print(f"FW files: {len(fw_files)}")
    
    return wifi_files, ddd_files, evt_files, bt_files, fw_files